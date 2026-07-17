import json
import os
import re
import uuid
import difflib
import hashlib
import ipaddress
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote
# Image upload failed. Please try again.
import sqlite3
try:
    import psycopg2
except ImportError:
    psycopg2 = None
from flask import Flask, flash, jsonify, render_template, request, redirect, url_for, session
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from flask_dance.contrib.google import make_google_blueprint, google
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
from services import camerpay as campay
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()
# search
if all(os.getenv(name) for name in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")):
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )
else:
    cloudinary.config(secure=True)

# Exceptions for image file
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
MAX_UPLOAD_SIZE_MB = 5
# resolve_internal_payment_status
def allowed_file(filename):

    return (
        '.' in filename
        and
        filename.rsplit('.', 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def env_flag(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def running_on_render():
    return env_flag("RENDER") or any(
        os.getenv(name)
        for name in ("RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL", "RENDER_INSTANCE_ID")
    )


USE_POSTGRES = bool(DATABASE_URL and not env_flag("USE_SQLITE") and (running_on_render() or env_flag("USE_POSTGRES")))


if env_flag("OAUTHLIB_INSECURE_TRANSPORT"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-this")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
SQLITE_DATABASE_PATH = os.getenv("SQLITE_DATABASE_PATH", os.path.join(app.root_path, "database.db")).strip()
if not SQLITE_DATABASE_PATH:
    SQLITE_DATABASE_PATH = os.path.join(app.root_path, "database.db")
app.config["GOOGLE_OAUTH_CLIENT_ID"] = (
    os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    or os.getenv("GOOGLE_CLIENT_ID")
    or ""
)
app.config["GOOGLE_OAUTH_CLIENT_SECRET"] = (
    os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    or os.getenv("GOOGLE_CLIENT_SECRET")
    or ""
)
GOOGLE_OAUTH_ENABLED = bool(
    app.config["GOOGLE_OAUTH_CLIENT_ID"] and app.config["GOOGLE_OAUTH_CLIENT_SECRET"]
)

if GOOGLE_OAUTH_ENABLED:
    google_bp = make_google_blueprint(
        client_id=app.config["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=app.config["GOOGLE_OAUTH_CLIENT_SECRET"],
        scope=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ],
        reprompt_select_account=True,
        redirect_to="google_authorized",
    )
    app.register_blueprint(google_bp, url_prefix="/login")

@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle oversized uploads gracefully.
    
    Flask raises RequestEntityTooLarge (413) when the request body exceeds
    MAX_CONTENT_LENGTH. This handler catches it, flashes a friendly message,
    and redirects back to the referrer (or home) so the form stays usable.
    """
    flash(
        f"Image is too large. Maximum allowed is {MAX_UPLOAD_SIZE_MB} MB. "
        "Please choose a smaller image and try again.",
        "error",
    )
    referrer = request.headers.get("Referer") or url_for("home")
    return redirect(referrer)


@app.template_filter("highlight")
def highlight(text, search):
    if text is None:
        return ""

    text = str(text)
    if not search:
        return text

    search = search.strip()
    if not search:
        return text

    terms = [term for term in search.split() if term]
    if not terms:
        return text

    pattern = re.compile(
        r"(" + "|".join(re.escape(term) for term in sorted(set(terms), key=len, reverse=True)) + r")",
        re.IGNORECASE,
    )

    highlighted = pattern.sub(lambda m: f"<mark>{m.group()}</mark>", text)
    return Markup(highlighted)


POSTGRES_SCHEMA_FILE = os.path.join(app.root_path, "postgres_schema.sql")
POSTGRES_TABLES = ("users", "listings", "payments", "reports")


class DatabaseRow(dict):
    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None
        self._columns = []

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def execute(self, query, params=None):
        sql = query.replace("?", "%s")
        if self._should_return_id(sql):
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            self._cursor.execute(sql, self._normalize_params(params))
            row = self._cursor.fetchone()
            self.lastrowid = row[0] if row else None
        else:
            self._cursor.execute(sql, self._normalize_params(params))
            self.lastrowid = None

        self._columns = [column[0] for column in self._cursor.description] if self._cursor.description else []
        return self

    def executemany(self, query, param_rows):
        sql = query.replace("?", "%s")
        normalized_rows = [self._normalize_params(params) or () for params in param_rows]
        self._cursor.executemany(sql, normalized_rows)
        self.lastrowid = None
        self._columns = []
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return self._make_row(row) if row is not None else None

    def fetchall(self):
        return [self._make_row(row) for row in self._cursor.fetchall()]

    def close(self):
        self._cursor.close()

    def _make_row(self, row):
        return DatabaseRow(self._columns, row) if self._columns else row

    def _normalize_params(self, params):
        if params is None:
            return None
        if isinstance(params, dict):
            return params

        normalized = tuple(params)
        return normalized or None

    def _should_return_id(self, sql):
        return bool(
            re.match(r"\s*INSERT\s+INTO\s+(users|listings|payments|reports)\b", sql, re.IGNORECASE)
            and not re.search(r"\bRETURNING\b", sql, re.IGNORECASE)
        )


class PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return PostgresCursor(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def normalize_database_url(database_url):
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://"):]
    return database_url


def get_db():
    if USE_POSTGRES:
        if psycopg2 is None:
            raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed.")
        return PostgresConnection(psycopg2.connect(normalize_database_url(DATABASE_URL)))

    conn = sqlite3.connect(SQLITE_DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_phone(raw_phone):
    if not raw_phone:
        return ""

    phone = re.sub(r"\D", "", raw_phone)
    if phone.startswith("00"):
        phone = phone[2:]

    national_number = ""
    if phone.startswith("237") and len(phone) == 12:
        national_number = phone[3:]
    elif len(phone) == 10 and phone.startswith("0"):
        national_number = phone[1:]
    elif len(phone) == 9:
        national_number = phone
    else:
        return ""

    if len(national_number) != 9 or national_number[0] not in {"2", "6"}:
        return ""

    phone = "237" + national_number

    return phone


def normalize_email(raw_email):
    if not raw_email:
        return ""
    return raw_email.strip().lower()


def get_session_user_id():
    raw_user_id = session.get("user_id")
    if raw_user_id is None:
        return None

    try:
        return int(raw_user_id)
    except (TypeError, ValueError):
        session.pop("user_id", None)
        return None


def get_session_phone():
    user_phone = normalize_phone(session.get("user_phone"))
    if user_phone:
        return user_phone

    user = current_user_record()
    if user:
        return normalize_phone(user.get("phone"))

    return ""


def sanitize_next_url(next_url):
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return url_for("home")
    return next_url


def listing_image_url(image_value):
    image_value = (image_value or "").strip()
    if not image_value:
        return ""
    if image_value.startswith(("http://", "https://")):
        return image_value
    return url_for("static", filename=f"uploads/{image_value}")


def upload_listing_image(image_file):
    upload_result = cloudinary.uploader.upload(
        image_file,
        folder="listings",
        resource_type="image",
        use_filename=True,
        unique_filename=True,
        overwrite=False,
    )
    secure_url = upload_result.get("secure_url")
    if not secure_url:
        raise RuntimeError("Cloudinary upload did not return secure_url.")
    return secure_url


def detect_phone_network(phone: str) -> str:
    """Detect whether a Cameroon phone number belongs to MTN or Orange.

    Returns 'mtn', 'orange', or '' if unknown.
    Cameroon mobile numbers are 9 digits starting with 6.

    MTN: 64x, 65x, 67x, 68x, 69x
    Orange: 62x, 66x, 655x, 656x, 657x, 658x, 659x
    Longer prefixes (e.g. 655) take priority over shorter ones (e.g. 65).
    """
    normalized = normalize_phone(phone)
    if not normalized or len(normalized) < 12:
        return ""

    national = normalized[3:]  # 9-digit national number

    if not national.startswith("6"):
        return ""

    # Three-digit prefix check (takes priority for Orange 655-659)
    # Longer, more specific matches first
    first_three = national[:3]
    first_two = national[:2]

    # Orange: 655, 656, 657, 658, 659
    if first_three in ("655", "656", "657", "658", "659"):
        return "orange"

    # Orange: 62x, 66x
    if first_two in ("62", "66"):
        return "orange"

    # MTN: 64x, 65x, 67x, 68x, 69x
    if first_two in ("64", "67", "68", "69"):
        return "mtn"

    # MTN: 65x (but NOT 655-659 which were caught above)
    if first_two == "65":
        return "mtn"

    return ""


def env_int(name, default):
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


PRICE_MIN = 500
PRICE_MAX = 50000000
DESCRIPTION_MAX_WORDS = 50
EXPIRED_LISTING_RETENTION_DAYS = 60
BOOST_LISTING_AMOUNT = env_int("BOOST_LISTING_AMOUNT", 100)
REPORT_REASONS = (
    "Spam",
    "Scam",
    "Wrong Category",
    "Inappropriate Content",
    "Other",
)
REPORT_COMMENT_MAX_LENGTH = 500
# Pending payments should stay recoverable long enough for CamerPay reconciliation.
# Default TTL: 300s (5 minutes) unless overridden by env.
PAYMENT_PENDING_TTL_SECONDS = max(env_int("PAYMENT_PENDING_TTL_SECONDS", 300), 300)
POLL_INTERVAL_SECONDS = env_int("POLL_INTERVAL_SECONDS", 30)


def current_date():
    return datetime.today().date()


def current_date_iso():
    return current_date().isoformat()


def expired_listing_cutoff_date():
    return current_date() - timedelta(days=EXPIRED_LISTING_RETENTION_DAYS)


def validate_leave_date(raw_leave_date):
    leave_date = (raw_leave_date or "").strip()
    if not leave_date:
        return "", None, "Leaving date is required."

    try:
        parsed_leave_date = datetime.strptime(leave_date, "%Y-%m-%d").date()
    except ValueError:
        return leave_date, None, "Leaving date is invalid."

    if parsed_leave_date < current_date():
        return leave_date, parsed_leave_date, "Leaving date cannot be in the past."

    return leave_date, parsed_leave_date, ""

def effective_boost_amount() -> int:
    return BOOST_LISTING_AMOUNT


PAYMENT_STATUS_PENDING = "PENDING"
PAYMENT_STATUS_SUCCESSFUL = "SUCCESSFUL"
PAYMENT_STATUS_FAILED = "FAILED"
PAYMENT_STATUS_CANCELLED = "CANCELLED"
PAYMENT_STATUS_REJECTED = "REJECTED"
PAYMENT_STATUS_EXPIRED = "EXPIRED"


def normalize_payment_status(status):
    return (status or "").strip().upper()


def build_payment_status_view(payment_row):
    status = resolve_internal_payment_status(payment_row.get("status")) or PAYMENT_STATUS_PENDING
    is_featured = bool(payment_row.get("is_featured"))
    provider_reference = str(payment_row.get("provider_reference") or "").strip()

    if status == PAYMENT_STATUS_SUCCESSFUL or is_featured:
        return {
            "state": "featured",
            "title": "Payment confirmed",
            "message": "Your payment was completed and your listing is now featured.",
            "show_recovery_action": False,
            "show_waiting_message": False,
            "is_terminal": True,
        }

    if status == PAYMENT_STATUS_FAILED:
        return {
            "state": "failed",
            "title": "Payment did not complete",
            "message": "Payment did not complete. Start a new boost request when you are ready to try again.",
            "show_recovery_action": False,
            "show_waiting_message": False,
            "is_terminal": True,
        }

    if status == PAYMENT_STATUS_CANCELLED:
        return {
            "state": "failed",
            "title": "Payment was cancelled",
            "message": "Payment did not complete because it was cancelled. Start a new boost request if you still want to feature this listing.",
            "show_recovery_action": False,
            "show_waiting_message": False,
            "is_terminal": True,
        }

    if status == PAYMENT_STATUS_REJECTED:
        return {
            "state": "failed",
            "title": "Payment was rejected",
            "message": "Payment did not complete because it was rejected or declined. Check your mobile money account, then start a new boost request to try again.",
            "show_recovery_action": False,
            "show_waiting_message": False,
            "is_terminal": True,
        }

    if status == PAYMENT_STATUS_EXPIRED:
        return {
            "state": "failed",
            "title": "Payment expired",
            "message": "Payment did not complete before the request expired. Start a new boost request when you are ready to try again.",
            "show_recovery_action": False,
            "show_waiting_message": False,
            "is_terminal": True,
        }

    if provider_reference:
        return {
            "state": "pending",
            "title": "Payment is still being confirmed",
            "message": "We are still waiting for CamerPay to confirm your payment. We will keep checking automatically for up to five minutes after you started the boost.",
            "show_recovery_action": True,
            "show_waiting_message": True,
            "is_terminal": False,
        }

    return {
        "state": "unverified",
        "title": "Payment needs attention",
        "message": "We could not find a CamerPay reference to verify this payment. Please start a new boost request if needed.",
        "show_recovery_action": False,
        "show_waiting_message": False,
        "is_terminal": False,
    }


def format_payment_date(raw_datetime):
    raw_datetime = (raw_datetime or "").strip()
    if not raw_datetime:
        return "Date unavailable"

    try:
        parsed = datetime.fromisoformat(raw_datetime.replace("Z", "+00:00"))
    except ValueError:
        return raw_datetime[:10] if len(raw_datetime) >= 10 else raw_datetime

    return parsed.strftime("%b %d, %Y at %H:%M").replace(" 0", " ")


def format_date_label(raw_datetime):
    raw_datetime = (raw_datetime or "").strip()
    if not raw_datetime:
        return "Not available"

    try:
        parsed = datetime.fromisoformat(raw_datetime.replace("Z", "+00:00"))
    except ValueError:
        return raw_datetime[:10] if len(raw_datetime) >= 10 else raw_datetime

    return parsed.strftime("%b %d, %Y").replace(" 0", " ")


def format_xaf_amount(amount):
    try:
        return f"{int(amount):,} XAF"
    except (TypeError, ValueError):
        return f"{amount or 0} XAF"


def build_dashboard_payment_view(payment_row):
    normalized_status = resolve_internal_payment_status(payment_row["status"]) or PAYMENT_STATUS_PENDING
    status_display = {
        PAYMENT_STATUS_SUCCESSFUL: ("Completed", "✅", "chip featured"),
        PAYMENT_STATUS_PENDING: ("Pending", "⏳", "chip"),
        PAYMENT_STATUS_FAILED: ("Failed", "❌", "chip expired"),
        PAYMENT_STATUS_CANCELLED: ("Cancelled", "↩", "chip expired"),
        PAYMENT_STATUS_REJECTED: ("Rejected", "🚫", "chip expired"),
        PAYMENT_STATUS_EXPIRED: ("Expired", "⏰", "chip expired"),
    }
    status_label, status_icon, badge_class = status_display.get(
        normalized_status,
        (normalized_status.title(), "ℹ️", "chip"),
    )

    return {
        "listing_id": payment_row["listing_id"],
        "listing_title": payment_row["listing_title"] or f"Listing #{payment_row['listing_id']}",
        "reference_id": payment_row["reference_id"],
        "amount": payment_row["amount"],
        "amount_label": format_xaf_amount(payment_row["amount"]),
        "status": normalized_status,
        "status_label": status_label,
        "status_icon": status_icon,
        "badge_class": badge_class,
        "phone": payment_row["phone"] or "",
        "created_at": payment_row["created_at"],
        "created_label": format_payment_date(payment_row["created_at"]),
        "can_check": normalized_status == PAYMENT_STATUS_PENDING,
    }


def row_value(row, key, default=""):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def table_has_column(cursor, table_name, column_name):
    if table_name not in POSTGRES_TABLES:
        return False

    if USE_POSTGRES:
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ?
              AND column_name = ?
            LIMIT 1
            """,
            (table_name, column_name),
        )
        return cursor.fetchone() is not None

    cursor.execute(f"PRAGMA table_info({table_name})")
    return any(row["name"] == column_name for row in cursor.fetchall())


def current_timestamp_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def ensure_user_verified_column(cursor):
    if table_has_column(cursor, "users", "is_verified"):
        return

    if USE_POSTGRES:
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified INTEGER DEFAULT 0")
    else:
        cursor.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")


def payment_next_url(default_url):
    raw_next = request.form.get("next") or request.args.get("next")
    if not raw_next:
        return default_url
    return sanitize_next_url(raw_next)


def clean_description(raw_description):
    return re.sub(r"\s+", " ", (raw_description or "")).strip()


def validate_listing_fields(title_raw, price_raw, phone_raw, description_raw):
    title = (title_raw or "").strip()
    phone = normalize_phone(phone_raw)
    description = clean_description(description_raw)
    compact_price = re.sub(r"\s+", "", (price_raw or ""))
    errors = []
    price_value = None

    if len(title) < 3:
        errors.append("Title must be at least 3 characters.")

    if not phone:
        errors.append("Enter a valid Cameroon phone number (9 digits or +237 format).")

    if not re.fullmatch(r"\d+", compact_price):
        errors.append("Price must contain digits only.")
    else:
        price_value = int(compact_price)
        if price_value < PRICE_MIN or price_value > PRICE_MAX:
            errors.append(f"Price must be between {PRICE_MIN} and {PRICE_MAX}.")

    description_word_count = len(description.split()) if description else 0
    if description_word_count > DESCRIPTION_MAX_WORDS:
        errors.append(f"Description is too long. Keep it under {DESCRIPTION_MAX_WORDS} words.")

    return {
        "errors": errors,
        "title": title,
        "phone": phone,
        "description": description,
        "price": str(price_value) if price_value is not None else "",
    }


def build_create_form_data(default_phone=""):
    return {
        "title": (request.form.get("title") or "").strip(),
        "price": (request.form.get("price") or "").strip(),
        "phone": (request.form.get("phone") or default_phone or "").strip(),
        "leave_date": (request.form.get("leave_date") or "").strip(),
        "category": (request.form.get("category") or "").strip(),
        "description": request.form.get("description") or "",
    }


def enable_insecure_oauth_for_localhost():
    host = (request.host or "").split(":", 1)[0].strip().lower()
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    is_local_name = host in local_hosts or host.startswith("localhost")
    is_private_ip = False

    try:
        is_private_ip = ipaddress.ip_address(host).is_private
    except ValueError:
        is_private_ip = False

    if (is_local_name or is_private_ip) and "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


@app.before_request
def oauth_dev_transport_guard():
    if env_flag("OAUTHLIB_INSECURE_TRANSPORT"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        return

    if request.scheme == "http":
        enable_insecure_oauth_for_localhost()


def set_user_session(user_row):
    session["user_id"] = int(user_row["id"])
    session["user_name"] = user_row["full_name"]
    session["user_phone"] = user_row["phone"]


def clear_user_session():
    session.pop("user_id", None)
    session.pop("user_phone", None)
    session.pop("user_name", None)


def make_google_phone_candidate(google_sub, salt=0):
    seed = f"google:{google_sub}:{salt}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    numeric = "".join(str(int(ch, 16) % 10) for ch in digest)
    national_mobile = "6" + numeric[:8]
    return normalize_phone(national_mobile)


def generate_unique_google_phone(cursor, google_sub):
    for salt in range(0, 1000):
        candidate = make_google_phone_candidate(google_sub, salt=salt)
        if not candidate:
            continue
        cursor.execute("SELECT id FROM users WHERE phone = ?", (candidate,))
        existing = cursor.fetchone()
        if existing is None:
            return candidate
    raise RuntimeError("Unable to generate a unique phone seed for Google OAuth user")


def get_authenticated_owner_phone():
    user = current_user_record()
    if not user:
        return ""

    normalized_phone = normalize_phone(user["phone"])
    if normalized_phone:
        return normalized_phone

    if user.get("auth_provider") != "google":
        return ""

    conn = get_db()
    cursor = conn.cursor()
    seed = user.get("google_sub") or user.get("email") or str(user["id"])
    replacement_phone = generate_unique_google_phone(cursor, seed)
    cursor.execute("UPDATE users SET phone = ? WHERE id = ?", (replacement_phone, user["id"]))
    conn.commit()
    conn.close()
    session["user_phone"] = replacement_phone
    return replacement_phone


def current_user_record():
    user_id = get_session_user_id()
    user_phone = normalize_phone(session.get("user_phone"))

    conn = get_db()
    cursor = conn.cursor()

    row = None
    if user_id is not None:
        cursor.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        row = cursor.fetchone()

    if row is None and user_phone:
        cursor.execute(
            "SELECT * FROM users WHERE phone = ?",
            (user_phone,),
        )
        row = cursor.fetchone()

    if row is None:
        conn.close()
        clear_user_session()
        return None

    set_user_session(row)
    conn.close()

    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "phone": row["phone"],
        "email": row["email"] or "",
        "google_sub": row["google_sub"] or "",
        "auth_provider": row["auth_provider"] or "local",
        "is_admin": bool(row["is_admin"] == 1),
        "is_active": bool(row["is_active"] == 1),
        "is_verified": bool(row_value(row, "is_verified", 0) == 1),
        "created_at": row["created_at"],
    }


def user_is_deactivated(user):
    if not user:
        return True
    return not user.get("is_active", True)


@app.context_processor
def inject_auth():
    user = current_user_record()
    return {
        "current_user": user,
        "is_logged_in": user is not None,
        "google_oauth_enabled": GOOGLE_OAUTH_ENABLED,
        "listing_image_url": listing_image_url,
    }


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = current_user_record()
        if user is None:
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            return redirect(url_for("login", next=next_url))
        if user_is_deactivated(user):
            clear_user_session()
            flash("Your account has been deactivated. Please contact support.", "error")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = current_user_record()
        if user is None:
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            return redirect(url_for("login", next=next_url))
        if not user.get("is_admin"):
            return render_error_page(
                403,
                "Admin access required",
                "You are signed in, but this area is only available to administrators.",
            )
        return view_func(*args, **kwargs)

    return wrapped


def default_error_actions():
    actions = [
        {
            "label": "Go to Listings",
            "url": url_for("home"),
            "primary": True,
        }
    ]

    if current_user_record():
        actions.append(
            {
                "label": "My Dashboard",
                "url": url_for("dashboard"),
                "primary": False,
            }
        )
    else:
        actions.append(
            {
                "label": "Login",
                "url": url_for("login", next=request.path),
                "primary": False,
            }
        )

    return actions


def render_error_page(status_code, title, message, actions=None):
    return (
        render_template(
            "error.html",
            status_code=status_code,
            title=title,
            message=message,
            actions=actions or default_error_actions(),
        ),
        status_code,
    )


@app.errorhandler(403)
def forbidden(error):
    description = getattr(error, "description", "") or "You do not have permission to access this page."
    return render_error_page(403, "Access restricted", description)


@app.errorhandler(404)
def not_found(error):
    description = getattr(error, "description", "") or "The page or listing you requested could not be found."
    return render_error_page(404, "Page not found", description)


def development_route_access_reason():
    if app.debug:
        return "app.debug is True"

    if env_flag("FLASK_DEBUG"):
        return "FLASK_DEBUG is enabled"

    remote_addr = (request.remote_addr or "").strip()
    try:
        if remote_addr and ipaddress.ip_address(remote_addr).is_loopback:
            return f"request is from localhost ({remote_addr})"
    except ValueError:
        pass

    return ""


def development_only(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        access_reason = development_route_access_reason()
        if not access_reason:
            app.logger.debug(
                "dev_payment_route denied path=%s reason=%s remote_addr=%s app_debug=%s flask_debug=%s",
                request.path,
                "no local/debug development signal matched",
                request.remote_addr,
                app.debug,
                os.getenv("FLASK_DEBUG", ""),
            )
            return render_error_page(
                404,
                "Page not found",
                "This development-only payment route is not available in the current environment.",
            )
        app.logger.debug(
            "dev_payment_route granted path=%s reason=%s remote_addr=%s",
            request.path,
            access_reason,
            request.remote_addr,
        )
        return view_func(*args, **kwargs)

    return wrapped


def resolve_internal_payment_status(provider_status):
    """Normalize a provider status string to an internal payment status.

    Maps CamerPay (and generic) status strings to canonical internal values:
      PENDING / INITIATED / PROCESSING  → PAYMENT_STATUS_PENDING
      SUCCESS / SUCCESSFUL / COMPLETED  → PAYMENT_STATUS_SUCCESSFUL
      CANCEL / CANCELED / CANCELLED     → PAYMENT_STATUS_CANCELLED
      REJECTED / DENIED / DECLINED      → PAYMENT_STATUS_REJECTED
      EXPIRED / TIMEOUT / TIMED_OUT     → PAYMENT_STATUS_EXPIRED
      FAIL / FAILED / ERROR             → PAYMENT_STATUS_FAILED

    Logs the raw and normalized status for debugging.
    """
    normalized = normalize_payment_status(provider_status)
    app.logger.info("payment_status_normalization raw=%s normalized=%s", provider_status, normalized or "(empty)")
    if not normalized:
        return ""
    if normalized in {"PENDING", "INITIATED", "PROCESSING"}:
        return PAYMENT_STATUS_PENDING
    if normalized in {"SUCCESS", PAYMENT_STATUS_SUCCESSFUL, "COMPLETED"}:
        app.logger.info("payment_status_mapped_to_successful raw=%s", provider_status)
        return PAYMENT_STATUS_SUCCESSFUL
    if normalized in {"CANCEL", "CANCELED", "CANCELLED"}:
        return PAYMENT_STATUS_CANCELLED
    if normalized in {"REJECTED", "DENIED", "DECLINED"}:
        return PAYMENT_STATUS_REJECTED
    if normalized in {"EXPIRED", "TIMEOUT", "TIMED_OUT", "TIME_OUT"}:
        return PAYMENT_STATUS_EXPIRED
    if normalized in {"FAIL", "FAILED", "ERROR"}:
        return PAYMENT_STATUS_FAILED
    return normalized


def payment_provider_feedback(result, default_message):
    try:
        status_code = int(result.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0

    if result.get("network_error"):
        return "Payment service is temporarily unavailable. Please try again."

    if status_code in {401, 403}:
        return "Payment service is temporarily unavailable. Please try again later."

    if status_code >= 500:
        return "Payment provider is temporarily unavailable. Please try again."

    friendly_message = (default_message or "Payment request could not be sent.").strip()
    if not friendly_message.endswith("."):
        friendly_message += "."
    return f"{friendly_message} Please try again."


def resolve_collect_phone(listing_phone, owner_phone):
    listing_contact = normalize_phone(listing_phone)
    if listing_contact:
        return listing_contact

    return normalize_phone(owner_phone) or (owner_phone or "").strip()


def create_pending_payment_record(cursor, listing_id, amount, phone):
    reference = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    cursor.execute(
        """
        INSERT INTO payments (listing_id, reference, amount, status, phone, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (listing_id, reference, amount, PAYMENT_STATUS_PENDING, phone, created_at),
    )
    return get_payment_by_id(cursor, cursor.lastrowid)


def get_payment_by_id(cursor, payment_id: int):
    cursor.execute(
        """
        SELECT p.id, p.listing_id, p.reference AS reference_id, p.provider_reference, p.amount, p.status, p.phone, p.created_at,
               l.owner_phone, l.phone AS listing_phone, l.title AS listing_title, l.is_featured
        FROM payments p
        JOIN listings l ON l.id = p.listing_id
        WHERE p.id = ?
        LIMIT 1
        """,
        (payment_id,),
    )
    return cursor.fetchone()


def get_listing_for_owner(cursor, listing_id, owner_phone):
    cursor.execute(
        "SELECT id, title, phone, owner_phone, is_featured FROM listings WHERE id = ? AND owner_phone = ?",
        (listing_id, owner_phone),
    )
    return cursor.fetchone()


def get_pending_payment_for_listing(cursor, listing_id):
    expire_stale_pending_payments(cursor, listing_id)
    cursor.execute(
        """
        SELECT id, listing_id, reference AS reference_id, provider_reference, amount, status, phone, created_at
        FROM payments
        WHERE listing_id = ? AND status = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (listing_id, PAYMENT_STATUS_PENDING),
    )
    return cursor.fetchone()


def get_payment_for_owner(cursor, payment_reference, owner_phone):
    cursor.execute(
        """
        SELECT p.id, p.listing_id, p.reference AS reference_id, p.provider_reference, p.amount, p.status, p.phone, p.created_at,
               l.owner_phone, l.phone AS listing_phone, l.title AS listing_title, l.is_featured
        FROM payments p
        JOIN listings l ON l.id = p.listing_id
        WHERE p.reference = ? AND l.owner_phone = ?
        LIMIT 1
        """,
        (payment_reference, owner_phone),
    )
    return cursor.fetchone()


def get_payment_by_reference(cursor, payment_reference):
    cursor.execute(
        """
        SELECT p.id, p.listing_id, p.reference AS reference_id, p.provider_reference, p.amount, p.status, p.phone, p.created_at,
               l.owner_phone, l.phone AS listing_phone, l.title AS listing_title, l.is_featured
        FROM payments p
        JOIN listings l ON l.id = p.listing_id
        WHERE p.reference = ? OR p.provider_reference = ?
        LIMIT 1
        """,
        (payment_reference, payment_reference),
    )
    return cursor.fetchone()

def update_payment_status_by_id(cursor, payment_id: int, status: str):
    normalized_status = resolve_internal_payment_status(status) or PAYMENT_STATUS_PENDING
    cursor.execute(
        "UPDATE payments SET status = ? WHERE id = ?",
        (normalized_status, payment_id),
    )


def update_payment_status_record(cursor, payment_reference, status):
    normalized_status = resolve_internal_payment_status(status) or PAYMENT_STATUS_PENDING
    cursor.execute(
        "UPDATE payments SET status = ? WHERE reference = ? OR provider_reference = ?",
        (normalized_status, payment_reference, payment_reference),
    )


def activate_listing_from_payment(cursor, listing_id):
    cursor.execute("UPDATE listings SET is_featured = 1 WHERE id = ?", (listing_id,))


def update_payment_reference_record(cursor, current_reference, new_reference):
    # Deprecated: keep local reference stable and store provider reference separately.
    update_payment_provider_reference(cursor, current_reference, new_reference)


def update_payment_provider_reference(cursor, local_reference: str, provider_reference: str):
    if not provider_reference or not local_reference:
        return
    if provider_reference == local_reference:
        return
    cursor.execute(
        "UPDATE payments SET provider_reference = ? WHERE reference = ?",
        (provider_reference, local_reference),
    )


def update_payment_phone_record(cursor, payment_reference, phone):
    if not phone:
        return
    cursor.execute(
        "UPDATE payments SET phone = ? WHERE reference = ? OR provider_reference = ?",
        (phone, payment_reference, payment_reference),
    )


def expire_stale_pending_payments(cursor, listing_id: int | None = None):
    if PAYMENT_PENDING_TTL_SECONDS <= 0:
        return

    cutoff = (datetime.utcnow() - timedelta(seconds=PAYMENT_PENDING_TTL_SECONDS)).isoformat(timespec="seconds")

    if listing_id is None:
        cursor.execute(
            """
            UPDATE payments
            SET status = ?
            WHERE status = ?
              AND created_at < ?
            """,
            (PAYMENT_STATUS_EXPIRED, PAYMENT_STATUS_PENDING, cutoff),
        )
        return

    cursor.execute(
        """
        UPDATE payments
        SET status = ?
        WHERE listing_id = ?
          AND status = ?
          AND created_at < ?
        """,
        (PAYMENT_STATUS_EXPIRED, listing_id, PAYMENT_STATUS_PENDING, cutoff),
    )


TERMINAL_PAYMENT_STATUSES = frozenset(
    {
        PAYMENT_STATUS_SUCCESSFUL,
        PAYMENT_STATUS_FAILED,
        PAYMENT_STATUS_CANCELLED,
        PAYMENT_STATUS_REJECTED,
        PAYMENT_STATUS_EXPIRED,
    }
)


def is_terminal_payment_status(status: str) -> bool:
    return resolve_internal_payment_status(status) in TERMINAL_PAYMENT_STATUSES


def payment_status_rank(status: str) -> int:
    normalized = resolve_internal_payment_status(status)
    if normalized == PAYMENT_STATUS_SUCCESSFUL:
        return 4
    if normalized in TERMINAL_PAYMENT_STATUSES:
        return 3
    if normalized == PAYMENT_STATUS_PENDING:
        return 1
    return 0


def should_apply_payment_status_update(current_status: str, new_status: str) -> bool:
    current = resolve_internal_payment_status(current_status) or PAYMENT_STATUS_PENDING
    new = resolve_internal_payment_status(new_status)
    if not new:
        return False
    if current == new:
        return False
    if current == PAYMENT_STATUS_SUCCESSFUL:
        return False
    if current in TERMINAL_PAYMENT_STATUSES and new == PAYMENT_STATUS_PENDING:
        return False
    return payment_status_rank(new) >= payment_status_rank(current)


def get_payment_provider_reference(payment_row) -> str:
    return str(payment_row["provider_reference"] or "").strip()


def get_payment_local_reference(payment_row) -> str:
    return str(payment_row["reference_id"] or "").strip()


def log_payment_event(event: str, **fields):
    safe_fields = {}
    for key, value in fields.items():
        if key in {"phone", "from"} and value:
            phone = str(value)
            safe_fields[key] = f"***{phone[-4:]}" if len(phone) >= 4 else "***"
        else:
            safe_fields[key] = value
    app.logger.info("payment_lifecycle event=%s %s", event, json.dumps(safe_fields, default=str))


def apply_payment_status_transition(cursor, payment_id: int, current_status: str, new_status: str) -> bool:
    if not should_apply_payment_status_update(current_status, new_status):
        log_payment_event(
            "status_skip",
            payment_id=payment_id,
            current_status=current_status,
            new_status=new_status,
        )
        return False

    normalized = resolve_internal_payment_status(new_status) or PAYMENT_STATUS_PENDING
    update_payment_status_by_id(cursor, payment_id, normalized)
    log_payment_event(
        "status_transition",
        payment_id=payment_id,
        from_status=current_status,
        to_status=normalized,
    )
    return True


def apply_successful_payment(cursor, payment_row) -> bool:
    current = resolve_internal_payment_status(payment_row["status"]) or PAYMENT_STATUS_PENDING
    if current == PAYMENT_STATUS_SUCCESSFUL and payment_row["is_featured"] == 1:
        return False

    apply_payment_status_transition(
        cursor,
        payment_row["id"],
        current,
        PAYMENT_STATUS_SUCCESSFUL,
    )
    activate_listing_from_payment(cursor, payment_row["listing_id"])
    log_payment_event(
        "listing_activated",
        payment_id=payment_row["id"],
        listing_id=payment_row["listing_id"],
        local_ref=get_payment_local_reference(payment_row),
    )
    return True


def find_payment_for_webhook(cursor, provider_reference: str, external_reference: str):
    payment = get_payment_by_reference(cursor, provider_reference)
    if payment is not None:
        return payment

    listing_id = extract_listing_id_from_external_reference(external_reference)
    if listing_id is None:
        return None

    pending_payment = get_pending_payment_for_listing(cursor, listing_id)
    if pending_payment is None:
        return None

    update_payment_provider_reference(
        cursor,
        get_payment_local_reference(pending_payment),
        provider_reference,
    )
    return get_payment_by_id(cursor, pending_payment["id"])


def extract_listing_id_from_external_reference(external_reference):
    raw_reference = (external_reference or "").strip()
    if not raw_reference:
        return None

    if raw_reference.isdigit():
        return int(raw_reference)

    match = re.search(r"listing-(\d+)", raw_reference, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

    return None


def load_postgres_schema():
    with open(POSTGRES_SCHEMA_FILE, "r", encoding="utf-8") as schema_file:
        return schema_file.read()


def reset_postgres_sequences(cursor):
    for table in POSTGRES_TABLES:
        cursor.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table}), 1),
                (SELECT COUNT(*) > 0 FROM {table})
            )
            """
        )


def init_postgres_db():
    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute(load_postgres_schema())

        cursor.execute("UPDATE listings SET owner_phone = phone WHERE owner_phone IS NULL OR owner_phone = ''")
        cursor.execute("UPDATE listings SET view_count = 0 WHERE view_count IS NULL")

        cursor.execute("UPDATE users SET auth_provider = 'local' WHERE auth_provider IS NULL OR auth_provider = ''")
        cursor.execute("UPDATE users SET is_admin = 0 WHERE is_admin IS NULL")
        cursor.execute("UPDATE users SET is_active = 1 WHERE is_active IS NULL")
        cursor.execute("UPDATE users SET is_verified = 0 WHERE is_verified IS NULL")

        payment_columns = set()
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'payments'
            """
        )
        for column in cursor.fetchall():
            payment_columns.add(column["column_name"])

        if "reference_id" in payment_columns:
            cursor.execute(
                """
                UPDATE payments
                SET reference = reference_id
                WHERE (reference IS NULL OR TRIM(reference) = '')
                  AND reference_id IS NOT NULL
                  AND TRIM(reference_id) != ''
                """
            )

        cursor.execute("SELECT id FROM payments WHERE reference IS NULL OR TRIM(reference) = ''")
        payment_rows_missing_reference = cursor.fetchall()
        for payment_row in payment_rows_missing_reference:
            cursor.execute(
                "UPDATE payments SET reference = ? WHERE id = ?",
                (str(uuid.uuid4()), payment_row["id"]),
            )

        fallback_payment_timestamp = datetime.utcnow().isoformat(timespec="seconds")
        cursor.execute(
            "UPDATE payments SET status = ? WHERE status IS NULL OR TRIM(status) = ''",
            (PAYMENT_STATUS_PENDING,),
        )
        cursor.execute(
            "UPDATE payments SET amount = 0 WHERE amount IS NULL",
        )
        cursor.execute(
            "UPDATE payments SET created_at = ? WHERE created_at IS NULL OR TRIM(created_at) = ''",
            (fallback_payment_timestamp,),
        )
        cursor.execute(
            "UPDATE payments SET phone = '' WHERE phone IS NULL",
        )

        fallback_report_timestamp = datetime.utcnow().isoformat(timespec="seconds")
        cursor.execute(
            "UPDATE reports SET reason = ? WHERE reason IS NULL OR TRIM(reason) = ''",
            ("Other",),
        )
        cursor.execute("UPDATE reports SET comment = '' WHERE comment IS NULL")
        cursor.execute(
            "UPDATE reports SET created_at = ? WHERE created_at IS NULL OR TRIM(created_at) = ''",
            (fallback_report_timestamp,),
        )

        reset_postgres_sequences(cursor)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# creating table
def init_db():
    if USE_POSTGRES:
        init_postgres_db()
        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS listings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            price TEXT,
            category TEXT,
            phone TEXT,
            owner_phone TEXT,
            leave_date TEXT,
            description TEXT,
            image TEXT,
            is_featured INTEGER DEFAULT 0,
            view_count INTEGER DEFAULT 0,
            updated_at TEXT
        )
        """
    )

    cursor.execute("PRAGMA table_info(listings)")
    columns = [row[1] for row in cursor.fetchall()]
    if "owner_phone" not in columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN owner_phone TEXT")
    if "view_count" not in columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN view_count INTEGER DEFAULT 0")
    if "updated_at" not in columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN updated_at TEXT")

    cursor.execute("UPDATE listings SET owner_phone = phone WHERE owner_phone IS NULL OR owner_phone = ''")
    cursor.execute("UPDATE listings SET view_count = 0 WHERE view_count IS NULL")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            email TEXT,
            google_sub TEXT,
            auth_provider TEXT NOT NULL DEFAULT 'local',
            is_admin INTEGER DEFAULT 0,
            is_verified INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    cursor.execute("PRAGMA table_info(users)")
    user_columns = [row[1] for row in cursor.fetchall()]

    if "email" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "google_sub" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
    if "auth_provider" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT DEFAULT 'local'")
    if "is_admin" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
    if "is_active" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1")
    if "is_verified" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")

    cursor.execute("UPDATE users SET auth_provider = 'local' WHERE auth_provider IS NULL OR auth_provider = ''")
    cursor.execute("UPDATE users SET is_admin = 0 WHERE is_admin IS NULL")
    cursor.execute("UPDATE users SET is_active = 1 WHERE is_active IS NULL")
    cursor.execute("UPDATE users SET is_verified = 0 WHERE is_verified IS NULL")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub_unique ON users(google_sub)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            reference TEXT NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            phone TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    cursor.execute("PRAGMA table_info(payments)")
    payment_columns = [row[1] for row in cursor.fetchall()]

    if "listing_id" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN listing_id INTEGER")
    if "reference" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN reference TEXT")
    if "amount" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN amount INTEGER DEFAULT 0")
    if "status" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN status TEXT DEFAULT 'PENDING'")
    if "phone" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN phone TEXT")
    if "created_at" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN created_at TEXT")
    if "provider_reference" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN provider_reference TEXT")

    payment_columns_set = set(payment_columns)
    if "reference_id" in payment_columns_set:
        cursor.execute(
            """
            UPDATE payments
            SET reference = reference_id
            WHERE (reference IS NULL OR TRIM(reference) = '')
              AND reference_id IS NOT NULL
              AND TRIM(reference_id) != ''
            """
        )

    cursor.execute("SELECT id FROM payments WHERE reference IS NULL OR TRIM(reference) = ''")
    payment_rows_missing_reference = cursor.fetchall()
    for payment_row in payment_rows_missing_reference:
        cursor.execute(
            "UPDATE payments SET reference = ? WHERE id = ?",
            (str(uuid.uuid4()), payment_row["id"]),
        )

    fallback_payment_timestamp = datetime.utcnow().isoformat(timespec="seconds")
    cursor.execute(
        "UPDATE payments SET status = ? WHERE status IS NULL OR TRIM(status) = ''",
        (PAYMENT_STATUS_PENDING,),
    )
    cursor.execute(
        "UPDATE payments SET amount = 0 WHERE amount IS NULL",
    )
    cursor.execute(
        "UPDATE payments SET created_at = ? WHERE created_at IS NULL OR TRIM(created_at) = ''",
        (fallback_payment_timestamp,),
    )
    cursor.execute(
        "UPDATE payments SET phone = '' WHERE phone IS NULL",
    )

    cursor.execute("DROP INDEX IF EXISTS idx_payments_reference_unique")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_reference_unique ON payments(reference)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_reference_unique ON payments(provider_reference)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_listing_id ON payments(listing_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            reporter_user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    cursor.execute("PRAGMA table_info(reports)")
    report_columns = [row[1] for row in cursor.fetchall()]

    if "listing_id" not in report_columns:
        cursor.execute("ALTER TABLE reports ADD COLUMN listing_id INTEGER")
    if "reporter_user_id" not in report_columns:
        cursor.execute("ALTER TABLE reports ADD COLUMN reporter_user_id INTEGER")
    if "reason" not in report_columns:
        cursor.execute("ALTER TABLE reports ADD COLUMN reason TEXT")
    if "comment" not in report_columns:
        cursor.execute("ALTER TABLE reports ADD COLUMN comment TEXT")
    if "created_at" not in report_columns:
        cursor.execute("ALTER TABLE reports ADD COLUMN created_at TEXT")

    fallback_report_timestamp = datetime.utcnow().isoformat(timespec="seconds")
    cursor.execute(
        "UPDATE reports SET reason = ? WHERE reason IS NULL OR TRIM(reason) = ''",
        ("Other",),
    )
    cursor.execute("UPDATE reports SET comment = '' WHERE comment IS NULL")
    cursor.execute(
        "UPDATE reports SET created_at = ? WHERE created_at IS NULL OR TRIM(created_at) = ''",
        (fallback_report_timestamp,),
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_listing_id ON reports(listing_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_reporter_user_id ON reports(reporter_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at)")

    conn.commit()
    conn.close()


def should_init_database():
    if env_flag("SKIP_APP_INIT_DB"):
        return False
    if USE_POSTGRES:
        return True
    if not os.path.exists(SQLITE_DATABASE_PATH):
        return True
    return env_flag("INIT_LOCAL_DB")


if should_init_database():
    init_db()


@app.route("/")
def home():
    conn = get_db()
    cursor = conn.cursor()

    category = request.args.get("category")
    search = request.args.get("search")
    min_price = request.args.get("min_price", "").strip()
    max_price = request.args.get("max_price", "").strip()
    show_filter = request.args.get("show", "").strip()
    sort = request.args.get("sort", "").strip()
    user_phone = get_session_phone()

    # Build base query conditions
    conditions = []
    params = []

    if category:
        conditions.append("category = ?")
        params.append(category)

    if search:
        pass  # skip SQL LIKE filter; fuzzy matching below handles it

    if min_price:
        try:
            conditions.append("CAST(price AS INTEGER) >= ?")
            params.append(int(min_price))
        except ValueError:
            pass

    if max_price:
        try:
            conditions.append("CAST(price AS INTEGER) <= ?")
            params.append(int(max_price))
        except ValueError:
            pass

    today_str = current_date_iso()
    expired_cutoff_str = expired_listing_cutoff_date().isoformat()

    if show_filter == "active":
        conditions.append("leave_date >= ?")
        params.append(today_str)
    elif show_filter == "featured":
        conditions.append("is_featured = 1")
        conditions.append("leave_date >= ?")
        params.append(expired_cutoff_str)
    elif show_filter == "expired":
        conditions.append("leave_date < ?")
        conditions.append("leave_date >= ?")
        params.extend([today_str, expired_cutoff_str])
    elif show_filter != "all":
        conditions.append("leave_date >= ?")
        params.append(expired_cutoff_str)

    where_clause = " AND ".join(conditions) if conditions else "1"

    # Determine sort order
    if sort == "price_asc":
        order_clause = "CAST(price AS INTEGER) ASC, id DESC"
    elif sort == "price_desc":
        order_clause = "CAST(price AS INTEGER) DESC, id DESC"
    elif sort == "ending_soon":
        order_clause = f"CASE WHEN leave_date >= '{today_str}' THEN 0 ELSE 1 END ASC, leave_date ASC, is_featured DESC"
    else:
        order_clause = "is_featured DESC, id DESC"

    cursor.execute(
        f"SELECT * FROM listings WHERE {where_clause} ORDER BY {order_clause}",
        params,
    )
    all_listings = cursor.fetchall()

    # For search, apply fuzzy matching on top of SQL LIKE
    if search:
        filtered = []
        for item in all_listings:
            searchable_fields = [
                item["title"],
                item["category"],
                item["price"],
                item["phone"],
                item["description"],
            ]
            matched = False
            for field in searchable_fields:
                if field is None:
                    continue
                words = field.lower().split()
                for word in words:
                    clean_search = re.sub(r"[^a-zA-Z0-9]", "", search.lower())
                    clean_word = re.sub(r"[^a-zA-Z0-9]", "", word.lower())
                    similarity = difflib.SequenceMatcher(None, clean_search, clean_word).ratio()
                    if clean_search in clean_word or similarity > 0.5:
                        matched = True
                        break
                if matched:
                    break
            if matched:
                filtered.append(item)
        all_listings = filtered

    seller_verified_by_phone = {}
    owner_phones = sorted({item["owner_phone"] for item in all_listings if item["owner_phone"]})
    if owner_phones and table_has_column(cursor, "users", "is_verified"):
        placeholders = ",".join("?" for _ in owner_phones)
        cursor.execute(
            f"SELECT phone, is_verified FROM users WHERE phone IN ({placeholders})",
            owner_phones,
        )
        seller_verified_by_phone = {
            seller_row["phone"]: bool(row_value(seller_row, "is_verified", 0) == 1)
            for seller_row in cursor.fetchall()
        }

    today = current_date()
    featured_listings = []
    active_listings = []
    expired_listings = []
    for item in all_listings:
        leave_date = datetime.strptime(item["leave_date"], "%Y-%m-%d").date()
        days_left = (leave_date - today).days
        is_expired = days_left < 0

        owner_phone = item["owner_phone"] or ""
        updated_at = row_value(item, "updated_at")
        listing_data = {
            "id": item["id"],
            "title": item["title"],
            "price": item["price"],
            "category": item["category"],
            "phone": item["phone"],
            "owner_phone": owner_phone,
            "leave_date": item["leave_date"],
            "description": item["description"] or "",
            "image": item["image"],
            "is_featured": item["is_featured"],
            "days_left": days_left,
            "is_expired": is_expired,
            "last_updated_label": format_payment_date(updated_at) if updated_at else "",
            "seller_is_verified": seller_verified_by_phone.get(owner_phone, False),
            "can_manage": bool(user_phone and user_phone == owner_phone),
        }

        if is_expired:
            expired_listings.append(listing_data)
        elif item["is_featured"] == 1:
            featured_listings.append(listing_data)
        else:
            active_listings.append(listing_data)

    enhanced_listings = featured_listings + active_listings + expired_listings

    conn.close()
    return render_template(
        "index.html",
        listings=enhanced_listings,
        search=search,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user_record():
        return redirect(url_for("home"))

    error = ""
    full_name = ""
    phone_input = ""

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone_input = normalize_phone(request.form.get("phone"))
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not full_name:
            error = "Full name is required."
        elif not phone_input:
            error = "Valid phone number is required."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm_password:
            error = "Passwords do not match."
        else:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users WHERE phone = ?", (phone_input,))
            existing_user = cursor.fetchone()

            if existing_user:
                error = "An account with this phone already exists."
            else:
                password_hash = generate_password_hash(password)
                created_at = datetime.utcnow().isoformat(timespec="seconds")
                cursor.execute(
                    """
                    INSERT INTO users (full_name, phone, password_hash, email, google_sub, auth_provider, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (full_name, phone_input, password_hash, None, None, "local", created_at),
                )
                conn.commit()
                cursor.execute("SELECT * FROM users WHERE phone = ?", (phone_input,))
                created_user = cursor.fetchone()
                if created_user is not None:
                    set_user_session(created_user)

            conn.close()

            if not error:
                return redirect(url_for("home"))

    return render_template(
        "register.html",
        error=error,
        success="",
        full_name=full_name,
        phone=phone_input,
    )


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    email = (request.form.get("email") or "").strip().lower()

    if request.method == "POST":
        if not email or "@" not in email:
            flash("Please enter a valid email address.", "error")
            return render_template("forgot_password.html")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE lower(email) = ?", (email,))
        user_row = cursor.fetchone()
        conn.close()

        if user_row is not None:
            token = str(uuid.uuid4())
            expires_at = (datetime.utcnow() + timedelta(minutes=30)).isoformat(timespec="seconds")
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS password_reset_tokens(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "INSERT INTO password_reset_tokens (user_id, token, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (user_row["id"], token, expires_at, datetime.utcnow().isoformat(timespec="seconds")),
            )
            conn.commit()
            conn.close()

            reset_url = url_for("reset_password", token=token, _external=True)
            app.logger.warning("PASSWORD_RESET_LINK email=%s url=%s", email, reset_url)

        flash("If that email is linked to an account, we sent a password reset link.", "success")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    user = current_user_record()
    if user and not user_is_deactivated(user):
        return redirect(url_for("home"))

    error = ""
    phone_input = ""
    next_url = sanitize_next_url(request.args.get("next") or request.form.get("next") or url_for("home"))

    if request.method == "POST":
        phone_input = normalize_phone(request.form.get("phone"))
        password = request.form.get("password", "")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE phone = ?", (phone_input,))
        user = cursor.fetchone()
        conn.close()

        if user is None:
            error = "Invalid phone or password."
        elif user["auth_provider"] == "google":
            error = "This account uses Google login. Use Continue with Google."
        elif not check_password_hash(user["password_hash"], password):
            error = "Invalid phone or password."
        elif user["is_active"] == 0:
            clear_user_session()
            error = "Your account has been deactivated. Please contact support."
        else:
            set_user_session(user)
            return redirect(next_url)

    return render_template("login.html", error=error, phone=phone_input, next=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    clear_user_session()
    session.pop("oauth_next", None)
    session.pop("google_oauth_token", None)
    return redirect(url_for("home"))

@app.route("/login/google")
def login_google():
    enable_insecure_oauth_for_localhost()

    if current_user_record():
        return redirect(url_for("home"))

    if not GOOGLE_OAUTH_ENABLED:
        return "Google OAuth is not configured yet. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.", 503

    next_url = sanitize_next_url(request.args.get("next") or url_for("home"))
    session["oauth_next"] = next_url
    return redirect(url_for("google.login"))


@app.route("/oauth/google/authorized")
def google_authorized():
    enable_insecure_oauth_for_localhost()

    if not GOOGLE_OAUTH_ENABLED:
        return "Google OAuth is not configured yet. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.", 503

    if not google.authorized:
        return redirect(url_for("google.login"))

    resp = google.get("/oauth2/v2/userinfo")
    if not resp.ok:
        return "Failed to fetch Google profile information.", 400

    profile = resp.json()
    google_sub = str(profile.get("id") or profile.get("sub") or "").strip()
    email = normalize_email(profile.get("email")) or None
    full_name = (profile.get("name") or "").strip() or (email.split("@")[0] if email else "Google User")

    if not google_sub and not email:
        return "Google account data is incomplete. Please try again.", 400

    conn = get_db()
    cursor = conn.cursor()

    user = None
    if google_sub:
        cursor.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,))
        user = cursor.fetchone()

    if user is None and email:
        cursor.execute("SELECT * FROM users WHERE lower(email) = lower(?)", (email,))
        user = cursor.fetchone()

    if user is None:
        phone_seed = generate_unique_google_phone(cursor, google_sub or email)
        created_at = datetime.utcnow().isoformat(timespec="seconds")
        password_hash = generate_password_hash(uuid.uuid4().hex)
        cursor.execute(
            """
            INSERT INTO users (full_name, phone, password_hash, email, google_sub, auth_provider, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (full_name, phone_seed, password_hash, email, google_sub or None, "google", created_at),
        )
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,))
        user = cursor.fetchone()
    else:
        updated_name = full_name or user["full_name"]
        updated_email = email if email is not None else user["email"]
        updated_google_sub = google_sub or user["google_sub"]
        updated_provider = "google"

        cursor.execute(
            """
            UPDATE users
            SET full_name = ?, email = ?, google_sub = ?, auth_provider = ?
            WHERE id = ?
            """,
            (updated_name, updated_email, updated_google_sub, updated_provider, user["id"]),
        )
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user["id"],))
        user = cursor.fetchone()

    conn.close()

    if user is None:
        return "Unable to complete Google sign-in. Please try again.", 500

    if user["is_active"] == 0:
        clear_user_session()
        flash("Your account has been deactivated. Please contact support.", "error")
        return redirect(url_for("login"))

    set_user_session(user)
    next_url = sanitize_next_url(session.pop("oauth_next", url_for("home")))
    return redirect(next_url)

@app.route("/create")
@login_required
def create():
    user = current_user_record()
    phone_prefill = user["phone"] if user and user.get("auth_provider") != "google" else ""
    return render_template(
        "create-listing.html",
        user_phone=phone_prefill,
        form_data={},
        form_errors=[],
        description_max_words=DESCRIPTION_MAX_WORDS,
        min_leave_date=current_date_iso(),
    )


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    # Prevent stale pending payments from locking the UI.
    expire_stale_pending_payments(cursor)
    conn.commit()

    cursor.execute(
        """
        SELECT * FROM listings
        WHERE owner_phone = ?
        ORDER BY is_featured DESC, id DESC
        """,
        (owner_phone,),
    )
    rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT p.id, p.listing_id, p.reference AS reference_id, p.amount, p.status, p.phone, p.created_at,
               l.title AS listing_title
        FROM payments p
        JOIN listings l ON l.id = p.listing_id
        WHERE l.owner_phone = ?
        ORDER BY p.id DESC
        """,
        (owner_phone,),
    )
    payment_rows = cursor.fetchall()

    conn.close()

    today = current_date()
    listings = []
    pending_payments_by_listing = {}
    payment_history = []
    stats = {
        "total": 0,
        "active": 0,
        "soon": 0,
        "expired": 0,
        "featured": 0,
    }

    for payment_row in payment_rows:
        normalized_status = resolve_internal_payment_status(payment_row["status"]) or PAYMENT_STATUS_PENDING
        if normalized_status == PAYMENT_STATUS_PENDING and payment_row["listing_id"] not in pending_payments_by_listing:
            pending_payments_by_listing[payment_row["listing_id"]] = payment_row["reference_id"]

        payment_history.append(build_dashboard_payment_view(payment_row))

    for row in rows:
        leave_date = datetime.strptime(row["leave_date"], "%Y-%m-%d").date()
        days_left = (leave_date - today).days
        is_expired = days_left < 0
        is_soon = 0 <= days_left <= 3
        is_featured = row["is_featured"] == 1
        updated_at = row_value(row, "updated_at")

        stats["total"] += 1
        if is_featured:
            stats["featured"] += 1
        if is_expired:
            stats["expired"] += 1
        else:
            stats["active"] += 1
            if is_soon:
                stats["soon"] += 1

        listings.append(
            {
                "id": row["id"],
                "title": row["title"],
                "price": row["price"],
                "category": row["category"],
                "phone": row["phone"],
                "leave_date": row["leave_date"],
                "description": row["description"] or "",
                "image": row["image"],
                "is_featured": row["is_featured"],
                "days_left": days_left,
                "is_expired": is_expired,
                "is_soon": is_soon,
                "pending_payment_reference": pending_payments_by_listing.get(row["id"], ""),
                "view_count": row["view_count"],
                "last_updated_label": format_payment_date(updated_at) if updated_at else "",
            }
        )

    return render_template(
        "dashboard.html",
        listings=listings,
        stats=stats,
        payment_history=payment_history[:20],
        boost_amount=effective_boost_amount(),
    )


@app.route("/account", methods=["GET", "POST"])
@login_required
def account_settings():
    user = current_user_record()
    if user is None:
        return redirect(url_for("login"))

    if request.method == "POST":
        current_password = request.form.get("current_password", "").strip()

        # Password change form
        if current_password:
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            errors = []

            if user.get("auth_provider") == "google":
                errors.append("Password management is not available for Google-authenticated accounts.")
            elif not current_password:
                errors.append("Please enter your current password.")
            elif not new_password:
                errors.append("Please enter a new password.")
            elif len(new_password) < 6:
                errors.append("New password must be at least 6 characters.")
            elif new_password == current_password:
                errors.append("New password must be different from your current password.")
            elif not confirm_password:
                errors.append("Please confirm your new password.")
            elif new_password != confirm_password:
                errors.append("New passwords do not match.")
            else:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
                row = cursor.fetchone()
                conn.close()

                if row is None:
                    errors.append("Account not found.")
                elif not check_password_hash(row["password_hash"], current_password):
                    errors.append("Current password is incorrect.")

            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template("account.html", user=user)

            new_hash = generate_password_hash(new_password)
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
            conn.commit()
            conn.close()
            flash("Password updated successfully.", "success")
            return redirect(url_for("account_settings"))

        # Account info update form
        full_name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        errors = []

        if not full_name:
            errors.append("Full name is required.")

        normalized_phone = normalize_phone(phone)
        if not normalized_phone:
            errors.append("Enter a valid Cameroon phone number (9 digits or +237 format).")

        if normalized_phone:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users WHERE phone = ? AND id != ?", (normalized_phone, user["id"]))
            existing = cursor.fetchone()
            conn.close()
            if existing is not None:
                errors.append("This phone number is already in use by another account.")

        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("account.html", user=user)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET full_name = ?, phone = ? WHERE id = ?", (full_name, normalized_phone, user["id"]))
        conn.commit()
        conn.close()
        session["user_name"] = full_name
        session["user_phone"] = normalized_phone
        flash("Account updated.", "success")
        return redirect(url_for("account_settings"))

    return render_template("account.html", user=user)


def _render_create_errors(conn, owner_phone, form_data, errors):
    conn.close()
    return (
        render_template(
            "create-listing.html",
            user_phone=owner_phone,
            form_data=form_data,
            form_errors=errors,
            description_max_words=DESCRIPTION_MAX_WORDS,
            min_leave_date=current_date_iso(),
        ),
        400,
    )


@app.route("/add", methods=["POST"])
@login_required
def add_listing():
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()
    form_data = build_create_form_data(default_phone=owner_phone)

    if not owner_phone:
        conn.close()
        return render_error_page(
            403,
            "Action not allowed",
            "You need an active account before you can create a listing.",
        )

    image = request.files.get("image")
    if image is None or not image.filename:
        return _render_create_errors(conn, owner_phone, form_data, ["Image is required."])

    safe_name = secure_filename(image.filename)
    if not safe_name:
        return _render_create_errors(conn, owner_phone, form_data, ["Invalid image filename."])

    if not allowed_file(image.filename):
        return _render_create_errors(conn, owner_phone, form_data, ["Invalid image format. Use PNG, JPG, JPEG, or WEBP."])

    validated = validate_listing_fields(
        title_raw=request.form.get("title"),
        price_raw=request.form.get("price"),
        phone_raw=request.form.get("phone") or owner_phone,
        description_raw=request.form.get("description"),
    )
    if validated["errors"]:
        return _render_create_errors(conn, owner_phone, form_data, validated["errors"])

    leave_date, _, leave_date_error = validate_leave_date(request.form.get("leave_date"))
    if leave_date_error:
        return _render_create_errors(conn, owner_phone, form_data, [leave_date_error])

    category = (request.form.get("category") or "").strip()
    if not category:
        return _render_create_errors(conn, owner_phone, form_data, ["Category is required."])

    title = validated["title"]
    price = validated["price"]
    phone = validated["phone"]
    description = validated["description"]
    try:
        image_url = upload_listing_image(image)
    except Exception:
        app.logger.exception("Cloudinary upload failed for listing image.")
        return _render_create_errors(conn, owner_phone, form_data, ["Image upload failed. Please try again."])
    is_featured = 0

    if table_has_column(cursor, "listings", "updated_at"):
        cursor.execute(
            "INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, price, category, phone, owner_phone, leave_date, description, image_url, is_featured, current_timestamp_iso()),
        )
    else:
        cursor.execute(
            "INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, price, category, phone, owner_phone, leave_date, description, image_url, is_featured),
        )
    listing_id = cursor.lastrowid

    conn.commit()
    conn.close()

    flash("Listing posted successfully.", "success")
    return redirect(url_for("listing_detail", id=listing_id))


@app.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    if not owner_phone:
        conn.close()
        return render_error_page(
            403,
            "Action not allowed",
            "You are not allowed to delete this listing.",
        )

    cursor.execute("SELECT id FROM listings WHERE id = ? AND owner_phone = ?", (id, owner_phone))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return render_error_page(
            404,
            "Listing not found",
            "We could not find that listing under your account.",
        )

    cursor.execute("DELETE FROM listings WHERE id = ? AND owner_phone = ?", (id, owner_phone))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/edit/<int:id>", methods=["GET"])
@login_required
def edit_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    if not owner_phone:
        conn.close()
        return render_error_page(
            403,
            "Action not allowed",
            "You are not allowed to edit this listing.",
        )

    cursor.execute("SELECT * FROM listings WHERE id = ? AND owner_phone = ?", (id, owner_phone))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return render_error_page(
            404,
            "Listing not found",
            "We could not find that listing under your account.",
        )

    conn.close()
    return render_template(
        "edit.html",
        listing=listing,
        form_errors=[],
        description_max_words=DESCRIPTION_MAX_WORDS,
        min_leave_date=current_date_iso(),
        renew_mode=request.args.get("renew") == "1",
    )


@app.route("/update/<int:id>", methods=["POST"])
@login_required
def update_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    if not owner_phone:
        conn.close()
        return render_error_page(
            403,
            "Action not allowed",
            "You are not allowed to update this listing.",
        )

    cursor.execute(
        "SELECT image, category, leave_date, title, price, phone, description, is_featured FROM listings WHERE id = ? AND owner_phone = ?",
        (id, owner_phone),
    )
    current_listing = cursor.fetchone()

    if current_listing is None:
        conn.close()
        return render_error_page(
            404,
            "Listing not found",
            "We could not find that listing under your account.",
        )

    listing_form = {
        "id": id,
        "image": current_listing["image"],
        "title": (request.form.get("title") or current_listing["title"] or "").strip(),
        "price": (request.form.get("price") or current_listing["price"] or "").strip(),
        "category": (request.form.get("category") or current_listing["category"] or "").strip(),
        "phone": (request.form.get("phone") or current_listing["phone"] or "").strip(),
        "leave_date": (request.form.get("leave_date") or current_listing["leave_date"] or "").strip(),
        "description": request.form.get("description", current_listing["description"] or ""),
        "is_featured": bool(current_listing["is_featured"] == 1),
    }

    def render_edit_errors(errors):
        conn.close()
        return (
            render_template(
                "edit.html",
                listing=listing_form,
                form_errors=errors,
                description_max_words=DESCRIPTION_MAX_WORDS,
                min_leave_date=current_date_iso(),
                renew_mode=request.args.get("renew") == "1",
            ),
            400,
        )

    current_image = current_listing["image"]
    validated = validate_listing_fields(
        title_raw=request.form.get("title", current_listing["title"]),
        price_raw=request.form.get("price", current_listing["price"]),
        phone_raw=request.form.get("phone", current_listing["phone"]),
        description_raw=request.form.get("description", current_listing["description"]),
    )
    if validated["errors"]:
        return render_edit_errors(validated["errors"])

    title = validated["title"]
    price = validated["price"]
    phone = validated["phone"]
    category = request.form.get("category", "").strip() or current_listing["category"]
    leave_date = request.form.get("leave_date", "").strip() or current_listing["leave_date"]
    description = validated["description"]
    leave_date, _, leave_date_error = validate_leave_date(leave_date)
    if leave_date_error:
        return render_edit_errors([leave_date_error])

    image = request.files.get("image")
    image_url = current_image

    if image and image.filename:
        safe_name = secure_filename(image.filename)
        if safe_name:
            if not allowed_file(image.filename):
                return render_edit_errors(["Invalid image format. Use PNG, JPG, JPEG, or WEBP."])
            try:
                image_url = upload_listing_image(image)
            except Exception:
                app.logger.exception("Cloudinary upload failed for listing image.")
                return render_edit_errors(["Image upload failed. Please try again."])

    is_featured = 1 if current_listing["is_featured"] == 1 else 0

    if table_has_column(cursor, "listings", "updated_at"):
        cursor.execute(
            "UPDATE listings SET title = ?, price = ?, category = ?, phone = ?, leave_date = ?, description = ?, image = ?, is_featured = ?, updated_at = ? WHERE id = ? AND owner_phone = ?",
            (title, price, category, phone, leave_date, description, image_url, is_featured, current_timestamp_iso(), id, owner_phone),
        )
    else:
        cursor.execute(
            "UPDATE listings SET title = ?, price = ?, category = ?, phone = ?, leave_date = ?, description = ?, image = ?, is_featured = ? WHERE id = ? AND owner_phone = ?",
            (title, price, category, phone, leave_date, description, image_url, is_featured, id, owner_phone),
        )
    conn.commit()
    conn.close()

    flash("Listing updated successfully.", "success")
    return redirect(url_for("listing_detail", id=id))


@app.route("/listing/<int:id>")
def listing_detail(id):
    conn = get_db()
    cursor = conn.cursor()
    user_phone = get_session_phone()
    today_str = current_date_iso()
    seller_verified_select = "u.is_verified" if table_has_column(cursor, "users", "is_verified") else "0"

    listing_detail_query = f"""
        SELECT l.*, u.full_name AS seller_name, u.created_at AS seller_created_at,
               {seller_verified_select} AS seller_is_verified,
               (
                   SELECT COUNT(*)
                   FROM listings active_listing
                   WHERE active_listing.owner_phone = l.owner_phone
                     AND active_listing.leave_date >= ?
               ) AS seller_active_listing_count
        FROM listings l
        LEFT JOIN users u ON u.phone = l.owner_phone
        WHERE l.id = ?
    """

    cursor.execute(listing_detail_query, (today_str, id))
    item = cursor.fetchone()

    if item is None:
        conn.close()
        return render_error_page(
            404,
            "Listing not found",
            "This listing may have been deleted or is no longer available.",
        )

    # Track views with session-based deduplication
    # Ensure viewed_listings is a set for efficient lookup and storage
    viewed_listings = session.get("viewed_listings", set())
    if isinstance(viewed_listings, list):
        viewed_listings = set(viewed_listings)
    if id not in viewed_listings:
        cursor.execute("UPDATE listings SET view_count = view_count + 1 WHERE id = ?", (id,))
        conn.commit()
        viewed_listings.add(id)
        session["viewed_listings"] = list(viewed_listings) # Store as list in session for JSON serialization
        # Refresh item to get updated view_count
        cursor.execute(listing_detail_query, (today_str, id))
        item = cursor.fetchone()

    can_manage = bool(user_phone and (item["owner_phone"] or "") == user_phone)
    pending_payment_reference = ""
    pending_payment_view = None
    if can_manage:
        pending_payment = get_pending_payment_for_listing(cursor, id)
        if pending_payment:
            pending_payment_reference = pending_payment["reference_id"]
            pending_payment_view = build_payment_status_view({
                "status": pending_payment["status"],
                "is_featured": item["is_featured"],
                "provider_reference": pending_payment.get("provider_reference", ""),
            })

    cursor.execute(
        """
        SELECT * FROM listings
        WHERE category = ?
        AND id != ?
        ORDER BY is_featured DESC, id DESC
        LIMIT 4
        """,
        (item["category"], item["id"]),
    )

    related_rows = cursor.fetchall()
    conn.close()

    today = current_date()
    leave_date = datetime.strptime(item["leave_date"], "%Y-%m-%d").date()
    days_left = (leave_date - today).days
    is_expired = days_left < 0
    updated_at = row_value(item, "updated_at")
    seller_created_at = row_value(item, "seller_created_at")
    seller_info = {
        "name": row_value(item, "seller_name") or "Seller",
        "member_since_label": format_date_label(seller_created_at) if seller_created_at else "",
        "active_listing_count": row_value(item, "seller_active_listing_count", 0) or 0,
        "last_updated_label": format_payment_date(updated_at) if updated_at else "",
        "is_verified": bool(row_value(item, "seller_is_verified", 0) == 1),
    }
    whatsapp_phone = normalize_phone(item["phone"])
    whatsapp_url = ""
    if not is_expired and whatsapp_phone:
        whatsapp_message = (
            f'Hi, I am interested in "{item["title"]}" listed on Azison '
            f'for {item["price"]} XAF. Is it still available?'
        )
        whatsapp_url = f"https://wa.me/{whatsapp_phone}?text={quote(whatsapp_message)}"

    related_items = []
    for related in related_rows:
        related_leave_date = datetime.strptime(related["leave_date"], "%Y-%m-%d").date()
        related_days_left = (related_leave_date - today).days

        if related_days_left < 0:
            continue

        related_owner_phone = related["owner_phone"] or ""
        related_items.append(
            {
                "id": related["id"],
                "title": related["title"],
                "price": related["price"],
                "category": related["category"],
                "image": related["image"],
                "days_left": related_days_left,
                "is_featured": related["is_featured"],
                "can_manage": bool(user_phone and related_owner_phone == user_phone),
            }
        )

    return render_template(
        "listing_detail.html",
        item=item,
        days_left=days_left,
        is_expired=is_expired,
        related_items=related_items,
        can_manage=can_manage,
        pending_payment_reference=pending_payment_reference,
        pending_payment_view=pending_payment_view,
        boost_amount=effective_boost_amount(),
        report_reasons=REPORT_REASONS,
        report_comment_max_length=REPORT_COMMENT_MAX_LENGTH,
        whatsapp_url=whatsapp_url,
        seller_info=seller_info,
    )


@app.route("/listing/<int:listing_id>/report", methods=["POST"])
@login_required
def report_listing(listing_id):
    user = current_user_record()
    reason = (request.form.get("reason") or "").strip()
    comment = (request.form.get("comment") or "").strip()
    next_url = url_for("listing_detail", id=listing_id)

    if reason not in REPORT_REASONS:
        flash("Report not sent. Choose a valid report reason.", "error")
        return redirect(next_url)

    if len(comment) > REPORT_COMMENT_MAX_LENGTH:
        flash(f"Report not sent. Keep your comment under {REPORT_COMMENT_MAX_LENGTH} characters.", "error")
        return redirect(next_url)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM listings WHERE id = ?", (listing_id,))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        flash("Report not sent. Listing not found.", "error")
        return redirect(url_for("home"))

    created_at = datetime.utcnow().isoformat(timespec="seconds")
    cursor.execute(
        """
        INSERT INTO reports (listing_id, reporter_user_id, reason, comment, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (listing_id, user["id"], reason, comment, created_at),
    )
    conn.commit()
    conn.close()

    flash("Report sent.", "success")
    return redirect(next_url)


@app.route("/payments/boost/<int:listing_id>/select", methods=["GET"])
@login_required
def select_boost_payment(listing_id):
    """Show payment method selection page."""
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()
    default_next = url_for("listing_detail", id=listing_id)
    next_url = payment_next_url(default_next)

    if not owner_phone:
        conn.close()
        flash("You are not allowed to boost this listing.", "error")
        return redirect(next_url)

    listing = get_listing_for_owner(cursor, listing_id, owner_phone)
    if listing is None:
        conn.close()
        flash("Listing not found or not allowed.", "error")
        return redirect(next_url)

    if listing["is_featured"] == 1:
        conn.close()
        flash("This listing is already featured.", "info")
        return redirect(next_url)

    existing_pending_payment = get_pending_payment_for_listing(cursor, listing_id)
    if existing_pending_payment is not None:
        conn.close()
        flash("A payment is already pending for this listing. Confirm it to activate featured status.", "info")
        return redirect(
            url_for(
                "verify_listing_payment",
                reference_id=existing_pending_payment["reference_id"],
                next=next_url,
            )
        )

    # Detect phone network for the payment number
    collect_phone = resolve_collect_phone(listing["phone"], owner_phone)
    if not collect_phone:
        conn.close()
        flash("Add a valid Cameroon mobile number to your listing before boosting.", "error")
        return redirect(next_url)

    detected_network = detect_phone_network(collect_phone)
    network_label = ""
    if detected_network == "mtn":
        network_label = "MTN MoMo"
    elif detected_network == "orange":
        network_label = "Orange Money"

    conn.close()
    return render_template(
        "select_payment.html",
        listing_id=listing_id,
        boost_amount=effective_boost_amount(),
        next=next_url,
        collect_phone=collect_phone,
        detected_network=detected_network,
        network_label=network_label,
    )


@app.route("/payments/boost/<int:listing_id>", methods=["POST"])
@login_required
def initiate_listing_boost(listing_id):
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()
    default_next = url_for("listing_detail", id=listing_id)
    next_url = payment_next_url(default_next)

    if not owner_phone:
        conn.close()
        flash("You are not allowed to boost this listing.", "error")
        return redirect(next_url)

    listing = get_listing_for_owner(cursor, listing_id, owner_phone)
    if listing is None:
        conn.close()
        flash("Listing not found or not allowed.", "error")
        return redirect(next_url)

    if listing["is_featured"] == 1:
        conn.close()
        flash("This listing is already featured.", "info")
        return redirect(next_url)

    existing_pending_payment = get_pending_payment_for_listing(cursor, listing_id)
    if existing_pending_payment is not None:
        conn.close()
        # Do NOT flash here — verify_listing_payment will show its own status message
        return redirect(
            url_for(
                "verify_listing_payment",
                reference_id=existing_pending_payment["reference_id"],
                next=next_url,
            )
        )

    collect_phone = resolve_collect_phone(listing["phone"], owner_phone)
    if not collect_phone:
        conn.close()
        flash("Add a valid Cameroon mobile number to your listing before boosting.", "error")
        return redirect(url_for("select_boost_payment", listing_id=listing_id))

    charge_amount = effective_boost_amount()
    payment_row = create_pending_payment_record(cursor, listing_id, charge_amount, collect_phone)
    local_reference = get_payment_local_reference(payment_row)

    external_reference = f"listing-{listing_id}-payment-{payment_row['id']}"
    log_payment_event(
        "collect_request",
        listing_id=listing_id,
        payment_id=payment_row["id"],
        local_ref=local_reference,
        amount=charge_amount,
        phone=collect_phone,
        external_reference=external_reference,
    )

    # Read payment_method from the form selection
    payment_method = (request.form.get("payment_method") or "").strip()

    # Backend validation: ensure payment method matches phone network
    detected_network = detect_phone_network(collect_phone)
    if payment_method == "mtn_momo" and detected_network == "orange":
        conn.close()
        flash("MTN MoMo is not available for this phone number. The detected network is Orange Money.", "error")
        return redirect(url_for("select_boost_payment", listing_id=listing_id))

    if payment_method == "orange_money" and detected_network == "mtn":
        conn.close()
        flash("Orange Money is not available for this phone number. The detected network is MTN MoMo.", "error")
        return redirect(url_for("select_boost_payment", listing_id=listing_id))

    campay_result = campay.request_collect(
        phone=collect_phone,
        amount=charge_amount,
        external_reference=external_reference,
        external_user=str(get_session_user_id() or ""),
        description="Boost Listing",
        payment_method=payment_method,
    )

    provider_reference = (campay_result.get("reference") or "").strip()
    log_payment_event(
        "collect_response",
        payment_id=payment_row["id"],
        local_ref=local_reference,
        provider_ref=provider_reference,
        ok=bool(campay_result.get("ok")),
        status_code=campay_result.get("status_code"),
        provider_status=campay_result.get("status"),
        network_error=bool(campay_result.get("network_error")),
    )

    if provider_reference:
        update_payment_provider_reference(cursor, local_reference, provider_reference)

    if not campay_result.get("ok"):
        apply_payment_status_transition(
            cursor,
            payment_row["id"],
            payment_row["status"],
            PAYMENT_STATUS_FAILED,
        )
        conn.commit()
        conn.close()

        # Determine the specific failure reason
        status_code = campay_result.get("status_code", 0)
        provider_status = (campay_result.get("status") or "").strip().lower()
        campay_error = str(campay_result.get("error") or "").strip().lower()
        network_error = campay_result.get("network_error", False)

        # Check if it's a "recent request exists" / "duplicate" scenario
        if "recent" in campay_error or "duplicate" in campay_error or status_code == 409:
            flash(
                "A recent payment request was detected. Please wait a moment before trying again.",
                "warning",
            )
        elif network_error:
            flash(
                "Mobile network temporarily unavailable. Please try again.",
                "error",
            )
        elif "rejected" in campay_error or "declined" in campay_error:
            flash("Payment was rejected. Check your mobile money account, then try again.", "error")
        elif "cancelled" in campay_error or "cancel" in campay_error:
            flash("Payment was cancelled. Start a new boost request when you are ready to try again.", "error")
        elif "timeout" in campay_error or "expired" in campay_error or "timed" in campay_error:
            flash("Payment request timed out. Start a new boost request to try again.", "error")
        else:
            flash(payment_provider_feedback(campay_result, "Payment request could not be sent."), "error")

        return redirect(url_for("listing_detail", id=listing_id))

    pay_url = (campay_result.get("pay_url") or "").strip()
    is_direct_method = payment_method in ("mtn_momo", "orange_money")

    conn.commit()
    conn.close()

    # For MTN / Orange direct methods: NEVER redirect to checkout page
    if pay_url and not is_direct_method:
        flash("Redirecting you to CamerPay to complete payment.", "success")
        return redirect(pay_url)

    if is_direct_method:
        ussd_code = "*126#" if payment_method == "mtn_momo" else "#157#"
        flash(
            f"✅ Payment request sent to your phone number ending in ***{collect_phone[-4:]}. "
            f"Approve the prompt on your phone to complete payment. "
            f"If you don't see a prompt, dial {ussd_code} to complete.",
            "success",
        )
    else:
        flash(
            "⚡ Payment initiated. Check the status below — once CamerPay confirms completion, "
            "your listing will be featured automatically.",
            "success",
        )

    return redirect(
        url_for(
            "verify_listing_payment",
            reference_id=local_reference,
            next=next_url,
        )
    )


@app.route("/payments/recover/<reference_id>", methods=["POST"])
@login_required
def recover_listing_payment(reference_id):
    """Attempt to recover a payment by querying the CamerPay status endpoint.

    Use this when the webhook was missed or delayed. If CamerPay confirms the
    payment is completed, the local payment record is updated and the listing
    boost is activated. The operation is idempotent.

    The recover button is only available on listings that are not yet featured
    but have a pending payment with a provider_reference.
    """
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    if not owner_phone:
        conn.close()
        flash("You are not authorized to recover this payment.", "error")
        return redirect(url_for("dashboard"))

    payment = get_payment_for_owner(cursor, reference_id, owner_phone)
    if payment is None:
        conn.close()
        flash("Payment not found or not allowed.", "error")
        return redirect(url_for("dashboard"))

    listing_id = payment["listing_id"]
    next_url = sanitize_next_url(request.form.get("next") or request.args.get("next") or url_for("listing_detail", id=listing_id))

    # Idempotent: if already successful and featured, just confirm.
    current_status = resolve_internal_payment_status(payment["status"]) or PAYMENT_STATUS_PENDING
    if current_status == PAYMENT_STATUS_SUCCESSFUL and payment["is_featured"] == 1:
        conn.close()
        return redirect(url_for("verify_listing_payment", reference_id=reference_id, next=next_url))

    provider_reference = get_payment_provider_reference(payment)
    if not provider_reference:
        conn.close()
        flash("No provider reference stored. Cannot attempt recovery.", "error")
        return redirect(next_url)

    log_payment_event(
        "recovery_attempt",
        payment_id=payment["id"],
        local_ref=get_payment_local_reference(payment),
        provider_ref=provider_reference,
        current_status=current_status,
    )

    # Query CamerPay for the actual transaction status.
    status_result = campay.get_transaction_status(provider_reference)
    provider_status = resolve_internal_payment_status(status_result.get("status"))
    if not provider_status and isinstance(status_result.get("data"), dict):
        provider_status = resolve_internal_payment_status(status_result["data"].get("status"))

    log_payment_event(
        "recovery_status_response",
        payment_id=payment["id"],
        local_ref=get_payment_local_reference(payment),
        provider_ref=provider_reference,
        provider_status=provider_status or status_result.get("status"),
        ok=bool(status_result.get("ok")),
        status_code=status_result.get("status_code"),
        network_error=bool(status_result.get("network_error")),
    )

    # Network failure — cannot determine status.
    if status_result.get("network_error"):
        conn.close()
        flash("We could not reach CamerPay right now. Please wait a moment and check again.", "warning")
        return redirect(url_for("verify_listing_payment", reference_id=reference_id, next=next_url))

    # No clear status returned — treat as indeterminate.
    if not provider_status:
        conn.close()
        flash("We could not confirm your payment status yet. Please wait a moment and check again.", "warning")
        return redirect(url_for("verify_listing_payment", reference_id=reference_id, next=next_url))

    # Update the local payment record.
    apply_payment_status_transition(
        cursor,
        payment["id"],
        current_status,
        provider_status,
    )

    if provider_status == PAYMENT_STATUS_SUCCESSFUL:
        payment = get_payment_by_id(cursor, payment["id"])
        apply_successful_payment(cursor, payment)
        conn.commit()
        conn.close()
        return redirect(url_for("verify_listing_payment", reference_id=reference_id, next=next_url))

    conn.commit()
    conn.close()
    return redirect(url_for("verify_listing_payment", reference_id=reference_id, next=next_url))


@app.route("/payments/verify/<reference_id>")
@login_required
def verify_listing_payment(reference_id):
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    if not owner_phone:
        conn.close()
        flash("You are not allowed to verify this payment.", "error")
        return redirect(url_for("dashboard"))

    payment = get_payment_for_owner(cursor, reference_id, owner_phone)
    if payment is None:
        conn.close()
        flash("Payment not found or not allowed.", "error")
        return redirect(url_for("dashboard"))

    listing_url = request.args.get("next") or url_for("listing_detail", id=payment["listing_id"])
    next_url = sanitize_next_url(listing_url)

    expire_stale_pending_payments(cursor, payment["listing_id"])
    payment = get_payment_for_owner(cursor, reference_id, owner_phone)
    if payment is None:
        conn.close()
        flash("Payment not found or not allowed.", "error")
        return redirect(url_for("dashboard"))

    current_status = resolve_internal_payment_status(payment["status"]) or PAYMENT_STATUS_PENDING
    payment_view = build_payment_status_view({
        "status": payment["status"],
        "is_featured": payment["is_featured"],
        "provider_reference": get_payment_provider_reference(payment),
    })

    if current_status == PAYMENT_STATUS_SUCCESSFUL and payment["is_featured"] == 1:
        conn.close()
        return render_template(
            "payment_status.html",
            payment=payment,
            payment_view=payment_view,
            next_url=next_url,
            listing_id=payment["listing_id"],
        )

    provider_reference = get_payment_provider_reference(payment)
    if not provider_reference:
        apply_payment_status_transition(
            cursor,
            payment["id"],
            current_status,
            PAYMENT_STATUS_FAILED,
        )
        conn.commit()
        conn.close()
        payment_view = build_payment_status_view({
            "status": PAYMENT_STATUS_FAILED,
            "is_featured": 0,
            "provider_reference": "",
        })
        return render_template(
            "payment_status.html",
            payment=payment,
            payment_view=payment_view,
            next_url=next_url,
            listing_id=payment["listing_id"],
        )

    log_payment_event(
        "verify_request",
        payment_id=payment["id"],
        local_ref=get_payment_local_reference(payment),
        provider_ref=provider_reference,
    )
    status_result = campay.get_transaction_status(provider_reference)
    provider_status = resolve_internal_payment_status(status_result.get("status"))

    if not provider_status and isinstance(status_result.get("data"), dict):
        provider_status = resolve_internal_payment_status(status_result["data"].get("status"))

    log_payment_event(
        "verify_response",
        payment_id=payment["id"],
        local_ref=get_payment_local_reference(payment),
        provider_ref=provider_reference,
        ok=bool(status_result.get("ok")),
        status_code=status_result.get("status_code"),
        provider_status=provider_status or status_result.get("status"),
        network_error=bool(status_result.get("network_error")),
    )

    if status_result.get("network_error") or not provider_status:
        app.logger.info(
            "verify_api_error payment_id=%d provider_ref=%s network_error=%s status_code=%s provider_status=%s — keeping current status",
            payment["id"],
            provider_reference,
            bool(status_result.get("network_error")),
            status_result.get("status_code"),
            provider_status or "(empty)",
        )
        if status_result.get("network_error") or not status_result.get("status_code"):
            conn.close()
            return render_template(
                "payment_status.html",
                payment=payment,
                payment_view=build_payment_status_view({
                    "status": current_status,
                    "is_featured": payment["is_featured"],
                    "provider_reference": provider_reference,
                }),
                next_url=next_url,
                listing_id=payment["listing_id"],
            )
        # Has a valid HTTP response (e.g. 404, 500) — surface the status from it
        # but do NOT force FAILED; use what the API actually returned.
        provider_status = provider_status or PAYMENT_STATUS_PENDING
        app.logger.info(
            "verify_api_error_fallback payment_id=%d status_code=%s falling_back_to=%s",
            payment["id"],
            status_result.get("status_code"),
            provider_status,
        )
        conn.close()
        return render_template(
            "payment_status.html",
            payment=payment,
            payment_view=build_payment_status_view({
                "status": provider_status,
                "is_featured": payment["is_featured"],
                "provider_reference": provider_reference,
            }),
            next_url=next_url,
            listing_id=payment["listing_id"],
        )

    payload = status_result.get("data")
    if isinstance(payload, dict):
        remote_phone = normalize_phone(payload.get("phone_number") or payload.get("from"))
        update_payment_phone_record(cursor, get_payment_local_reference(payment), remote_phone)

    apply_payment_status_transition(
        cursor,
        payment["id"],
        current_status,
        provider_status,
    )

    if provider_status == PAYMENT_STATUS_SUCCESSFUL:
        payment = get_payment_by_id(cursor, payment["id"])
        apply_successful_payment(cursor, payment)
        conn.commit()
        conn.close()
        return render_template(
            "payment_status.html",
            payment=payment,
            payment_view=build_payment_status_view({
                "status": PAYMENT_STATUS_SUCCESSFUL,
                "is_featured": 1,
                "provider_reference": provider_reference,
            }),
            next_url=next_url,
            listing_id=payment["listing_id"],
        )

    conn.commit()
    conn.close()
    return render_template(
        "payment_status.html",
        payment=payment,
        payment_view=build_payment_status_view({
            "status": provider_status,
            "is_featured": payment["is_featured"],
            "provider_reference": provider_reference,
        }),
        next_url=next_url,
        listing_id=payment["listing_id"],
    )


def mock_payment_redirect_url(payment):
    default_next = url_for("listing_detail", id=payment["listing_id"]) if payment else url_for("dashboard")
    return payment_next_url(default_next)


@app.route("/dev/mock-payment-success/<reference_id>")
@development_only
def mock_payment_success(reference_id):
    conn = get_db()
    cursor = conn.cursor()
    payment = get_payment_by_reference(cursor, reference_id)

    if payment is None:
        conn.close()
        flash("Payment not found.", "error")
        return redirect(url_for("dashboard"))

    next_url = mock_payment_redirect_url(payment)
    apply_successful_payment(cursor, payment)
    conn.commit()
    conn.close()

    flash("Mock payment marked successful. Listing is now featured.", "success")
    return redirect(next_url)


@app.route("/dev/mock-payment-failure/<reference_id>")
@development_only
def mock_payment_failure(reference_id):
    conn = get_db()
    cursor = conn.cursor()
    payment = get_payment_by_reference(cursor, reference_id)

    if payment is None:
        conn.close()
        flash("Payment not found.", "error")
        return redirect(url_for("dashboard"))

    next_url = mock_payment_redirect_url(payment)
    update_payment_status_by_id(cursor, payment["id"], PAYMENT_STATUS_FAILED)
    log_payment_event(
        "dev_mock_failure",
        payment_id=payment["id"],
        listing_id=payment["listing_id"],
        local_ref=get_payment_local_reference(payment),
    )
    conn.commit()
    conn.close()

    flash("Mock payment marked failed. Listing was not activated.", "error")
    return redirect(next_url)


@app.route("/payments/camerpay/webhook", methods=["POST"])
def camerpay_webhook():
    """Webhook endpoint for CamerPay payment status updates.

    CamerPay sends POST with form-urlencoded or JSON body with fields:
    - uuid (or transaction_uuid): CamerPay transaction ID
    - invoice_id: our external reference (listing-{id}-payment-{id})
    - status: completed / pending / failed / cancelled / expired
    - amount: transaction amount (may include decimal, e.g. "10000.00")
    - signature: HMAC-SHA256 of uuid|invoice_id|status|amount

    The signature is verified using CAMERPAY_CALLBACK_SECRET.
    Test webhooks (test=1) are always accepted with HTTP 200.
    """
    # Accept both JSON and form-urlencoded payloads
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        payload = {key: value for key, value in request.values.items()}

    app.logger.info(
        "camerpay_webhook_received content_type=%s keys=%s",
        request.content_type or "unknown",
        sorted(payload.keys()),
    )

    if not payload:
        app.logger.warning("camerpay_webhook_rejected: empty payload")
        return jsonify({"ok": False, "message": "Empty payload"}), 400

    # Support both 'uuid' and 'transaction_uuid' field names
    transaction_uuid = str(payload.get("uuid") or payload.get("transaction_uuid") or "").strip()
    invoice_id = str(payload.get("invoice_id") or "").strip()
    raw_status = str(payload.get("status") or "").strip()
    is_test = str(payload.get("test") or "").strip() == "1"

    log_payment_event(
        "webhook_received",
        provider_ref=transaction_uuid,
        invoice_id=invoice_id,
        status=raw_status,
        test=is_test,
    )

    if not transaction_uuid or not raw_status:
        missing = []
        if not transaction_uuid:
            missing.append("uuid/transaction_uuid")
        if not raw_status:
            missing.append("status")
        app.logger.warning(
            "camerpay_webhook_rejected: missing fields=%s keys=%s",
            ",".join(missing),
            sorted(payload.keys()),
        )
        return jsonify({"ok": False, "message": f"Missing required fields: {', '.join(missing)}"}), 400

    # Test webhooks: always accept
    if is_test:
        app.logger.info(
            "camerpay_webhook_test_accepted uuid=%s status=%s",
            transaction_uuid,
            raw_status,
        )
        return jsonify({"ok": True, "message": "Test webhook accepted"}), 200

    # Signature verification (skip for test webhooks)
    callback_secret = (os.getenv("CAMERPAY_CALLBACK_SECRET") or "").strip()
    if callback_secret:
        verification_result = campay.verify_webhook_signature(payload, callback_secret)
        if not verification_result.get("ok"):
            app.logger.warning(
                "camerpay_webhook_signature_invalid uuid=%s invoice_id=%s error=%s",
                transaction_uuid,
                invoice_id,
                verification_result.get("error"),
            )
            return jsonify({"ok": False, "message": verification_result.get("error", "Invalid signature")}), 403

    # Map CamerPay status to internal status
    provider_status = resolve_internal_payment_status(raw_status)
    if not provider_status:
        app.logger.warning(
            "camerpay_webhook_unrecognized_status uuid=%s status=%s",
            transaction_uuid,
            raw_status,
        )
        provider_status = PAYMENT_STATUS_PENDING

    conn = get_db()
    cursor = conn.cursor()

    # Find payment by transaction_uuid (provider_reference) or invoice_id (external_reference)
    payment = find_payment_for_webhook(cursor, transaction_uuid, invoice_id)

    if payment is None:
        conn.close()
        app.logger.info(
            "camerpay_webhook_unmatched uuid=%s invoice_id=%s",
            transaction_uuid,
            invoice_id,
        )
        return jsonify({"ok": True, "message": "Webhook received, payment not found"}), 200

    # Update phone if provided in webhook
    remote_phone = normalize_phone(payload.get("customer_phone") or payload.get("phone_number") or "")
    update_payment_phone_record(cursor, get_payment_local_reference(payment), remote_phone)

    previous_status = resolve_internal_payment_status(payment["status"]) or PAYMENT_STATUS_PENDING

    if provider_status == PAYMENT_STATUS_SUCCESSFUL:
        apply_successful_payment(cursor, payment)
    else:
        apply_payment_status_transition(
            cursor,
            payment["id"],
            previous_status,
            provider_status,
        )

    conn.commit()
    conn.close()

    log_payment_event(
        "webhook_applied",
        payment_id=payment["id"],
        listing_id=payment["listing_id"],
        local_ref=get_payment_local_reference(payment),
        provider_ref=transaction_uuid,
        previous_status=previous_status,
        new_status=provider_status,
    )

    return jsonify({"ok": True, "status": provider_status}), 200


@app.route("/admin")
@admin_required
def admin_panel():
    conn = get_db()
    cursor = conn.cursor()
    ensure_user_verified_column(cursor)
    conn.commit()

    cursor.execute(
        """
        SELECT l.*, u.full_name as owner_name, u.is_verified as owner_is_verified
        FROM listings l
        LEFT JOIN users u ON u.phone = l.owner_phone
        ORDER BY l.id DESC
        """
    )
    rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT u.id, u.full_name, u.phone, u.email, u.auth_provider, u.is_admin, u.is_active, u.is_verified, u.created_at,
               (SELECT COUNT(*) FROM listings l WHERE l.owner_phone = u.phone) AS listing_count
        FROM users u
        ORDER BY u.is_active ASC, u.id DESC
        """
    )
    user_rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT r.id, r.listing_id, r.reporter_user_id, r.reason, r.comment, r.created_at,
               l.title AS listing_title, l.category AS listing_category,
               u.full_name AS reporter_name, u.phone AS reporter_phone, u.email AS reporter_email
        FROM reports r
        LEFT JOIN listings l ON l.id = r.listing_id
        LEFT JOIN users u ON u.id = r.reporter_user_id
        ORDER BY r.id DESC
        """
    )
    report_rows = cursor.fetchall()

    conn.close()

    today = current_date()
    listings = []
    for row in rows:
        leave_date = datetime.strptime(row["leave_date"], "%Y-%m-%d").date()
        days_left = (leave_date - today).days
        is_expired = days_left < 0
        is_soon = 0 <= days_left <= 3

        listings.append(
            {
                "id": row["id"],
                "title": row["title"],
                "price": row["price"],
                "category": row["category"],
                "phone": row["phone"],
                "owner_phone": row["owner_phone"] or "",
                "owner_name": row["owner_name"] or "Unknown",
                "owner_is_verified": bool(row_value(row, "owner_is_verified", 0) == 1),
                "leave_date": row["leave_date"],
                "description": row["description"] or "",
                "image": row["image"],
                "is_featured": row["is_featured"],
                "view_count": row["view_count"],
                "days_left": days_left,
                "is_expired": is_expired,
                "is_soon": is_soon,
            }
        )

    users = []
    for user_row in user_rows:
        users.append(
            {
                "id": user_row["id"],
                "full_name": user_row["full_name"] or "",
                "phone": user_row["phone"] or "",
                "email": user_row["email"] or "",
                "auth_provider": user_row["auth_provider"] or "local",
                "is_admin": bool(user_row["is_admin"] == 1),
                "is_active": bool(user_row["is_active"] == 1),
                "is_verified": bool(row_value(user_row, "is_verified", 0) == 1),
                "created_at": user_row["created_at"] or "",
                "listing_count": user_row["listing_count"] or 0,
            }
        )

    reports = []
    for report_row in report_rows:
        reports.append(
            {
                "id": report_row["id"],
                "listing_id": report_row["listing_id"],
                "reporter_user_id": report_row["reporter_user_id"],
                "reason": report_row["reason"] or "Other",
                "comment": report_row["comment"] or "",
                "created_at": report_row["created_at"] or "",
                "listing_title": report_row["listing_title"] or "",
                "listing_category": report_row["listing_category"] or "",
                "reporter_name": report_row["reporter_name"] or "Unknown",
                "reporter_phone": report_row["reporter_phone"] or "",
                "reporter_email": report_row["reporter_email"] or "",
                "listing_exists": report_row["listing_title"] is not None,
            }
        )

    return render_template("admin.html", listings=listings, users=users, reports=reports)


@app.route("/admin/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def admin_toggle_user_active(user_id):
    admin_user = current_user_record()
    if admin_user and admin_user.get("id") == user_id:
        flash("You cannot deactivate your own admin account.", "error")
        return redirect(url_for("admin_panel"))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, is_active, is_admin FROM users WHERE id = ?", (user_id,))
    target_user = cursor.fetchone()

    if target_user is None:
        conn.close()
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))

    new_status = 0 if target_user["is_active"] == 1 else 1
    cursor.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, user_id))
    conn.commit()
    conn.close()

    if new_status == 1:
        flash("User activated successfully.", "success")
    else:
        flash("User deactivated successfully.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/toggle-verified", methods=["POST"])
@admin_required
def admin_toggle_user_verified(user_id):
    conn = get_db()
    cursor = conn.cursor()
    ensure_user_verified_column(cursor)

    cursor.execute("SELECT id, is_verified FROM users WHERE id = ?", (user_id,))
    target_user = cursor.fetchone()

    if target_user is None:
        conn.close()
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))

    new_status = 0 if row_value(target_user, "is_verified", 0) == 1 else 1
    cursor.execute("UPDATE users SET is_verified = ? WHERE id = ?", (new_status, user_id))
    conn.commit()
    conn.close()

    if new_status == 1:
        flash("Seller marked as verified.", "success")
    else:
        flash("Seller verification removed.", "info")
    return redirect(url_for("admin_panel"))


@app.route("/admin/delete/<int:id>", methods=["POST"])
@admin_required
def admin_delete_listing(id):
    raw_next = request.form.get("next") or ""
    next_url = sanitize_next_url(raw_next or url_for("admin_panel"))
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM listings WHERE id = ?", (id,))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        if raw_next:
            flash("Listing not found.", "error")
            return redirect(next_url)
        return render_error_page(
            404,
            "Listing not found",
            "This listing may have already been deleted.",
        )

    cursor.execute("DELETE FROM listings WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    flash("Listing deleted successfully", "success")
    return redirect(next_url)


@app.route("/admin/reports/<int:report_id>/dismiss", methods=["POST"])
@admin_required
def admin_dismiss_report(report_id):
    """Delete a single report row without touching the listing itself."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM reports WHERE id = ?", (report_id,))
    report = cursor.fetchone()
    if report is None:
        conn.close()
        flash("Report not found.", "error")
        return redirect(url_for("admin_panel"))

    cursor.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    conn.commit()
    conn.close()

    flash("Report dismissed.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/reports/clear", methods=["POST"])
@admin_required
def admin_clear_reports():
    """Bulk-delete all report rows. Reports only \u2014 listings are untouched."""
    confirm = (request.form.get("confirm") or "").strip().lower()
    if confirm not in ("yes", "true", "1"):
        flash("Bulk clear cancelled. Confirmation token missing.", "error")
        return redirect(url_for("admin_panel"))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS total FROM reports")
    total = cursor.fetchone()["total"] or 0
    cursor.execute("DELETE FROM reports")
    conn.commit()
    conn.close()

    flash(f"Cleared {total} report(s). Listings were preserved.", "success")
    return redirect(url_for("admin_panel"))


def _confirm_token_matches(value):
    return (value or "").strip().lower() in {"yes", "true", "1"}


@app.route("/admin/users/deactivate-all", methods=["POST"])
@admin_required
def admin_deactivate_all_users():
    """Deactivate every non-admin user. Admins are always preserved."""
    if not _confirm_token_matches(request.form.get("confirm")):
        flash("Bulk deactivate cancelled. Confirmation token missing.", "error")
        return redirect(url_for("admin_panel"))

    admin_user = current_user_record()
    admin_id = admin_user.get("id") if admin_user else None

    conn = get_db()
    cursor = conn.cursor()
    query = "UPDATE users SET is_active = 0 WHERE is_admin = 0"
    params = []
    if admin_id is not None:
        query += " AND id != ?"
        params.append(admin_id)
    cursor.execute(query, params)
    affected = cursor.rowcount or 0
    conn.commit()
    conn.close()

    flash(f"Deactivated {affected} user(s). Admin accounts were preserved.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/delete-all", methods=["POST"])
@admin_required
def admin_delete_all_users():
    """Hard-delete every non-admin user along with their listings, payments and reports.

    This is destructive: removes user rows, their listings, their payment records,
    and reports they filed or that target their listings.
    Admin accounts are always preserved, and you cannot delete your own account.
    """
    if not _confirm_token_matches(request.form.get("confirm")):
        flash("Bulk delete cancelled. Confirmation token missing.", "error")
        return redirect(url_for("admin_panel"))

    typed_phrase = (request.form.get("typed_phrase") or "").strip()
    if typed_phrase != "DELETE ALL USERS":
        flash('Bulk delete cancelled. Type "DELETE ALL USERS" to confirm.', "error")
        return redirect(url_for("admin_panel"))

    admin_user = current_user_record()
    admin_id = admin_user.get("id") if admin_user else None

    conn = get_db()
    cursor = conn.cursor()

    # Build the set of non-admin user ids we are allowed to delete.
    user_query = "SELECT id, phone FROM users WHERE is_admin = 0"
    user_params = []
    if admin_id is not None:
        user_query += " AND id != ?"
        user_params.append(admin_id)
    cursor.execute(user_query, user_params)
    victims = cursor.fetchall()
    victim_ids = [row["id"] for row in victims]
    victim_phones = [row["phone"] for row in victims if row["phone"]]

    if not victim_ids:
        conn.close()
        flash("No non-admin users to delete.", "info")
        return redirect(url_for("admin_panel"))

    placeholders = ",".join("?" for _ in victim_ids)

    cursor.execute(
        f"SELECT id FROM listings WHERE owner_phone IN ({','.join('?' for _ in victim_phones)})" if victim_phones else "SELECT id FROM listings WHERE 0",
        victim_phones,
    )
    listing_rows = cursor.fetchall()
    listing_ids = [row["id"] for row in listing_rows]

    # Wipe dependent rows first to keep the database tidy.
    if listing_ids:
        listing_placeholders = ",".join("?" for _ in listing_ids)
        cursor.execute(
            f"DELETE FROM payments WHERE listing_id IN ({listing_placeholders})",
            listing_ids,
        )
        cursor.execute(
            f"DELETE FROM reports WHERE listing_id IN ({listing_placeholders})",
            listing_ids,
        )
        cursor.execute(
            f"DELETE FROM listings WHERE id IN ({listing_placeholders})",
            listing_ids,
        )

    cursor.execute(
        f"DELETE FROM reports WHERE reporter_user_id IN ({placeholders})",
        victim_ids,
    )
    cursor.execute(
        f"DELETE FROM users WHERE id IN ({placeholders})",
        victim_ids,
    )
    conn.commit()

    conn.close()

    flash(
        f"Deleted {len(victim_ids)} user(s), {len(listing_ids)} listing(s) and their related data. Admin accounts were preserved.",
        "success",
    )
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/bulk-delete", methods=["POST"])
@admin_required
def admin_bulk_delete_users():
    """Delete selected non-admin users.
    
    Accepts a list of user_ids from the form, verifies none are admins and none
    are the current admin, then deletes them along with their listings, payments,
    and reports. Requires confirmation via the 'confirm' field.
    """
    if not _confirm_token_matches(request.form.get("confirm")):
        flash("Bulk delete cancelled. Confirmation token missing.", "error")
        return redirect(url_for("admin_panel"))

    admin_user = current_user_record()
    admin_id = admin_user.get("id") if admin_user else None

    raw_ids = request.form.getlist("user_ids")
    if not raw_ids:
        flash("No users selected.", "error")
        return redirect(url_for("admin_panel"))

    selected_ids = []
    for rid in raw_ids:
        try:
            selected_ids.append(int(rid))
        except (TypeError, ValueError):
            continue

    if not selected_ids:
        flash("No valid user IDs provided.", "error")
        return redirect(url_for("admin_panel"))

    conn = get_db()
    cursor = conn.cursor()

    # Filter out admins and the current admin
    placeholders = ",".join("?" for _ in selected_ids)
    cursor.execute(
        f"SELECT id, phone, is_admin FROM users WHERE id IN ({placeholders})",
        selected_ids,
    )
    target_rows = cursor.fetchall()

    victim_ids = []
    victim_phones = []
    skipped = 0
    for row in target_rows:
        if row["is_admin"] == 1:
            skipped += 1
            continue
        if admin_id is not None and row["id"] == admin_id:
            skipped += 1
            continue
        victim_ids.append(row["id"])
        if row["phone"]:
            victim_phones.append(row["phone"])

    if not victim_ids:
        conn.close()
        flash("No deletable users found. Admins and your own account are preserved.", "info")
        return redirect(url_for("admin_panel"))

    # Collect listing IDs owned by these users
    listing_ids = []
    if victim_phones:
        phone_placeholders = ",".join("?" for _ in victim_phones)
        cursor.execute(
            f"SELECT id FROM listings WHERE owner_phone IN ({phone_placeholders})",
            victim_phones,
        )
        listing_ids = [row["id"] for row in cursor.fetchall()]

    # Delete dependent data
    if listing_ids:
        listing_ph = ",".join("?" for _ in listing_ids)
        cursor.execute(f"DELETE FROM payments WHERE listing_id IN ({listing_ph})", listing_ids)
        cursor.execute(f"DELETE FROM reports WHERE listing_id IN ({listing_ph})", listing_ids)
        cursor.execute(f"DELETE FROM listings WHERE id IN ({listing_ph})", listing_ids)

    victim_ph = ",".join("?" for _ in victim_ids)
    cursor.execute(f"DELETE FROM reports WHERE reporter_user_id IN ({victim_ph})", victim_ids)
    cursor.execute(f"DELETE FROM users WHERE id IN ({victim_ph})", victim_ids)
    conn.commit()
    conn.close()

    flash(
        f"Deleted {len(victim_ids)} user(s), {len(listing_ids)} listing(s) and their related data. "
        f"{'Skipped ' + str(skipped) + ' admin account(s).' if skipped else ''}",
        "success",
    )
    return redirect(url_for("admin_panel"))


@app.route("/admin/listings/delete-all", methods=["POST"])
@admin_required
def admin_delete_all_listings():
    """Hard-delete every listing. This is destructive."""
    if not _confirm_token_matches(request.form.get("confirm")):
        flash("Bulk delete cancelled. Confirmation token missing.", "error")
        return redirect(url_for("admin_panel"))

    typed_phrase = (request.form.get("typed_phrase") or "").strip()
    if typed_phrase != "DELETE ALL LISTINGS":
        flash('Bulk delete cancelled. Type "DELETE ALL LISTINGS" to confirm.', "error")
        return redirect(url_for("admin_panel"))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM listings")
    listing_rows = cursor.fetchall()
    listing_ids = [row["id"] for row in listing_rows]

    if not listing_ids:
        conn.close()
        flash("No listings to delete.", "info")
        return redirect(url_for("admin_panel"))

    placeholders = ",".join("?" for _ in listing_ids)
    cursor.execute(
        f"DELETE FROM payments WHERE listing_id IN ({placeholders})",
        listing_ids,
    )
    cursor.execute(
        f"DELETE FROM reports WHERE listing_id IN ({placeholders})",
        listing_ids,
    )
    cursor.execute("DELETE FROM listings")
    conn.commit()

    conn.close()

    flash(f"Deleted {len(listing_ids)} listing(s).", "success")
    return redirect(url_for("admin_panel"))


# ---------------------------------------------------------------------------
# Background status poller: periodically checks pending payments against the
# CamerPay status API to activate boosts even when the webhook is lost.
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = env_int("POLL_INTERVAL_SECONDS", 30)


def poll_pending_payments_periodic():
    """Query all pending payments that have a provider_reference and check their
    status via the CamerPay API. This is the fallback for webhooks that never
    arrive (e.g. network issues on CamerPay's side or the user's phone).

    Runs in a background thread managed by APScheduler.
    """
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Only check payments that are still PENDING, have a provider reference
        # to query, and were created within the last 5 minutes (avoids polling
        # payments that are already stale or will be expired by the TTL).
        cutoff = (datetime.utcnow() - timedelta(seconds=PAYMENT_PENDING_TTL_SECONDS)).isoformat(timespec="seconds")
        cursor.execute(
            """
            SELECT p.id, p.listing_id, p.reference AS reference_id, p.provider_reference,
                   p.amount, p.status, p.phone, p.created_at,
                   l.owner_phone, l.is_featured
            FROM payments p
            JOIN listings l ON l.id = p.listing_id
            WHERE p.status = ?
              AND p.provider_reference IS NOT NULL
              AND TRIM(p.provider_reference) != ''
              AND p.created_at >= ?
            ORDER BY p.id ASC
            """,
            (PAYMENT_STATUS_PENDING, cutoff),
        )
        pending_payments = cursor.fetchall()

        if not pending_payments:
            conn.close()
            return

        app.logger.info(
            "poller_tick pending_count=%d",
            len(pending_payments),
        )

        for payment_row in pending_payments:
            provider_reference = str(payment_row["provider_reference"] or "").strip()
            if not provider_reference:
                continue

            try:
                status_result = campay.get_transaction_status(provider_reference)
            except Exception as exc:
                app.logger.error(
                    "poller_status_error payment_id=%d provider_ref=%s error=%s",
                    payment_row["id"],
                    provider_reference,
                    str(exc),
                )
                continue

            provider_status = resolve_internal_payment_status(status_result.get("status"))
            if not provider_status and isinstance(status_result.get("data"), dict):
                provider_status = resolve_internal_payment_status(status_result["data"].get("status"))

            if not provider_status or provider_status == PAYMENT_STATUS_PENDING:
                continue

            log_payment_event(
                "poller_status_resolved",
                payment_id=payment_row["id"],
                local_ref=get_payment_local_reference(payment_row),
                provider_ref=provider_reference,
                ok=bool(status_result.get("ok")),
                status_code=status_result.get("status_code"),
                provider_status=provider_status,
            )

            current_status = resolve_internal_payment_status(payment_row["status"]) or PAYMENT_STATUS_PENDING
            if current_status == PAYMENT_STATUS_SUCCESSFUL and payment_row["is_featured"] == 1:
                continue

            if provider_status == PAYMENT_STATUS_SUCCESSFUL:
                apply_successful_payment(cursor, payment_row)
            else:
                apply_payment_status_transition(
                    cursor,
                    payment_row["id"],
                    current_status,
                    provider_status,
                )

            conn.commit()

        conn.close()

    except Exception as exc:
        app.logger.error("poller_unhandled_error error=%s", str(exc))
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_scheduler = BackgroundScheduler()


def _start_status_poller():
    """Start the background status poller. Safe to call multiple times — the
    scheduler will only be started once.
    """
    if _scheduler.running:
        return

    _scheduler.add_job(
        poll_pending_payments_periodic,
        "interval",
        seconds=POLL_INTERVAL_SECONDS,
        id="camerpay_status_poller",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()
    app.logger.info(
        "status_poller_started interval=%ds",
        POLL_INTERVAL_SECONDS,
    )


# Ensure the poller starts once the app is fully configured.
_start_status_poller()


if __name__ == "__main__":
    app.run(debug=env_flag("FLASK_DEBUG"))

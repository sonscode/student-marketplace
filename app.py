import json
import os
import re
import uuid
import difflib
import hashlib
import ipaddress
from datetime import datetime, timedelta
from functools import wraps

import sqlite3
from flask import Flask, flash, jsonify, render_template, request, redirect, url_for, session
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from flask_dance.contrib.google import make_google_blueprint, google
from dotenv import load_dotenv
import services.campay as campay

# Exceptions for image file
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

def allowed_file(filename):

    return (
        '.' in filename
        and
        filename.rsplit('.', 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )

load_dotenv()


def env_flag(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


if env_flag("OAUTHLIB_INSECURE_TRANSPORT"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-this")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
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


UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def get_db():
    conn = sqlite3.connect("database.db")
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
BOOST_LISTING_AMOUNT = env_int("BOOST_LISTING_AMOUNT", 500)
# Pending payments should not lock listings forever.
# Default TTL: 120s (2 minutes) unless overridden by env.
PAYMENT_PENDING_TTL_SECONDS = env_int("PAYMENT_PENDING_TTL_SECONDS", 120)

# CamPay sandbox enforces a low collect limit (ER201). Override via env if needed.
CAMPAY_DEMO_MAX_BOOST_XAF = env_int("CAMPAY_DEMO_MAX_BOOST_XAF", 25)


def campay_base_url_is_demo() -> bool:
    return "demo.campay.net" in (os.getenv("CAMPAY_BASE_URL") or "").lower()


def effective_boost_amount() -> int:
    if campay_base_url_is_demo():
        return min(BOOST_LISTING_AMOUNT, CAMPAY_DEMO_MAX_BOOST_XAF)
    return BOOST_LISTING_AMOUNT


PAYMENT_STATUS_PENDING = "PENDING"
PAYMENT_STATUS_SUCCESSFUL = "SUCCESSFUL"
PAYMENT_STATUS_FAILED = "FAILED"
PAYMENT_STATUS_CANCELLED = "CANCELLED"
PAYMENT_STATUS_REJECTED = "REJECTED"
PAYMENT_STATUS_EXPIRED = "EXPIRED"


def normalize_payment_status(status):
    return (status or "").strip().upper()


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
            "SELECT id, full_name, phone, email, google_sub, auth_provider, created_at FROM users WHERE id = ?",
            (user_id,),
        )
        row = cursor.fetchone()

    if row is None and user_phone:
        cursor.execute(
            "SELECT id, full_name, phone, email, google_sub, auth_provider, created_at FROM users WHERE phone = ?",
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
        "created_at": row["created_at"],
    }


@app.context_processor
def inject_auth():
    user = current_user_record()
    return {
        "current_user": user,
        "is_logged_in": user is not None,
        "google_oauth_enabled": GOOGLE_OAUTH_ENABLED,
    }


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if current_user_record() is None:
            next_url = request.path
            if request.query_string:
                next_url += "?" + request.query_string.decode("utf-8")
            return redirect(url_for("login", next=next_url))
        return view_func(*args, **kwargs)

    return wrapped


def resolve_internal_payment_status(provider_status):
    normalized = normalize_payment_status(provider_status)
    if not normalized:
        return ""
    if normalized in {"PENDING", "INITIATED", "PROCESSING"}:
        return PAYMENT_STATUS_PENDING
    if normalized in {"SUCCESS", PAYMENT_STATUS_SUCCESSFUL}:
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
    payload = result.get("data")
    if isinstance(payload, dict):
        reason = str(
            payload.get("message")
            or payload.get("reason")
            or payload.get("detail")
            or ""
        ).strip()
        if reason:
            return f"{default_message} ({reason})"

    provider_error = str(result.get("error") or "").strip()
    if provider_error:
        return f"{default_message} ({provider_error})"

    return default_message


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
               l.owner_phone, l.is_featured
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
               l.owner_phone, l.is_featured
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
               l.owner_phone, l.is_featured
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


# creating table
def init_db():
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
            is_featured INTEGER DEFAULT 0
        )
        """
    )

    cursor.execute("PRAGMA table_info(listings)")
    columns = [row[1] for row in cursor.fetchall()]
    if "owner_phone" not in columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN owner_phone TEXT")

    cursor.execute("UPDATE listings SET owner_phone = phone WHERE owner_phone IS NULL OR owner_phone = ''")

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

    cursor.execute("UPDATE users SET auth_provider = 'local' WHERE auth_provider IS NULL OR auth_provider = ''")
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

    conn.commit()
    conn.close()


init_db()


@app.route("/")
def home():
    conn = get_db()
    cursor = conn.cursor()

    category = request.args.get("category")
    search = request.args.get("search")
    user_phone = get_session_phone()

    if category and search:
        query = f"%{search}%"
        cursor.execute(
            """
            SELECT * FROM listings
            WHERE category = ?
            AND (
                title LIKE ?
                OR category LIKE ?
                OR price LIKE ?
                OR phone LIKE ?
                OR description LIKE ?
            )
            ORDER BY is_featured DESC, id DESC
            """,
            (category, query, query, query, query, query),
        )

    elif category:
        cursor.execute(
            """
            SELECT * FROM listings
            WHERE category = ?
            ORDER BY is_featured DESC, id DESC
            """,
            (category,),
        )

    elif search:
        cursor.execute(
            """
            SELECT * FROM listings
            ORDER BY is_featured DESC, id DESC
            """
        )

        all_listings = cursor.fetchall()
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

                    if clean_search in clean_word or similarity > 0.50:
                        matched = True
                        break

                if matched:
                    break

            if matched:
                filtered.append(item)

        listings = filtered

    else:
        cursor.execute(
            """
            SELECT * FROM listings
            ORDER BY is_featured DESC, id DESC
            """
        )

    if "listings" not in locals():
        listings = cursor.fetchall()

    enhanced_listings = []
    for item in listings:
        leave_date = datetime.strptime(item["leave_date"], "%Y-%m-%d").date()
        today = datetime.today().date()
        days_left = (leave_date - today).days
        if days_left < 0:
            continue

        owner_phone = item["owner_phone"] or ""

        enhanced_listings.append(
            {
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
                "can_manage": bool(user_phone and user_phone == owner_phone),
            }
        )

    conn.close()
    return render_template("index.html", listings=enhanced_listings, search=search)


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


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user_record():
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
        SELECT p.id, p.listing_id, p.reference AS reference_id, p.amount, p.status, p.phone, p.created_at
        FROM payments p
        JOIN listings l ON l.id = p.listing_id
        WHERE l.owner_phone = ?
        ORDER BY p.id DESC
        """,
        (owner_phone,),
    )
    payment_rows = cursor.fetchall()

    conn.close()

    today = datetime.today().date()
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

        payment_history.append(
            {
                "listing_id": payment_row["listing_id"],
                "reference_id": payment_row["reference_id"],
                "amount": payment_row["amount"],
                "status": normalized_status,
                "phone": payment_row["phone"] or "",
                "created_at": payment_row["created_at"],
            }
        )

    for row in rows:
        leave_date = datetime.strptime(row["leave_date"], "%Y-%m-%d").date()
        days_left = (leave_date - today).days
        is_expired = days_left < 0
        is_soon = 0 <= days_left <= 3
        is_featured = row["is_featured"] == 1

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
            }
        )

    return render_template(
        "dashboard.html",
        listings=listings,
        stats=stats,
        payment_history=payment_history[:20],
        boost_amount=effective_boost_amount(),
    )


@app.route("/add", methods=["POST"])
@login_required
def add_listing():
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()
    form_data = build_create_form_data(default_phone=owner_phone)

    def render_create_errors(errors):
        conn.close()
        return (
            render_template(
                "create-listing.html",
                user_phone=owner_phone,
                form_data=form_data,
                form_errors=errors,
                description_max_words=DESCRIPTION_MAX_WORDS,
            ),
            400,
        )

    if not owner_phone:
        conn.close()
        return "Not allowed", 403

    image = request.files.get("image")
    if image is None or not image.filename:
        return render_create_errors(["Image is required."])

    safe_name = secure_filename(image.filename)
    if not safe_name:
        return render_create_errors(["Invalid image filename."])

    if not allowed_file(image.filename):
        return render_create_errors(["Invalid image format. Use PNG, JPG, JPEG, or WEBP."])

    validated = validate_listing_fields(
        title_raw=request.form.get("title"),
        price_raw=request.form.get("price"),
        phone_raw=request.form.get("phone") or owner_phone,
        description_raw=request.form.get("description"),
    )
    if validated["errors"]:
        return render_create_errors(validated["errors"])

    leave_date = (request.form.get("leave_date") or "").strip()
    try:
        datetime.strptime(leave_date, "%Y-%m-%d")
    except ValueError:
        return render_create_errors(["Leaving date is invalid."])

    category = (request.form.get("category") or "").strip()
    if not category:
        return render_create_errors(["Category is required."])

    title = validated["title"]
    price = validated["price"]
    phone = validated["phone"]
    description = validated["description"]
    filename = str(uuid.uuid4()) + "_" + safe_name
    image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    is_featured = 0

    cursor.execute(
        "INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, price, category, phone, owner_phone, leave_date, description, filename, is_featured),
    )

    conn.commit()
    conn.close()

    return redirect(url_for("home"))


@app.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    if not owner_phone:
        conn.close()
        return "Not allowed", 403

    cursor.execute("SELECT image FROM listings WHERE id = ? AND owner_phone = ?", (id, owner_phone))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return "Listing not found or not allowed", 404

    image_name = listing["image"]
    if image_name:
        image_path = os.path.join(app.config["UPLOAD_FOLDER"], image_name)
        if os.path.exists(image_path):
            os.remove(image_path)

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
        return "Not allowed", 403

    cursor.execute("SELECT * FROM listings WHERE id = ? AND owner_phone = ?", (id, owner_phone))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return "Listing not found or not allowed", 404

    conn.close()
    return render_template(
        "edit.html",
        listing=listing,
        form_errors=[],
        description_max_words=DESCRIPTION_MAX_WORDS,
    )


@app.route("/update/<int:id>", methods=["POST"])
@login_required
def update_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    owner_phone = get_authenticated_owner_phone()

    if not owner_phone:
        conn.close()
        return "Not allowed", 403

    cursor.execute(
        "SELECT image, category, leave_date, title, price, phone, description, is_featured FROM listings WHERE id = ? AND owner_phone = ?",
        (id, owner_phone),
    )
    current_listing = cursor.fetchone()

    if current_listing is None:
        conn.close()
        return "Listing not found or not allowed", 404

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
    try:
        datetime.strptime(leave_date, "%Y-%m-%d")
    except ValueError:
        return render_edit_errors(["Leaving date is invalid."])

    image = request.files.get("image")
    image_filename = current_image

    if image and image.filename:
        safe_name = secure_filename(image.filename)
        if safe_name:
            image_filename = str(uuid.uuid4()) + "_" + safe_name
            if not allowed_file(image.filename):
                return render_edit_errors(["Invalid image format. Use PNG, JPG, JPEG, or WEBP."])
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))

            if current_image:
                old_image_path = os.path.join(app.config["UPLOAD_FOLDER"], current_image)
                if os.path.exists(old_image_path):
                    os.remove(old_image_path)

    is_featured = 1 if current_listing["is_featured"] == 1 else 0

    cursor.execute(
        "UPDATE listings SET title = ?, price = ?, category = ?, phone = ?, leave_date = ?, description = ?, image = ?, is_featured = ? WHERE id = ? AND owner_phone = ?",
        (title, price, category, phone, leave_date, description, image_filename, is_featured, id, owner_phone),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("home"))


@app.route("/listing/<int:id>")
def listing_detail(id):
    conn = get_db()
    cursor = conn.cursor()
    user_phone = get_session_phone()

    cursor.execute("SELECT * FROM listings WHERE id = ?", (id,))
    item = cursor.fetchone()

    if item is None:
        conn.close()
        return "Listing not found", 404

    can_manage = bool(user_phone and (item["owner_phone"] or "") == user_phone)
    pending_payment_reference = ""
    if can_manage:
        pending_payment = get_pending_payment_for_listing(cursor, id)
        if pending_payment:
            pending_payment_reference = pending_payment["reference_id"]

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

    today = datetime.today().date()
    leave_date = datetime.strptime(item["leave_date"], "%Y-%m-%d").date()
    days_left = (leave_date - today).days

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
        related_items=related_items,
        can_manage=can_manage,
        pending_payment_reference=pending_payment_reference,
        boost_amount=effective_boost_amount(),
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
        flash("A payment is already pending for this listing. Confirm it to activate featured status.", "info")
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
        return redirect(next_url)

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

    campay_result = campay.request_collect(
        phone=collect_phone,
        amount=charge_amount,
        external_reference=external_reference,
        external_user=str(get_session_user_id() or ""),
        description="Boost Listing",
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
        flash(payment_provider_feedback(campay_result, "Payment request could not be sent."), "error")
        return redirect(next_url)

    ussd_code = ""
    if isinstance(campay_result.get("data"), dict):
        ussd_code = str(campay_result["data"].get("ussd_code") or "").strip()

    conn.commit()
    conn.close()
    if ussd_code:
        flash(
            f"Request sent. Phone prompt should appear automatically. If needed, dial {ussd_code}.",
            "success",
        )
    else:
        flash("Request sent. Approve the prompt on your phone, then verify payment.", "success")
    return redirect(
        url_for(
            "verify_listing_payment",
            reference_id=local_reference,
            next=next_url,
        )
    )


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

    listing_url = url_for("listing_detail", id=payment["listing_id"])
    next_url = payment_next_url(listing_url)

    expire_stale_pending_payments(cursor, payment["listing_id"])
    payment = get_payment_for_owner(cursor, reference_id, owner_phone)
    if payment is None:
        conn.close()
        flash("Payment not found or not allowed.", "error")
        return redirect(url_for("dashboard"))

    current_status = resolve_internal_payment_status(payment["status"]) or PAYMENT_STATUS_PENDING
    if current_status == PAYMENT_STATUS_SUCCESSFUL and payment["is_featured"] == 1:
        conn.close()
        flash("Payment already confirmed. Listing is featured.", "success")
        return redirect(next_url)

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
        flash("This payment cannot be verified (missing provider reference). Please retry boosting.", "error")
        return redirect(next_url)

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

    if status_result.get("network_error"):
        conn.close()
        flash(payment_provider_feedback(status_result, "Could not verify payment right now."), "error")
        return redirect(next_url)

    if not provider_status:
        if status_result.get("status_code", 0) > 0:
            provider_status = PAYMENT_STATUS_FAILED
        else:
            conn.close()
            flash(payment_provider_feedback(status_result, "Could not verify payment right now."), "error")
            return redirect(next_url)

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
        flash("Payment successful. Your listing is now featured.", "success")
        return redirect(next_url)

    conn.commit()
    conn.close()

    if provider_status == PAYMENT_STATUS_PENDING:
        flash("Payment is still pending. Complete the prompt on your phone, then verify again.", "info")
    elif provider_status in {PAYMENT_STATUS_CANCELLED, PAYMENT_STATUS_REJECTED}:
        flash("Payment was cancelled or rejected. You can try boosting again to start a new request.", "error")
    elif provider_status == PAYMENT_STATUS_EXPIRED:
        flash("Payment expired. Please start a new boost request.", "error")
    else:
        flash(payment_provider_feedback(status_result, "Payment failed."), "error")
    return redirect(next_url)


@app.route("/payments/campay/webhook", methods=["GET", "POST"])
def campay_webhook():
    payload = request.get_json(silent=True) if request.method == "POST" else None
    if not isinstance(payload, dict) or not payload:
        payload = {key: value for key, value in request.values.items()}

    payment_reference = (payload.get("reference") or "").strip()
    raw_status = (payload.get("status") or "").strip()
    external_reference = (payload.get("external_reference") or "").strip()
    log_payment_event(
        "webhook_received",
        provider_ref=payment_reference,
        status=raw_status,
        external_reference=external_reference,
        payload_keys=sorted(payload.keys()),
    )

    if not payment_reference or not raw_status:
        return jsonify({"ok": False, "message": "Missing reference or status"}), 400

    webhook_key = (os.getenv("CAMPAY_WEBHOOK_KEY") or "").strip()
    if webhook_key:
        verification_result = campay.verify_webhook_signature(payload, webhook_key)
        if not verification_result.get("ok"):
            return jsonify({"ok": False, "message": verification_result.get("error", "Invalid signature")}), 403

    provider_status = resolve_internal_payment_status(raw_status)
    if not provider_status:
        provider_status = PAYMENT_STATUS_PENDING

    conn = get_db()
    cursor = conn.cursor()

    payment = find_payment_for_webhook(cursor, payment_reference, external_reference)

    if payment is None:
        conn.close()
        log_payment_event("webhook_unmatched", provider_ref=payment_reference, external_reference=external_reference)
        return jsonify({"ok": True, "message": "Webhook received, payment not found"}), 200

    remote_phone = normalize_phone(payload.get("phone_number") or payload.get("from"))
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
        provider_ref=payment_reference,
        previous_status=previous_status,
        new_status=provider_status,
    )

    return jsonify({"ok": True, "status": provider_status}), 200

if __name__ == "__main__":
    app.run(debug=env_flag("FLASK_DEBUG"))

# boost_amount
# reference_id
# 
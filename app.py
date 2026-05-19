import os
import re
import uuid
import difflib
import hashlib
import ipaddress
from datetime import datetime
from functools import wraps

import sqlite3
from flask import Flask, flash, render_template, request, redirect, url_for, session
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from flask_dance.contrib.google import make_google_blueprint, google
from dotenv import load_dotenv
from services import momo

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

PAYMENT_STATUS_PENDING = "PENDING"
PAYMENT_STATUS_SUCCESSFUL = "SUCCESSFUL"
PAYMENT_STATUS_FAILED = "FAILED"


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
    if normalized == PAYMENT_STATUS_SUCCESSFUL:
        return PAYMENT_STATUS_SUCCESSFUL
    if normalized in {"FAILED", "REJECTED", "CANCELLED", "TIMEOUT", "EXPIRED"}:
        return PAYMENT_STATUS_FAILED
    return normalized


def payment_provider_feedback(result, default_message):
    payload = result.get("data")
    if not isinstance(payload, dict):
        return default_message

    reason = str(payload.get("reason") or payload.get("message") or "").strip()
    if not reason:
        return default_message
    return f"{default_message} ({reason})"


def create_pending_payment_record(cursor, listing_id, amount):
    reference_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat(timespec="seconds")
    cursor.execute(
        """
        INSERT INTO payments (listing_id, reference_id, amount, status, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (listing_id, reference_id, amount, PAYMENT_STATUS_PENDING, created_at),
    )
    cursor.execute(
        "SELECT id, listing_id, reference_id, amount, status, created_at FROM payments WHERE id = ?",
        (cursor.lastrowid,),
    )
    return cursor.fetchone()


def get_listing_for_owner(cursor, listing_id, owner_phone):
    cursor.execute(
        "SELECT id, title, owner_phone, is_featured FROM listings WHERE id = ? AND owner_phone = ?",
        (listing_id, owner_phone),
    )
    return cursor.fetchone()


def get_pending_payment_for_listing(cursor, listing_id):
    cursor.execute(
        """
        SELECT id, listing_id, reference_id, amount, status, created_at
        FROM payments
        WHERE listing_id = ? AND status = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (listing_id, PAYMENT_STATUS_PENDING),
    )
    return cursor.fetchone()


def get_payment_for_owner(cursor, reference_id, owner_phone):
    cursor.execute(
        """
        SELECT p.id, p.listing_id, p.reference_id, p.amount, p.status, p.created_at,
               l.owner_phone, l.is_featured
        FROM payments p
        JOIN listings l ON l.id = p.listing_id
        WHERE p.reference_id = ? AND l.owner_phone = ?
        LIMIT 1
        """,
        (reference_id, owner_phone),
    )
    return cursor.fetchone()


def update_payment_status_record(cursor, reference_id, status):
    normalized_status = resolve_internal_payment_status(status) or PAYMENT_STATUS_PENDING
    cursor.execute(
        "UPDATE payments SET status = ? WHERE reference_id = ?",
        (normalized_status, reference_id),
    )


def activate_listing_from_payment(cursor, listing_id):
    cursor.execute("UPDATE listings SET is_featured = 1 WHERE id = ?", (listing_id,))


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
            reference_id TEXT NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    cursor.execute("PRAGMA table_info(payments)")
    payment_columns = [row[1] for row in cursor.fetchall()]

    if "listing_id" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN listing_id INTEGER")
    if "reference_id" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN reference_id TEXT")
    if "amount" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN amount INTEGER DEFAULT 0")
    if "status" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN status TEXT DEFAULT 'PENDING'")
    if "created_at" not in payment_columns:
        cursor.execute("ALTER TABLE payments ADD COLUMN created_at TEXT")

    cursor.execute("SELECT id FROM payments WHERE reference_id IS NULL OR TRIM(reference_id) = ''")
    payment_rows_missing_reference = cursor.fetchall()
    for payment_row in payment_rows_missing_reference:
        cursor.execute(
            "UPDATE payments SET reference_id = ? WHERE id = ?",
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

    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_reference_unique ON payments(reference_id)")
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
        SELECT p.id, p.listing_id, p.reference_id, p.amount, p.status, p.created_at
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
        boost_amount=BOOST_LISTING_AMOUNT,
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
        boost_amount=BOOST_LISTING_AMOUNT,
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

    payment_row = create_pending_payment_record(cursor, listing_id, BOOST_LISTING_AMOUNT)
    conn.commit()

    momo_result = momo.request_to_pay(
        reference_id=payment_row["reference_id"],
        phone=owner_phone,
        amount=BOOST_LISTING_AMOUNT,
        external_id=f"listing-{listing_id}-payment-{payment_row['id']}",
    )

    if not momo_result.get("ok"):
        update_payment_status_record(cursor, payment_row["reference_id"], PAYMENT_STATUS_FAILED)
        conn.commit()
        conn.close()
        flash(payment_provider_feedback(momo_result, "Payment request could not be sent."), "error")
        return redirect(next_url)

    conn.close()
    flash("Payment request sent. Approve the prompt on your phone, then verify payment.", "success")
    return redirect(
        url_for(
            "verify_listing_payment",
            reference_id=payment_row["reference_id"],
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

    current_status = resolve_internal_payment_status(payment["status"]) or PAYMENT_STATUS_PENDING
    if current_status == PAYMENT_STATUS_SUCCESSFUL and payment["is_featured"] == 1:
        conn.close()
        flash("Payment already confirmed. Listing is featured.", "success")
        return redirect(next_url)

    status_result = momo.get_request_to_pay_status(reference_id)
    provider_status = resolve_internal_payment_status(status_result.get("status"))

    if not status_result.get("ok") and not provider_status:
        conn.close()
        flash(payment_provider_feedback(status_result, "Could not verify payment right now."), "error")
        return redirect(next_url)

    if not provider_status:
        provider_status = current_status

    update_payment_status_record(cursor, reference_id, provider_status)

    if provider_status == PAYMENT_STATUS_SUCCESSFUL:
        activate_listing_from_payment(cursor, payment["listing_id"])
        conn.commit()
        conn.close()
        flash("Payment successful. Your listing is now featured.", "success")
        return redirect(next_url)

    conn.commit()
    conn.close()

    if provider_status == PAYMENT_STATUS_PENDING:
        flash("Payment is still pending. Complete the MoMo prompt and verify again.", "info")
    else:
        flash(payment_provider_feedback(status_result, "Payment failed or was cancelled."), "error")
    return redirect(next_url)


if __name__ == "__main__":
    app.run(debug=env_flag("FLASK_DEBUG"))

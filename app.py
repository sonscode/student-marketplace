import os
import re
import uuid
import difflib
import hashlib
from datetime import datetime
from functools import wraps

import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from flask_dance.contrib.google import make_google_blueprint, google

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")
app.config["GOOGLE_OAUTH_CLIENT_ID"] = (
    os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    or os.environ.get("GOOGLE_CLIENT_ID")
    or ""
)
app.config["GOOGLE_OAUTH_CLIENT_SECRET"] = (
    os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    or os.environ.get("GOOGLE_CLIENT_SECRET")
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
    if len(phone) == 9:
        phone = "237" + phone

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
    return "88" + numeric[:10]


def generate_unique_google_phone(cursor, google_sub):
    for salt in range(0, 1000):
        candidate = make_google_phone_candidate(google_sub, salt=salt)
        cursor.execute("SELECT id FROM users WHERE phone = ?", (candidate,))
        existing = cursor.fetchone()
        if existing is None:
            return candidate
    raise RuntimeError("Unable to generate a unique phone seed for Google OAuth user")


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
    if current_user_record():
        return redirect(url_for("home"))

    if not GOOGLE_OAUTH_ENABLED:
        return "Google OAuth is not configured yet. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.", 503

    next_url = sanitize_next_url(request.args.get("next") or url_for("home"))
    session["oauth_next"] = next_url
    return redirect(url_for("google.login"))


@app.route("/oauth/google/authorized")
def google_authorized():
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
    return render_template("create-listing.html", user_phone=phone_prefill)


@app.route("/add", methods=["POST"])
@login_required
def add_listing():
    conn = get_db()
    cursor = conn.cursor()

    image = request.files.get("image")
    if image is None or not image.filename:
        conn.close()
        return "Image is required", 400

    safe_name = secure_filename(image.filename)
    if not safe_name:
        conn.close()
        return "Invalid image filename", 400

    description = request.form.get("description", "").strip()
    filename = str(uuid.uuid4()) + "_" + safe_name
    image.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

    owner_phone = get_session_phone()
    phone = normalize_phone(request.form.get("phone")) or owner_phone

    title = request.form["title"]
    price = request.form["price"]
    category = request.form["category"]
    leave_date = request.form["leave_date"]
    is_featured = 1 if "featured" in request.form else 0

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

    user_phone = get_session_phone()
    cursor.execute("SELECT owner_phone, image FROM listings WHERE id = ?", (id,))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return "Listing not found", 404

    owner_phone = listing["owner_phone"] or ""
    if user_phone != owner_phone:
        conn.close()
        return "Not allowed", 403

    image_name = listing["image"]
    if image_name:
        image_path = os.path.join(app.config["UPLOAD_FOLDER"], image_name)
        if os.path.exists(image_path):
            os.remove(image_path)

    cursor.execute("DELETE FROM listings WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))


@app.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    user_phone = get_session_phone()

    cursor.execute("SELECT * FROM listings WHERE id = ?", (id,))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return "Listing not found", 404

    owner_phone = listing["owner_phone"] or ""
    if user_phone != owner_phone:
        conn.close()
        return "Not allowed", 403

    conn.close()
    return render_template("edit.html", listing=listing)


@app.route("/update/<int:id>", methods=["POST"])
@login_required
def update_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    user_phone = get_session_phone()

    cursor.execute("SELECT image, owner_phone, category, leave_date FROM listings WHERE id = ?", (id,))
    current_listing = cursor.fetchone()

    if current_listing is None:
        conn.close()
        return "Listing not found", 404

    owner_phone = current_listing["owner_phone"] or ""
    if user_phone != owner_phone:
        conn.close()
        return "Not allowed", 403

    current_image = current_listing["image"]

    title = request.form["title"]
    price = request.form["price"]
    phone = normalize_phone(request.form.get("phone")) or user_phone
    category = request.form.get("category", "").strip() or current_listing["category"]
    leave_date = request.form.get("leave_date", "").strip() or current_listing["leave_date"]
    description = request.form.get("description", "").strip()
    image = request.files.get("image")
    image_filename = current_image

    if image and image.filename:
        safe_name = secure_filename(image.filename)
        if safe_name:
            image_filename = str(uuid.uuid4()) + "_" + safe_name
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))

            if current_image:
                old_image_path = os.path.join(app.config["UPLOAD_FOLDER"], current_image)
                if os.path.exists(old_image_path):
                    os.remove(old_image_path)

    cursor.execute(
        "UPDATE listings SET title = ?, price = ?, category = ?, phone = ?, leave_date = ?, description = ?, image = ? WHERE id = ?",
        (title, price, category, phone, leave_date, description, image_filename, id),
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
        can_manage=bool(user_phone and (item["owner_phone"] or "") == user_phone),
    )


if __name__ == "__main__":
    app.run(debug=True)

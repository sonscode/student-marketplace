import os
import re
import uuid
import difflib
from markupsafe import Markup
from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
from werkzeug.utils import secure_filename
import sqlite3

app = Flask(__name__)
@app.template_filter('highlight')


def highlight(text, search):
    if text is None:
        return ""

    text = str(text)
    if not search:
        return text

    search = search.strip()
    if not search:
        return text

    # Highlight each search token (works better for multi-word queries).
    terms = [term for term in search.split() if term]
    if not terms:
        return text

    pattern = re.compile(
        r"(" + "|".join(re.escape(term) for term in sorted(set(terms), key=len, reverse=True)) + r")",
        re.IGNORECASE
    )

    highlighted = pattern.sub(
        lambda m: f"<mark>{m.group()}</mark>",
        text
    )

    return Markup(highlighted)

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

#connecting to DB
def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

def normalize_phone(raw_phone):
    if not raw_phone:
        return ""

    phone = re.sub(r'\D', '', raw_phone)

    # If it's a local number (no country code)
    if len(phone) == 9:
        phone = '237' + phone

    return phone

# creating table
def init_db():
    conn=get_db()
    cursor=conn.cursor()
    cursor.execute("""
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
    """)

    cursor.execute("PRAGMA table_info(listings)")
    columns = [row[1] for row in cursor.fetchall()]
    if "owner_phone" not in columns:
        cursor.execute("ALTER TABLE listings ADD COLUMN owner_phone TEXT")

    # backfill old rows
    cursor.execute("UPDATE listings SET owner_phone = phone WHERE owner_phone IS NULL OR owner_phone = ''")

    conn.commit()
    conn.close()
init_db()


@app.route('/')
def home():
    conn=get_db()
    cursor=conn.cursor()

    category = request.args.get('category')
    search = request.args.get('search')
    user_phone = normalize_phone(request.args.get('user_phone'))
    
    # Fetch listings
# Fetch listings

    if category and search:
        query = f"%{search}%"

        cursor.execute("""
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
            """, (category, query, query, query, query, query))

    elif category:
        cursor.execute("""
            SELECT * FROM listings
            WHERE category = ?
            ORDER BY is_featured DESC, id DESC
            """, (category,))

    elif search:
        cursor.execute("""
            SELECT * FROM listings
            ORDER BY is_featured DESC, id DESC
            """)

        all_listings = cursor.fetchall()

        filtered = []

        for item in all_listings:
            # searchable_text = f"{item['title']} {item['category']}"
            searchable_fields = [
                item['title'],
                item['category'],
                item['price'],
                item['phone'],
                item['description']
                ]
            matched = False

            for field in searchable_fields:
                if field is None:
                    continue

                words = field.lower().split()

                for word in words:
                    clean_search = re.sub(r'[^a-zA-Z0-9]', '', search.lower())
                    clean_word = re.sub(r'[^a-zA-Z0-9]', '', word.lower())

                    similarity = difflib.SequenceMatcher(
                        None,
                        clean_search,
                        clean_word
                    ).ratio()

                    if (
                        clean_search in clean_word
                        or similarity > 0.50
                    ):
                        matched = True
                        break

                if matched:
                    break

            if matched:
                filtered.append(item)

        listings = filtered

    else:
        cursor.execute("""
            SELECT * FROM listings
            ORDER BY is_featured DESC, id DESC
            """)
        
    if 'listings' not in locals():
        listings = cursor.fetchall()

    enhanced_listings = []

    for item in listings:
        leave_date = datetime.strptime(item['leave_date'], "%Y-%m-%d").date()
        today = datetime.today().date()
        days_left = (leave_date - today).days
        if days_left < 0:
            continue   # hide expired listings

        owner_phone = item["owner_phone"] or ""

        enhanced_listings.append({
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
            "can_manage": bool(user_phone and user_phone == owner_phone)
        })
    conn.close()
    return render_template("index.html", listings=enhanced_listings, search=search, user_phone=user_phone)

@app.route('/add', methods=['POST'])
def add_listing():
    conn=get_db()
    cursor=conn.cursor()

    image = request.files['image']
    description = request.form['description'].strip()
    # filename = secure_filename(image.filename)
    filename = str(uuid.uuid4()) + "_" + secure_filename(image.filename)
    image.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    phone = normalize_phone(request.form['phone'])
    owner_phone = phone

    title=request.form['title']
    price=request.form['price']
    category = request.form['category']
    leave_date = request.form['leave_date']
    # phone = request.form['phone'].replace('+', '').replace(' ', '')  # Remove + and spaces from phone number
    is_featured = 1 if 'featured' in request.form else 0

    cursor.execute(
        "INSERT INTO listings (title, price, category, phone, owner_phone, leave_date, description, image, is_featured) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, price, category, phone, owner_phone, leave_date, description, filename, is_featured)
    )

    conn.commit()
    conn.close()

    return redirect(url_for('home', user_phone=owner_phone))

@app.route('/create')
def create():
    user_phone = normalize_phone(request.args.get('user_phone'))
    return render_template('create-listing.html', user_phone=user_phone)

@app.route('/delete/<int:id>', methods=['POST'])
def delete_listing(id):
    conn=get_db()
    cursor=conn.cursor()

    user_phone = normalize_phone(request.form.get('user_phone') or request.args.get('user_phone'))
    cursor.execute("SELECT owner_phone, image FROM listings WHERE id = ?", (id,))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return "Listing not found", 404

    owner_phone = listing["owner_phone"] or ""
    if not user_phone or user_phone != owner_phone:
        conn.close()
        return "Not allowed", 403

    image_name = listing["image"]
    if image_name:
        image_path = os.path.join(app.config['UPLOAD_FOLDER'], image_name)
        if os.path.exists(image_path):
            os.remove(image_path)

    cursor.execute("DELETE FROM listings WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('home', user_phone=user_phone))

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    user_phone = normalize_phone(request.args.get('user_phone'))

    cursor.execute("SELECT * FROM listings WHERE id = ?", (id,))
    listing = cursor.fetchone()

    if listing is None:
        conn.close()
        return "Listing not found", 404

    owner_phone = listing["owner_phone"] or ""
    if not user_phone or user_phone != owner_phone:
        conn.close()
        return "Not allowed", 403

    conn.close()
    return render_template('edit.html', listing=listing, user_phone=user_phone)

@app.route('/update/<int:id>', methods=['POST'])
def update_listing(id):
    conn = get_db()
    cursor = conn.cursor()
    user_phone = normalize_phone(request.form.get('user_phone') or request.args.get('user_phone'))

    cursor.execute("SELECT image, owner_phone FROM listings WHERE id = ?", (id,))
    current_listing = cursor.fetchone()

    if current_listing is None:
        conn.close()
        return "Listing not found", 404

    owner_phone = current_listing["owner_phone"] or ""
    if not user_phone or user_phone != owner_phone:
        conn.close()
        return "Not allowed", 403

    current_image = current_listing["image"]

    title = request.form['title']
    price = request.form['price']
    phone = normalize_phone(request.form['phone'])
    description = request.form['description'].strip()
    image = request.files.get('image')
    image_filename = current_image

    if image and image.filename:
        safe_name = secure_filename(image.filename)
        if safe_name:
            image_filename = str(uuid.uuid4()) + "_" + safe_name
            image.save(os.path.join(app.config['UPLOAD_FOLDER'], image_filename))

            # Delete old image after a successful replacement save.
            if current_image:
                old_image_path = os.path.join(app.config['UPLOAD_FOLDER'], current_image)
                if os.path.exists(old_image_path):
                    os.remove(old_image_path)

    cursor.execute(
        "UPDATE listings SET title = ?, price = ?, phone = ?, description = ?, image = ? WHERE id = ?",
        (title, price, phone, description, image_filename, id)
    )
    conn.commit()
    conn.close()

    return redirect(url_for('home', user_phone=user_phone))

@app.route('/listing/<int:id>')
def listing_detail(id):
    conn = get_db()
    cursor = conn.cursor()
    user_phone = normalize_phone(request.args.get('user_phone'))

    cursor.execute("SELECT * FROM listings WHERE id = ?", (id,))
    item = cursor.fetchone()
    conn.close()

    if item is None:
        return "Listing not found", 404

    leave_date = datetime.strptime(item['leave_date'], "%Y-%m-%d").date()
    today = datetime.today().date()
    days_left = (leave_date - today).days

    return render_template(
        'listing_detail.html',
        item=item,
        days_left=days_left,
        user_phone=user_phone,
        can_manage=bool(user_phone and (item["owner_phone"] or "") == user_phone)
    )

if __name__ == '__main__':
    app.run(debug=True)

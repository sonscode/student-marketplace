import os
import re
import uuid
import difflib
from markupsafe import Markup
from flask import Flask, render_template, request, redirect
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
            leave_date TEXT,
            description TEXT, 
            image TEXT,      
            is_featured INTEGER DEFAULT 0
            )
    """)
    conn.commit()
    conn.close()
init_db()


@app.route('/')
def home():
    conn=get_db()
    cursor=conn.cursor()

    category = request.args.get('category')
    search = request.args.get('search')
    
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
            )
            ORDER BY is_featured DESC, id DESC
            """, (category, query, query, query))

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

        enhanced_listings.append({
            "id": item["id"],
            "title": item["title"],
            "price": item["price"],
            "category": item["category"],
            "phone": item["phone"],
            "leave_date": item["leave_date"],
            " ": item["description"],
            "image": item["image"],
            "is_featured": item["is_featured"],
            "days_left": days_left
        })
    conn.close()
    return render_template("index.html", listings=enhanced_listings, search=search)

@app.route('/add', methods=['POST'])
def add_listing():
    conn=get_db()
    cursor=conn.cursor()

    image = request.files['image']
    description = request.form['description']
    # filename = secure_filename(image.filename)
    filename = str(uuid.uuid4()) + "_" + secure_filename(image.filename)
    image.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    raw_phone = request.form['phone']
    phone = re.sub(r'\D', '', raw_phone)

    # If it's a local number (no country code)
    if len(phone) == 9:
        phone = '237' + phone

    title=request.form['title']
    price=request.form['price']
    category = request.form['category']
    leave_date = request.form['leave_date']
    # phone = request.form['phone'].replace('+', '').replace(' ', '')  # Remove + and spaces from phone number
    is_featured = 1 if 'featured' in request.form else 0

    cursor.execute("INSERT INTO listings (title, price, category, phone, leave_date, description, image, is_featured) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (title, price, category, phone, leave_date, description, filename, is_featured))

    conn.commit()
    conn.close()

    return redirect('/')

@app.route('/create')
def create():
    return render_template('create-listing.html')

@app.route('/delete/<int:id>', methods=['POST'])
def delete_listing(id):
    conn=get_db()
    cursor=conn.cursor()
    cursor.execute("DELETE FROM listings WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect('/')

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit_listing(id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM listings WHERE id = ?", (id,))
    listing = cursor.fetchone()
    conn.close()

    return render_template('edit.html', listing=listing)

@app.route('/update/<int:id>', methods=['POST'])
def update_listing(id):
    conn = get_db()
    cursor = conn.cursor()

    title = request.form['title']
    price = request.form['price']
    phone = request.form['phone']

    cursor.execute("UPDATE listings SET title = ?, price = ?, phone = ? WHERE id = ?", (title, price, phone, id))
    conn.commit()
    conn.close()

    return redirect('/')

if __name__ == '__main__':
    app.run(debug=True)

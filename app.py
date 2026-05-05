from flask import Flask, render_template, request, redirect
import sqlite3

app = Flask(__name__)

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
    
    # Fetch listings
    if category:
        cursor.execute("SELECT * FROM listings WHERE category = ? ORDER BY is_featured DESC, id DESC", (category,))
    else:
        cursor.execute("SELECT * FROM listings ORDER BY is_featured DESC, id DESC")

    listings = cursor.fetchall()
    conn.close()
    return render_template("index.html", listings=listings)  

@app.route('/add', methods=['POST'])
def add_listing():
    conn=get_db()
    cursor=conn.cursor()

    title=request.form['title']
    price=request.form['price']
    category = request.form['category']
    is_featured = 1 if 'featured' in request.form else 0

    cursor.execute("INSERT INTO listings (title, price, category, is_featured) VALUES (?, ?, ?, ?)", (title, price, category, is_featured))
    
    conn.commit()
    conn.close()

    return redirect('/')

@app.route('/create')
def create():
    return render_template('create-listing.html')

    # listings = [
    #     {'title': 'Bed', 'price': '20000 XAF'},
    #     {'title': 'Gas cooker', 'price': '15000 XAF'},
    #     {'title': 'Study table', 'price': '10000 XAF'}
    # ]
if __name__ == '__main__':
    app.run(debug=True)
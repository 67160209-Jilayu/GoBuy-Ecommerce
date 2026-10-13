import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "shop.db")
UPLOAD_DIR = os.path.join(BASE, "static", "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
ORDER_STATUSES = ["pending", "paid", "processing", "shipped", "delivered", "cancelled"]
LOW_STOCK_DEFAULT = 5

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


# ---------- database ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, phone TEXT DEFAULT '', is_admin INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS addresses(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  full_name TEXT, line1 TEXT, city TEXT, postal_code TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
  price REAL NOT NULL, stock INTEGER NOT NULL DEFAULT 0,
  low_stock_threshold INTEGER NOT NULL DEFAULT 5,
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  image TEXT DEFAULT '', active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'pending', total REAL NOT NULL,
  ship_name TEXT, ship_line1 TEXT, ship_city TEXT, ship_postal TEXT, ship_phone TEXT,
  note TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
  name TEXT NOT NULL, price REAL NOT NULL, qty INTEGER NOT NULL);
"""

SEED_PRODUCTS = [
    ("Ceramic Pour-Over Set", "Hand-glazed dripper and carafe for a clean, bright cup.", 1290, 14, "Kitchen"),
    ("Linen Apron", "Washed linen apron with adjustable straps and deep pockets.", 690, 25, "Kitchen"),
    ("Bamboo Cutting Board", "Durable, knife-friendly board with juice groove.", 550, 3, "Kitchen"),
    ("Soy Candle - Matcha", "Slow-burning soy wax candle, 40 hours.", 420, 40, "Home"),
    ("Woven Storage Basket", "Seagrass basket, great for blankets or laundry.", 890, 8, "Home"),
    ("Ceramic Plant Pot", "Matte white pot with drainage and bamboo tray.", 350, 2, "Home"),
    ("Organic Cotton Tee", "Soft, relaxed-fit tee in natural dye.", 590, 30, "Apparel"),
    ("Canvas Tote Bag", "Heavyweight canvas tote with inner pocket.", 390, 50, "Apparel"),
    ("Green Tea Sampler", "Five single-origin teas from northern Thailand.", 480, 0, "Pantry"),
    ("Wildflower Honey 350g", "Raw, unfiltered honey from local apiaries.", 320, 22, "Pantry"),
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.execute("INSERT INTO users(name,email,password_hash,is_admin) VALUES(?,?,?,1)",
                     ("Store Owner", "admin@shop.local", generate_password_hash("admin123")))
    if not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        for n, d, p, s, c in SEED_PRODUCTS:
            conn.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (c,))
            cid = conn.execute("SELECT id FROM categories WHERE name=?", (c,)).fetchone()[0]
            conn.execute("INSERT INTO products(name,description,price,stock,category_id) VALUES(?,?,?,?,?)",
                         (n, d, p, s, cid))
    conn.commit()
    conn.close()


# ---------- helpers ----------
@app.before_request
def load_user():
    g.user = None
    if "uid" in session:
        g.user = db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
        if g.user is None:
            session.pop("uid", None)


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not g.user["is_admin"]:
            abort(403)
        return f(*a, **kw)
    return wrapper


def get_cart():
    return session.setdefault("cart", {})


def cart_items():
    cart = get_cart()
    items, total = [], 0.0
    for pid, qty in cart.items():
        p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            continue
        qty = min(int(qty), p["stock"])
        if qty <= 0:
            continue
        sub = p["price"] * qty
        total += sub
        items.append({"p": p, "qty": qty, "sub": sub})
    return items, total


@app.context_processor
def inject():
    count = sum(int(q) for q in session.get("cart", {}).values())
    return {"cart_count": count, "statuses": ORDER_STATUSES, "now": datetime.now()}


@app.template_filter("money")
def money(v):
    return f"฿{v:,.0f}" if float(v).is_integer() else f"฿{v:,.2f}"


def save_image(file):
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        flash("Image must be png, jpg, webp or gif.", "error")
        return None
    name = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    file.save(os.path.join(UPLOAD_DIR, name))
    return "uploads/" + name


# ---------- storefront ----------
@app.route("/")
def catalog():
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", type=int)
    sort = request.args.get("sort", "new")
    sql = "SELECT p.*, c.name AS category FROM products p LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1"
    args = []
    if q:
        sql += " AND (p.name LIKE ? OR p.description LIKE ?)"
        args += [f"%{q}%"] * 2
    if cat:
        sql += " AND p.category_id=?"
        args.append(cat)
    sql += {"price_asc": " ORDER BY p.price ASC", "price_desc": " ORDER BY p.price DESC",
            "name": " ORDER BY p.name"}.get(sort, " ORDER BY p.id DESC")
    products = db().execute(sql, args).fetchall()
    cats = db().execute("SELECT * FROM categories ORDER BY name").fetchall()
    return render_template("catalog.html", products=products, cats=cats, q=q, cat=cat, sort=sort)


@app.route("/product/<int:pid>")
def product(pid):
    p = db().execute("SELECT p.*, c.name AS category FROM products p LEFT JOIN categories c "
                     "ON c.id=p.category_id WHERE p.id=? AND p.active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    return render_template("product.html", p=p)


# ---------- cart ----------
@app.post("/cart/add/<int:pid>")
def cart_add(pid):
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    qty = max(1, request.form.get("qty", 1, type=int))
    cart = get_cart()
    new = cart.get(str(pid), 0) + qty
    if new > p["stock"]:
        new = p["stock"]
        flash(f"Only {p['stock']} in stock.", "error")
    if new > 0:
        cart[str(pid)] = new
        flash(f"Added {p['name']} to cart.", "ok")
    session.modified = True
    return redirect(request.referrer or url_for("cart"))


@app.route("/cart", methods=["GET", "POST"])
def cart():
    if request.method == "POST":
        c = get_cart()
        for key, val in request.form.items():
            if key.startswith("qty_"):
                pid = key[4:]
                try:
                    q = int(val)
                except ValueError:
                    continue
                if q <= 0:
                    c.pop(pid, None)
                else:
                    c[pid] = q
        session.modified = True
        return redirect(url_for("cart"))
    items, total = cart_items()
    return render_template("cart.html", items=items, total=total)


@app.post("/cart/remove/<int:pid>")
def cart_remove(pid):
    get_cart().pop(str(pid), None)
    session.modified = True
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    items, total = cart_items()
    if not items:
        flash("Your cart is empty.", "info")
        return redirect(url_for("catalog"))
    addrs = db().execute("SELECT * FROM addresses WHERE user_id=?", (g.user["id"],)).fetchall()
    if request.method == "POST":
        f = request.form
        sel = f.get("address_id")
        if sel and sel != "new":
            a = db().execute("SELECT * FROM addresses WHERE id=? AND user_id=?",
                             (sel, g.user["id"])).fetchone()
            if not a:
                abort(400)
            ship = (a["full_name"], a["line1"], a["city"], a["postal_code"], a["phone"])
        else:
            ship = tuple(f.get(k, "").strip() for k in ("full_name", "line1", "city", "postal_code", "phone"))
            if not all(ship):
                flash("Please complete all shipping fields.", "error")
                return render_template("checkout.html", items=items, total=total, addrs=addrs, form=f)
            if f.get("save_address"):
                db().execute("INSERT INTO addresses(user_id,full_name,line1,city,postal_code,phone) "
                             "VALUES(?,?,?,?,?,?)", (g.user["id"], *ship))
        # re-check stock atomically
        conn = db()
        for it in items:
            cur = conn.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                               (it["qty"], it["p"]["id"], it["qty"]))
            if cur.rowcount == 0:
                conn.rollback()
                flash(f"Sorry, {it['p']['name']} just ran out of stock.", "error")
                return redirect(url_for("cart"))
        cur = conn.execute(
            "INSERT INTO orders(user_id,status,total,ship_name,ship_line1,ship_city,ship_postal,ship_phone,note) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (g.user["id"], "pending", total, *ship, f.get("note", "").strip()))
        oid = cur.lastrowid
        for it in items:
            conn.execute("INSERT INTO order_items(order_id,product_id,name,price,qty) VALUES(?,?,?,?,?)",
                         (oid, it["p"]["id"], it["p"]["name"], it["p"]["price"], it["qty"]))
        conn.commit()
        session["cart"] = {}
        return redirect(url_for("order_done", oid=oid))
    return render_template("checkout.html", items=items, total=total, addrs=addrs, form={})


@app.route("/order/<int:oid>")
@login_required
def order_done(oid):
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or (o["user_id"] != g.user["id"] and not g.user["is_admin"]):
        abort(404)
    items = db().execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
    return render_template("account/order.html", o=o, items=items)


# ---------- accounts ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not name or "@" not in email or len(pw) < 6:
            flash("Enter your name, a valid email and a password of 6+ characters.", "error")
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            flash("That email is already registered.", "error")
        else:
            cur = db().execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?)",
                               (name, email, generate_password_hash(pw)))
            db().commit()
            session["uid"] = cur.lastrowid
            flash("Welcome! Your account is ready.", "ok")
            return redirect(url_for("catalog"))
    return render_template("account/register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            cart = session.get("cart", {})
            session.clear()
            session["cart"] = cart
            session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("admin_dashboard") if u["is_admin"] else url_for("catalog")
            return redirect(nxt)
        flash("Invalid email or password.", "error")
    return render_template("account/login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("catalog"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name:
            flash("Name is required.", "error")
        else:
            db().execute("UPDATE users SET name=?, phone=? WHERE id=?", (name, phone, g.user["id"]))
            new_pw = request.form.get("new_password", "")
            if new_pw:
                if len(new_pw) < 6 or not check_password_hash(g.user["password_hash"],
                                                              request.form.get("current_password", "")):
                    flash("Password not changed: check current password and use 6+ characters.", "error")
                    return redirect(url_for("account"))
                db().execute("UPDATE users SET password_hash=? WHERE id=?",
                             (generate_password_hash(new_pw), g.user["id"]))
            db().commit()
            flash("Profile updated.", "ok")
        return redirect(url_for("account"))
    orders = db().execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    addrs = db().execute("SELECT * FROM addresses WHERE user_id=?", (g.user["id"],)).fetchall()
    return render_template("account/account.html", orders=orders, addrs=addrs)


@app.post("/account/address")
@login_required
def address_add():
    f = request.form
    vals = tuple(f.get(k, "").strip() for k in ("full_name", "line1", "city", "postal_code", "phone"))
    if all(vals):
        db().execute("INSERT INTO addresses(user_id,full_name,line1,city,postal_code,phone) "
                     "VALUES(?,?,?,?,?,?)", (g.user["id"], *vals))
        db().commit()
        flash("Address saved.", "ok")
    else:
        flash("Please complete all address fields.", "error")
    return redirect(url_for("account") + "#addresses")


@app.post("/account/address/<int:aid>/delete")
@login_required
def address_delete(aid):
    db().execute("DELETE FROM addresses WHERE id=? AND user_id=?", (aid, g.user["id"]))
    db().commit()
    return redirect(url_for("account") + "#addresses")


# ---------- admin ----------
@app.route("/admin")
@admin_required
def admin_dashboard():
    q = db().execute
    stats = {
        "revenue": q("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='cancelled'").fetchone()[0],
        "today": q("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='cancelled' "
                   "AND date(created_at)=date('now')").fetchone()[0],
        "month": q("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='cancelled' "
                   "AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')").fetchone()[0],
        "orders": q("SELECT COUNT(*) FROM orders").fetchone()[0],
        "pending": q("SELECT COUNT(*) FROM orders WHERE status IN ('pending','paid','processing')").fetchone()[0],
        "customers": q("SELECT COUNT(*) FROM users WHERE is_admin=0").fetchone()[0],
    }
    stats["aov"] = stats["revenue"] / max(1, q("SELECT COUNT(*) FROM orders WHERE status!='cancelled'").fetchone()[0])
    recent = q("SELECT o.*, u.name AS customer FROM orders o LEFT JOIN users u ON u.id=o.user_id "
               "ORDER BY o.id DESC LIMIT 6").fetchall()
    low = q("SELECT * FROM products WHERE active=1 AND stock<=low_stock_threshold ORDER BY stock").fetchall()
    daily = q("SELECT date(created_at) d, SUM(total) t FROM orders WHERE status!='cancelled' "
              "AND date(created_at)>=date('now','-6 day') GROUP BY d").fetchall()
    dmap = {r["d"]: r["t"] for r in daily}
    from datetime import timedelta
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    chart = [{"label": d[5:], "value": dmap.get(d, 0)} for d in days]
    top = q("SELECT name, SUM(qty) qty, SUM(qty*price) rev FROM order_items GROUP BY name "
            "ORDER BY rev DESC LIMIT 5").fetchall()
    return render_template("admin/dashboard.html", stats=stats, recent=recent, low=low,
                           chart=chart, cmax=max([c["value"] for c in chart] + [1]), top=top)


@app.route("/admin/orders")
@admin_required
def admin_orders():
    status = request.args.get("status", "")
    sql = "SELECT o.*, u.name AS customer, u.email FROM orders o LEFT JOIN users u ON u.id=o.user_id"
    args = []
    if status in ORDER_STATUSES:
        sql += " WHERE o.status=?"
        args.append(status)
    orders = db().execute(sql + " ORDER BY o.id DESC", args).fetchall()
    return render_template("admin/orders.html", orders=orders, status=status)


@app.route("/admin/orders/<int:oid>", methods=["GET", "POST"])
@admin_required
def admin_order(oid):
    o = db().execute("SELECT o.*, u.name AS customer, u.email FROM orders o "
                     "LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    if request.method == "POST":
        new = request.form.get("status")
        if new in ORDER_STATUSES and new != o["status"]:
            conn = db()
            # restock when cancelling, re-deduct if reviving a cancelled order
            if new == "cancelled" and o["status"] != "cancelled":
                conn.execute("UPDATE products SET stock=stock+(SELECT qty FROM order_items oi WHERE "
                             "oi.order_id=? AND oi.product_id=products.id) WHERE id IN "
                             "(SELECT product_id FROM order_items WHERE order_id=?)", (oid, oid))
            elif o["status"] == "cancelled":
                conn.execute("UPDATE products SET stock=MAX(0,stock-(SELECT qty FROM order_items oi WHERE "
                             "oi.order_id=? AND oi.product_id=products.id)) WHERE id IN "
                             "(SELECT product_id FROM order_items WHERE order_id=?)", (oid, oid))
            conn.execute("UPDATE orders SET status=? WHERE id=?", (new, oid))
            conn.commit()
            flash(f"Order #{oid} marked {new}.", "ok")
        return redirect(url_for("admin_order", oid=oid))
    items = db().execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
    return render_template("admin/order.html", o=o, items=items)


@app.route("/admin/products")
@admin_required
def admin_products():
    q = request.args.get("q", "").strip()
    sql = ("SELECT p.*, c.name AS category FROM products p LEFT JOIN categories c ON c.id=p.category_id")
    args = []
    if q:
        sql += " WHERE p.name LIKE ?"
        args.append(f"%{q}%")
    products = db().execute(sql + " ORDER BY p.stock<=p.low_stock_threshold DESC, p.name", args).fetchall()
    return render_template("admin/products.html", products=products, q=q)


def _product_form(pid=None):
    conn = db()
    p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone() if pid else None
    if pid and not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        try:
            price = float(f.get("price", ""))
            stock = int(f.get("stock", 0))
            thr = int(f.get("low_stock_threshold", LOW_STOCK_DEFAULT))
        except ValueError:
            price = None
        if not name or price is None or price < 0 or stock < 0 or thr < 0:
            flash("Name, a valid price and non-negative stock are required.", "error")
        else:
            cat = f.get("new_category", "").strip()
            if cat:
                conn.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (cat,))
                cid = conn.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone()[0]
            else:
                cid = f.get("category_id", type=int)
            img = save_image(request.files.get("image")) or (p["image"] if p else "")
            active = 1 if f.get("active") else 0
            if p:
                conn.execute("UPDATE products SET name=?,description=?,price=?,stock=?,low_stock_threshold=?,"
                             "category_id=?,image=?,active=? WHERE id=?",
                             (name, f.get("description", ""), price, stock, thr, cid, img, active, pid))
            else:
                conn.execute("INSERT INTO products(name,description,price,stock,low_stock_threshold,"
                             "category_id,image,active) VALUES(?,?,?,?,?,?,?,?)",
                             (name, f.get("description", ""), price, stock, thr, cid, img, active))
            conn.commit()
            flash("Product saved.", "ok")
            return redirect(url_for("admin_products"))
    cats = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    return render_template("admin/product_form.html", p=p, cats=cats)


@app.route("/admin/products/new", methods=["GET", "POST"])
@admin_required
def admin_product_new():
    return _product_form()


@app.route("/admin/products/<int:pid>", methods=["GET", "POST"])
@admin_required
def admin_product_edit(pid):
    return _product_form(pid)


@app.post("/admin/products/<int:pid>/stock")
@admin_required
def admin_stock(pid):
    stock = request.form.get("stock", type=int)
    if stock is not None and stock >= 0:
        db().execute("UPDATE products SET stock=? WHERE id=?", (stock, pid))
        db().commit()
        flash("Stock updated.", "ok")
    return redirect(request.referrer or url_for("admin_products"))


@app.post("/admin/products/<int:pid>/delete")
@admin_required
def admin_product_delete(pid):
    # order history keeps its own copy of name/price, so deleting is safe
    db().execute("DELETE FROM products WHERE id=?", (pid,))
    db().commit()
    flash("Product removed.", "ok")
    return redirect(url_for("admin_products"))


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, msg="You don't have access to this page."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, msg="We couldn't find that page."), 404


os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)

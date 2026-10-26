import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

try:  # optional: load a local .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = bool(DATABASE_URL)          # Neon / Postgres in production, SQLite for local dev
IS_PROD = bool(os.environ.get("RENDER"))
USE_CLOUDINARY = bool(os.environ.get("CLOUDINARY_URL"))
STORE_TZ = ZoneInfo(os.environ.get("STORE_TZ", "Asia/Bangkok"))

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row
if USE_CLOUDINARY:
    import cloudinary
    import cloudinary.uploader

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "shop.db")
UPLOAD_DIR = os.path.join(BASE, "static", "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_VIDEO_EXT = {"mp4", "webm", "mov", "m4v", "ogg"}
ORDER_STATUSES = ["pending", "paid", "processing", "shipped", "delivered", "cancelled"]
LOW_STOCK_DEFAULT = 5

app = Flask(__name__)
if IS_PROD and not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY must be set in production")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(SESSION_COOKIE_SECURE=IS_PROD, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


# ---------- database ----------
class Conn:
    """Thin wrapper so the same `?`-style SQL runs on SQLite and Postgres."""

    def __init__(self):
        if USE_PG:
            # prepare_threshold=None keeps it compatible with Neon's pooled (pgbouncer) endpoint
            self.c = psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)
        else:
            self.c = sqlite3.connect(DB_PATH)
            self.c.row_factory = sqlite3.Row
            self.c.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, args=()):
        if USE_PG:
            sql = sql.replace("?", "%s")
        return self.c.execute(sql, tuple(args))

    def commit(self):
        self.c.commit()

    def rollback(self):
        self.c.rollback()

    def close(self):
        self.c.close()


def first(row):
    """First column of a row (rows are dicts on Postgres, sqlite3.Row on SQLite)."""
    return next(iter(row.values())) if USE_PG else row[0]


def db():
    if "db" not in g:
        g.db = Conn()
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id {PK}, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, phone TEXT DEFAULT '', is_admin INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS addresses(
  id {PK}, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  full_name TEXT, line1 TEXT, city TEXT, postal_code TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS categories(
  id {PK}, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  id {PK}, name TEXT NOT NULL, description TEXT DEFAULT '',
  price DOUBLE PRECISION NOT NULL, stock INTEGER NOT NULL DEFAULT 0,
  low_stock_threshold INTEGER NOT NULL DEFAULT 5,
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  image TEXT DEFAULT '', video TEXT DEFAULT '', active INTEGER DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS orders(
  id {PK}, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'pending', total DOUBLE PRECISION NOT NULL,
  ship_name TEXT, ship_line1 TEXT, ship_city TEXT, ship_postal TEXT, ship_phone TEXT,
  note TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS order_items(
  id {PK}, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
  name TEXT NOT NULL, price DOUBLE PRECISION NOT NULL, qty INTEGER NOT NULL);
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
    conn = Conn()
    if USE_PG:  # several gunicorn workers start at once; serialize schema setup
        conn.execute("SELECT pg_advisory_lock(727274)")
    schema = SCHEMA.replace("{PK}", "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY")
    for stmt in schema.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    # migrate databases created before video support
    if USE_PG:
        conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS video TEXT DEFAULT ''")
    elif "video" not in [r[1] for r in conn.execute("PRAGMA table_info(products)")]:
        conn.execute("ALTER TABLE products ADD COLUMN video TEXT DEFAULT ''")

    if not conn.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
        email = os.environ.get("ADMIN_EMAIL", "admin@shop.local").strip().lower()
        pw = os.environ.get("ADMIN_PASSWORD") or (None if USE_PG else "admin123")
        if pw:
            conn.execute("INSERT INTO users(name,email,password_hash,is_admin) VALUES(?,?,?,1)",
                         ("Store Owner", email, generate_password_hash(pw)))
        else:
            print("WARNING: no admin user. Set ADMIN_EMAIL and ADMIN_PASSWORD to create one.")
    seed = os.environ.get("SEED_DEMO", "0" if USE_PG else "1") == "1"
    if seed and not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        for n, d, p, st, c in SEED_PRODUCTS:
            conn.execute("INSERT INTO categories(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (c,))
            cid = first(conn.execute("SELECT id FROM categories WHERE name=?", (c,)).fetchone())
            conn.execute("INSERT INTO products(name,description,price,stock,category_id) VALUES(?,?,?,?,?)",
                         (n, d, p, st, cid))
    conn.commit()
    conn.close()


# ---------- helpers ----------
@app.before_request
def load_user():
    g.user = None
    if request.endpoint == "static":
        return
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


def to_local(v):
    """DB timestamps are UTC; convert to the store's timezone."""
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return v.replace(tzinfo=timezone.utc).astimezone(STORE_TZ)


@app.template_filter("dt")
def fmt_dt(v):
    return to_local(v).strftime("%Y-%m-%d %H:%M") if v else ""


@app.template_filter("media")
def media_url(path):
    """Cloudinary URLs are absolute; local dev uploads live under /static."""
    if path.startswith("http"):
        return path
    return url_for("static", filename=path)


def delete_media(url, kind):
    """Best-effort removal of a replaced/deleted Cloudinary asset."""
    if not (USE_CLOUDINARY and url and url.startswith("http")):
        return
    m = re.search(r"/upload/(?:v\d+/)?(.+?)(?:\.\w+)?$", url)
    if m:
        try:
            cloudinary.uploader.destroy(m.group(1), resource_type=kind)
        except Exception:
            app.logger.exception("Cloudinary delete failed for %s", url)


def save_media(file, allowed=ALLOWED_EXT, label="Image must be png, jpg, webp or gif.", kind="image"):
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed:
        flash(label, "error")
        return None
    if USE_CLOUDINARY:
        try:
            up = cloudinary.uploader.upload_large if kind == "video" else cloudinary.uploader.upload
            return up(file.stream, resource_type=kind, folder="gobuy")["secure_url"]
        except Exception:
            app.logger.exception("Cloudinary upload failed")
            flash("Upload failed, please try again.", "error")
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
            "VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (g.user["id"], "pending", total, *ship, f.get("note", "").strip()))
        oid = first(cur.fetchone())
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
            cur = db().execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?) RETURNING id",
                               (name, email, generate_password_hash(pw)))
            uid = first(cur.fetchone())
            db().commit()
            session["uid"] = uid
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

    def one(sql, args=()):
        return first(q(sql, args).fetchone())

    def utc(d):
        return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    today0 = datetime.now(STORE_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = today0.replace(day=1)
    live = "status!='cancelled'"
    stats = {
        "revenue": one(f"SELECT COALESCE(SUM(total),0) FROM orders WHERE {live}"),
        "today": one(f"SELECT COALESCE(SUM(total),0) FROM orders WHERE {live} AND created_at>=?", (utc(today0),)),
        "month": one(f"SELECT COALESCE(SUM(total),0) FROM orders WHERE {live} AND created_at>=?", (utc(month0),)),
        "orders": one("SELECT COUNT(*) FROM orders"),
        "pending": one("SELECT COUNT(*) FROM orders WHERE status IN ('pending','paid','processing')"),
        "customers": one("SELECT COUNT(*) FROM users WHERE is_admin=0"),
    }
    stats["aov"] = stats["revenue"] / max(1, one(f"SELECT COUNT(*) FROM orders WHERE {live}"))
    recent = q("SELECT o.*, u.name AS customer FROM orders o LEFT JOIN users u ON u.id=o.user_id "
               "ORDER BY o.id DESC LIMIT 6").fetchall()
    low = q("SELECT * FROM products WHERE active=1 AND stock<=low_stock_threshold ORDER BY stock").fetchall()
    week0 = today0 - timedelta(days=6)
    dmap = {}
    for row in q(f"SELECT created_at, total FROM orders WHERE {live} AND created_at>=?", (utc(week0),)).fetchall():
        d = to_local(row["created_at"]).strftime("%Y-%m-%d")
        dmap[d] = dmap.get(d, 0) + row["total"]
    days = [(week0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
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
            lines = conn.execute("SELECT product_id, qty FROM order_items WHERE order_id=? "
                                 "AND product_id IS NOT NULL", (oid,)).fetchall()
            for ln in lines:
                if new == "cancelled" and o["status"] != "cancelled":
                    conn.execute("UPDATE products SET stock=stock+? WHERE id=?", (ln["qty"], ln["product_id"]))
                elif o["status"] == "cancelled":
                    conn.execute("UPDATE products SET stock=CASE WHEN stock>=? THEN stock-? ELSE 0 END "
                                 "WHERE id=?", (ln["qty"], ln["qty"], ln["product_id"]))
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
                conn.execute("INSERT INTO categories(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (cat,))
                cid = first(conn.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone())
            else:
                cid = f.get("category_id", type=int)
            old_img, old_vid = (p["image"], p["video"]) if p else ("", "")
            img = save_media(request.files.get("image")) or old_img
            vid = save_media(request.files.get("video"), ALLOWED_VIDEO_EXT,
                             "Video must be mp4, webm, mov, m4v or ogg.", "video") or old_vid
            if f.get("remove_video"):
                vid = ""
            if old_img and img != old_img:
                delete_media(old_img, "image")
            if old_vid and vid != old_vid:
                delete_media(old_vid, "video")
            active = 1 if f.get("active") else 0
            if p:
                conn.execute("UPDATE products SET name=?,description=?,price=?,stock=?,low_stock_threshold=?,"
                             "category_id=?,image=?,video=?,active=? WHERE id=?",
                             (name, f.get("description", ""), price, stock, thr, cid, img, vid, active, pid))
            else:
                conn.execute("INSERT INTO products(name,description,price,stock,low_stock_threshold,"
                             "category_id,image,video,active) VALUES(?,?,?,?,?,?,?,?,?)",
                             (name, f.get("description", ""), price, stock, thr, cid, img, vid, active))
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
    p = db().execute("SELECT image, video FROM products WHERE id=?", (pid,)).fetchone()
    if p:
        delete_media(p["image"], "image")
        delete_media(p["video"], "video")
    db().execute("DELETE FROM products WHERE id=?", (pid,))
    db().commit()
    flash("Product removed.", "ok")
    return redirect(url_for("admin_products"))


@app.errorhandler(413)
def too_large(_):
    flash("File too large (max 100 MB).", "error")
    return redirect(request.referrer or url_for("admin_products"))


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

import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

try:  # optional: load a local .env file
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
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
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_FILES_PER_SAVE = 12
MAX_CART_LINES = 50
MAX_QTY = 999
MIN_PASSWORD = 8
NON_UPLOAD_BODY_LIMIT = 2 * 1024 * 1024
AUTO_CONFIRM_DAYS = 7      # shipped orders confirm themselves after this many days
ISSUE_WINDOW_DAYS = 14     # how long after delivery a customer can report a problem
REVIEW_PAGE = 8
MAX_REVIEW_PHOTOS = 3
REVIEW_BODY_LIMIT = 32 * 1024 * 1024
ORDER_STATUSES = ["pending", "paid", "processing", "shipped", "delivered", "cancelled"]
LOW_STOCK_DEFAULT = 5

app = Flask(__name__)
if (IS_PROD or USE_PG) and not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY must be set in production")
if IS_PROD:  # behind Render's proxy: trust one hop for client IP / scheme / host
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(SESSION_COOKIE_SECURE=IS_PROD, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=14))
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024


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


DUMMY_HASH = generate_password_hash("not-a-real-password")

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
  note TEXT DEFAULT '', token TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS product_media(
  id {PK}, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, url TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS order_events(
  id {PK}, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  status TEXT NOT NULL, note TEXT DEFAULT '', actor TEXT DEFAULT '', at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS product_reviews(
  id {PK}, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL, title TEXT DEFAULT '', body TEXT DEFAULT '',
  hidden INTEGER NOT NULL DEFAULT 0, admin_reply TEXT DEFAULT '', replied_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE(product_id, user_id));
CREATE TABLE IF NOT EXISTS review_media(
  id {PK}, review_id INTEGER NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
  url TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS review_votes(
  review_id INTEGER NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY(review_id, user_id));
CREATE TABLE IF NOT EXISTS auth_attempts(
  id {PK}, key TEXT NOT NULL, at TIMESTAMP NOT NULL);
CREATE INDEX IF NOT EXISTS auth_attempts_key ON auth_attempts(key, at);
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

    # orders.token makes checkout idempotent (a double-submit cannot create two orders)
    if USE_PG:
        conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS token TEXT")
    elif "token" not in [r[1] for r in conn.execute("PRAGMA table_info(orders)")]:
        conn.execute("ALTER TABLE orders ADD COLUMN token TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_token_uq ON orders(token)")
    # delivery tracking / receipt confirmation columns
    for col, decl in (("carrier", "TEXT DEFAULT ''"), ("tracking", "TEXT DEFAULT ''"),
                      ("shipped_at", "TIMESTAMP"), ("received_at", "TIMESTAMP"),
                      ("issue", "TEXT DEFAULT ''"), ("issue_at", "TIMESTAMP")):
        if USE_PG:
            conn.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col} {decl}")
        elif col not in [x[1] for x in conn.execute("PRAGMA table_info(orders)")]:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS reviews_product ON product_reviews(product_id)")

    # one-time move of the old single image/video columns into product_media
    for col, kind in (("image", "image"), ("video", "video")):
        conn.execute(
            f"INSERT INTO product_media(product_id,kind,url,position) "
            f"SELECT id,'{kind}',{col},0 FROM products WHERE {col} IS NOT NULL AND {col}!='' "
            f"AND NOT EXISTS (SELECT 1 FROM product_media m WHERE m.product_id=products.id AND m.kind='{kind}')")

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


IntegrityErrors = (psycopg.IntegrityError,) if USE_PG else (sqlite3.IntegrityError,)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.before_request
def guards():
    if request.endpoint == "static" or request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    # only an admin saving a product may send a large body; everyone else is capped before any parsing
    big_ok = bool(g.user and g.user["is_admin"] and request.path.startswith("/admin/products"))
    limit = NON_UPLOAD_BODY_LIMIT
    if big_ok:
        limit = app.config["MAX_CONTENT_LENGTH"]
    elif g.user and re.fullmatch(r"/product/\d+/review", request.path):
        limit = REVIEW_BODY_LIMIT
    if (request.content_length or 0) > limit:
        abort(413)
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf", "")
    if not sent or not expected or not hmac.compare_digest(sent, expected):
        # log *why*: no session cookie reached us (cookie blocked/expired) vs. a stale page's token
        app.logger.warning("CSRF rejected on %s %s: session_has_token=%s form_has_token=%s",
                           request.method, request.path, "csrf" in session, bool(sent))
        msg = "Your session expired. Please try again."
        if is_xhr():
            return jsonify(error=msg), 400
        flash(msg, "error")
        return redirect(back("catalog"))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' https://res.cloudinary.com data: blob:; "
                            "media-src 'self' https://res.cloudinary.com blob:; "
                            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    if IS_PROD:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.endpoint != "static" and request.method == "GET":
        resp.headers.setdefault("Cache-Control", "no-store" if session.get("uid") else "no-cache")
    return resp


def back(default):
    """Redirect target for 'return to previous page' that can never leave this site."""
    ref = request.referrer
    if ref and urlparse(ref).netloc == request.host:
        return ref
    return url_for(default)


def _utc(offset_seconds=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def throttled(key, limit=10, window=900):
    """Counted in the database so the limit holds across all gunicorn workers."""
    n = first(db().execute("SELECT COUNT(*) FROM auth_attempts WHERE key=? AND at>=?",
                           (key, _utc(window))).fetchone())
    return n >= limit


def record(key):
    conn = db()
    conn.execute("INSERT INTO auth_attempts(key, at) VALUES(?,?)", (key, _utc()))
    conn.execute("DELETE FROM auth_attempts WHERE at<?", (_utc(86400),))
    conn.commit()


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
    for pid, qty in list(cart.items()):
        if not str(pid).isdigit() or not isinstance(qty, int):
            continue
        p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (int(pid),)).fetchone()
        if not p:
            continue
        qty = min(qty, p["stock"], MAX_QTY)
        if qty <= 0:
            continue
        sub = p["price"] * qty
        total += sub
        items.append({"p": p, "qty": qty, "sub": sub})
    return items, total


def sync_cart(items):
    """Make the stored cart match what can actually be bought (stock/availability changes)."""
    fresh = {str(it["p"]["id"]): it["qty"] for it in items}
    if fresh != {k: v for k, v in get_cart().items()}:
        session["cart"] = fresh
        flash("Some items in your cart changed because of stock or availability.", "info")


@app.context_processor
def inject():
    count = sum(int(q) for q in session.get("cart", {}).values())
    return {"cart_count": count, "statuses": ORDER_STATUSES, "now": datetime.now(), "csrf_token": csrf_token}


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


@app.template_filter("day")
def fmt_day(v):
    return to_local(v).strftime("%d %b %Y") if v else ""


@app.template_filter("reviewer")
def reviewer_name(name):
    """Privacy-friendly display name: 'Somchai K.'"""
    parts = (name or "Customer").split()
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0]}."


@app.template_filter("media")
def media_url(path):
    """Cloudinary URLs are absolute; local dev uploads live under /static."""
    if path.startswith("http"):
        return path
    return url_for("static", filename=path)


@app.template_filter("poster")
def poster_url(url):
    """Cloudinary can render a still frame of a video by swapping the extension."""
    if url.startswith("http") and "/video/upload/" in url:
        return re.sub(r"\.\w+$", ".jpg", url)
    return ""


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
        except Exception as e:  # only admins can reach this, so showing the reason is safe
            app.logger.exception("Cloudinary upload failed")
            flash(f"Upload of {file.filename} failed: {e}", "error")
            return None
    if IS_PROD:  # Render's disk is wiped on every restart/deploy: never keep media there
        flash("Image storage is not configured. Set CLOUDINARY_URL in Render's environment variables.", "error")
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
    sql = ("SELECT p.*, c.name AS category, rv.avg_r, rv.n_r FROM products p "
           "LEFT JOIN categories c ON c.id=p.category_id "
           "LEFT JOIN (SELECT product_id, CAST(AVG(rating) AS DOUBLE PRECISION) AS avg_r, COUNT(*) AS n_r "
           "FROM product_reviews WHERE hidden=0 GROUP BY product_id) rv ON rv.product_id=p.id "
           "WHERE p.active=1")
    args = []
    if q:
        sql += " AND (p.name LIKE ? OR p.description LIKE ?)"
        args += [f"%{q}%"] * 2
    if cat:
        sql += " AND p.category_id=?"
        args.append(cat)
    sql += {"price_asc": " ORDER BY p.price ASC", "price_desc": " ORDER BY p.price DESC",
            "name": " ORDER BY p.name",
            "rating": " ORDER BY COALESCE(rv.avg_r,0) DESC, COALESCE(rv.n_r,0) DESC"}.get(sort, " ORDER BY p.id DESC")
    products = db().execute(sql, args).fetchall()
    cats = db().execute("SELECT * FROM categories ORDER BY name").fetchall()
    return render_template("catalog.html", products=products, cats=cats, q=q, cat=cat, sort=sort)


def can_review(conn, uid, pid):
    """Only people whose order containing this product was confirmed received may review it."""
    return bool(conn.execute("SELECT 1 FROM orders o JOIN order_items i ON i.order_id=o.id "
                             "WHERE o.user_id=? AND i.product_id=? AND o.status='delivered' LIMIT 1",
                             (uid, pid)).fetchone())


def load_reviews(conn, pid, sort, stars, page, uid):
    base = "FROM product_reviews r JOIN users u ON u.id=r.user_id WHERE r.product_id=? AND r.hidden=0"
    args = [pid]
    if stars in (1, 2, 3, 4, 5):
        base += " AND r.rating=?"
        args.append(stars)
    total = first(conn.execute(f"SELECT COUNT(*) {base}", args).fetchone())
    order = {"helpful": "helpful DESC, r.created_at DESC", "high": "r.rating DESC, r.created_at DESC",
             "low": "r.rating ASC, r.created_at DESC"}.get(sort, "r.created_at DESC")
    rows = [dict(x) for x in conn.execute(
        f"SELECT r.*, u.name AS author, (SELECT COUNT(*) FROM review_votes v WHERE v.review_id=r.id) AS helpful "
        f"{base} ORDER BY {order} LIMIT ? OFFSET ?", (*args, REVIEW_PAGE, (page - 1) * REVIEW_PAGE)).fetchall()]
    ids = [x["id"] for x in rows]
    photos, voted = {}, set()
    if ids:
        marks = ",".join("?" * len(ids))
        for m in conn.execute(f"SELECT review_id, url FROM review_media WHERE review_id IN ({marks}) "
                              f"ORDER BY position, id", ids).fetchall():
            photos.setdefault(m["review_id"], []).append(m["url"])
        if uid:
            voted = {v["review_id"] for v in conn.execute(
                f"SELECT review_id FROM review_votes WHERE user_id=? AND review_id IN ({marks})",
                (uid, *ids)).fetchall()}
    for x in rows:
        x["photos"] = photos.get(x["id"], [])
        x["voted"] = x["id"] in voted
    return rows, total


@app.route("/product/<int:pid>")
def product(pid):
    conn = db()
    p = conn.execute("SELECT p.*, c.name AS category FROM products p LEFT JOIN categories c "
                     "ON c.id=p.category_id WHERE p.id=? AND p.active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    media = conn.execute("SELECT * FROM product_media WHERE product_id=? ORDER BY position, id", (pid,)).fetchall()
    dist = {k: 0 for k in (5, 4, 3, 2, 1)}
    for row in conn.execute("SELECT rating, COUNT(*) AS n FROM product_reviews WHERE product_id=? AND hidden=0 "
                            "GROUP BY rating", (pid,)).fetchall():
        dist[row["rating"]] = row["n"]
    count = sum(dist.values())
    avg = sum(k * v for k, v in dist.items()) / count if count else 0
    sort = request.args.get("rsort", "recent")
    stars = request.args.get("stars", type=int)
    page = max(1, request.args.get("rpage", 1, type=int) or 1)
    reviews, filtered = load_reviews(conn, pid, sort, stars, page, g.user["id"] if g.user else None)
    mine, eligible = None, False
    if g.user:
        mine = conn.execute("SELECT * FROM product_reviews WHERE product_id=? AND user_id=?",
                            (pid, g.user["id"])).fetchone()
        eligible = bool(mine) or can_review(conn, g.user["id"], pid)
    return render_template("product.html", p=p, media=media, dist=dist, count=count, avg=avg,
                           reviews=reviews, filtered=filtered, rsort=sort, star_filter=stars, page=page,
                           pages=max(1, -(-filtered // REVIEW_PAGE)), mine=mine, eligible=eligible)


@app.route("/product/<int:pid>/review", methods=["GET", "POST"])
@login_required
def review_form(pid):
    conn = db()
    p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    mine = conn.execute("SELECT * FROM product_reviews WHERE product_id=? AND user_id=?",
                        (pid, g.user["id"])).fetchone()
    if not mine and not can_review(conn, g.user["id"], pid):
        flash("You can review a product once your order containing it has been marked as received.", "info")
        return redirect(url_for("product", pid=pid))
    photos = conn.execute("SELECT * FROM review_media WHERE review_id=? ORDER BY position, id",
                          (mine["id"],)).fetchall() if mine else []
    if request.method == "POST":
        f = request.form
        rating = f.get("rating", type=int)
        title, body = f.get("title", "").strip()[:100], f.get("body", "").strip()[:2000]
        if rating not in (1, 2, 3, 4, 5):
            flash("Please choose a star rating.", "error")
            return jsonify(redirect=request.url) if is_xhr() else render_template(
                "review_form.html", p=p, mine=mine, photos=photos, form=f)
        if not mine and throttled(f"rev:{g.user['id']}", limit=20, window=86400):
            flash("You've posted a lot of reviews today. Please try again tomorrow.", "error")
            return done(url_for("product", pid=pid))
        now = _utc()
        if mine:
            rid = mine["id"]
            conn.execute("UPDATE product_reviews SET rating=?, title=?, body=?, updated_at=? WHERE id=?",
                         (rating, title, body, now, rid))
        else:
            rid = first(conn.execute(
                "INSERT INTO product_reviews(product_id,user_id,rating,title,body,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) RETURNING id", (pid, g.user["id"], rating, title, body, now, now)).fetchone())
            record(f"rev:{g.user['id']}")
        keep = set(f.getlist("keep", type=int))
        kept = 0
        for ph in photos:
            if ph["id"] in keep:
                kept += 1
            else:
                conn.execute("DELETE FROM review_media WHERE id=?", (ph["id"],))
                delete_media(ph["url"], "image")
        for fl in [x for x in request.files.getlist("media") if x and x.filename][:MAX_REVIEW_PHOTOS]:
            if kept >= MAX_REVIEW_PHOTOS:
                flash(f"Only {MAX_REVIEW_PHOTOS} photos per review.", "error")
                break
            if fl.filename.rsplit(".", 1)[-1].lower() not in ALLOWED_EXT:
                flash(f"{fl.filename}: photos must be png, jpg, webp or gif.", "error")
                continue
            if file_size(fl) > MAX_IMAGE_BYTES:
                flash(f"{fl.filename}: too large (max 10 MB).", "error")
                continue
            url = save_media(fl, ALLOWED_EXT, "", "image")
            if url:
                conn.execute("INSERT INTO review_media(review_id,url,position) VALUES(?,?,?)", (rid, url, kept))
                kept += 1
        conn.commit()
        flash("Thanks! Your review has been saved.", "ok")
        return done(url_for("product", pid=pid) + "#reviews")
    return render_template("review_form.html", p=p, mine=mine, photos=photos, form={})


def _delete_review(conn, rid):
    for m in conn.execute("SELECT url FROM review_media WHERE review_id=?", (rid,)).fetchall():
        delete_media(m["url"], "image")
    conn.execute("DELETE FROM review_media WHERE review_id=?", (rid,))
    conn.execute("DELETE FROM review_votes WHERE review_id=?", (rid,))
    conn.execute("DELETE FROM product_reviews WHERE id=?", (rid,))


@app.post("/review/<int:rid>/delete")
@login_required
def review_delete(rid):
    conn = db()
    rv = conn.execute("SELECT * FROM product_reviews WHERE id=?", (rid,)).fetchone()
    if not rv or (rv["user_id"] != g.user["id"] and not g.user["is_admin"]):
        abort(404)
    _delete_review(conn, rid)
    conn.commit()
    flash("Review deleted.", "ok")
    return redirect(back("catalog"))


@app.post("/review/<int:rid>/helpful")
@login_required
def review_helpful(rid):
    conn = db()
    rv = conn.execute("SELECT * FROM product_reviews WHERE id=? AND hidden=0", (rid,)).fetchone()
    if not rv:
        abort(404)
    if rv["user_id"] == g.user["id"]:
        flash("You can't vote on your own review.", "info")
    elif conn.execute("SELECT 1 FROM review_votes WHERE review_id=? AND user_id=?", (rid, g.user["id"])).fetchone():
        conn.execute("DELETE FROM review_votes WHERE review_id=? AND user_id=?", (rid, g.user["id"]))
        conn.commit()
    else:
        conn.execute("INSERT INTO review_votes(review_id,user_id) VALUES(?,?)", (rid, g.user["id"]))
        conn.commit()
    return redirect(back("catalog"))


# ---------- order lifecycle ----------
def log_event(conn, oid, status, actor, note=""):
    conn.execute("INSERT INTO order_events(order_id,status,note,actor,at) VALUES(?,?,?,?,?)",
                 (oid, status, note, actor, _utc()))


def set_status(conn, oid, new, actor, note="", carrier=None, tracking=None):
    """Single place where an order changes state: stock, timestamps, tracking and the timeline."""
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    old = o["status"]
    new_carrier = (o["carrier"] or "") if carrier is None else carrier
    new_tracking = (o["tracking"] or "") if tracking is None else tracking
    ship_changed = (new_carrier, new_tracking) != ((o["carrier"] or ""), (o["tracking"] or ""))
    if new == old and not ship_changed:
        return False
    if new != old:
        lines = conn.execute("SELECT product_id, qty FROM order_items WHERE order_id=? "
                             "AND product_id IS NOT NULL", (oid,)).fetchall()
        for ln in lines:  # cancelling returns stock; reviving a cancelled order takes it again
            if new == "cancelled":
                conn.execute("UPDATE products SET stock=stock+? WHERE id=?", (ln["qty"], ln["product_id"]))
            elif old == "cancelled":
                conn.execute("UPDATE products SET stock=CASE WHEN stock>=? THEN stock-? ELSE 0 END WHERE id=?",
                             (ln["qty"], ln["qty"], ln["product_id"]))
        sets, args = ["status=?"], [new]
        if new == "shipped" and not o["shipped_at"]:
            sets.append("shipped_at=?"); args.append(_utc())
        if new == "delivered" and not o["received_at"]:
            sets.append("received_at=?"); args.append(_utc())
        conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", (*args, oid))
    if ship_changed:
        conn.execute("UPDATE orders SET carrier=?, tracking=? WHERE id=?", (new_carrier, new_tracking, oid))
    if new != old:
        log_event(conn, oid, new, actor, note)
    else:
        log_event(conn, oid, new, actor, note or "Tracking details updated")
    return True


def auto_confirm(conn):
    """Orders that were shipped long ago are confirmed automatically (no cron needed: runs on page views)."""
    rows = conn.execute("SELECT id FROM orders WHERE status='shipped' AND COALESCE(shipped_at, created_at)<? "
                        "AND (issue IS NULL OR issue='')", (_utc(AUTO_CONFIRM_DAYS * 86400),)).fetchall()
    for row in rows:
        set_status(conn, row["id"], "delivered", "system",
                   f"Automatically confirmed {AUTO_CONFIRM_DAYS} days after shipping")
    if rows:
        conn.commit()


def own_order(oid):
    o = db().execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, g.user["id"])).fetchone()
    if not o:
        abort(404)
    return o


# ---------- cart ----------
@app.post("/cart/add/<int:pid>")
def cart_add(pid):
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    qty = min(MAX_QTY, max(1, request.form.get("qty", 1, type=int) or 1))
    cart = get_cart()
    if str(pid) not in cart and len(cart) >= MAX_CART_LINES:
        flash("Your cart is full. Please check out or remove an item first.", "error")
        return redirect(url_for("cart"))
    new = cart.get(str(pid), 0) + qty
    if new > p["stock"]:
        new = p["stock"]
        flash(f"Only {p['stock']} in stock.", "error")
    if new > 0:
        cart[str(pid)] = new
        flash(f"Added {p['name']} to cart.", "ok")
    session.permanent = True
    session.modified = True
    return redirect(back("cart"))


@app.route("/cart", methods=["GET", "POST"])
def cart():
    if request.method == "POST":
        c = get_cart()
        for key, val in request.form.items():
            if key.startswith("qty_") and key[4:].isdigit():
                pid = key[4:]
                try:
                    q = int(val)
                except ValueError:
                    continue
                if q <= 0:
                    c.pop(pid, None)
                elif pid in c:  # can only change lines already in the cart
                    c[pid] = min(q, MAX_QTY)
        session.modified = True
        return redirect(url_for("cart"))
    items, total = cart_items()
    sync_cart(items)
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
    if request.method == "GET":
        sync_cart(items)
    if request.method == "POST":
        f = request.form
        token = f.get("token", "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            abort(400)
        sel = f.get("address_id")
        if sel and sel != "new":
            a = db().execute("SELECT * FROM addresses WHERE id=? AND user_id=?",
                             (sel, g.user["id"])).fetchone()
            if not a:
                abort(400)
            ship = (a["full_name"], a["line1"], a["city"], a["postal_code"], a["phone"])
        else:
            ship = tuple(f.get(k, "").strip()[:200] for k in ("full_name", "line1", "city", "postal_code", "phone"))
            if not all(ship):
                flash("Please complete all shipping fields.", "error")
                return render_template("checkout.html", items=items, total=total, addrs=addrs, form=f,
                                       token=uuid.uuid4().hex)
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
        try:
            cur = conn.execute(
                "INSERT INTO orders(user_id,status,total,ship_name,ship_line1,ship_city,ship_postal,ship_phone,"
                "note,token) VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
                (g.user["id"], "pending", round(total, 2), *ship, f.get("note", "").strip()[:500], token))
        except IntegrityErrors:  # same checkout form submitted twice: keep the first order only
            conn.rollback()
            dup = conn.execute("SELECT id FROM orders WHERE token=? AND user_id=?", (token, g.user["id"])).fetchone()
            return redirect(url_for("order_done", oid=dup["id"]) if dup else url_for("cart"))
        oid = first(cur.fetchone())
        for it in items:
            conn.execute("INSERT INTO order_items(order_id,product_id,name,price,qty) VALUES(?,?,?,?,?)",
                         (oid, it["p"]["id"], it["p"]["name"], it["p"]["price"], it["qty"]))
        log_event(conn, oid, "pending", "customer", "Order placed")
        conn.commit()
        session["cart"] = {}
        return redirect(url_for("order_done", oid=oid))
    return render_template("checkout.html", items=items, total=total, addrs=addrs, form={}, token=uuid.uuid4().hex)


@app.route("/order/<int:oid>")
@login_required
def order_done(oid):
    conn = db()
    auto_confirm(conn)
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or (o["user_id"] != g.user["id"] and not g.user["is_admin"]):
        abort(404)
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
    events = conn.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY at, id", (oid,)).fetchall()
    reviewed = {x["product_id"] for x in conn.execute(
        "SELECT product_id FROM product_reviews WHERE user_id=?", (o["user_id"],)).fetchall()}
    can_report = o["status"] == "shipped" or (
        o["status"] == "delivered" and o["received_at"] and
        to_local(o["received_at"]) > datetime.now(STORE_TZ) - timedelta(days=ISSUE_WINDOW_DAYS))
    auto_on = None
    if o["status"] == "shipped" and not o["issue"]:
        auto_on = to_local(o["shipped_at"] or o["created_at"]) + timedelta(days=AUTO_CONFIRM_DAYS)
    return render_template("account/order.html", o=o, items=items, events=events, reviewed=reviewed,
                           can_report=can_report, auto_on=auto_on, mine=o["user_id"] == g.user["id"])


@app.post("/order/<int:oid>/received")
@login_required
def order_received(oid):
    o = own_order(oid)
    if o["status"] != "shipped":
        flash("This order isn't waiting for confirmation.", "info")
    else:
        set_status(db(), oid, "delivered", "customer", "Customer confirmed the parcel was received")
        db().commit()
        flash("Thank you! Your order is complete — you can now review the items you bought.", "ok")
    return redirect(url_for("order_done", oid=oid))


@app.post("/order/<int:oid>/cancel")
@login_required
def order_cancel(oid):
    o = own_order(oid)
    if o["status"] != "pending":
        flash("Only orders that haven't been processed yet can be cancelled. Please report a problem instead.", "error")
    else:
        set_status(db(), oid, "cancelled", "customer", "Cancelled by customer")
        db().commit()
        flash("Your order has been cancelled.", "ok")
    return redirect(url_for("order_done", oid=oid))


@app.post("/order/<int:oid>/issue")
@login_required
def order_issue(oid):
    o = own_order(oid)
    msg = request.form.get("message", "").strip()[:1000]
    recent = o["received_at"] and to_local(o["received_at"]) > datetime.now(STORE_TZ) - timedelta(days=ISSUE_WINDOW_DAYS)
    if o["status"] not in ("shipped", "delivered") or (o["status"] == "delivered" and not recent):
        flash("Problems can only be reported for shipped orders, or within "
              f"{ISSUE_WINDOW_DAYS} days after delivery.", "error")
    elif len(msg) < 10:
        flash("Please describe the problem in a few words (10+ characters).", "error")
    else:
        db().execute("UPDATE orders SET issue=?, issue_at=? WHERE id=?", (msg, _utc(), oid))
        log_event(db(), oid, o["status"], "customer", "Problem reported: " + msg[:200])
        db().commit()
        flash("Your report was sent to the store. We'll get back to you soon.", "ok")
    return redirect(url_for("order_done", oid=oid))


# ---------- accounts ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:100]
        email = request.form.get("email", "").strip().lower()[:254]
        pw = request.form.get("password", "")
        rkey = "reg:" + (request.remote_addr or "")
        if throttled(rkey, limit=10):
            flash("Too many sign-ups from this address. Please try again later.", "error")
        elif not name or "@" not in email or not (MIN_PASSWORD <= len(pw) <= 128):
            flash(f"Enter your name, a valid email and a password of {MIN_PASSWORD}-128 characters.", "error")
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            flash("That email is already registered.", "error")
        else:
            cur = db().execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?) RETURNING id",
                               (name, email, generate_password_hash(pw)))
            uid = first(cur.fetchone())
            db().commit()
            record(rkey)
            cart, csrf = session.get("cart", {}), session.get("csrf")
            session.clear()
            session["cart"] = cart
            if csrf:
                session["csrf"] = csrf
            session["uid"] = uid
            session.permanent = True
            flash("Welcome! Your account is ready.", "ok")
            return redirect(url_for("catalog"))
    return render_template("account/register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:254]
        lkey = "login:" + (request.remote_addr or "")
        if throttled(lkey):
            flash("Too many failed attempts. Please wait 15 minutes and try again.", "error")
            return render_template("account/login.html"), 429
        u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        pw = request.form.get("password", "")[:256]
        # always run one hash check so response time doesn't reveal whether the email exists
        ok = check_password_hash(u["password_hash"] if u else DUMMY_HASH, pw) and u
        if ok:
            cart, csrf = session.get("cart", {}), session.get("csrf")
            session.clear()
            session["cart"] = cart
            if csrf:
                session["csrf"] = csrf
            session["uid"] = u["id"]
            session.permanent = True
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                nxt = url_for("admin_dashboard") if u["is_admin"] else url_for("catalog")
            return redirect(nxt)
        record(lkey)
        flash("Invalid email or password.", "error")
    return render_template("account/login.html")


@app.post("/logout")
def logout():
    csrf = session.get("csrf")
    session.clear()
    if csrf:
        session["csrf"] = csrf
    return redirect(url_for("catalog"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:100]
        phone = request.form.get("phone", "").strip()[:50]
        if not name:
            flash("Name is required.", "error")
        else:
            db().execute("UPDATE users SET name=?, phone=? WHERE id=?", (name, phone, g.user["id"]))
            new_pw = request.form.get("new_password", "")
            if new_pw:
                if not (MIN_PASSWORD <= len(new_pw) <= 128) or not check_password_hash(g.user["password_hash"],
                                                              request.form.get("current_password", "")):
                    flash(f"Password not changed: check current password and use {MIN_PASSWORD}+ characters.", "error")
                    return redirect(url_for("account"))
                db().execute("UPDATE users SET password_hash=? WHERE id=?",
                             (generate_password_hash(new_pw), g.user["id"]))
            db().commit()
            flash("Profile updated.", "ok")
        return redirect(url_for("account"))
    auto_confirm(db())
    orders = db().execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    addrs = db().execute("SELECT * FROM addresses WHERE user_id=?", (g.user["id"],)).fetchall()
    return render_template("account/account.html", orders=orders, addrs=addrs)


@app.post("/account/address")
@login_required
def address_add():
    f = request.form
    vals = tuple(f.get(k, "").strip()[:200] for k in ("full_name", "line1", "city", "postal_code", "phone"))
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
    auto_confirm(db())
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
        "issues": one("SELECT COUNT(*) FROM orders WHERE issue IS NOT NULL AND issue!=''"),
        "to_reply": one("SELECT COUNT(*) FROM product_reviews WHERE (admin_reply IS NULL OR admin_reply='') AND hidden=0"),
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
    auto_confirm(db())
    status = request.args.get("status", "")
    sql = "SELECT o.*, u.name AS customer, u.email FROM orders o LEFT JOIN users u ON u.id=o.user_id"
    args = []
    if status in ORDER_STATUSES:
        sql += " WHERE o.status=?"
        args.append(status)
    elif status == "issue":
        sql += " WHERE o.issue IS NOT NULL AND o.issue!=''"
    orders = db().execute(sql + " ORDER BY o.id DESC", args).fetchall()
    return render_template("admin/orders.html", orders=orders, status=status)


@app.route("/admin/orders/<int:oid>", methods=["GET", "POST"])
@admin_required
def admin_order(oid):
    conn = db()
    auto_confirm(conn)
    o = conn.execute("SELECT o.*, u.name AS customer, u.email FROM orders o "
                     "LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    if request.method == "POST":
        f = request.form
        note = f.get("note", "").strip()[:200]
        if f.get("action") == "resolve_issue":
            conn.execute("UPDATE orders SET issue='', issue_at=NULL WHERE id=?", (oid,))
            log_event(conn, oid, o["status"], "admin", "Problem resolved" + (f": {note}" if note else ""))
            conn.commit()
            flash("Problem marked as resolved.", "ok")
        else:
            new = f.get("status")
            carrier = f["carrier"].strip()[:60] if "carrier" in f else None
            tracking = f["tracking"].strip()[:100] if "tracking" in f else None
            if new in ORDER_STATUSES and set_status(conn, oid, new, "admin", note, carrier, tracking):
                conn.commit()
                flash(f"Order #{oid} updated.", "ok")
        return redirect(url_for("admin_order", oid=oid))
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
    events = conn.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY at, id", (oid,)).fetchall()
    return render_template("admin/order.html", o=o, items=items, events=events)


@app.route("/admin/reviews")
@admin_required
def admin_reviews():
    flt = request.args.get("f", "")
    page = max(1, request.args.get("page", 1, type=int) or 1)
    where = {"hidden": "r.hidden=1", "unreplied": "(r.admin_reply IS NULL OR r.admin_reply='')",
             "low": "r.rating<=2"}.get(flt, "1=1")
    total = first(db().execute(f"SELECT COUNT(*) FROM product_reviews r WHERE {where}").fetchone())
    rows = [dict(x) for x in db().execute(
        f"SELECT r.*, u.name AS author, p.name AS product_name FROM product_reviews r "
        f"JOIN users u ON u.id=r.user_id JOIN products p ON p.id=r.product_id WHERE {where} "
        f"ORDER BY r.created_at DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,)).fetchall()]
    for x in rows:
        x["photos"] = [m["url"] for m in db().execute(
            "SELECT url FROM review_media WHERE review_id=? ORDER BY position, id", (x["id"],)).fetchall()]
    return render_template("admin/reviews.html", reviews=rows, flt=flt, page=page, pages=max(1, -(-total // 20)))


@app.post("/admin/reviews/<int:rid>/hide")
@admin_required
def admin_review_hide(rid):
    db().execute("UPDATE product_reviews SET hidden=CASE WHEN hidden=1 THEN 0 ELSE 1 END WHERE id=?", (rid,))
    db().commit()
    return redirect(back("admin_reviews"))


@app.post("/admin/reviews/<int:rid>/reply")
@admin_required
def admin_review_reply(rid):
    text = request.form.get("reply", "").strip()[:1000]
    db().execute("UPDATE product_reviews SET admin_reply=?, replied_at=? WHERE id=?",
                 (text, _utc() if text else None, rid))
    db().commit()
    flash("Reply saved." if text else "Reply removed.", "ok")
    return redirect(back("admin_reviews"))


@app.route("/admin/diagnostics", methods=["GET", "POST"])
@admin_required
def admin_diagnostics():
    """Self-check for deployment problems: database, media storage, and a real test upload."""
    info = {"Database": "Postgres (Neon)" if USE_PG else "SQLite (local file)",
            "Environment": "Render (production)" if IS_PROD else "local",
            "Media storage": "Cloudinary" if USE_CLOUDINARY else "local disk (static/uploads)"}
    if USE_CLOUDINARY:
        cfg = cloudinary.config()
        info["Cloudinary cloud name"] = cfg.cloud_name or "MISSING"
        info["Cloudinary API key"] = (cfg.api_key[:3] + "…") if cfg.api_key else "MISSING"
        info["Cloudinary API secret"] = "set" if cfg.api_secret else "MISSING"
    result = None
    if request.method == "POST":
        if not USE_CLOUDINARY:
            result = ("error", "CLOUDINARY_URL is not set, so uploads " +
                      ("will fail on Render." if IS_PROD else "are saved to local disk."))
        else:
            tiny_png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
            try:
                up = cloudinary.uploader.upload(tiny_png, folder="gobuy-test")
                cloudinary.uploader.destroy(up["public_id"])
                result = ("ok", "Test upload to Cloudinary succeeded.")
            except Exception as e:
                app.logger.exception("Cloudinary diagnostics failed")
                result = ("error", f"Cloudinary rejected the upload: {e}")
    return render_template("admin/diagnostics.html", info=info, result=result)


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


def is_xhr():
    return request.headers.get("X-Requested-With") == "fetch"


def done(url):
    return jsonify(redirect=url) if is_xhr() else redirect(url)


def file_size(f):
    pos = f.stream.tell()
    f.stream.seek(0, 2)
    n = f.stream.tell()
    f.stream.seek(pos)
    return n


def sync_cover(conn, pid):
    """products.image / products.video mirror the first image / video of the gallery."""
    def firsturl(kind):
        row = conn.execute("SELECT url FROM product_media WHERE product_id=? AND kind=? "
                           "ORDER BY position, id LIMIT 1", (pid, kind)).fetchone()
        return row["url"] if row else ""
    conn.execute("UPDATE products SET image=?, video=? WHERE id=?", (firsturl("image"), firsturl("video"), pid))


def save_gallery(conn, pid):
    """Apply the form's gallery: keep/reorder existing items, drop removed ones, add new uploads."""
    keep = request.form.getlist("keep", type=int)
    rows = conn.execute("SELECT * FROM product_media WHERE product_id=?", (pid,)).fetchall()
    present = {r["id"] for r in rows}
    for r in rows:
        if r["id"] not in keep:
            conn.execute("DELETE FROM product_media WHERE id=?", (r["id"],))
            delete_media(r["url"], r["kind"])
    pos = 0
    for mid in keep:
        if mid in present:
            conn.execute("UPDATE product_media SET position=? WHERE id=?", (pos, mid))
            pos += 1
    files = [f for f in request.files.getlist("media") if f and f.filename]
    if len(files) > MAX_FILES_PER_SAVE:
        flash(f"Only the first {MAX_FILES_PER_SAVE} files were uploaded.", "error")
    for f in files[:MAX_FILES_PER_SAVE]:
        ext = f.filename.rsplit(".", 1)[-1].lower()
        if ext in ALLOWED_VIDEO_EXT:
            kind, allowed, limit = "video", ALLOWED_VIDEO_EXT, MAX_VIDEO_BYTES
        elif ext in ALLOWED_EXT:
            kind, allowed, limit = "image", ALLOWED_EXT, MAX_IMAGE_BYTES
        else:
            flash(f"{f.filename}: unsupported file type.", "error")
            continue
        if file_size(f) > limit:
            flash(f"{f.filename}: too large (max {limit // 1024 // 1024} MB for a {kind}).", "error")
            continue
        url = save_media(f, allowed, "", kind)
        if url:
            conn.execute("INSERT INTO product_media(product_id,kind,url,position) VALUES(?,?,?,?)",
                         (pid, kind, url, pos))
            pos += 1
    sync_cover(conn, pid)


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
            if is_xhr():
                return jsonify(redirect=request.url)
        else:
            cat = f.get("new_category", "").strip()
            if cat:
                conn.execute("INSERT INTO categories(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (cat,))
                cid = first(conn.execute("SELECT id FROM categories WHERE name=?", (cat,)).fetchone())
            else:
                cid = f.get("category_id", type=int)
            active = 1 if f.get("active") else 0
            if p:
                conn.execute("UPDATE products SET name=?,description=?,price=?,stock=?,low_stock_threshold=?,"
                             "category_id=?,active=? WHERE id=?",
                             (name, f.get("description", ""), price, stock, thr, cid, active, pid))
            else:
                cur = conn.execute("INSERT INTO products(name,description,price,stock,low_stock_threshold,"
                                   "category_id,active) VALUES(?,?,?,?,?,?,?) RETURNING id",
                                   (name, f.get("description", ""), price, stock, thr, cid, active))
                pid = first(cur.fetchone())
            save_gallery(conn, pid)
            conn.commit()
            flash("Product saved.", "ok")
            return done(url_for("admin_products"))
    cats = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    media = conn.execute("SELECT * FROM product_media WHERE product_id=? ORDER BY position, id",
                         (pid,)).fetchall() if pid else []
    return render_template("admin/product_form.html", p=p, cats=cats, media=media)


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
    return redirect(back("admin_products"))


@app.post("/admin/products/<int:pid>/delete")
@admin_required
def admin_product_delete(pid):
    # order history keeps its own copy of name/price, so deleting is safe
    for m in db().execute("SELECT kind, url FROM product_media WHERE product_id=?", (pid,)).fetchall():
        delete_media(m["url"], m["kind"])
    for m in db().execute("SELECT url FROM review_media WHERE review_id IN "
                          "(SELECT id FROM product_reviews WHERE product_id=?)", (pid,)).fetchall():
        delete_media(m["url"], "image")
    db().execute("DELETE FROM products WHERE id=?", (pid,))
    db().commit()
    flash("Product removed.", "ok")
    return redirect(url_for("admin_products"))


@app.errorhandler(413)
def too_large(_):
    msg = "Upload too large (max 300 MB per save). Try fewer files at once."
    if is_xhr():
        return jsonify(error=msg), 413
    flash(msg, "error")
    return redirect(back("admin_products"))


@app.errorhandler(400)
def bad_request(_):
    return render_template("error.html", code=400,
                           msg="Your session expired or the request was invalid. Please go back, refresh and try again."), 400


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

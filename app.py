"""
Student Lottery Web App
=======================
Pages:
  /spin       — Spend 1 token to spin for a random student name
  /lottery    — Student of the Day: match the name for 100 free tokens
  /inventory  — View saved names, trade for tokens, craft trades
  /users      — Browse all users, send friend requests
  /inbox      — Requests, Messages, Open Trades
  /login      — Log in
  /register   — Create an account
"""

import csv
import os
import random
from datetime import datetime, date
from pathlib import Path
from functools import wraps

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash

from flask import (
    Flask, render_template, jsonify, session,
    redirect, request, url_for, flash, g,
)
import stripe

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if "channel_binding" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.split("&channel_binding")[0]

CSV_FILE = "student_directory_names.csv"
FREE_TOKENS = 10
TOKEN_PRICE_CENTS = 99
TOKENS_PER_PURCHASE = 10
MAX_INVENTORY = 10
JACKPOT_BONUS = 100

TRADE_VALUES = {
    "common": 0,
    "rare": 3,
    "epic": 20,
    "legendary": 50,
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            tokens INTEGER DEFAULT 3,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(first_name, last_name)
        );
    """)
    cur.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='users' AND column_name='spins_remaining')
            THEN
                ALTER TABLE users RENAME COLUMN spins_remaining TO tokens;
            END IF;
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='users' AND column_name='username')
            THEN
                ALTER TABLE users RENAME COLUMN username TO first_name;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT DEFAULT '';
            END IF;
        END $$;
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL,
            saved_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_winners (
            draw_date DATE PRIMARY KEY,
            winner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            winner_name TEXT NOT NULL DEFAULT '',
            winner_tokens INTEGER NOT NULL DEFAULT 0,
            drawn_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    cur.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='daily_winners' AND column_name='name')
            THEN
                ALTER TABLE daily_winners RENAME COLUMN name TO winner_name;
                ALTER TABLE daily_winners ADD COLUMN IF NOT EXISTS winner_user_id INTEGER;
                ALTER TABLE daily_winners ADD COLUMN IF NOT EXISTS winner_tokens INTEGER DEFAULT 0;
            END IF;
        END $$;
    """)

    # --- Social / trade tables ---

    cur.execute("""
        CREATE TABLE IF NOT EXISTS friendships (
            id SERIAL PRIMARY KEY,
            requester_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            addressee_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(requester_id, addressee_id)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            receiver_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            creator_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_offer_items (
            id SERIAL PRIMARY KEY,
            trade_id INTEGER REFERENCES trades(id) ON DELETE CASCADE,
            inventory_id INTEGER REFERENCES inventory(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_responses (
            id SERIAL PRIMARY KEY,
            trade_id INTEGER REFERENCES trades(id) ON DELETE CASCADE,
            responder_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_response_items (
            id SERIAL PRIMARY KEY,
            response_id INTEGER REFERENCES trade_responses(id) ON DELETE CASCADE,
            inventory_id INTEGER REFERENCES inventory(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL
        );
    """)

    # --- Daily deck tables ---

    cur.execute("""
        CREATE TABLE IF NOT EXISTS published_decks (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            published_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(user_id)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deck_cards (
            id SERIAL PRIMARY KEY,
            deck_id INTEGER REFERENCES published_decks(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deck_upvotes (
            id SERIAL PRIMARY KEY,
            deck_id INTEGER REFERENCES published_decks(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(deck_id, user_id)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deck_comments (
            id SERIAL PRIMARY KEY,
            deck_id INTEGER REFERENCES published_decks(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def load_names():
    names = []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            n = row.get("name", "").strip()
            if n:
                names.append(n)
    return names

NAMES = load_names()
NAMES_LOWER = {n.lower() for n in NAMES}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapped


def get_current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id = %s", (session["user_id"],))
    user = cur.fetchone()
    cur.close()
    return user


# ---------------------------------------------------------------------------
# Student of the Day logic
# ---------------------------------------------------------------------------

def _auto_trade_all_inventory(cur):
    """Convert every user's inventory into tokens based on rarity, then clear inventory."""
    cur.execute("SELECT id, user_id, rarity FROM inventory")
    items = cur.fetchall()
    user_bonus = {}
    for item in items:
        uid = item["user_id"]
        tokens = TRADE_VALUES.get(item["rarity"], 0)
        user_bonus[uid] = user_bonus.get(uid, 0) + tokens
    for uid, bonus in user_bonus.items():
        if bonus > 0:
            cur.execute("UPDATE users SET tokens = tokens + %s WHERE id = %s", (bonus, uid))
    cur.execute("DELETE FROM inventory")
    cur.execute("DELETE FROM published_decks")


def draw_student_of_the_day():
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM daily_winners WHERE draw_date = %s", (date.today(),))
    row = cur.fetchone()
    if row:
        cur.close()
        return row

    # Auto-trade all inventory into tokens first
    _auto_trade_all_inventory(cur)
    db.commit()

    # The winner is the user with the most tokens
    cur.execute("""
        SELECT id, first_name, last_name, tokens
        FROM users ORDER BY tokens DESC, created_at ASC LIMIT 1
    """)
    top_user = cur.fetchone()
    if not top_user:
        cur.close()
        return None

    winner_name = f"{top_user['first_name']} {top_user['last_name']}"
    cur.execute(
        """INSERT INTO daily_winners (draw_date, winner_user_id, winner_name, winner_tokens)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING *""",
        (date.today(), top_user["id"], winner_name, top_user["tokens"]),
    )
    row = cur.fetchone()
    db.commit()
    cur.close()
    if row:
        return row
    cur2 = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur2.execute("SELECT * FROM daily_winners WHERE draw_date = %s", (date.today(),))
    row = cur2.fetchone()
    cur2.close()
    return row


def get_recent_winners(limit=7):
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM daily_winners ORDER BY draw_date DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Badge count helper (unread items for nav)
# ---------------------------------------------------------------------------

def get_inbox_counts(user_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM friendships WHERE addressee_id=%s AND status='pending'", (user_id,))
    requests = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages WHERE receiver_id=%s AND is_read=FALSE", (user_id,))
    msgs = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM trade_responses tr
        JOIN trades t ON tr.trade_id=t.id
        WHERE t.creator_id=%s AND tr.status='pending'
    """, (user_id,))
    trade_notifs = cur.fetchone()[0]
    cur.close()
    return requests + msgs + trade_notifs


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        password = request.form.get("password", "").strip()
        if not first_name or not last_name or not password:
            flash("First name, last name, and password are all required.")
            return redirect(url_for("register_page"))
        if len(password) < 4:
            flash("Password must be at least 4 characters.")
            return redirect(url_for("register_page"))

        full_name = f"{first_name} {last_name}"
        if full_name.lower() not in NAMES_LOWER:
            flash("Your name was not found in the student directory. Please use your real name as it appears in the campus directory.")
            return redirect(url_for("register_page"))

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                "INSERT INTO users (first_name, last_name, password_hash, tokens) VALUES (%s, %s, %s, %s) RETURNING id",
                (first_name, last_name, generate_password_hash(password), FREE_TOKENS),
            )
            user_id = cur.fetchone()[0]
            db.commit()
            session["user_id"] = user_id
            session["display_name"] = full_name
            return redirect(url_for("spin_page"))
        except psycopg2.errors.UniqueViolation:
            db.rollback()
            flash("An account with that name already exists.")
            return redirect(url_for("register_page"))
        finally:
            cur.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        password = request.form.get("password", "").strip()
        db = get_db()
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM users WHERE LOWER(first_name)=LOWER(%s) AND LOWER(last_name)=LOWER(%s)",
            (first_name, last_name),
        )
        user = cur.fetchone()
        cur.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["display_name"] = f"{user['first_name']} {user['last_name']}"
            return redirect(url_for("spin_page"))
        flash("Invalid name or password.")
        return redirect(url_for("login_page"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Routes — Spin page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("spin_page"))


@app.route("/spin")
@login_required
def spin_page():
    user = get_current_user()
    sample = random.sample(NAMES, min(30, len(NAMES)))
    badge = get_inbox_counts(user["id"])
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM inventory WHERE user_id = %s ORDER BY saved_at DESC", (user["id"],))
    inv_items = cur.fetchall()
    cur.close()
    return render_template("spin.html", tokens=user["tokens"], name_pool=sample,
                           inbox_badge=badge, inv_items=inv_items,
                           inv_count=len(inv_items), max_inventory=MAX_INVENTORY)


@app.route("/api/spin", methods=["POST"])
@login_required
def api_spin():
    user = get_current_user()
    if user["tokens"] <= 0:
        return jsonify({"error": "no_tokens", "message": "You're out of tokens!"}), 403

    winner = random.choice(NAMES)
    reel = random.sample(NAMES, min(20, len(NAMES)))
    if winner not in reel:
        reel[-1] = winner

    roll = random.random()
    if roll < 0.02:
        rarity = "legendary"
    elif roll < 0.10:
        rarity = "epic"
    elif roll < 0.42:
        rarity = "rare"
    else:
        rarity = "common"

    jackpot = False
    today = draw_student_of_the_day()
    if today and winner.lower() == today["winner_name"].lower():
        jackpot = True
        rarity = "legendary"

    db = get_db()
    cur = db.cursor()
    bonus = JACKPOT_BONUS if jackpot else 0
    cur.execute(
        "UPDATE users SET tokens = tokens - 1 + %s WHERE id = %s RETURNING tokens",
        (bonus, user["id"]),
    )
    new_tokens = cur.fetchone()[0]
    db.commit()
    cur.close()

    return jsonify({
        "winner": winner,
        "reel": reel,
        "rarity": rarity,
        "jackpot": jackpot,
        "tokens": new_tokens,
    })


# ---------------------------------------------------------------------------
# Routes — Save to inventory
# ---------------------------------------------------------------------------

@app.route("/api/save-to-inventory", methods=["POST"])
@login_required
def save_to_inventory():
    data = request.get_json()
    name = data.get("name", "").strip()
    rarity = data.get("rarity", "common")
    replace_id = data.get("replace_id")
    if not name:
        return jsonify({"error": "No name provided"}), 400

    user = get_current_user()
    db = get_db()
    cur = db.cursor()

    if replace_id:
        cur.execute("DELETE FROM inventory WHERE id = %s AND user_id = %s", (int(replace_id), user["id"]))

    cur.execute("SELECT COUNT(*) FROM inventory WHERE user_id = %s", (user["id"],))
    count = cur.fetchone()[0]
    if count >= MAX_INVENTORY:
        db.rollback()
        cur.close()
        return jsonify({"error": "inventory_full", "count": count, "max": MAX_INVENTORY}), 400

    cur.execute(
        "INSERT INTO inventory (user_id, name, rarity) VALUES (%s, %s, %s)",
        (user["id"], name, rarity),
    )
    db.commit()
    cur.execute("SELECT COUNT(*) FROM inventory WHERE user_id = %s", (user["id"],))
    new_count = cur.fetchone()[0]
    cur.close()
    return jsonify({"success": True, "message": f"Saved {name} to inventory!", "inv_count": new_count})


# ---------------------------------------------------------------------------
# Routes — Inventory page
# ---------------------------------------------------------------------------

@app.route("/inventory")
@login_required
def inventory_page():
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM inventory WHERE user_id = %s ORDER BY saved_at DESC", (user["id"],))
    items = cur.fetchall()

    # Open trades from other users (for NameMarket tab)
    cur.execute("""
        SELECT t.*, (u.first_name || ' ' || u.last_name) AS creator_name FROM trades t
        JOIN users u ON t.creator_id=u.id
        WHERE t.status='open' AND t.creator_id != %s
        ORDER BY t.created_at DESC LIMIT 50
    """, (user["id"],))
    open_trades = cur.fetchall()
    for t in open_trades:
        cur.execute("SELECT name, rarity FROM trade_offer_items WHERE trade_id=%s", (t["id"],))
        t["offer_items"] = cur.fetchall()

    cur.execute("SELECT id FROM published_decks WHERE user_id=%s", (user["id"],))
    has_published = cur.fetchone() is not None

    cur.close()
    badge = get_inbox_counts(user["id"])
    return render_template("inventory.html", items=items, trade_values=TRADE_VALUES,
                           tokens=user["tokens"], inbox_badge=badge, open_trades=open_trades,
                           has_published=has_published)


@app.route("/api/trade-for-tokens/<int:item_id>", methods=["POST"])
@login_required
def trade_for_tokens(item_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM inventory WHERE id = %s AND user_id = %s", (item_id, user["id"]))
    item = cur.fetchone()
    if not item:
        cur.close()
        return jsonify({"error": "Item not found"}), 404

    tokens_gained = TRADE_VALUES.get(item["rarity"], 0)
    if tokens_gained <= 0:
        cur.close()
        return jsonify({"error": "This card can only be discarded, not traded for tokens"}), 400
    cur.execute("DELETE FROM inventory WHERE id = %s", (item_id,))
    cur.execute(
        "UPDATE users SET tokens = tokens + %s WHERE id = %s RETURNING tokens",
        (tokens_gained, user["id"]),
    )
    new_tokens = cur.fetchone()["tokens"]
    db.commit()
    cur.close()
    return jsonify({"success": True, "tokens_gained": tokens_gained, "tokens": new_tokens})


@app.route("/api/discard/<int:item_id>", methods=["POST"])
@login_required
def discard_item(item_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM inventory WHERE id = %s AND user_id = %s", (item_id, user["id"]))
    db.commit()
    cur.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Routes — Stripe payment (buy tokens)
# ---------------------------------------------------------------------------

@app.route("/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session():
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{TOKENS_PER_PURCHASE} Tokens — Student Lottery"},
                    "unit_amount": TOKEN_PRICE_CENTS,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=url_for("payment_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("spin_page", _external=True),
            api_key=STRIPE_SECRET_KEY,
        )
        return jsonify({"url": checkout.url})
    except Exception as exc:
        print(f"Stripe error: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/payment-success")
@login_required
def payment_success():
    user = get_current_user()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE users SET tokens = tokens + %s WHERE id = %s",
        (TOKENS_PER_PURCHASE, user["id"]),
    )
    db.commit()
    cur.close()
    return redirect(url_for("spin_page"))


# ---------------------------------------------------------------------------
# Routes — Student of the Day
# ---------------------------------------------------------------------------

@app.route("/lottery")
@login_required
def lottery_page():
    user = get_current_user()
    winner = draw_student_of_the_day()
    recent = get_recent_winners()
    badge = get_inbox_counts(user["id"])
    return render_template("lottery.html", winner=winner, recent=recent,
                           tokens=user["tokens"], inbox_badge=badge)


@app.route("/api/lottery/today")
def api_lottery_today():
    winner = draw_student_of_the_day()
    if winner:
        return jsonify({
            "draw_date": str(winner["draw_date"]),
            "winner_name": winner["winner_name"],
            "winner_tokens": winner["winner_tokens"],
        })
    return jsonify({})


# ---------------------------------------------------------------------------
# Routes — Users page (browse & add friends)
# ---------------------------------------------------------------------------

@app.route("/users")
@login_required
def users_page():
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    q = request.args.get("q", "").strip()
    if q:
        cur.execute("""SELECT id, first_name, last_name, created_at FROM users
                        WHERE (first_name || ' ' || last_name) ILIKE %s AND id != %s
                        ORDER BY LOWER(last_name), LOWER(first_name)""",
                     (f"%{q}%", user["id"]))
    else:
        cur.execute("""SELECT id, first_name, last_name, created_at FROM users
                        WHERE id != %s
                        ORDER BY LOWER(last_name), LOWER(first_name)""",
                     (user["id"],))
    all_users = cur.fetchall()

    cur.execute("""
        SELECT addressee_id AS uid FROM friendships WHERE requester_id=%s
        UNION
        SELECT requester_id AS uid FROM friendships WHERE addressee_id=%s
    """, (user["id"], user["id"]))
    related = {r["uid"] for r in cur.fetchall()}

    cur.execute("SELECT addressee_id FROM friendships WHERE requester_id=%s AND status='pending'", (user["id"],))
    pending_sent = {r["addressee_id"] for r in cur.fetchall()}

    cur.execute("""
        SELECT requester_id FROM friendships WHERE addressee_id=%s AND status='accepted'
        UNION
        SELECT addressee_id FROM friendships WHERE requester_id=%s AND status='accepted'
    """, (user["id"], user["id"]))
    friends = {r["requester_id"] for r in cur.fetchall()}

    cur.close()
    badge = get_inbox_counts(user["id"])
    return render_template("users.html", all_users=all_users, related=related,
                           pending_sent=pending_sent, friends=friends,
                           tokens=user["tokens"], inbox_badge=badge, search_q=q)


@app.route("/api/friend-request/<int:target_id>", methods=["POST"])
@login_required
def send_friend_request(target_id):
    user = get_current_user()
    if target_id == user["id"]:
        return jsonify({"error": "Can't friend yourself"}), 400
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO friendships (requester_id, addressee_id) VALUES (%s, %s)",
            (user["id"], target_id),
        )
        db.commit()
    except psycopg2.errors.UniqueViolation:
        db.rollback()
    cur.close()
    return jsonify({"success": True})


@app.route("/api/friend-request/<int:req_id>/accept", methods=["POST"])
@login_required
def accept_friend(req_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor()
    cur.execute("UPDATE friendships SET status='accepted' WHERE id=%s AND addressee_id=%s", (req_id, user["id"]))
    db.commit()
    cur.close()
    return jsonify({"success": True})


@app.route("/api/friend-request/<int:req_id>/decline", methods=["POST"])
@login_required
def decline_friend(req_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM friendships WHERE id=%s AND addressee_id=%s", (req_id, user["id"]))
    db.commit()
    cur.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Routes — Inbox page (requests, messages, trades)
# ---------------------------------------------------------------------------

@app.route("/inbox")
@login_required
def inbox_page():
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Pending friend requests TO me
    cur.execute("""
        SELECT f.id, f.created_at, (u.first_name || ' ' || u.last_name) AS display_name, u.id AS from_id
        FROM friendships f JOIN users u ON f.requester_id=u.id
        WHERE f.addressee_id=%s AND f.status='pending'
        ORDER BY f.created_at DESC
    """, (user["id"],))
    requests_list = cur.fetchall()

    # My friends
    cur.execute("""
        SELECT u.id, (u.first_name || ' ' || u.last_name) AS display_name FROM users u WHERE u.id IN (
            SELECT CASE WHEN requester_id=%s THEN addressee_id ELSE requester_id END
            FROM friendships WHERE status='accepted' AND (requester_id=%s OR addressee_id=%s)
        ) ORDER BY u.last_name, u.first_name
    """, (user["id"], user["id"], user["id"]))
    friends = cur.fetchall()

    # Latest message per friend (for preview)
    friend_ids = [f["id"] for f in friends]
    last_msgs = {}
    unread_counts = {}
    for fid in friend_ids:
        cur.execute("""
            SELECT content, created_at, sender_id FROM messages
            WHERE (sender_id=%s AND receiver_id=%s) OR (sender_id=%s AND receiver_id=%s)
            ORDER BY created_at DESC LIMIT 1
        """, (user["id"], fid, fid, user["id"]))
        m = cur.fetchone()
        if m:
            last_msgs[fid] = m
        cur.execute("SELECT COUNT(*) as cnt FROM messages WHERE sender_id=%s AND receiver_id=%s AND is_read=FALSE",
                     (fid, user["id"]))
        unread_counts[fid] = cur.fetchone()["cnt"]

    # My open trades + responses (for My Name Trades tab)
    cur.execute("""
        SELECT t.* FROM trades t WHERE t.creator_id=%s ORDER BY t.created_at DESC
    """, (user["id"],))
    my_trades = cur.fetchall()
    for t in my_trades:
        cur.execute("SELECT name, rarity FROM trade_offer_items WHERE trade_id=%s", (t["id"],))
        t["offer_items"] = cur.fetchall()
        cur.execute("""
            SELECT tr.id AS response_id, tr.status AS resp_status, tr.created_at,
                   (u.first_name || ' ' || u.last_name) AS responder_name
            FROM trade_responses tr JOIN users u ON tr.responder_id=u.id
            WHERE tr.trade_id=%s ORDER BY tr.created_at DESC
        """, (t["id"],))
        t["responses"] = cur.fetchall()
        for resp in t["responses"]:
            cur.execute("SELECT name, rarity FROM trade_response_items WHERE response_id=%s", (resp["response_id"],))
            resp["offer_items"] = cur.fetchall()

    # My published deck
    cur.execute("SELECT * FROM published_decks WHERE user_id=%s", (user["id"],))
    my_deck = cur.fetchone()
    if my_deck:
        cur.execute("SELECT name, rarity FROM deck_cards WHERE deck_id=%s", (my_deck["id"],))
        my_deck["cards"] = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS cnt FROM deck_upvotes WHERE deck_id=%s", (my_deck["id"],))
        my_deck["upvotes"] = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT dc.content, dc.created_at,
                   (u.first_name || ' ' || u.last_name) AS author_name
            FROM deck_comments dc JOIN users u ON dc.user_id=u.id
            WHERE dc.deck_id=%s ORDER BY dc.created_at ASC
        """, (my_deck["id"],))
        my_deck["comments"] = cur.fetchall()

    cur.close()
    badge = get_inbox_counts(user["id"])
    return render_template("inbox.html",
                           requests_list=requests_list,
                           friends=friends, last_msgs=last_msgs, unread_counts=unread_counts,
                           my_trades=my_trades, my_deck=my_deck,
                           tokens=user["tokens"], inbox_badge=badge)


# ---------------------------------------------------------------------------
# Routes — Messaging
# ---------------------------------------------------------------------------

@app.route("/api/messages/<int:friend_id>", methods=["GET"])
@login_required
def get_messages(friend_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT m.*, (u.first_name || ' ' || u.last_name) AS sender_name FROM messages m
        JOIN users u ON m.sender_id=u.id
        WHERE (m.sender_id=%s AND m.receiver_id=%s) OR (m.sender_id=%s AND m.receiver_id=%s)
        ORDER BY m.created_at ASC
    """, (user["id"], friend_id, friend_id, user["id"]))
    msgs = cur.fetchall()
    # mark as read
    cur.execute("UPDATE messages SET is_read=TRUE WHERE sender_id=%s AND receiver_id=%s AND is_read=FALSE",
                (friend_id, user["id"]))
    db.commit()
    cur.close()
    result = []
    for m in msgs:
        result.append({
            "id": m["id"],
            "sender_id": m["sender_id"],
            "sender_name": m["sender_name"],
            "content": m["content"],
            "created_at": m["created_at"].isoformat(),
            "is_mine": m["sender_id"] == user["id"],
        })
    return jsonify(result)


@app.route("/api/messages/<int:friend_id>", methods=["POST"])
@login_required
def send_message(friend_id):
    user = get_current_user()
    data = request.get_json()
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "Empty message"}), 400
    if len(content) > 500:
        return jsonify({"error": "Message too long (max 500 chars)"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO messages (sender_id, receiver_id, content) VALUES (%s, %s, %s)",
        (user["id"], friend_id, content),
    )
    db.commit()
    cur.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Routes — Open Trades
# ---------------------------------------------------------------------------

@app.route("/api/trades", methods=["POST"])
@login_required
def create_trade():
    """Create a trade from inventory items."""
    user = get_current_user()
    data = request.get_json()
    item_ids = data.get("item_ids", [])
    if not item_ids:
        return jsonify({"error": "Select at least one card to trade"}), 400

    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Verify ownership
    cur.execute("SELECT * FROM inventory WHERE user_id=%s AND id = ANY(%s)", (user["id"], item_ids))
    items = cur.fetchall()
    if len(items) != len(item_ids):
        cur.close()
        return jsonify({"error": "Some items not found in your inventory"}), 400

    cur.execute("INSERT INTO trades (creator_id) VALUES (%s) RETURNING id", (user["id"],))
    trade_id = cur.fetchone()["id"]

    for item in items:
        cur.execute(
            "INSERT INTO trade_offer_items (trade_id, inventory_id, name, rarity) VALUES (%s, %s, %s, %s)",
            (trade_id, item["id"], item["name"], item["rarity"]),
        )

    db.commit()
    cur.close()
    return jsonify({"success": True, "trade_id": trade_id})


@app.route("/api/trades/<int:trade_id>/respond", methods=["POST"])
@login_required
def respond_to_trade(trade_id):
    """Respond to an open trade with your own items."""
    user = get_current_user()
    data = request.get_json()
    item_ids = data.get("item_ids", [])
    if not item_ids:
        return jsonify({"error": "Select at least one card to offer"}), 400

    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM trades WHERE id=%s AND status='open'", (trade_id,))
    trade = cur.fetchone()
    if not trade:
        cur.close()
        return jsonify({"error": "Trade not found or closed"}), 404
    if trade["creator_id"] == user["id"]:
        cur.close()
        return jsonify({"error": "Can't respond to your own trade"}), 400

    cur.execute("SELECT * FROM inventory WHERE user_id=%s AND id = ANY(%s)", (user["id"], item_ids))
    items = cur.fetchall()
    if len(items) != len(item_ids):
        cur.close()
        return jsonify({"error": "Some items not found"}), 400

    cur.execute(
        "INSERT INTO trade_responses (trade_id, responder_id) VALUES (%s, %s) RETURNING id",
        (trade_id, user["id"]),
    )
    response_id = cur.fetchone()["id"]

    for item in items:
        cur.execute(
            "INSERT INTO trade_response_items (response_id, inventory_id, name, rarity) VALUES (%s, %s, %s, %s)",
            (response_id, item["id"], item["name"], item["rarity"]),
        )

    db.commit()
    cur.close()
    return jsonify({"success": True})


@app.route("/api/trade-responses/<int:response_id>/accept", methods=["POST"])
@login_required
def accept_trade_response(response_id):
    """Accept a trade response — swap items between users."""
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT tr.*, t.creator_id, t.id AS trade_id FROM trade_responses tr
        JOIN trades t ON tr.trade_id=t.id
        WHERE tr.id=%s AND t.creator_id=%s AND tr.status='pending'
    """, (response_id, user["id"]))
    resp = cur.fetchone()
    if not resp:
        cur.close()
        return jsonify({"error": "Response not found"}), 404

    # Get items from both sides
    cur.execute("SELECT * FROM trade_offer_items WHERE trade_id=%s", (resp["trade_id"],))
    my_items = cur.fetchall()
    cur.execute("SELECT * FROM trade_response_items WHERE response_id=%s", (response_id,))
    their_items = cur.fetchall()

    # Verify all items still exist
    for item in my_items:
        if item["inventory_id"]:
            cur.execute("SELECT id FROM inventory WHERE id=%s AND user_id=%s", (item["inventory_id"], user["id"]))
            if not cur.fetchone():
                cur.close()
                return jsonify({"error": "One of your items is no longer available"}), 400
    for item in their_items:
        if item["inventory_id"]:
            cur.execute("SELECT id FROM inventory WHERE id=%s AND user_id=%s", (item["inventory_id"], resp["responder_id"]))
            if not cur.fetchone():
                cur.close()
                return jsonify({"error": "One of their items is no longer available"}), 400

    # Swap: transfer my items to responder, their items to me
    for item in my_items:
        if item["inventory_id"]:
            cur.execute("UPDATE inventory SET user_id=%s WHERE id=%s", (resp["responder_id"], item["inventory_id"]))
    for item in their_items:
        if item["inventory_id"]:
            cur.execute("UPDATE inventory SET user_id=%s WHERE id=%s", (user["id"], item["inventory_id"]))

    # Close trade
    cur.execute("UPDATE trades SET status='completed' WHERE id=%s", (resp["trade_id"],))
    cur.execute("UPDATE trade_responses SET status='accepted' WHERE id=%s", (response_id,))
    # Decline all other responses
    cur.execute("UPDATE trade_responses SET status='declined' WHERE trade_id=%s AND id!=%s AND status='pending'",
                (resp["trade_id"], response_id))

    db.commit()
    cur.close()
    return jsonify({"success": True})


@app.route("/api/trade-responses/<int:response_id>/decline", methods=["POST"])
@login_required
def decline_trade_response(response_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE trade_responses SET status='declined'
        FROM trades WHERE trade_responses.trade_id=trades.id
        AND trade_responses.id=%s AND trades.creator_id=%s
    """, (response_id, user["id"]))
    db.commit()
    cur.close()
    return jsonify({"success": True})


@app.route("/api/trades/<int:trade_id>/cancel", methods=["POST"])
@login_required
def cancel_trade(trade_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor()
    cur.execute("UPDATE trades SET status='cancelled' WHERE id=%s AND creator_id=%s AND status='open'", (trade_id, user["id"]))
    db.commit()
    cur.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Routes — Browse Decks
# ---------------------------------------------------------------------------

@app.route("/decks")
@login_required
def decks_page():
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT d.id, d.published_at,
               (u.first_name || ' ' || u.last_name) AS owner_name,
               u.id AS owner_id,
               (SELECT COUNT(*) FROM deck_upvotes WHERE deck_id=d.id) AS upvotes,
               EXISTS(SELECT 1 FROM deck_upvotes WHERE deck_id=d.id AND user_id=%s) AS user_upvoted
        FROM published_decks d
        JOIN users u ON d.user_id=u.id
        ORDER BY upvotes DESC, d.published_at DESC
    """, (user["id"],))
    decks = cur.fetchall()
    for d in decks:
        cur.execute("SELECT name, rarity FROM deck_cards WHERE deck_id=%s", (d["id"],))
        d["cards"] = cur.fetchall()
        cur.execute("""
            SELECT dc.id, dc.content, dc.created_at,
                   (u.first_name || ' ' || u.last_name) AS author_name
            FROM deck_comments dc JOIN users u ON dc.user_id=u.id
            WHERE dc.deck_id=%s ORDER BY dc.created_at ASC
        """, (d["id"],))
        d["comments"] = cur.fetchall()

    cur.execute("SELECT id FROM published_decks WHERE user_id=%s", (user["id"],))
    has_published = cur.fetchone() is not None

    cur.close()
    badge = get_inbox_counts(user["id"])
    return render_template("decks.html", decks=decks, tokens=user["tokens"],
                           inbox_badge=badge, has_published=has_published)


@app.route("/api/publish-deck", methods=["POST"])
@login_required
def publish_deck():
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT id FROM published_decks WHERE user_id=%s", (user["id"],))
    if cur.fetchone():
        cur.close()
        return jsonify({"error": "You already published a deck today"}), 400

    cur.execute("SELECT * FROM inventory WHERE user_id=%s", (user["id"],))
    items = cur.fetchall()
    if not items:
        cur.close()
        return jsonify({"error": "Your inventory is empty"}), 400

    cur.execute("INSERT INTO published_decks (user_id) VALUES (%s) RETURNING id", (user["id"],))
    deck_id = cur.fetchone()["id"]
    for item in items:
        cur.execute("INSERT INTO deck_cards (deck_id, name, rarity) VALUES (%s, %s, %s)",
                    (deck_id, item["name"], item["rarity"]))
    db.commit()
    cur.close()
    return jsonify({"success": True})


@app.route("/api/decks/<int:deck_id>/upvote", methods=["POST"])
@login_required
def upvote_deck(deck_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("INSERT INTO deck_upvotes (deck_id, user_id) VALUES (%s, %s)", (deck_id, user["id"]))
        db.commit()
    except psycopg2.errors.UniqueViolation:
        db.rollback()
        cur.execute("DELETE FROM deck_upvotes WHERE deck_id=%s AND user_id=%s", (deck_id, user["id"]))
        db.commit()
    cur.execute("SELECT COUNT(*) FROM deck_upvotes WHERE deck_id=%s", (deck_id,))
    count = cur.fetchone()[0]
    cur.close()
    return jsonify({"success": True, "upvotes": count})


@app.route("/api/decks/<int:deck_id>/comment", methods=["POST"])
@login_required
def comment_on_deck(deck_id):
    user = get_current_user()
    data = request.get_json()
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "Empty comment"}), 400
    if len(content) > 300:
        return jsonify({"error": "Comment too long (max 300 chars)"}), 400
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "INSERT INTO deck_comments (deck_id, user_id, content) VALUES (%s, %s, %s) RETURNING id, created_at",
        (deck_id, user["id"], content),
    )
    row = cur.fetchone()
    db.commit()
    cur.close()
    display = f"{user['first_name']} {user['last_name']}"
    return jsonify({"success": True, "comment": {
        "id": row["id"], "content": content, "author_name": display,
        "created_at": row["created_at"].isoformat(),
    }})


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Loaded {len(NAMES)} student names from {CSV_FILE}")
    print(f"Stripe key loaded: {'yes' if STRIPE_SECRET_KEY else 'NO'}")
    print(f"Database connected: {'yes' if DATABASE_URL else 'NO'}")
    app.run(debug=True, host="0.0.0.0", port=port)

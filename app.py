"""
Student Lottery Web App
=======================
Pages:
  /spin       — Spin for random student names with rarity tiers
  /lottery    — Daily midnight lottery: winner gets 100 free spins
  /inventory  — View your saved names, trade them for spins
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
FREE_SPINS = 3
SPIN_PRICE_CENTS = 99
EXTRA_SPINS_PER_PURCHASE = 10
MAX_INVENTORY = 10

TRADE_VALUES = {
    "common": 1,
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
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            spins_remaining INTEGER DEFAULT 3,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
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
            name TEXT NOT NULL,
            drawn_at TIMESTAMPTZ DEFAULT NOW()
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
# Daily lottery logic
# ---------------------------------------------------------------------------

def draw_todays_winner():
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM daily_winners WHERE draw_date = %s", (date.today(),))
    row = cur.fetchone()
    if row:
        cur.close()
        return row
    winner = random.choice(NAMES)
    cur.execute(
        "INSERT INTO daily_winners (draw_date, name) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING *",
        (date.today(), winner),
    )
    row = cur.fetchone()
    db.commit()
    cur.close()
    if row:
        return row
    # Race condition: another request inserted first
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
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            flash("Username and password are required.")
            return redirect(url_for("register_page"))
        if len(password) < 4:
            flash("Password must be at least 4 characters.")
            return redirect(url_for("register_page"))
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                (username, generate_password_hash(password)),
            )
            user_id = cur.fetchone()[0]
            db.commit()
            session["user_id"] = user_id
            session["username"] = username
            return redirect(url_for("spin_page"))
        except psycopg2.errors.UniqueViolation:
            db.rollback()
            flash("That username is already taken.")
            return redirect(url_for("register_page"))
        finally:
            cur.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        db = get_db()
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("spin_page"))
        flash("Invalid username or password.")
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
    return render_template("spin.html", spins_remaining=user["spins_remaining"])


@app.route("/api/spin", methods=["POST"])
@login_required
def api_spin():
    user = get_current_user()
    if user["spins_remaining"] <= 0:
        return jsonify({"error": "no_spins", "message": "You've used all your spins!"}), 403

    winner = random.choice(NAMES)
    reel = random.sample(NAMES, min(20, len(NAMES)))
    if winner not in reel:
        reel[-1] = winner

    # Rarity: 58% common(grey), 32% rare(blue), 8% epic(purple), 2% legendary(gold)
    roll = random.random()
    if roll < 0.02:
        rarity = "legendary"
    elif roll < 0.10:
        rarity = "epic"
    elif roll < 0.42:
        rarity = "rare"
    else:
        rarity = "common"

    # Check jackpot: spin matches today's lottery winner
    jackpot = False
    today = draw_todays_winner()
    if today and winner == today["name"]:
        jackpot = True
        rarity = "legendary"

    # Deduct spin
    db = get_db()
    cur = db.cursor()
    bonus = 100 if jackpot else 0
    cur.execute(
        "UPDATE users SET spins_remaining = spins_remaining - 1 + %s WHERE id = %s RETURNING spins_remaining",
        (bonus, user["id"]),
    )
    new_spins = cur.fetchone()[0]
    db.commit()
    cur.close()

    return jsonify({
        "winner": winner,
        "reel": reel,
        "rarity": rarity,
        "jackpot": jackpot,
        "spins_remaining": new_spins,
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
    if not name:
        return jsonify({"error": "No name provided"}), 400

    user = get_current_user()
    db = get_db()
    cur = db.cursor()

    # Check inventory count
    cur.execute("SELECT COUNT(*) FROM inventory WHERE user_id = %s", (user["id"],))
    count = cur.fetchone()[0]
    if count >= MAX_INVENTORY:
        cur.close()
        return jsonify({"error": "Inventory full! Max 10 names."}), 400

    cur.execute(
        "INSERT INTO inventory (user_id, name, rarity) VALUES (%s, %s, %s)",
        (user["id"], name, rarity),
    )
    db.commit()
    cur.close()
    return jsonify({"success": True, "message": f"Saved {name} to inventory!"})


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
    cur.close()
    return render_template("inventory.html", items=items, trade_values=TRADE_VALUES, spins_remaining=user["spins_remaining"])


@app.route("/api/trade/<int:item_id>", methods=["POST"])
@login_required
def trade_item(item_id):
    user = get_current_user()
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM inventory WHERE id = %s AND user_id = %s", (item_id, user["id"]))
    item = cur.fetchone()
    if not item:
        cur.close()
        return jsonify({"error": "Item not found"}), 404

    spins_gained = TRADE_VALUES.get(item["rarity"], 1)
    cur.execute("DELETE FROM inventory WHERE id = %s", (item_id,))
    cur.execute(
        "UPDATE users SET spins_remaining = spins_remaining + %s WHERE id = %s RETURNING spins_remaining",
        (spins_gained, user["id"]),
    )
    new_spins = cur.fetchone()["spins_remaining"]
    db.commit()
    cur.close()
    return jsonify({"success": True, "spins_gained": spins_gained, "spins_remaining": new_spins})


# ---------------------------------------------------------------------------
# Routes — Stripe payment
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
                    "product_data": {"name": "10 Extra Spins — Student Lottery"},
                    "unit_amount": SPIN_PRICE_CENTS,
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
        "UPDATE users SET spins_remaining = spins_remaining + %s WHERE id = %s",
        (EXTRA_SPINS_PER_PURCHASE, user["id"]),
    )
    db.commit()
    cur.close()
    return redirect(url_for("spin_page"))


# ---------------------------------------------------------------------------
# Routes — Lottery page
# ---------------------------------------------------------------------------

@app.route("/lottery")
@login_required
def lottery_page():
    winner = draw_todays_winner()
    recent = get_recent_winners()
    return render_template("lottery.html", winner=winner, recent=recent)


@app.route("/api/lottery/today")
def api_lottery_today():
    winner = draw_todays_winner()
    return jsonify(dict(winner) if winner else {})


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

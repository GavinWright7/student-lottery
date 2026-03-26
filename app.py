"""
Student Lottery Web App
=======================
Two pages:
  1. /spin     — 3 free spins, then Stripe paywall ($50 for 10 more)
  2. /lottery  — Daily midnight lottery: one student wins $1,000

Run:
  pip install -r requirements.txt
  python app.py
"""

import csv
import os
import random
import sqlite3
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from flask import (
    Flask, render_template, jsonify, session,
    redirect, request, url_for,
)
import stripe

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me-to-a-real-secret-key")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")

CSV_FILE = "student_directory_names.csv"
DB_FILE = "lottery.db"
FREE_SPINS = 3
SPIN_PRICE_CENTS = 99  # $0.99
EXTRA_SPINS_PER_PURCHASE = 10

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_winners (
            draw_date TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            drawn_at  TEXT NOT NULL
        );
    """)
    conn.commit()
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
# Daily lottery logic
# ---------------------------------------------------------------------------

def get_todays_winner():
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM daily_winners WHERE draw_date = ?",
        (date.today().isoformat(),),
    ).fetchone()
    conn.close()
    return row


def draw_todays_winner():
    existing = get_todays_winner()
    if existing:
        return dict(existing)
    winner = random.choice(NAMES)
    now = datetime.now().isoformat()
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO daily_winners (draw_date, name, drawn_at) VALUES (?, ?, ?)",
        (date.today().isoformat(), winner, now),
    )
    conn.commit()
    conn.close()
    return {"draw_date": date.today().isoformat(), "name": winner, "drawn_at": now}


def get_recent_winners(limit=7):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM daily_winners ORDER BY draw_date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Routes — Spin page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("spin_page"))


@app.route("/spin")
def spin_page():
    session["spins_used"] = 0
    session["spins_allowed"] = FREE_SPINS
    return render_template(
        "spin.html",
        spins_used=session["spins_used"],
        spins_allowed=session["spins_allowed"],
    )


@app.route("/api/spin", methods=["POST"])
def api_spin():
    if "spins_used" not in session:
        session["spins_used"] = 0
    if "spins_allowed" not in session:
        session["spins_allowed"] = FREE_SPINS

    if session["spins_used"] >= session["spins_allowed"]:
        return jsonify({"error": "no_spins", "message": "You've used all your spins!"}), 403

    winner = random.choice(NAMES)
    reel = random.sample(NAMES, min(20, len(NAMES)))
    if winner not in reel:
        reel[-1] = winner

    session["spins_used"] += 1
    session.modified = True

    return jsonify({
        "winner": winner,
        "reel": reel,
        "spins_used": session["spins_used"],
        "spins_allowed": session["spins_allowed"],
    })


# ---------------------------------------------------------------------------
# Routes — Stripe payment
# ---------------------------------------------------------------------------

@app.route("/create-checkout-session", methods=["POST"])
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
def payment_success():
    session["spins_used"] = 0
    session["spins_allowed"] = session.get("spins_allowed", FREE_SPINS) + EXTRA_SPINS_PER_PURCHASE
    session.modified = True
    return redirect(url_for("spin_page"))


# ---------------------------------------------------------------------------
# Routes — Lottery page
# ---------------------------------------------------------------------------

@app.route("/lottery")
def lottery_page():
    winner = draw_todays_winner()
    recent = get_recent_winners()
    return render_template("lottery.html", winner=winner, recent=recent)


@app.route("/api/lottery/today")
def api_lottery_today():
    winner = draw_todays_winner()
    return jsonify(winner)


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Loaded {len(NAMES)} student names from {CSV_FILE}")
    print(f"Stripe key loaded: {'yes' if STRIPE_SECRET_KEY else 'NO — check .env'}")
    app.run(debug=True, host="0.0.0.0", port=port)

import os
import json
import re
import time
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import random
from flask import Flask, render_template, request, jsonify, session, redirect,send_from_directory,abort, Response
from fpdf import FPDF
from dotenv import load_dotenv
from travel_data import get_places, get_weather

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
DB_FILE = "itineraries.db"

SYSTEM_PROMPT = """
You are a friendly AI travel planning assistant.
Your job is to collect 5 things from the user, one question at a time:
1. destination (city or region)
2. number of days
3. total budget (always ask which currency, for example INR or USD)
4. interests (e.g. adventure, heritage, food, nature, shopping)
5. travel start date (ask the user, never guess it)

Rules:
- Ask only ONE question at a time, in short friendly sentences.
- Reply in the same language the user writes in (Hindi/Hinglish or English).
- If the user gives several details at once, do not ask for them again.
- When you have ALL 5 details, say a short confirmation message and then,
  at the very end, add the details in exactly this format (start_date as YYYY-MM-DD):
<PREFS>{"destination": "...", "days": 0, "budget": "...", "interests": ["...", "..."], "start_date": "YYYY-MM-DD"}</PREFS>
- Do not add the <PREFS> block before you have all 5 details.
- Never answer your own question and never invent or assume a value. Add the <PREFS> block only after the user has actually replied with all 5 details.
- Never ask a question in the same message as the <PREFS> block.
"""

ITINERARY_RULES = """
You are an expert travel planner. Create a day-wise itinerary using the real places
provided below. Do not invent attraction names; you may add meal and transport tips.

Rules:
- Plan exactly the number of days the traveller asked for, each with Morning, Afternoon and Evening.
- Label each day with its real date, using start_date from the traveller details, like "Day 1 (20 November 2026)".
- Every Morning, Afternoon and Evening slot must name a real place from the list. Do not use vague activities like "take a walk".
- Match the traveller's interests and group places that are in the same area on the same day.
- Stay within the budget. End with an estimated budget breakdown (stay, food, local transport,
  activities, total) in the traveller's currency, and say clearly whether it fits the budget.
- The weather list contains forecasts only for the trip days that have one. Use it for those days
  (suggest indoor plans if it rains). For days without a forecast, say it is not available yet.
- Write in simple, friendly English. Use **bold** for day headings.
"""

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "demo-secret-key-change-me")

history = []  # full chat history is kept here
last_result = {}  # har user ki latest itinerary yahan rehti hai, Save dabane tak


def call_gemini(contents, system_prompt):
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
    }
    for attempt in range(3):
        req = urllib.request.Request(
            URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))  # busy: wait and try again
                continue
            raise


def ask_gemini(user_message):
    history.append({"role": "user", "parts": [{"text": user_message}]})
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        SYSTEM_PROMPT
        + f"\nToday's date is {today}. Convert dates like 'tomorrow' or '20 November' "
        "to YYYY-MM-DD, using the next upcoming such date."
    )
    try:
        text = call_gemini(history, prompt)
    except Exception:
        history.pop()
        raise
    history.append({"role": "model", "parts": [{"text": text}]})
    return text


def filter_weather(weather, prefs):
    """Keep only forecast days that fall inside the trip dates."""
    try:
        start = datetime.strptime(prefs.get("start_date", ""), "%Y-%m-%d").date()
    except ValueError:
        return weather
    try:
        n = int(prefs.get("days") or 1)
    except (TypeError, ValueError):
        n = 1
    end = start + timedelta(days=max(n, 1) - 1)
    kept = []
    for w in weather:
        d = datetime.strptime(w["date"], "%Y-%m-%d").date()
        if start <= d <= end:
            kept.append(w)
    return kept


def build_itinerary(prefs):
    city = prefs.get("destination", "")
    interests = prefs.get("interests") or ["sightseeing"]
    if isinstance(interests, str):
        interests = [interests]
    interests = list(interests)
    if "sightseeing" not in interests:
        interests.append("sightseeing")

    try:
        places = get_places(city, interests, limit=6)
    except Exception as e:
        print("Places error:", e)
        places = {}
    try:
        weather = filter_weather(get_weather(city), prefs)
    except Exception as e:
        print("Weather error:", e)
        weather = []

    prompt = (
        ITINERARY_RULES
        + "\n\nTRAVELLER:\n" + json.dumps(prefs, ensure_ascii=False)
        + "\n\nREAL PLACES:\n" + json.dumps(places, ensure_ascii=False)
        + "\n\nWEATHER FORECAST (only trip days that have one):\n" + json.dumps(weather, ensure_ascii=False)
    )
    return call_gemini(
        [{"role": "user", "parts": [{"text": prompt}]}],
        "You are an expert travel planner.",
    )


def save_itinerary(prefs, itinerary):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS itineraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                destination TEXT,
                days TEXT,
                budget TEXT,
                interests TEXT,
                itinerary TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO itineraries (created_at, destination, days, budget, interests, itinerary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(prefs.get("destination", "")),
                str(prefs.get("days", "")),
                str(prefs.get("budget", "")),
                ", ".join(prefs.get("interests", [])) if isinstance(prefs.get("interests"), list) else str(prefs.get("interests", "")),
                itinerary,
            ),
        )

@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")
def get_trip(trip_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM itineraries WHERE id=? AND user_id=?",
            (trip_id, session["user_id"])).fetchone()

@app.route("/trip/<int:trip_id>")
def view_trip(trip_id):
    if "user_id" not in session:
        return redirect("/login")
    trip = get_trip(trip_id)
    if not trip:
        abort(404)
    return render_template("trip.html", trip=trip)

def clean(text):
    text = (text or "").replace("**", "").replace("•", "-").replace("₹", "Rs.")
    for a, b in {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")

    
class ItineraryPDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 9)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"AI Travel Agent  |  Page {self.page_no()}", align="C")
        
@app.route("/trip/<int:trip_id>/pdf")
def trip_pdf(trip_id):
    if "user_id" not in session:
        return redirect("/login")
    trip = get_trip(trip_id)
    if not trip:
        abort(404)

    pdf = ItineraryPDF()
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # ---- Header band ----
    pdf.set_fill_color(30, 58, 110)
    pdf.rect(0, 0, 210, 38, style="F")
    pdf.set_xy(15, 10)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 10, clean(f"{trip['destination']} Trip Itinerary"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(15)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Planned by AI Travel Agent", new_x="LMARGIN", new_y="NEXT")

    # ---- Trip info box ----
    pdf.set_xy(15, 45)
    pdf.set_fill_color(234, 240, 250)
    pdf.set_text_color(30, 58, 110)
    pdf.set_font("Helvetica", "B", 11)
    info = f"{trip['days']} days   |   Budget: {trip['budget']}   |   Interests: {trip['interests']}"
    pdf.multi_cell(0, 9, clean(info), fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # ---- Itinerary body ----
    for raw in clean(trip["itinerary"]).split("\n"):
        line = raw.strip().lstrip("*-# ").strip()
        if not line:
            pdf.ln(2)
            continue

        if re.match(r"(?i)^day\s*\d+", line):
            pdf.ln(3)
            pdf.set_fill_color(46, 95, 170)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 13)
            pdf.multi_cell(0, 9, " " + line, fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

        elif re.match(r"(?i)^(estimated\s+)?budget", line):
            pdf.ln(4)
            pdf.set_fill_color(39, 150, 100)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 13)
            pdf.multi_cell(0, 9, " " + line, fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

        else:
            pdf.set_text_color(40, 40, 40)
            pdf.set_font("Helvetica", "", 11)
            m = re.match(r"(?i)^(morning|afternoon|evening)\s*[:\-]\s*(.*)", line)
            pdf.set_x(18)
            if m:
                pdf.multi_cell(0, 6, f"**{m.group(1).title()}:** {m.group(2)}",
                               markdown=True, new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

    return Response(bytes(pdf.output()), mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="itinerary-{trip_id}.pdf"'})     
@app.route("/")
def home():
    if "user_id" not in session:
        return redirect("/login")
    return redirect("/dashboard")

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE, name TEXT, budget TEXT,
            travel_style TEXT, purpose TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS itineraries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, destination TEXT, days TEXT,
            budget TEXT, interests TEXT, itinerary TEXT, user_id INTEGER)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, trip_id INTEGER,
            title TEXT, content TEXT, created_at TEXT)""")

init_db()
@app.route("/plan")
def plan_page():
    if "user_id" not in session:
        return redirect("/login")
    history.clear()   # har baar Plan Trip kholne par nayi chat
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json["message"]
    try:
        text = ask_gemini(user_message)
    except Exception as e:
        print("API error:", e)
        if hasattr(e, "read"):
            print(e.read().decode("utf-8", errors="ignore"))
        return jsonify({"reply": "Oops, I could not connect to the AI. Please try again in a minute."})

    prefs = None
    match = re.search(r"<PREFS>(.*?)</PREFS>", text, re.DOTALL)
    if match:
        try:
            prefs = json.loads(match.group(1))
            print("Collected preferences:", prefs)
        except json.JSONDecodeError:
            prefs = None
        text = re.sub(r"<PREFS>.*?</PREFS>", "", text, flags=re.DOTALL).strip()

    if prefs and "?" in text:
        prefs = None  # the bot is still asking something, so the details are not complete yet
    if prefs:
        try:
            itinerary = build_itinerary(prefs)
            last_result[session.get("user_id")] = (prefs, itinerary)
            text = text + "\n\n" + itinerary
        except Exception as e:
            print("Itinerary error:", e)
            if hasattr(e, "read"):
                print(e.read().decode("utf-8", errors="ignore"))
            text = text + "\n\n(Sorry, I could not build the itinerary right now. Please send another message to try again.)"
        history[-1]["parts"][0]["text"] = text  # so the bot remembers the itinerary

    return jsonify({"reply": text, "prefs": prefs})

@app.route("/reset", methods=["POST"])
def reset():
    history.clear()
    return jsonify({"ok": True})

@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/send-otp", methods=["POST"])
def send_otp():
    phone = request.json.get("phone", "").strip()
    if not (phone.isdigit() and len(phone) == 10):
        return jsonify({"ok": False, "error": "Please enter a 10 digit phone number"})
    otp = str(random.randint(100000, 999999))
    session["otp"] = otp
    session["otp_phone"] = phone
    return jsonify({"ok": True, "demo_otp": otp})  # demo mode: OTP screen par dikhega


@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    otp = request.json.get("otp", "").strip()
    if otp and otp == session.get("otp"):
        phone = session["otp_phone"]
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT OR IGNORE INTO users (phone) VALUES (?)", (phone,))
            row = conn.execute("SELECT id, name FROM users WHERE phone = ?", (phone,)).fetchone()
        session["user_id"] = row[0]
        session.pop("otp", None)
        return jsonify({"ok": True, "next": "/" if row[1] else "/profile"})
    return jsonify({"ok": False, "error": "Wrong OTP, try again"})


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/profile")
def profile_page():
    if "user_id" not in session:
        return redirect("/login")
    with sqlite3.connect(DB_FILE) as conn:
        u = conn.execute(
            "SELECT name, budget, travel_style, purpose FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
    return render_template("profile.html", u=u)


@app.route("/save-profile", methods=["POST"])
def save_profile():
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "Please login first"})
    d = request.json
    name = d.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Please enter your name"})
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "UPDATE users SET name = ?, budget = ?, travel_style = ?, purpose = ? WHERE id = ?",
            (name, d.get("budget", ""), d.get("travel_style", ""), d.get("purpose", ""), session["user_id"]),
        )
    return jsonify({"ok": True, "next": "/"})

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")
    with sqlite3.connect(DB_FILE) as conn:
        user = conn.execute(
            "SELECT name, budget FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        trips = conn.execute(
            "SELECT id, created_at, destination, days, budget FROM itineraries "
            "WHERE user_id = ? ORDER BY id DESC",
            (session["user_id"],),
        ).fetchall()
    return render_template("dashboard.html", user=user, trips=trips)


@app.route("/save-trip", methods=["POST"])
def save_trip():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "error": "Please login first"})
    data = last_result.get(uid)
    if not data:
        return jsonify({"ok": False, "error": "Please generate an itinerary first(or it has already been saved) ."})
    prefs, itinerary = data
    interests = prefs.get("interests", "")
    if isinstance(interests, list):
        interests = ", ".join(interests)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO itineraries (created_at, destination, days, budget, interests, itinerary, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                str(prefs.get("destination", "")),
                str(prefs.get("days", "")),
                str(prefs.get("budget", "")),
                str(interests),
                itinerary,
                uid,
            ),
        )
    last_result.pop(uid, None)
    return jsonify({"ok": True})


@app.route("/memories")
def memories_page():
    if "user_id" not in session:
        return redirect("/login")
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        trips = conn.execute(
            "SELECT id, destination FROM itineraries WHERE user_id = ? ORDER BY id DESC",
            (session["user_id"],),
        ).fetchall()
        notes = conn.execute(
            "SELECT m.id, m.title, m.content, m.created_at, m.trip_id, i.destination "
            "FROM memories m LEFT JOIN itineraries i ON i.id = m.trip_id "
            "WHERE m.user_id = ? ORDER BY m.id DESC",
            (session["user_id"],),
        ).fetchall()
    return render_template(
        "memories.html",
        trips=[dict(t) for t in trips],
        notes=[dict(n) for n in notes],
    )


@app.route("/memories/save", methods=["POST"])
def memory_save():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "error": "Please login first"})
    d = request.json or {}
    title = (d.get("title") or "").strip()
    content = (d.get("content") or "").strip()
    note_id = d.get("id")
    try:
        trip_id = int(d.get("trip_id")) if d.get("trip_id") else None
    except (TypeError, ValueError):
        trip_id = None
    if not title and not content:
        return jsonify({"ok": False, "error": "Please write something first"})
    with sqlite3.connect(DB_FILE) as conn:
        if note_id:
            conn.execute(
                "UPDATE memories SET title = ?, content = ?, trip_id = ? WHERE id = ? AND user_id = ?",
                (title, content, trip_id, note_id, uid),
            )
        else:
            conn.execute(
                "INSERT INTO memories (user_id, trip_id, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, trip_id, title, content, datetime.now().isoformat(timespec="seconds")),
            )
    return jsonify({"ok": True})
@app.route("/memories/delete", methods=["POST"])
def memory_delete():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"ok": False, "error": "Please login first"})
    note_id = (request.json or {}).get("id")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (note_id, uid))
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True)
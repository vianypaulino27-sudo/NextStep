"""NextStep web server: serves the page and a JSON API backed by Postgres.

Env (names only; see .env.example and DATABASE.md):
  DATABASE_URL       Postgres connection string (required)
  SECRET_KEY         Flask session signing key (required in production)
  RENDER             set automatically on Render -> Secure session cookie
  ANTHROPIC_API_KEY  OPTIONAL and currently unused: if ever set, the assistant may
                     use an external model; the built-in grounded responder is the default
  ASSISTANT_MODEL    OPTIONAL model id for that dormant path

Run locally:  DATABASE_URL=... python3 -m flask --app server run
On Render:    gunicorn server:app
"""
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import date, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

import bcrypt
import psycopg
from flask import Flask, jsonify, request, send_file, session
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import assistant
import college_match as cm
from urlcheck import allowed_listing_url, safe_external_url

BASE_DIR = Path(__file__).resolve().parent
PAGE_FILE = BASE_DIR / "Index.html.html"  # the student's page; name kept as-is

POINTS_PER_STEP = 100  # every checklist step is worth exactly 100 points

log = logging.getLogger("nextstep")

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_NAME="nextstep_session",  # own name so other local Flask apps don't clash
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),  # https on Render
    MAX_CONTENT_LENGTH=64 * 1024,  # JSON bodies only; nothing large is ever uploaded
)
if os.environ.get("RENDER") and not os.environ.get("SECRET_KEY"):
    log.warning("SECRET_KEY is not set: sessions will reset on every restart")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- database
@contextmanager
def db():
    """One short-lived connection per request; commits on success, rolls back on error."""
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10) as conn:
        yield conn


def err(msg, code=400, **extra):
    return jsonify({"error": msg, **extra}), code


def current_user_id():
    return session.get("user_id")


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user_id():
            return err("Please sign in first.", 401)
        return fn(*a, **kw)

    return wrapper


@app.errorhandler(psycopg.OperationalError)
def db_down(e):
    log.error("database unavailable: %s", e.__class__.__name__)
    return err("The database is not reachable right now. Please try again in a minute.", 503)


@app.errorhandler(413)
def too_large(e):
    return err("That request is too large.", 413)


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Small in-memory sliding-window limiter (per process). Enough for one
    gunicorn worker on a small site; swap for Redis if the site scales out."""

    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key, limit, window):
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False, int(window - (now - q[0])) + 1
            q.append(now)
            return True, 0

    def reset(self):
        with self.lock:
            self.hits.clear()


limiter = RateLimiter()


def client_ip():
    # Render puts the real client first in X-Forwarded-For
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def rate_limited(name, limit, window, per="user"):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            who = (current_user_id() if per == "user" else None) or client_ip()
            ok, retry = limiter.allow(f"{name}:{who}", limit, window)
            if not ok:
                resp, code = err(f"Too many requests. Please wait {retry} seconds and try again.", 429,
                                 retry_after=retry)
                resp.headers["Retry-After"] = str(retry)
                return resp, code
            return fn(*a, **kw)

        return wrapper

    return deco


# ---------------------------------------------------------------- request hardening
@app.before_request
def same_origin_writes():
    """Reject cross-site state-changing requests (defence in depth on top of SameSite=Lax)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return err("Cross-site request blocked.", 403)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# ---------------------------------------------------------------- users & points
def points_summary(conn, uid):
    """Points are DERIVED, never stored as the source of truth:
         score     = 100 x completed checklist steps
         spent     = sum of reward costs redeemed
         available = score - spent (what the Gift Shop can spend; shown as >= 0)
    """
    row = conn.execute(
        """SELECT
             (SELECT count(*) FROM user_checklist_progress p JOIN checklist_items c ON c.id = p.checklist_item_id
               WHERE p.user_id = %(u)s AND p.is_completed) AS completed,
             (SELECT count(*) FROM checklist_items) AS total,
             (SELECT COALESCE(sum(rs.cost_points), 0) FROM redemptions r JOIN rewards_shop rs ON rs.id = r.reward_id
               WHERE r.user_id = %(u)s) AS spent""",
        {"u": uid},
    ).fetchone()
    score = POINTS_PER_STEP * row["completed"]
    available = score - int(row["spent"])
    return {"score": score, "completed": row["completed"], "total": row["total"], "spent": int(row["spent"]),
            "available": max(0, available), "overspent": max(0, -available)}


def sync_points_cache(conn, uid, summary):
    # users.points_balance is kept only as a cache of the derived balance (old code read it).
    conn.execute("UPDATE users SET points_balance=%s WHERE id=%s AND points_balance IS DISTINCT FROM %s",
                 (summary["available"], uid, summary["available"]))


def user_json(row, summary):
    return {
        "id": str(row["id"]),
        "name": row["full_name"],
        "email": row["email"],
        "role": row["role"],
        "gpa": float(row["unweighted_gpa"]) if row["unweighted_gpa"] is not None else None,
        "budget": row["annual_budget"],
        "points": summary["score"],          # checklist score (100 per completed step)
        "balance": summary["available"],     # spendable in the Gift Shop
        "pointsDetail": summary,
        "collegePrefs": row.get("college_prefs"),
    }


def fetch_user(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id=%s", (uid,)).fetchone()


# ---------------------------------------------------------------- page
@app.get("/")
def index():
    resp = send_file(PAGE_FILE, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/resources")
def resources():
    """Verified official links and tutorial videos (knowledge/resources.json)."""
    return jsonify(assistant.resources())


# ---------------------------------------------------------------- auth
@app.post("/api/login")
@rate_limited("login", 10, 300, per="ip")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()[:254]
    password = str(data.get("password") or "")[:200]
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(email)=%s", (email,)).fetchone()
        try:
            ok = bool(row) and bcrypt.checkpw(password.encode(), row["password_hash"].encode())
        except ValueError:
            ok = False
        if not ok:
            return err("Incorrect email or password.", 401)
        summary = points_summary(conn, row["id"])
    session.clear()
    session.permanent = True
    session["user_id"] = str(row["id"])
    return jsonify(user_json(row, summary))


@app.post("/api/signup")
@rate_limited("signup", 5, 3600, per="ip")
def signup():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:120]
    email = str(data.get("email") or "").strip().lower()[:254]
    password = str(data.get("password") or "")
    if not name or not EMAIL_RE.match(email):
        return err("Please enter your name and a valid email.")
    if not 8 <= len(password) <= 200:
        return err("Password must be at least 8 characters.")
    try:
        gpa = float(data.get("gpa")) if data.get("gpa") not in (None, "") else None
        budget = int(float(data.get("budget"))) if data.get("budget") not in (None, "") else None
    except (TypeError, ValueError):
        return err("GPA and budget must be numbers.")
    if gpa is not None and not 0 <= gpa <= 4:
        return err("GPA must be between 0 and 4.0.")
    if budget is not None and not 0 <= budget <= 1_000_000:
        return err("Budget must be between 0 and 1,000,000.")
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE lower(email)=%s", (email,)).fetchone():
            return err("An account with that email already exists. Please sign in.", 409)
        row = conn.execute(
            """INSERT INTO users (full_name, email, password_hash, role, unweighted_gpa, annual_budget, points_balance)
               VALUES (%s,%s,%s,'student',%s,%s,0) RETURNING *""",
            (name, email, pw_hash, gpa, budget),
        ).fetchone()
        summary = points_summary(conn, row["id"])
    session.clear()
    session.permanent = True
    session["user_id"] = str(row["id"])
    return jsonify(user_json(row, summary)), 201


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    uid = current_user_id()
    if not uid:
        return jsonify({"user": None})
    with db() as conn:
        row = fetch_user(conn, uid)
        if not row:
            session.clear()
            return jsonify({"user": None})
        summary = points_summary(conn, uid)
    return jsonify({"user": user_json(row, summary)})


@app.put("/api/me")
@login_required
def update_me():
    """Edit Student Profile modal: name, GPA, budget."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:120]
    try:
        gpa = float(data.get("gpa"))
        budget = int(float(data.get("budget")))
    except (TypeError, ValueError):
        return err("GPA and budget must be numbers.")
    if not name or not 0 <= gpa <= 4 or not 0 <= budget <= 1_000_000:
        return err("Please enter a name, a GPA between 0 and 4.0, and a budget between 0 and 1,000,000.")
    uid = current_user_id()
    with db() as conn:
        row = conn.execute(
            "UPDATE users SET full_name=%s, unweighted_gpa=%s, annual_budget=%s WHERE id=%s RETURNING *",
            (name, gpa, budget, uid),
        ).fetchone()
        if not row:
            session.clear()
            return err("Please sign in first.", 401)
        summary = points_summary(conn, uid)
    return jsonify(user_json(row, summary))


@app.get("/api/points")
@login_required
def points():
    with db() as conn:
        return jsonify(points_summary(conn, current_user_id()))


# ---------------------------------------------------------------- checklist
@app.get("/api/checklist")
def checklist():
    uid = current_user_id()
    with db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.title, c.tag, COALESCE(p.is_completed, false) AS done
               FROM checklist_items c
               LEFT JOIN user_checklist_progress p ON p.checklist_item_id=c.id AND p.user_id=%s
               ORDER BY c.id""",
            (uid,),
        ).fetchall()
    return jsonify([
        {"id": r["id"], "title": r["title"], "tag": r["tag"], "points": POINTS_PER_STEP, "done": r["done"]}
        for r in rows
    ])


@app.put("/api/checklist/<int:item_id>")
@login_required
@rate_limited("checklist", 120, 60)
def set_checklist(item_id):
    """Set a step done/undone. Idempotent: the score is recomputed from the
    completion state, so repeating a request can never add or remove points twice."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("done"), bool):
        return err("Send {\"done\": true} or {\"done\": false}.")
    done = body["done"]
    uid = current_user_id()
    with db() as conn:
        if not conn.execute("SELECT 1 FROM checklist_items WHERE id=%s", (item_id,)).fetchone():
            return err("No such checklist item.", 404)
        conn.execute(
            """INSERT INTO user_checklist_progress (user_id, checklist_item_id, is_completed, completed_at)
               VALUES (%s,%s,%s, CASE WHEN %s THEN CURRENT_TIMESTAMP END)
               ON CONFLICT (user_id, checklist_item_id) DO UPDATE
               SET is_completed=EXCLUDED.is_completed,
                   completed_at=CASE WHEN EXCLUDED.is_completed AND user_checklist_progress.is_completed
                                     THEN user_checklist_progress.completed_at ELSE EXCLUDED.completed_at END""",
            (uid, item_id, done, done),
        )
        summary = points_summary(conn, uid)
        sync_points_cache(conn, uid, summary)
    return jsonify({"id": item_id, "done": done, "points": summary["score"], "summary": summary})


@app.post("/api/checklist/reset")
@login_required
def reset_checklist():
    uid = current_user_id()
    with db() as conn:
        conn.execute(
            "UPDATE user_checklist_progress SET is_completed=false, completed_at=NULL WHERE user_id=%s", (uid,))
        summary = points_summary(conn, uid)
        sync_points_cache(conn, uid, summary)
    return jsonify({"ok": True, "points": summary["score"], "summary": summary})


# ---------------------------------------------------------------- scholarships
def deadline_state(deadline, status):
    if deadline and deadline < date.today():
        return "closed"
    return status or "unknown"


def scholarship_json(r):
    custom = bool(r["is_custom"])
    return {
        "id": r["slug"] or str(r["id"]),
        "title": r["title"],
        "provider": r["provider"],
        "amount": r["amount"],
        "amountText": r.get("amount_text") or (f"${r['amount']:,}" if r["amount"] else None),
        "deadline": r["deadline"].isoformat() if r["deadline"] else None,
        "deadlineText": r.get("deadline_text"),
        "status": "custom" if custom else deadline_state(r["deadline"], r.get("status")),
        "eligibility": r.get("eligibility"),
        "scope": r.get("scope"),
        "desc": r["description"],
        "applyUrl": None if custom else allowed_listing_url(r.get("apply_url")),
        "sourceUrl": None if custom else allowed_listing_url(r.get("source_url")),
        "lastVerified": r["last_verified"].isoformat() if r.get("last_verified") else None,
        "verified": bool(r.get("is_verified")),
        "active": bool(r.get("is_active", True)),
        "isCustom": custom,
    }


def find_scholarship(conn, key, uid):
    """Look up by slug or uuid. Custom scholarships are visible only to their creator."""
    return conn.execute(
        """SELECT * FROM scholarships WHERE (slug=%s OR id::text=%s)
             AND (NOT COALESCE(is_custom,false) OR created_by_user_id=%s)""",
        (key, key, uid),
    ).fetchone()


@app.get("/api/scholarships")
def scholarships():
    """Active verified scholarships plus the signed-in user's own custom ones, and
    the user's saved list (which can include retired items they saved earlier)."""
    uid = current_user_id()
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM scholarships
               WHERE (NOT COALESCE(is_custom,false) AND is_active AND is_verified)
                  OR (is_custom AND created_by_user_id=%s)
               ORDER BY COALESCE(is_custom,false) DESC,
                        CASE WHEN deadline IS NOT NULL AND deadline < CURRENT_DATE THEN 1 ELSE 0 END,
                        CASE status WHEN 'open' THEN 0 WHEN 'upcoming' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END,
                        deadline NULLS LAST, title""",
            (uid,),
        ).fetchall()
        saved, saved_items = [], []
        if uid:
            for r in conn.execute(
                """SELECT s.* FROM user_saved_scholarships us JOIN scholarships s ON s.id=us.scholarship_id
                   WHERE us.user_id=%s ORDER BY us.saved_at""", (uid,)):
                saved.append(r["slug"] or str(r["id"]))
                saved_items.append(scholarship_json(r))
    return jsonify({"scholarships": [scholarship_json(r) for r in rows], "saved": saved, "savedItems": saved_items})


@app.post("/api/scholarships")
@login_required
@rate_limited("custom-scholarship", 20, 3600)
def add_scholarship():
    """Add Custom Scholarship modal. Visible only to its creator; earns no points."""
    d = request.get_json(silent=True) or {}
    title = str(d.get("title") or "").strip()[:200]
    provider = str(d.get("provider") or "").strip()[:200]
    desc = str(d.get("desc") or "").strip()[:2000]
    try:
        amount = int(float(d.get("amount")))
    except (TypeError, ValueError):
        return err("Amount must be a number.")
    if not 0 < amount <= 10_000_000:
        return err("Amount must be a positive number.")
    try:
        deadline = date.fromisoformat(str(d.get("deadline") or "").strip())
    except ValueError:
        return err("Deadline must be a date (YYYY-MM-DD).")
    if not (title and provider and desc):
        return err("Please fill in the title, provider and description.")
    uid = current_user_id()
    with db() as conn:
        row = conn.execute(
            """INSERT INTO scholarships (title, provider, amount, deadline, description, is_custom, created_by_user_id,
                                         is_active, status)
               VALUES (%s,%s,%s,%s,%s,true,%s,true,'custom') RETURNING *""",
            (title, provider, amount, deadline, desc, uid),
        ).fetchone()
    return jsonify({"scholarship": scholarship_json(row)}), 201


@app.delete("/api/scholarships/<key>")
@login_required
def delete_scholarship(key):
    """Delete one of your own custom scholarships."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM scholarships WHERE (slug=%s OR id::text=%s) AND is_custom AND created_by_user_id=%s",
            (key, key, current_user_id()),
        )
    if cur.rowcount == 0:
        return err("Not found.", 404)
    return jsonify({"ok": True})


@app.post("/api/scholarships/<key>/save")
@login_required
@rate_limited("save", 120, 60)
def save_scholarship(key):
    """Add to the saved list (idempotent; the primary key prevents duplicates; no points)."""
    uid = current_user_id()
    with db() as conn:
        sch = find_scholarship(conn, key, uid)
        if not sch:
            return err("No such scholarship.", 404)
        conn.execute(
            "INSERT INTO user_saved_scholarships (user_id, scholarship_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (uid, sch["id"]),
        )
    return jsonify({"saved": True, "scholarship": scholarship_json(sch)})


@app.delete("/api/scholarships/<key>/save")
@login_required
def unsave_scholarship(key):
    uid = current_user_id()
    with db() as conn:
        sch = find_scholarship(conn, key, uid)
        if not sch:
            return err("No such scholarship.", 404)
        conn.execute("DELETE FROM user_saved_scholarships WHERE user_id=%s AND scholarship_id=%s", (uid, sch["id"]))
    return jsonify({"saved": False})


# ---------------------------------------------------------------- rewards
@app.get("/api/rewards")
def rewards():
    with db() as conn:
        rows = conn.execute("SELECT * FROM rewards_shop ORDER BY cost_points, slug").fetchall()
    return jsonify([
        {"id": r["slug"] or str(r["id"]), "title": r["title"], "cost": r["cost_points"], "category": r["category"],
         "icon": r["icon_name"], "desc": r["description"], "stock": r["stock_quantity"]}
        for r in rows
    ])


@app.post("/api/redemptions")
@login_required
@rate_limited("redeem", 10, 3600)
def redeem():
    """Redeem a reward: the balance is derived (earned - spent) and checked inside a
    transaction that locks the user row, so double clicks cannot overspend."""
    d = request.get_json(silent=True) or {}
    address = str(d.get("address") or "").strip()[:300]
    option = str(d.get("option") or "").strip()[:100] or None
    if not address:
        return err("Please enter a shipping address or delivery email.")
    uid = current_user_id()
    with db() as conn:
        conn.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
        reward = conn.execute(
            "SELECT * FROM rewards_shop WHERE slug=%s OR id::text=%s FOR UPDATE",
            (str(d.get("reward_id")), str(d.get("reward_id"))),
        ).fetchone()
        if not reward:
            return err("No such reward.", 404)
        if reward["stock_quantity"] is not None and reward["stock_quantity"] <= 0:
            return err("Sorry, that reward is out of stock.", 409)
        if points_summary(conn, uid)["available"] < reward["cost_points"]:
            return err("Not enough points for this reward.", 409)
        red = conn.execute(
            """INSERT INTO redemptions (user_id, reward_id, shipping_address_or_email, size_or_option)
               VALUES (%s,%s,%s,%s) RETURNING id, status""",
            (uid, reward["id"], address, option),
        ).fetchone()
        conn.execute(
            "UPDATE rewards_shop SET stock_quantity = stock_quantity - 1 WHERE id=%s AND stock_quantity IS NOT NULL",
            (reward["id"],),
        )
        summary = points_summary(conn, uid)
        sync_points_cache(conn, uid, summary)
    return jsonify({"id": str(red["id"]), "status": red["status"], "points": summary["score"],
                    "balance": summary["available"], "summary": summary}), 201


# ---------------------------------------------------------------- tutors & mentors
DEMO_LABEL = "Demo profile — fictional example, not a verified mentor."


def person_json(r, full=False):
    out = {
        "id": r["slug"] or str(r["id"]),
        "kind": r["kind"],
        "name": r["display_name"] or r.get("full_name") or "Mentor",
        "school": r["university"],
        "major": r["major"],
        "classYear": r.get("class_year"),
        "pronouns": r.get("pronouns"),
        "testScore": r["test_scores"],
        "rate": r["rate"],
        "bio": r["bio"],
        "specialties": r["specialties"] or [],
        "helpAreas": r.get("help_areas") or [],
        "languages": r.get("languages") or [],
        "isDemo": bool(r.get("is_demo")),
        "verified": bool(r.get("verified")),
        "demoLabel": DEMO_LABEL if r.get("is_demo") else None,
        "availability": r.get("availability"),      # None -> "Availability not provided"
        "responseTime": r.get("response_time"),     # None -> not shown
    }
    if full:
        out.update({
            "journey": r.get("journey"),
            "academicInterests": r.get("academic_interests") or [],
            "careerInterests": r.get("career_interests") or [],
            "activities": r.get("activities") or [],
            "experienceAreas": r.get("experience_areas") or [],
            "verifiedNote": r.get("verified_note") if r.get("verified") else None,
            "selfReportedNote": None if r.get("is_demo") else (
                "Profile details are self-reported by the mentor" +
                ("; NextStep verified: " + r["verified_note"] if r.get("verified") and r.get("verified_note") else ".")),
        })
    return out


PEOPLE_SQL = """SELECT m.*, u.full_name FROM mentors_and_tutors m LEFT JOIN users u ON u.id=m.user_id
                WHERE m.status <> 'pending_verification'"""


def people(kind):
    with db() as conn:
        rows = conn.execute(PEOPLE_SQL + " AND m.kind=%s ORDER BY m.verified DESC, m.slug NULLS LAST, m.created_at",
                            (kind,)).fetchall()
    return jsonify([person_json(r) for r in rows])


@app.get("/api/tutors")
def tutors():
    return people("tutor")


@app.get("/api/mentors")
def mentors():
    return people("mentor")


@app.get("/api/mentors/<key>")
def mentor_profile(key):
    """Full profile. Viewing it never creates a request."""
    with db() as conn:
        r = conn.execute(PEOPLE_SQL + " AND (m.slug=%s OR m.id::text=%s)", (key, key)).fetchone()
    if not r:
        return err("No such mentor or tutor.", 404)
    return jsonify(person_json(r, full=True))


REQUEST_TOPICS = ["College list", "Applications", "Essays", "Financial aid / FAFSA / TAP", "Scholarships",
                  "Internships & careers", "Test prep", "College transition", "Other"]


def request_json(r):
    return {"id": str(r["id"]), "mentorId": r["slug"] or str(r["mentor_id"]), "mentorName": r["display_name"],
            "isDemo": bool(r["is_demo"]), "type": r["request_type"], "topics": r["topics"] or [],
            "notes": r["notes"], "status": r["status"], "requestedAt": r["requested_at"].isoformat()}


@app.get("/api/mentor-requests")
@login_required
def my_mentor_requests():
    with db() as conn:
        rows = conn.execute(
            """SELECT r.*, m.slug, m.display_name, m.is_demo FROM mentor_requests r
               JOIN mentors_and_tutors m ON m.id=r.mentor_id
               WHERE r.student_id=%s ORDER BY r.requested_at DESC""", (current_user_id(),)).fetchall()
    return jsonify({"requests": [request_json(r) for r in rows], "topics": REQUEST_TOPICS})


@app.post("/api/mentor-requests")
@login_required
@rate_limited("mentor-request", 10, 3600)
def mentor_request():
    """Create a mentorship / tutoring session request. One pending request per
    student+mentor+type (partial unique index). No points are awarded."""
    d = request.get_json(silent=True) or {}
    req_type = d.get("type") or "mentorship"
    if req_type not in ("act_sat_tutoring", "mentorship"):
        return err("Unknown request type.")
    topics = d.get("topics") or []
    if not isinstance(topics, list) or any(t not in REQUEST_TOPICS for t in topics):
        return err("Choose topics from the list.")
    notes = str(d.get("notes") or "").strip()
    if len(notes) > 1000:
        return err("Please keep your note under 1,000 characters.")
    if req_type == "mentorship" and not topics:
        return err("Choose at least one thing you'd like help with.")
    uid = current_user_id()
    with db() as conn:
        m = conn.execute(
            "SELECT id, kind, is_demo FROM mentors_and_tutors WHERE (slug=%s OR id::text=%s) "
            "AND status <> 'pending_verification'",
            (str(d.get("mentor_id")), str(d.get("mentor_id"))),
        ).fetchone()
        if not m:
            return err("No such tutor or mentor.", 404)
        try:
            with conn.transaction():
                row = conn.execute(
                    """INSERT INTO mentor_requests (student_id, mentor_id, request_type, notes, topics, updated_at)
                       VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP) RETURNING id, status""",
                    (uid, m["id"], req_type, notes or None, topics),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            return err("You already have a pending request with this person. You can see it under My requests.", 409)
    return jsonify({"id": str(row["id"]), "status": row["status"], "isDemo": bool(m["is_demo"])}), 201


@app.post("/api/mentor-requests/<req_id>/cancel")
@login_required
def cancel_mentor_request(req_id):
    with db() as conn:
        row = conn.execute(
            """UPDATE mentor_requests SET status='cancelled', updated_at=CURRENT_TIMESTAMP
               WHERE id::text=%s AND student_id=%s AND status='pending' RETURNING id""",
            (req_id, current_user_id()),
        ).fetchone()
    if not row:
        return err("No pending request with that id.", 404)
    return jsonify({"ok": True, "status": "cancelled"})


@app.post("/api/mentor-applications")
@rate_limited("mentor-apply", 5, 3600, per="ip")
def mentor_application():
    """'Apply as Mentor/Tutor' form (no login). Stored as a pending, unverified
    profile; hidden from the site until staff verify it."""
    d = request.get_json(silent=True) or {}
    name = str(d.get("name") or "").strip()[:200]
    university = str(d.get("university") or "").strip()[:200]
    major = str(d.get("major") or "").strip()[:200]
    bio = str(d.get("bio") or "").strip()[:1500]
    help_areas = d.get("help_areas") if isinstance(d.get("help_areas"), list) else []
    help_areas = [str(x).strip()[:80] for x in help_areas if str(x).strip()][:10]
    if not (name and university and major):
        return err("Please fill in your name, college and major.")
    with db() as conn:
        row = conn.execute(
            """INSERT INTO mentors_and_tutors (kind, display_name, university, major, bio, status, help_areas,
                                               is_demo, verified)
               VALUES ('mentor', %s, %s, %s, %s, 'pending_verification', %s, false, false) RETURNING id""",
            (name, university, major, bio, help_areas),
        ).fetchone()
    return jsonify({"id": str(row["id"]), "status": "pending_verification"}), 201


# ---------------------------------------------------------------- colleges (College Scorecard)
_trgm = {"checked": False, "on": False}
_college_cache = {"rows": None, "at": 0.0}
COLLEGE_CACHE_SECONDS = 600

LIST_COLS = ("unitid, name, alias, city, state, control, website, admission_rate, open_admission, "
             "cost_of_attendance, avg_net_price, undergrad_enrollment, high_degree")


def has_trgm(conn):
    if not _trgm["checked"]:
        _trgm["on"] = bool(conn.execute("SELECT 1 FROM pg_extension WHERE extname='pg_trgm'").fetchone())
        _trgm["checked"] = True
    return _trgm["on"]


def college_brief(r):
    return {"unitid": r["unitid"], "name": r["name"], "city": r["city"], "state": r["state"],
            "type": cm.CONTROL_LABELS.get(r["control"], "Unknown"), "website": safe_external_url(r["website"]),
            "admissionRate": float(r["admission_rate"]) if r["admission_rate"] is not None else None,
            "openAdmission": r["open_admission"], "costOfAttendance": r["cost_of_attendance"],
            "avgNetPrice": r["avg_net_price"], "undergrads": r["undergrad_enrollment"],
            "highestDegree": cm.DEGREE_LABELS.get(r["high_degree"])}


def data_source(conn):
    src = conn.execute("SELECT * FROM college_data_source WHERE id='scorecard'").fetchone()
    if not src:
        return None
    return {"title": src["title"], "url": src["source_url"], "file": src["file_name"], "released": src["released"],
            "years": src["data_years"], "rowCount": src["row_count"],
            "importedAt": src["imported_at"].date().isoformat(),
            "coverage": "U.S. colleges and universities that take part in federal student aid (Title IV) and award "
                        "at least an associate degree. International institutions are not included."}


@app.get("/api/colleges/meta")
def colleges_meta():
    with db() as conn:
        return jsonify({"source": data_source(conn), "majors": cm.MAJORS, "incomeBands": cm.INCOME_BANDS,
                        "types": cm.CONTROL_LABELS, "states": cm.STATES})


@app.get("/api/colleges/search")
@rate_limited("college-search", 90, 60, per="ip")
def college_search():
    """Search by full or partial name (case/punctuation-insensitive, abbreviations
    expanded, misspellings tolerated via pg_trgm when available), paginated."""
    raw = (request.args.get("q") or "").strip()[:100]
    variants = cm.query_variants(raw)
    if not variants or len(variants[0]) < 2:
        return err("Type at least 2 letters of a college name.")
    try:
        page = max(1, min(200, int(request.args.get("page", 1))))
        per = max(1, min(50, int(request.args.get("per_page", 10))))
    except ValueError:
        return err("page and per_page must be numbers.")
    state = (request.args.get("state") or "").upper()[:2] or None
    with db() as conn:
        trgm = has_trgm(conn)
        token_clauses = []
        params = {"state": state, "lim": per + 1, "off": (page - 1) * per, "q": variants[-1], "q0": variants[0]}
        for vi, v in enumerate(variants):
            parts = []
            for ti, t in enumerate(v.split()[:8]):
                params[f"t{vi}_{ti}"] = f"%{t}%"  # normalised text has no LIKE wildcards
                parts.append(f"search_text LIKE %(t{vi}_{ti})s")
            token_clauses.append("(" + " AND ".join(parts) + ")")
        match = " OR ".join(token_clauses)
        rank = ""
        if trgm:
            match = f"({match}) OR word_similarity(%(q)s, search_text) >= 0.55"
            rank = "word_similarity(%(q)s, search_text) DESC, similarity(%(q)s, search_text) DESC,"
        sql = f"""SELECT {LIST_COLS}, count(*) OVER () AS total FROM college_scorecard
                  WHERE ({match}) AND (%(state)s::text IS NULL OR state = %(state)s)
                  ORDER BY (search_text LIKE %(q0)s || '%%') DESC, {rank} undergrad_enrollment DESC NULLS LAST, name
                  LIMIT %(lim)s OFFSET %(off)s"""
        rows = conn.execute(sql, params).fetchall()
        fuzzy = False
        if not rows and trgm and page == 1:
            # nothing matched: offer the closest names as "did you mean"
            rows = conn.execute(
                f"""SELECT {LIST_COLS}, 0 AS total FROM college_scorecard
                    WHERE word_similarity(%(q)s, search_text) >= 0.35
                      AND (%(state)s::text IS NULL OR state = %(state)s)
                    ORDER BY word_similarity(%(q)s, search_text) DESC LIMIT 5""", params).fetchall()
            fuzzy = bool(rows)
        total = rows[0]["total"] if rows and not fuzzy else 0
    return jsonify({"query": raw, "page": page, "perPage": per, "total": total, "hasMore": len(rows) > per,
                    "didYouMean": fuzzy, "results": [college_brief(r) for r in rows[:per]],
                    "coverage": "U.S. Title IV institutions only (College Scorecard)."})


def college_detail_json(r, src):
    bands = r["net_price_by_income"] or {}
    sat = cm.sat_range(r)
    return {
        "unitid": r["unitid"], "name": r["name"], "alias": r["alias"], "city": r["city"], "state": r["state"],
        "zip": r["zip"], "website": safe_external_url(r["website"]),
        "netPriceCalculator": safe_external_url(r["net_price_calc_url"]),
        "type": cm.CONTROL_LABELS.get(r["control"], "Unknown"),
        "highestDegree": cm.DEGREE_LABELS.get(r["high_degree"]),
        "predominantDegree": cm.DEGREE_LABELS.get(r["pred_degree"]),
        "locale": cm.LOCALE_LABELS.get(r["locale"]),
        "undergrads": r["undergrad_enrollment"],
        "admissions": {
            "rate": float(r["admission_rate"]) if r["admission_rate"] is not None else None,
            "openAdmission": r["open_admission"],
            "testPolicy": cm.TEST_POLICY.get(r["test_policy"]),
            "satAverage": r["sat_avg"], "satMiddle50": list(sat) if sat else None,
            "actMiddle50": [r["act_25"], r["act_75"]] if r["act_25"] and r["act_75"] else None,
        },
        "cost": {
            "tuitionInState": r["tuition_in_state"], "tuitionOutOfState": r["tuition_out_state"],
            "costOfAttendance": r["cost_of_attendance"], "avgNetPrice": r["avg_net_price"],
            "netPriceByIncome": [{"band": cm.INCOME_BANDS[k], "key": k, "amount": bands[k]}
                                 for k in cm.INCOME_BANDS if k in bands],
            "note": ("Tuition & fees are the published price of classes. Cost of attendance adds housing, food, "
                     "books, transportation and personal costs. Net price is the average actually paid after "
                     "grants and scholarships."),
            "inStateNote": ("Public colleges charge in-state and out-of-state students different tuition; private "
                            "colleges usually charge everyone the same. Cost of attendance and net price are "
                            "Scorecard averages, not your personal price."),
        },
        "outcomes": {
            "completionRate": float(r["completion_rate"]) if r["completion_rate"] is not None else None,
            "retentionRate": float(r["retention_rate"]) if r["retention_rate"] is not None else None,
            "medianDebt": r["median_debt"], "medianEarnings10yr": r["median_earnings_10yr"],
        },
        "aid": {"pellShare": float(r["pell_share"]) if r["pell_share"] is not None else None},
        "programs": {"bachelor": r["bachelor_programs"], "associate": r["associate_programs"]},
        "flags": {k: r[k] for k in ("hbcu", "hsi", "women_only", "men_only", "distance_only")},
        "sources": [
            {"title": "College Scorecard page (U.S. Department of Education)",
             "url": f"https://collegescorecard.ed.gov/school/?{r['unitid']}"},
            {"title": "College Navigator (NCES)", "url": f"https://nces.ed.gov/collegenavigator/?id={r['unitid']}"},
        ],
        "dataSource": src,
    }


@app.get("/api/colleges/<int:unitid>")
def college_detail(unitid):
    uid = current_user_id()
    with db() as conn:
        r = conn.execute("SELECT * FROM college_scorecard WHERE unitid=%s", (unitid,)).fetchone()
        if not r:
            return err("No college with that ID in the College Scorecard data.", 404)
        saved = bool(uid and conn.execute(
            "SELECT 1 FROM user_saved_colleges WHERE user_id=%s AND unitid=%s", (uid, unitid)).fetchone())
        out = college_detail_json(r, data_source(conn))
    out["saved"] = saved
    return jsonify(out)


def all_college_rows(conn):
    now = time.monotonic()
    if _college_cache["rows"] is None or now - _college_cache["at"] > COLLEGE_CACHE_SECONDS:
        _college_cache["rows"] = conn.execute(
            """SELECT unitid, name, city, state, control, website, net_price_calc_url, admission_rate, open_admission,
                      sat_rw_25, sat_rw_75, sat_math_25, sat_math_75, act_25, act_75, cost_of_attendance,
                      avg_net_price, net_price_by_income, completion_rate, undergrad_enrollment, high_degree,
                      bachelor_programs, associate_programs, men_only, women_only, hbcu, hsi
               FROM college_scorecard""").fetchall()
        _college_cache["at"] = now
    return _college_cache["rows"]


@app.post("/api/colleges/match")
@rate_limited("college-match", 30, 60, per="ip")
def college_match():
    d = request.get_json(silent=True) or {}
    try:
        prefs = cm.validate_prefs(d)
    except cm.InputError as e:
        return err("Please fix the highlighted fields.", 400, fields=e.errors)
    try:
        page = max(1, min(100, int(d.get("page") or 1)))
    except (TypeError, ValueError):
        page = 1
    uid = current_user_id()
    with db() as conn:
        result = cm.recommend(all_college_rows(conn), prefs, page=page, per_page=12)
        src = data_source(conn)
        if uid:  # remember the student's last matcher inputs
            conn.execute("UPDATE users SET college_prefs=%s WHERE id=%s", (Jsonb(prefs), uid))
    out = []
    for c, ev in result["results"]:
        b = college_brief({**c, "alias": None})
        b.update({"fit": ev["fit"], "reasons": ev["reasons"], "cautions": ev["cautions"], "matches": ev["matches"],
                  "affordability": ev["affordability"], "verify": ev["verify"],
                  "netPriceCalculator": safe_external_url(c.get("net_price_calc_url"))})
        out.append(b)
    return jsonify({"prefs": prefs, "total": result["total"], "counts": result["counts"], "page": result["page"],
                    "perPage": result["per_page"], "results": out, "source": src,
                    "disclaimer": ("Labels use published admission rates and SAT/ACT ranges from College Scorecard. "
                                   "They are not predictions or guarantees of admission. Scorecard publishes no GPA "
                                   "data, so GPA is only a rough signal. Colleges are never removed because of "
                                   "sticker price.")})


@app.get("/api/colleges/saved")
@login_required
def saved_colleges():
    cols = ", ".join("c." + c.strip() for c in LIST_COLS.split(","))
    with db() as conn:
        rows = conn.execute(
            f"""SELECT {cols} FROM user_saved_colleges s JOIN college_scorecard c ON c.unitid=s.unitid
                WHERE s.user_id=%s ORDER BY s.saved_at""", (current_user_id(),)).fetchall()
    return jsonify([college_brief(r) for r in rows])


@app.post("/api/colleges/<int:unitid>/save")
@login_required
def save_college(unitid):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM college_scorecard WHERE unitid=%s", (unitid,)).fetchone():
            return err("No such college.", 404)
        conn.execute("INSERT INTO user_saved_colleges (user_id, unitid) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                     (current_user_id(), unitid))
    return jsonify({"saved": True})


@app.delete("/api/colleges/<int:unitid>/save")
@login_required
def unsave_college(unitid):
    with db() as conn:
        conn.execute("DELETE FROM user_saved_colleges WHERE user_id=%s AND unitid=%s", (current_user_id(), unitid))
    return jsonify({"saved": False})


# ---------------------------------------------------------------- internships
def internship_json(r):
    return {
        "id": r["slug"], "title": r["title"], "org": r["org"], "target": r["target"],
        "location": r["location"], "format": r["format"], "desc": r["description"],
        "responsibilities": r["responsibilities"], "eligibility": r["eligibility"], "ageGrade": r["age_grade"],
        "deadline": r["deadline"].isoformat() if r["deadline"] else None, "deadlineText": r["deadline_text"],
        "status": deadline_state(r["deadline"], r["status"]),
        "compensation": r["compensation"] or "Not specified", "compensationText": r["compensation_text"],
        "skills": r["skills"], "applyUrl": allowed_listing_url(r["apply_url"]),
        "sourceUrl": allowed_listing_url(r["source_url"]),
        "lastVerified": r["last_verified"].isoformat() if r["last_verified"] else None,
    }


@app.get("/api/internships")
def internships():
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM internships WHERE is_active AND is_verified
               ORDER BY CASE WHEN deadline IS NOT NULL AND deadline < CURRENT_DATE THEN 1 ELSE 0 END,
                        CASE status WHEN 'open' THEN 0 WHEN 'upcoming' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END,
                        target DESC, title""").fetchall()
    return jsonify([internship_json(r) for r in rows])


# ---------------------------------------------------------------- assistant
GENERIC_LISTING_WORDS = set("""scholarship scholarships program programs award awards grant grants foundation fund
the of for and a an in at nys new york deadline due when is what s college prize youth national
application apply open close closes closed""".split())


def _distinct_tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in GENERIC_LISTING_WORDS and w not in assistant.STOP and len(w) > 1}


def match_listings(question, items):
    """Strict name match so we never answer about a different scholarship:
    at least half of the question's distinctive words must belong to the listing."""
    qt = _distinct_tokens(question)
    if not qt:
        return []
    hits = []
    for it in items:
        overlap = qt & _distinct_tokens(it["title"] + " " + it["provider"])
        if overlap and len(overlap) / len(qt) >= 0.5:
            hits.append((len(overlap), it))
    return [it for _, it in sorted(hits, key=lambda t: -t[0])]


COLLEGE_Q_NOISE = re.compile(
    r"\b(what|whats|is|the|acceptance|admission|admit|rate|net|price|cost|of|attendance|tuition|graduation|"
    r"retention|at|for|how|much|does|hard|selective|to|get|into|in|sat|act|scores?|range|it|a|an|s)\b")


def assistant_context(conn, uid):
    def own_summary():
        s = points_summary(conn, uid)
        r = conn.execute(
            """SELECT (SELECT count(*) FROM user_saved_scholarships WHERE user_id=%(u)s) AS saved,
                      (SELECT count(*) FROM mentor_requests WHERE student_id=%(u)s) AS requests,
                      (SELECT count(*) FROM mentor_requests WHERE student_id=%(u)s AND status='pending') AS pending""",
            {"u": uid}).fetchone()
        return {**s, **r}

    def find_scholarship(question):
        rows = conn.execute("SELECT * FROM scholarships WHERE is_active AND is_verified").fetchall()
        items = [{"title": r["title"], "provider": r["provider"],
                  "deadline": r["deadline"].isoformat() if r["deadline"] else None,
                  "deadline_text": r["deadline_text"], "status": deadline_state(r["deadline"], r["status"]),
                  "last_verified": r["last_verified"].isoformat() if r["last_verified"] else None,
                  "apply_url": allowed_listing_url(r["apply_url"])} for r in rows]
        return match_listings(question, items)

    def find_college(question):
        q = re.sub(r"\s+", " ", COLLEGE_Q_NOISE.sub(" ", cm.normalize(question))).strip()
        if len(q) < 3 or not has_trgm(conn):
            return None
        r = conn.execute(
            """SELECT * FROM college_scorecard WHERE word_similarity(%(q)s, search_text) >= 0.6
               ORDER BY word_similarity(%(q)s, search_text) DESC, similarity(%(q)s, search_text) DESC,
                        undergrad_enrollment DESC NULLS LAST LIMIT 1""", {"q": q}).fetchone()
        if not r:
            return None
        rate = r["admission_rate"]
        lines = [f"From College Scorecard (U.S. Department of Education) for {r['name']} ({r['city']}, {r['state']}):",
                 "- Admission rate (fall 2024): " + (f"{round(float(rate) * 100)}%" if rate is not None else
                                                      ("open admission" if r["open_admission"] else "not reported"))]
        if cm.sat_range(r):
            lo, hi = cm.sat_range(r)
            lines.append(f"- SAT middle 50% (approximate total): {lo}-{hi}")
        lines.append("- Average cost of attendance (2023-24): " + (
            f"${r['cost_of_attendance']:,}" if r["cost_of_attendance"] else "not reported"))
        lines.append("- Average net price after grants (2023-24): " + (
            f"${r['avg_net_price']:,}" if r["avg_net_price"] else "not reported"))
        lines.append("- Graduation rate (within 150% of normal time): " + (
            f"{round(float(r['completion_rate']) * 100)}%" if r["completion_rate"] is not None else "not reported"))
        lines.append("These are published averages, not predictions. Check the college's own website, and search "
                     "for it in the College Matcher to see all the details.")
        return {"text": "\n".join(lines), "name": r["name"], "website": safe_external_url(r["website"]),
                "source_url": f"https://collegescorecard.ed.gov/school/?{r['unitid']}"}

    return {"own_summary": own_summary, "find_scholarship": find_scholarship, "find_college": find_college}


@app.post("/api/assistant")
@login_required
@rate_limited("assistant", 20, 60)
def assistant_endpoint():
    """Grounded help assistant. Message content is never logged."""
    d = request.get_json(silent=True) or {}
    msg = d.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return err("Please type a question.")
    if len(msg) > assistant.MAX_QUESTION_CHARS:
        return err(f"Please keep questions under {assistant.MAX_QUESTION_CHARS} characters.", 413)
    uid = current_user_id()
    with db() as conn:
        reply = assistant.answer(msg, assistant_context(conn, uid))
    if assistant.provider_configured() and reply.get("matched") is None:
        # Dormant: only when ANTHROPIC_API_KEY is configured and the knowledge base had no answer.
        try:
            text = assistant.ask_provider(msg)
            if text:
                reply = {**reply, "answer": text, "engine": "external-model",
                         "basis_note": "Generated by an AI model from NextStep's reference notes. It may contain "
                                       "mistakes, so check the official links."}
        except Exception as e:  # noqa: BLE001 - any provider failure falls back to the built-in answer
            log.warning("assistant provider failed: %s", e.__class__.__name__)
            reply["providerError"] = True
    return jsonify(reply)


if __name__ == "__main__":
    app.run(debug=True)

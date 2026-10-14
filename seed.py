"""Idempotent seed for NextStep demo data.

Run after schema.sql (fresh DB) or migrate.py (existing DB):

    DATABASE_URL=postgresql://... python3 seed.py

What it does (safe to run any number of times):
  * inserts the 7 checklist steps if they are missing,
  * upserts the Gift Shop items by slug,
  * gives seed users that still carry a placeholder password hash a bcrypt
    hash of the demo password `demo1234`,
  * loads the curated, verified listings and demo mentor profiles from data/*.json
    (scripts/import_listings.py),
  * gives the main demo student (alex.rivera@example.com) a starting state:
    checklist steps 1-2 done and two verified scholarships saved - only if
    those rows are missing.

It no longer loads the page's original made-up scholarships, colleges,
internships or mentor photos (see DATABASE.md: "Data sources").
Colleges come from scripts/import_scorecard.py.
"""
import os
import re
import sys

import bcrypt
import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import import_listings  # noqa: E402

DEMO_PASSWORD = "demo1234"
BCRYPT_RE = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")

CHECKLIST = [
    (1, "Create StudentAid.gov Account (FSA ID)", "FAFSA"),
    (2, "Gather tax documents (Prior-Prior Year W-2 & 1040)", "FAFSA"),
    (3, "Fill out FAFSA demographic info & add preferred schools", "FAFSA"),
    (4, "Sign and submit FAFSA with student & parent FSA IDs", "FAFSA"),
    (5, "Link to NYS TAP Application via FAFSA submission page", "TAP"),
    (6, "Complete HESC TAP profile and confirm NYS school code", "TAP"),
    (7, "Take 1 Official SAT or ACT Practice Test", "SAT/ACT"),
]

REWARDS = [
    ("rew-1", "$5 Amazon Gift Card", 300, "Gift Card", "credit-card", "Digital code for school supplies, books, or electronics."),
    ("rew-2", "$5 Target Gift Card", 300, "Gift Card", "shopping-cart", "Digital code sent to your email by NextStep staff."),
    ("rew-3", "$5 Starbucks Gift Card", 300, "Gift Card", "coffee", "Grab a coffee before your next study session or test prep practice."),
    ("rew-4", "Official College Pennant", 500, "Merch", "flag", "Felt pennant for Columbia, NYU, SUNY, or CUNY."),
    ("rew-5", "College Logo T-Shirt", 800, "Merch", "shirt", "Cotton university t-shirt of your choice."),
    ("rew-6", "University Hoodie / Sweatshirt", 1200, "Merch", "package", "College hoodie sent to your home."),
]

DEMO_STUDENT = "alex.rivera@example.com"
DEMO_DONE_ITEMS = [1, 2]
DEMO_SAVED = ["jack-kent-cooke-college-scholarship", "cuny-macaulay-honors-college"]


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        for item_id, title, tag in CHECKLIST:
            cur.execute("""INSERT INTO checklist_items (id, title, tag, point_reward) VALUES (%s,%s,%s,100)
                           ON CONFLICT (id) DO NOTHING""", (item_id, title, tag))
        cur.execute("SELECT setval(pg_get_serial_sequence('checklist_items','id'), (SELECT max(id) FROM checklist_items))")

        for r in REWARDS:
            cur.execute(
                """INSERT INTO rewards_shop (slug, title, cost_points, category, icon_name, description)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (slug) DO UPDATE SET title=EXCLUDED.title, cost_points=EXCLUDED.cost_points,
                     category=EXCLUDED.category, icon_name=EXCLUDED.icon_name, description=EXCLUDED.description""", r)

        demo_hash = bcrypt.hashpw(DEMO_PASSWORD.encode(), bcrypt.gensalt()).decode()
        cur.execute("SELECT id, password_hash FROM users")
        for uid, h in cur.fetchall():
            if not BCRYPT_RE.match(h or ""):
                cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (demo_hash, uid))

        import_listings.import_all(conn)

        for item_id in DEMO_DONE_ITEMS:
            cur.execute(
                """INSERT INTO user_checklist_progress (user_id, checklist_item_id, is_completed, completed_at)
                   SELECT id, %s, true, CURRENT_TIMESTAMP FROM users WHERE email=%s
                   ON CONFLICT (user_id, checklist_item_id) DO NOTHING""", (item_id, DEMO_STUDENT))
        for slug in DEMO_SAVED:
            cur.execute(
                """INSERT INTO user_saved_scholarships (user_id, scholarship_id)
                   SELECT u.id, s.id FROM users u, scholarships s WHERE u.email=%s AND s.slug=%s
                   ON CONFLICT DO NOTHING""", (DEMO_STUDENT, slug))

        conn.commit()
        for t in ["users", "checklist_items", "scholarships", "rewards_shop", "mentors_and_tutors",
                  "internships", "college_scorecard", "user_checklist_progress", "user_saved_scholarships"]:
            cur.execute(f"SELECT count(*) FROM {t}")
            print(f"{t}: {cur.fetchone()[0]}")


if __name__ == "__main__":
    main()

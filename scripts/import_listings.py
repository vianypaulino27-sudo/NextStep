"""Load the curated, verified listings in data/*.json into the database.

  data/scholarships.json   -> scholarships        (is_verified = true)
  data/internships.json    -> internships         (is_verified = true)
  data/mentors_demo.json   -> mentors_and_tutors  (is_demo = true, verified = false)

Usage:
  DATABASE_URL=... python3 scripts/import_listings.py                       # everything
  DATABASE_URL=... python3 scripts/import_listings.py --only internships,mentors

Rows are upserted by slug, so editing a JSON entry and re-running updates it.
A listing removed from the JSON is NOT deleted: it is marked is_active=false
(students may have saved it). Every outbound URL must be https and is checked
with server.safe_external_url before it is stored.

NOTE: scholarships need the "post" migration (nullable amount/deadline) first;
`python3 migrate.py --post` runs this script for you.
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from urlcheck import safe_external_url  # noqa: E402

KINDS = ("scholarships", "internships", "mentors")


def load(name):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


def _url(u, what):
    ok = safe_external_url(u)
    if not ok:
        raise ValueError(f"{what}: refusing unsafe or non-https URL {u!r}")
    return ok


def _date(v):
    return date.fromisoformat(v) if v else None


def import_scholarships(conn):
    items = load("scholarships.json")["scholarships"]
    slugs = []
    for s in items:
        slugs.append(s["slug"])
        conn.execute(
            """INSERT INTO scholarships (slug, title, provider, amount, deadline, min_gpa, major, description,
                   is_custom, apply_url, source_url, last_verified, status, amount_text, deadline_text,
                   eligibility, scope, is_verified, is_active)
               VALUES (%s,%s,%s,NULL,%s,NULL,NULL,%s,false,%s,%s,%s,%s,%s,%s,%s,%s,true,true)
               ON CONFLICT (slug) DO UPDATE SET title=EXCLUDED.title, provider=EXCLUDED.provider,
                 amount=NULL, deadline=EXCLUDED.deadline, min_gpa=NULL, major=NULL, description=EXCLUDED.description,
                 is_custom=false, apply_url=EXCLUDED.apply_url, source_url=EXCLUDED.source_url,
                 last_verified=EXCLUDED.last_verified, status=EXCLUDED.status, amount_text=EXCLUDED.amount_text,
                 deadline_text=EXCLUDED.deadline_text, eligibility=EXCLUDED.eligibility, scope=EXCLUDED.scope,
                 is_verified=true, is_active=true""",
            (s["slug"], s["title"], s["provider"], _date(s.get("deadline")), s["description"],
             _url(s["apply_url"], s["slug"]), _url(s.get("source_url") or s["apply_url"], s["slug"]),
             _date(s.get("last_verified")), s.get("status") or "unknown", s.get("amount_text"),
             s.get("deadline_text"), s.get("eligibility"), s.get("scope")),
        )
    # verified rows no longer in the file are retired, never deleted
    conn.execute("UPDATE scholarships SET is_active=false WHERE is_verified AND NOT (slug = ANY(%s))", (slugs,))
    return len(slugs)


def import_internships(conn):
    items = load("internships.json")["internships"]
    slugs = []
    for i in items:
        slugs.append(i["slug"])
        pay = i.get("compensation_text") or i.get("compensation") or "Not specified"
        conn.execute(
            """INSERT INTO internships (slug, title, org, target, pay, description, location, format, responsibilities,
                   eligibility, age_grade, deadline, deadline_text, compensation, compensation_text, skills,
                   apply_url, source_url, last_verified, status, is_verified, is_active)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,true)
               ON CONFLICT (slug) DO UPDATE SET title=EXCLUDED.title, org=EXCLUDED.org, target=EXCLUDED.target,
                 pay=EXCLUDED.pay, description=EXCLUDED.description, location=EXCLUDED.location,
                 format=EXCLUDED.format, responsibilities=EXCLUDED.responsibilities,
                 eligibility=EXCLUDED.eligibility, age_grade=EXCLUDED.age_grade, deadline=EXCLUDED.deadline,
                 deadline_text=EXCLUDED.deadline_text, compensation=EXCLUDED.compensation,
                 compensation_text=EXCLUDED.compensation_text, skills=EXCLUDED.skills,
                 apply_url=EXCLUDED.apply_url, source_url=EXCLUDED.source_url,
                 last_verified=EXCLUDED.last_verified, status=EXCLUDED.status, is_verified=true, is_active=true""",
            (i["slug"], i["title"], i["org"], i["audience"], pay[:200], i["description"], i.get("location"),
             i.get("format"), i.get("responsibilities"), i.get("eligibility"), i.get("age_grade"),
             _date(i.get("deadline")), i.get("deadline_text"), i.get("compensation") or "Not specified",
             i.get("compensation_text"), i.get("skills"), _url(i["apply_url"], i["slug"]),
             _url(i.get("source_url") or i["apply_url"], i["slug"]), _date(i.get("last_verified")),
             i.get("status") or "unknown"),
        )
    conn.execute("UPDATE internships SET is_active=false WHERE is_verified AND NOT (slug = ANY(%s))", (slugs,))
    return len(slugs)


def import_mentors(conn):
    people = load("mentors_demo.json")["people"]
    for p in people:
        conn.execute(
            """INSERT INTO mentors_and_tutors (slug, kind, user_id, display_name, university, major, test_scores, rate,
                   status, bio, avatar_url, specialties, is_demo, verified, pronouns, class_year,
                   academic_interests, career_interests, journey, help_areas, activities, experience_areas,
                   languages, availability, response_time)
               VALUES (%s,%s,(SELECT id FROM users WHERE lower(email)=lower(%s)),%s,%s,%s,%s,%s,'available',%s,NULL,%s,
                       true,false,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL)
               ON CONFLICT (slug) DO UPDATE SET kind=EXCLUDED.kind, user_id=EXCLUDED.user_id,
                 display_name=EXCLUDED.display_name, university=EXCLUDED.university, major=EXCLUDED.major,
                 test_scores=EXCLUDED.test_scores, rate=EXCLUDED.rate, status='available', bio=EXCLUDED.bio,
                 specialties=EXCLUDED.specialties, is_demo=true, verified=false,
                 pronouns=EXCLUDED.pronouns, class_year=EXCLUDED.class_year,
                 academic_interests=EXCLUDED.academic_interests, career_interests=EXCLUDED.career_interests,
                 journey=EXCLUDED.journey, help_areas=EXCLUDED.help_areas, activities=EXCLUDED.activities,
                 experience_areas=EXCLUDED.experience_areas, languages=EXCLUDED.languages,
                 availability=NULL, response_time=NULL""",
            (p["slug"], p["kind"], p.get("login_email") or "", p["display_name"], p["university"], p["major"],
             p.get("test_scores"), p.get("rate"), p["bio"], p.get("specialties") or [], p.get("pronouns"),
             p.get("class_year"), p.get("academic_interests") or [], p.get("career_interests") or [],
             p.get("journey"), p.get("help_areas") or [], p.get("activities") or [],
             p.get("experience_areas") or [], p.get("languages") or []),
        )
    return len(people)


def import_all(conn, only=KINDS):
    out = {}
    if "scholarships" in only:
        out["scholarships"] = import_scholarships(conn)
    if "internships" in only:
        out["internships"] = import_internships(conn)
    if "mentors" in only:
        out["mentors"] = import_mentors(conn)
    print("import_listings:", out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=",".join(KINDS), help="comma list of: " + ", ".join(KINDS))
    args = ap.parse_args(argv)
    only = [k.strip() for k in args.only.split(",") if k.strip()]
    bad = set(only) - set(KINDS)
    if bad:
        ap.error(f"unknown: {bad}")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        import_all(conn, only)
        conn.commit()


if __name__ == "__main__":
    main()

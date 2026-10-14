"""Test harness.

Every test session builds a throw-away Postgres SCHEMA (ns_test_<random>) on the
database in DATABASE_URL, points the app at it with search_path, and drops the
schema at the end. Real tables in `public` are never read or written by the
app under test (extensions such as uuid-ossp / pg_trgm are only *used* from public).

  DATABASE_URL=postgresql://... python3 -m pytest -q

The schema is built the same way production was: the original (legacy)
schema.sql from before this change, then migrate.py pre + post, then a small
fixture of real College Scorecard rows (tests/fixtures/scorecard_sample.csv).
"""
import os
import secrets
import sys
import uuid
from pathlib import Path

import bcrypt
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

BASE_URL = os.environ.get("DATABASE_URL")  # captured before the fixture swaps it
TEST_PASSWORD = "test-pass-" + secrets.token_hex(4)

CHECKLIST = [
    (1, "Create StudentAid.gov Account (FSA ID)", "FAFSA"),
    (2, "Gather tax documents (Prior-Prior Year W-2 & 1040)", "FAFSA"),
    (3, "Fill out FAFSA demographic info & add preferred schools", "FAFSA"),
    (4, "Sign and submit FAFSA with student & parent FSA IDs", "FAFSA"),
    (5, "Link to NYS TAP Application via FAFSA submission page", "TAP"),
    (6, "Complete HESC TAP profile and confirm NYS school code", "TAP"),
    (7, "Take 1 Official SAT or ACT Practice Test", "SAT/ACT"),
]


@pytest.fixture(scope="session")
def test_schema_name(test_db):
    return test_db.split("search_path=")[1].split(",")[0]


@pytest.fixture(scope="session")
def test_db():
    base = BASE_URL
    if not base:
        pytest.skip("DATABASE_URL not set")
    schema = "ns_test_" + secrets.token_hex(4)
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    url = make_conninfo(base, options=f"-c search_path={schema},public")
    os.environ["DATABASE_URL"] = url
    try:
        with psycopg.connect(url) as conn:
            conn.execute((ROOT / "tests" / "legacy_schema.sql").read_text())
            pw = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(4)).decode()
            for email, role in [("alex@test.invalid", "student"), ("sam@test.invalid", "student"),
                                ("mentor@test.invalid", "mentor")]:
                conn.execute("INSERT INTO users (full_name, email, password_hash, role, unweighted_gpa, annual_budget,"
                             " points_balance) VALUES (%s,%s,%s,%s,3.4,18000,400)",
                             (email.split("@")[0].title(), email, pw, role))
            for i, t, tag in CHECKLIST:
                conn.execute("INSERT INTO checklist_items (id, title, tag, point_reward) VALUES (%s,%s,%s,100)",
                             (i, t, tag))
            conn.execute("""INSERT INTO rewards_shop (slug, title, cost_points, category, icon_name, description)
                            VALUES ('rew-1', '$5 Test Card', 300, 'Gift Card', 'gift', 'test'),
                                   ('rew-big', 'Big Item', 1200, 'Merch', 'gift', 'test')""")
            # legacy fictional rows, as in production before the migration
            conn.execute("""INSERT INTO scholarships (slug, title, provider, amount, deadline, description)
                            VALUES ('sch-1', 'Future STEM Leaders Award', 'Fictional', 10000, '2026-11-15', 'x')""")
            import migrate
            migrate.run_pre(conn)
            migrate.run_post(conn)
            # the "post" phase must be idempotent too
            migrate.run_pre(conn)
            migrate.run_post(conn)
            import import_scorecard
            with open(ROOT / "tests" / "fixtures" / "scorecard_sample.csv", encoding="utf-8-sig", newline="") as fh:
                import_scorecard.upsert(conn, import_scorecard.read_rows(fh))
            n = conn.execute("SELECT count(*) FROM college_scorecard").fetchone()[0]
            import_scorecard.record_source(conn, "scorecard_sample.csv", n)
            conn.commit()
        yield url
    finally:
        os.environ["DATABASE_URL"] = base
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture(scope="session")
def app(test_db):
    import server
    server.app.config.update(TESTING=True)
    server._trgm.update(checked=False)
    server._college_cache.update(rows=None)
    return server.app


@pytest.fixture(autouse=True)
def _reset_limits(request):
    if "app" in request.fixturenames or "client" in request.fixturenames:
        import server
        server.limiter.reset()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def conn(test_db):
    with psycopg.connect(test_db) as c:
        yield c


def signup(client, **kw):
    email = kw.pop("email", f"s-{uuid.uuid4().hex[:10]}@test.invalid")
    body = {"name": "Test Student", "email": email, "password": TEST_PASSWORD, "gpa": 3.2, "budget": 15000, **kw}
    r = client.post("/api/signup", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


@pytest.fixture
def student(app):
    """A fresh signed-in student (own client) for each test."""
    c = app.test_client()
    user = signup(c)
    return c, user


def login(client, email):
    r = client.post("/api/login", json={"email": email, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.get_json()
    return r.get_json()

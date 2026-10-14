"""schema.sql (fresh install) and legacy schema + migrate.py (upgrade) must agree."""
import secrets
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

import conftest

ROOT = Path(__file__).resolve().parent.parent


def columns(conn, schema):
    return {(r[0], r[1]) for r in conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema=%s", (schema,))}


def test_fresh_schema_matches_migrated_schema(test_db, test_schema_name):
    base = conftest.BASE_URL
    fresh = "ns_test_fresh_" + secrets.token_hex(3)
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{fresh}"')
    try:
        url = make_conninfo(base, options=f"-c search_path={fresh},public")
        with psycopg.connect(url) as conn:
            conn.execute((ROOT / "schema.sql").read_text())
            import migrate
            migrate.run_pre(conn)          # must be a no-op on the current schema
            conn.commit()
            with psycopg.connect(base) as c2:
                assert columns(c2, fresh) == columns(c2, test_schema_name)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{fresh}" CASCADE')


def test_existing_rows_preserved_by_migration(conn):
    # the legacy fictional scholarship is retired, not deleted (saved lists keep working)
    row = conn.execute("SELECT is_active, is_verified FROM scholarships WHERE slug='sch-1'").fetchone()
    assert row == (False, False)


def test_seed_is_idempotent(test_db, conn):
    import seed
    seed.main()
    seed.main()
    assert conn.execute("SELECT count(*) FROM checklist_items").fetchone()[0] == 7
    assert conn.execute("SELECT count(*) FROM scholarships WHERE is_verified AND is_active").fetchone()[0] == 18

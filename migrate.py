"""Additive, idempotent schema migrations for NextStep.

Two phases, so the live site keeps working while old code is still deployed:

  python3 migrate.py            # phase "pre"  - safe to run while the OLD server.py is live
  python3 migrate.py --post     # phase "post" - run once AFTER the new server.py is deployed

"pre" only adds things (columns with defaults, new tables, indexes, the pg_trgm
extension). Nothing the old code reads is renamed, dropped or made NULL.

"post" does the steps that the old code could not cope with:
  * scholarships.amount / deadline become nullable (real scholarships often
    publish no fixed amount or no deadline; old code called .isoformat() on it),
  * loads the verified scholarships/internships from data/*.json,
  * retires the original fictional example rows (is_active=false; not deleted),
  * snapshots each user's old mutable points_balance into legacy_points_balance
    and replaces points_balance with the derived value (see server.points_summary).

Every statement is idempotent; running either phase again is a no-op.
Env: DATABASE_URL.
"""
import argparse
import os
import sys

import psycopg

PRE = r"""
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------- users
ALTER TABLE users ADD COLUMN IF NOT EXISTS legacy_points_balance integer;   -- snapshot of the old mutable counter
ALTER TABLE users ADD COLUMN IF NOT EXISTS college_prefs jsonb;             -- last College Matcher inputs

-- ---------------------------------------------------------------- scholarships
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS apply_url      text;      -- official application / info page (https)
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS source_url     text;      -- page the facts were verified on
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS last_verified  date;
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS status         varchar;   -- open | upcoming | closed | unknown
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS amount_text    varchar;   -- amount exactly as published
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS deadline_text  varchar;   -- deadline as published
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS eligibility    text;
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS scope          varchar;   -- National | New York State | NYC
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS is_verified    boolean NOT NULL DEFAULT false;
ALTER TABLE scholarships ADD COLUMN IF NOT EXISTS is_active      boolean NOT NULL DEFAULT true;

-- ---------------------------------------------------------------- mentors
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS is_demo            boolean NOT NULL DEFAULT false;
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS verified           boolean NOT NULL DEFAULT false;
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS verified_note      text;     -- what staff verified (e.g. enrollment)
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS pronouns           varchar;
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS class_year         varchar;
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS academic_interests text[] NOT NULL DEFAULT '{}';
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS career_interests   text[] NOT NULL DEFAULT '{}';
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS journey            text;
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS help_areas         text[] NOT NULL DEFAULT '{}';
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS activities         text[] NOT NULL DEFAULT '{}';
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS experience_areas   text[] NOT NULL DEFAULT '{}';
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS languages          text[] NOT NULL DEFAULT '{}';
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS availability       text;     -- NULL = "Availability not provided"
ALTER TABLE mentors_and_tutors ADD COLUMN IF NOT EXISTS response_time      text;     -- NULL = not provided

ALTER TABLE mentor_requests ADD COLUMN IF NOT EXISTS topics     text[] NOT NULL DEFAULT '{}';
ALTER TABLE mentor_requests ADD COLUMN IF NOT EXISTS updated_at timestamptz;
-- allow students to withdraw a request (superset of the old values)
ALTER TABLE mentor_requests DROP CONSTRAINT IF EXISTS mentor_requests_status_check;
ALTER TABLE mentor_requests ADD CONSTRAINT mentor_requests_status_check
  CHECK (status IN ('pending', 'accepted', 'completed', 'declined', 'cancelled'));
-- at most one open (pending) request per student, mentor and request type
CREATE UNIQUE INDEX IF NOT EXISTS mentor_requests_one_pending
  ON mentor_requests (student_id, mentor_id, request_type) WHERE status = 'pending';

-- ---------------------------------------------------------------- internships
ALTER TABLE internships ADD COLUMN IF NOT EXISTS location          varchar;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS format            varchar;  -- In-person | Remote | Hybrid | NULL
ALTER TABLE internships ADD COLUMN IF NOT EXISTS responsibilities  text;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS eligibility       text;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS age_grade         varchar;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS deadline          date;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS deadline_text     varchar;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS compensation      varchar;  -- Paid | Stipend | Unpaid | Not specified
ALTER TABLE internships ADD COLUMN IF NOT EXISTS compensation_text varchar;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS skills            text;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS apply_url         text;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS source_url        text;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS last_verified     date;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS status            varchar;  -- open | upcoming | closed | unknown
ALTER TABLE internships ADD COLUMN IF NOT EXISTS is_verified       boolean NOT NULL DEFAULT false;
ALTER TABLE internships ADD COLUMN IF NOT EXISTS is_active         boolean NOT NULL DEFAULT true;

-- ---------------------------------------------------------------- colleges (College Scorecard)
-- The old `colleges` table (3 fictional example schools) is left in place for
-- the old code; the new code reads only college_scorecard.
CREATE TABLE IF NOT EXISTS college_scorecard (
  unitid             integer PRIMARY KEY,          -- IPEDS UNITID
  opeid              varchar,
  name               varchar NOT NULL,
  alias              varchar,
  city               varchar,
  state              varchar(2),
  zip                varchar,
  website            text,                         -- INSTURL, normalised to https://
  net_price_calc_url text,                         -- NPCURL
  control            smallint,                     -- 1 public, 2 private nonprofit, 3 private for-profit
  pred_degree        smallint,                     -- PREDDEG
  high_degree        smallint,                     -- HIGHDEG
  locale             smallint,
  latitude           numeric,
  longitude          numeric,
  undergrad_enrollment integer,                    -- UGDS
  admission_rate     numeric,                      -- ADM_RATE (0..1)
  open_admission     boolean,                      -- OPENADMP = 1
  test_policy        smallint,                     -- ADMCON7
  sat_avg            integer,
  sat_rw_25 integer, sat_rw_75 integer, sat_math_25 integer, sat_math_75 integer,
  act_25 integer, act_75 integer,
  tuition_in_state   integer,                      -- TUITIONFEE_IN
  tuition_out_state  integer,                      -- TUITIONFEE_OUT
  cost_of_attendance integer,                      -- COSTT4_A (or COSTT4_P)
  avg_net_price      integer,                      -- NPT4_PUB / NPT4_PRIV
  net_price_by_income jsonb,                       -- {"0-30000": n, ...} NPT41..45
  completion_rate    numeric,                      -- C150_4 (or C150_L4)
  retention_rate     numeric,                      -- RET_FT4 (or RET_FTL4)
  pell_share         numeric,                      -- PCTPELL
  median_debt        integer,                      -- GRAD_DEBT_MDN
  median_earnings_10yr integer,                    -- MD_EARN_WNE_P10
  bachelor_programs  text[] NOT NULL DEFAULT '{}', -- CIP 2-digit families with a bachelor's offered
  associate_programs text[] NOT NULL DEFAULT '{}',
  hbcu boolean, hsi boolean, women_only boolean, men_only boolean, distance_only boolean,
  search_text        text NOT NULL DEFAULT '',      -- normalised name + alias + city for search
  imported_at        timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS college_scorecard_state ON college_scorecard (state);

CREATE TABLE IF NOT EXISTS college_data_source (
  id           text PRIMARY KEY,                 -- 'scorecard'
  title        text NOT NULL,
  source_url   text NOT NULL,
  file_name    text,
  released     text,
  data_years   jsonb,                            -- metric -> cohort/year description
  row_count    integer,
  imported_at  timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_saved_colleges (
  user_id   uuid    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  unitid    integer NOT NULL REFERENCES college_scorecard(unitid) ON DELETE CASCADE,
  saved_at  timestamptz DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, unitid)
);
"""

# pg_trgm powers misspelling-tolerant college search. Optional: the server falls
# back to ILIKE matching when the extension is unavailable.
TRGM = r"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS college_scorecard_search_trgm ON college_scorecard USING gin (search_text gin_trgm_ops);
"""

POST = r"""
ALTER TABLE scholarships ALTER COLUMN amount   DROP NOT NULL;
ALTER TABLE scholarships ALTER COLUMN deadline DROP NOT NULL;

-- The original example scholarships/internships were made up by the page; keep
-- the rows (nobody's saved data is lost) but stop listing them.
UPDATE scholarships SET is_active = false WHERE slug IN ('sch-1', 'sch-2', 'sch-3') AND is_active;
UPDATE internships  SET is_active = false WHERE slug IN ('int-1', 'int-2', 'int-3') AND is_active;

-- Points: keep a record of the old mutable counter, then store the derived value.
UPDATE users SET legacy_points_balance = COALESCE(points_balance, 0) WHERE legacy_points_balance IS NULL;
"""


def run_pre(conn):
    conn.execute(PRE)
    try:
        with conn.transaction():
            conn.execute(TRGM)
        print("pg_trgm: available")
    except psycopg.Error as e:  # extension not permitted -> ILIKE fallback
        print(f"pg_trgm: NOT available ({e.__class__.__name__}); search falls back to ILIKE")


def run_post(conn):
    conn.execute(POST)
    # derived points cache (server.points_summary is the source of truth)
    conn.execute(
        """UPDATE users u SET points_balance = GREATEST(0,
               100 * (SELECT count(*) FROM user_checklist_progress p
                      WHERE p.user_id = u.id AND p.is_completed)
             - COALESCE((SELECT sum(r.cost) FROM (
                    SELECT rs.cost_points AS cost FROM redemptions rd
                    JOIN rewards_shop rs ON rs.id = rd.reward_id WHERE rd.user_id = u.id) r), 0))"""
    )
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "scripts"))
    import import_listings  # noqa: E402  (scripts/import_listings.py)

    import_listings.import_all(conn)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--post", action="store_true", help="run the post-deploy phase (after new server.py is live)")
    args = ap.parse_args(argv)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        run_pre(conn)
        if args.post:
            run_post(conn)
        conn.commit()
    print("migrate: done (" + ("pre+post" if args.post else "pre") + ")")


if __name__ == "__main__":
    main()

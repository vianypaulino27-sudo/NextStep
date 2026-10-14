# NextStep: architecture, data and operations

NextStep is one page (`Index.html.html`, served at `/`) plus `static/app.js`,
backed by a small Flask API (`server.py`) and a Postgres database. There is
no build step: Tailwind and Lucide load from pinned CDN versions.

```
Index.html.html   page markup (branding, tabs, modals)
static/app.js     all front-end logic; every DB/user string is escaped (esc()) before innerHTML
server.py         Flask API: auth, checklist/points, scholarships, rewards, mentors, colleges, internships, assistant
college_match.py  College Scorecard search helpers + explainable matcher (pure functions)
assistant.py      grounded "NextStep Assistant" (knowledge base + DB lookups); dormant optional AI hook
urlcheck.py       outbound-link safety (https only + allowlist)
migrate.py        additive, idempotent schema migrations (pre / post deploy)
seed.py           idempotent demo data (checklist steps, gift shop, demo listings, demo student state)
scripts/import_scorecard.py   load College Scorecard data
scripts/import_listings.py    load data/scholarships.json, data/internships.json, data/mentors_demo.json
scripts/check_links.py        re-verify every curated link and both videos (read-only)
data/*.json       curated, verified listings (edit these to update the site)
knowledge/*.json  assistant knowledge base + official links/videos (resources.json)
tests/            pytest suite (runs in a throw-away schema; never touches real rows)
```

## Demo login

Prefilled on the sign-in form: `alex.rivera@example.com` / `demo1234`. Other seed
users (same password): students `sam.wu@example.com`, `marcus.v@example.com`; tutors
`david.kim@cornell.edu`, `maya.p@columbia.edu`; mentor `sarah.chen@columbia.edu`.
New student accounts need a password of at least 8 characters.

## Environment variables (names only; see `.env.example`)

| Name | Where | Required | Purpose |
|---|---|---|---|
| `DATABASE_URL` | Render > csh-nextstep > Environment | yes | Postgres connection string |
| `SECRET_KEY` | Render env | yes in production | Signs the session cookie (random per process if unset, which logs everyone out on restart) |
| `RENDER` | set by Render automatically | - | Makes the session cookie `Secure` |
| `ANTHROPIC_API_KEY` | (not set) | no | **Dormant.** If ever set, the assistant may call Anthropic for questions its knowledge base can't answer. Decision 2026-10: built-in assistant only. |
| `ASSISTANT_MODEL` | (not set) | no | Model id for that dormant path (default `claude-haiku-4-5-20251001`) |

No API key is needed for colleges: College Scorecard data is imported from the official bulk file.

## Points (checklist, Gift Shop)

Points are **derived**, never stored as the source of truth (`server.points_summary`):

- **checklist score** = 100 x completed checklist steps (shown in the header)
- **spent** = sum of the costs of the user's redemptions
- **available** = score - spent (shown in the Gift Shop, floored at 0)

Checking a step that's already done, or unchecking one that's already undone, changes
nothing, so points can't be farmed. Saving scholarships, adding custom scholarships and
requesting tutors no longer award points (all three could be repeated for free points).
Redemptions lock the user row and re-check the derived balance inside the transaction.
`users.points_balance` is kept only as a cache of `available` for the old code.

**Existing users.** The post-deploy migration copies each user's old counter into
`users.legacy_points_balance` (an audit record) and then sets `points_balance` to the derived value.
Balances on 2026-10-14, before the post phase: Alex Rivera 1450 (3 steps done, no
redemptions), so he gets 300 after migration. Sam Wu 750 and Marcus V 150 (0 steps), so 0 each. Most of the old
numbers came from fake starting data and repeated toggling or saving. There were 0 redemptions, so nobody
loses anything they had redeemed.

## Tables

| Table | What it holds |
|---|---|
| `users` | Accounts (bcrypt hash, role, GPA, budget, `points_balance` cache, `legacy_points_balance`, `college_prefs`) |
| `checklist_items` | The 7 FAFSA / TAP / SAT-ACT steps |
| `user_checklist_progress` | Completion per user+step (`UNIQUE(user_id, checklist_item_id)`) |
| `scholarships` | Verified listings (`is_verified`, `apply_url`, `source_url`, `last_verified`, `status`, `amount_text`, `deadline_text`, `eligibility`, `scope`) and students' private custom entries (`is_custom`). Retired rows have `is_active=false` |
| `user_saved_scholarships` | Saved list (PK prevents duplicates) |
| `mentors_and_tutors` | Mentor/tutor profiles: `is_demo`, `verified`, `verified_note`, pronouns, class year, interests, journey, help areas, activities, experience, languages, `availability`/`response_time` (NULL = not provided). Mentor applications are `status='pending_verification'` and hidden |
| `mentor_requests` | Session requests: topics, notes, status (`pending/accepted/declined/completed/cancelled`). Partial unique index: one pending request per student+mentor+type |
| `rewards_shop`, `redemptions` | Gift Shop and redemption requests (fulfilled by staff) |
| `internships` | Verified programs: location, format, eligibility, age/grade, deadline, compensation, apply/source URL, `last_verified`, `status` |
| `college_scorecard` | One row per institution from College Scorecard (see below) |
| `college_data_source` | Source URL, release, row count and per-metric data years for the import |
| `user_saved_colleges` | Saved colleges per user |
| `colleges` | LEGACY: the 3 fictional example colleges. Not read by the new code; kept for the old deployed code; safe to drop later |

`schema.sql` is the full schema for an empty database. An existing database is
upgraded with `migrate.py`. Both are tested to produce identical columns.

## Data sources, licensing and update path

**Colleges: U.S. Department of Education College Scorecard.**
"Most Recent Institution-Level Data" (`Most-Recent-Cohorts-Institution_06102026.zip`,
released June 10, 2026) from https://collegescorecard.ed.gov/data/. It's a U.S. government work
(public domain), and the site attributes it on every college view. We import currently operating
institutions that award at least an associate degree (`CURROPER=1`, `HIGHDEG>=2`):
**3,879 institutions** (1,611 public, 1,647 private nonprofit, 621 for-profit; 2,440 offer
bachelor's degrees). Coverage: **only U.S. Title IV institutions**, with no international
schools. Data years (from the official data dictionary's cohort map): admissions and test
scores fall 2024; tuition 2024-25; cost of attendance and net price 2023-24; graduation rate
for the fall 2018 cohort; retention fall 2023; programs offered AY2024-25; median debt FY2020-21
completers; earnings measured 2020-21. Scorecard publishes **no GPA data**, so the matcher
uses GPA only as a coarse signal and says so. Each college page links to its College
Scorecard and College Navigator (NCES) pages.

Refresh when a new file is published (about once a year). Update `ZIP_URL`/`RELEASED`/`DATA_YEARS` in the script, then:

```sh
python3 scripts/import_scorecard.py --download        # or: ... path/to/Most-Recent-Cohorts-Institution.csv
```

**Scholarships and internships** are curated in `data/scholarships.json` (18) and
`data/internships.json` (18). Each URL was opened and read on 2026-10-14, and every
amount, deadline and eligibility field was copied from that page (short supporting
quotes are kept in `evidence`). `null` means the page didn't say. Sites that block
scripts (403/429) were checked in a browser and are flagged in the JSON. To update:
edit the JSON (and `last_verified`), then

```sh
python3 scripts/check_links.py                         # re-check every link + both videos
python3 scripts/import_listings.py --only scholarships,internships
```

Listings removed from the JSON are retired (`is_active=false`), not deleted. A
deadline in the past always shows as "closed".

**Outbound links** are https only and must be on an allowlist built from the
curated JSON files (`urlcheck.py`). The server never redirects (no `/go?url=`), so
there's no open redirect. Apply links open in a new tab with `rel="noopener noreferrer"`.

**Mentors.** All current mentor and tutor profiles are fictional demos
(`data/mentors_demo.json`, `is_demo=true`). They're labeled "Demo profile — fictional
example, not a verified mentor." and have no photos or availability. Real mentors apply
through "Apply as Mentor/Tutor". Their profile stays hidden (`pending_verification`)
until staff set `status='available'`, `verified=true` and `verified_note` (what was checked).
Unverified real profiles are shown as "self-reported".

**FAFSA/TAP videos and links** live in `knowledge/resources.json` with the titles verified from
YouTube. The FAFSA video's owner disabled embedding, so the page shows its thumbnail
linking to YouTube. The TAP video (HESC's NYSStudentAid channel) is embedded via
youtube-nocookie.com.

**Assistant knowledge** is in `knowledge/assistant_kb.json`. Each entry is marked
`nextstep` (how the site works), `official` (facts read on the linked official page) or
`general` (general guidance). The assistant also reads the signed-in student's own
counts, verified scholarship deadlines and College Scorecard facts from the database.
It never shows other users' data, and message text is never logged.

## API

| Method & path | Login | Purpose |
|---|---|---|
| `GET /` | | Page |
| `GET /api/resources` | | Videos + official links |
| `POST /api/login`, `/api/signup`, `/api/logout`; `GET/PUT /api/me` | (PUT: yes) | Auth/profile (login 10/5min per IP, signup 5/h per IP) |
| `GET /api/points` | yes | Derived points summary |
| `GET /api/checklist`; `PUT /api/checklist/<id>` `{done: bool}`; `POST /api/checklist/reset` | (writes: yes) | Checklist (idempotent) |
| `GET /api/scholarships` | | Active verified listings + own custom ones + own saved list |
| `POST /api/scholarships`; `DELETE /api/scholarships/<id>` | yes | Own custom scholarships |
| `POST`/`DELETE /api/scholarships/<id>/save` | yes | Save / unsave (no points) |
| `GET /api/rewards`; `POST /api/redemptions` | (POST: yes) | Gift Shop |
| `GET /api/tutors`, `/api/mentors`, `/api/mentors/<id>` | | Profiles (no contact info) |
| `GET/POST /api/mentor-requests`; `POST /api/mentor-requests/<id>/cancel` | yes | Own session requests |
| `POST /api/mentor-applications` | | Apply as mentor (pending; 5/h per IP) |
| `GET /api/colleges/meta`, `/api/colleges/search?q=&page=&per_page=&state=`, `/api/colleges/<unitid>` | | Search + detail |
| `POST /api/colleges/match` | | Matcher (validated; remembers prefs when signed in) |
| `GET /api/colleges/saved`; `POST/DELETE /api/colleges/<unitid>/save` | yes | Saved colleges |
| `GET /api/internships` | | Verified internships |
| `POST /api/assistant` `{message}` | yes | Assistant (20/min per user, max 800 chars) |

Security: session cookie `nextstep_session` is HttpOnly + SameSite=Lax (+ Secure on
Render). State-changing `/api` requests with a foreign `Origin` are rejected. All SQL is
parameterised. Bodies are capped at 64 KB. Rate limits are in-memory per process,
which is fine for one gunicorn worker. Use a shared store if you scale out.

## Migrations and deploy

```sh
# 1. While the OLD code is still live (already run on 2026-10-14):
python3 migrate.py                                      # additive columns/tables + pg_trgm
python3 scripts/import_scorecard.py --download          # colleges
python3 scripts/import_listings.py --only internships,mentors
# 2. Deploy the new code (Render: pip install -r requirements.txt; gunicorn server:app)
# 3. Right after the deploy:
python3 migrate.py --post     # nullable amount/deadline, verified scholarships, retire fictional rows, derive points
python3 seed.py               # optional: refresh Gift Shop text, give the demo student a starting state
```

## Tests

```sh
pip install -r requirements-dev.txt
DATABASE_URL=postgresql://... python3 -m pytest -q
```

The suite creates a throw-away schema (`ns_test_<random>`) on that database, builds it
from the legacy schema + `migrate.py` (pre and post), loads 31 real Scorecard rows
from `tests/fixtures/scorecard_sample.csv`, and drops the schema at the end. Rows in
`public` are never touched. One test makes a live call to the Anthropic API with a
deliberately invalid key to check the failure path.

"""Checklist + points: score is always 100 x completed steps (spec section 4)."""
from conftest import login, signup


def score(c):
    return c.get("/api/me").get_json()["user"]["points"]


def put(c, item, done):
    return c.put(f"/api/checklist/{item}", json={"done": done})


def test_spec_table(student):
    c, user = student
    assert user["points"] == 0 and score(c) == 0                     # initial state
    assert put(c, 1, True).get_json()["points"] == 100                  # complete one
    assert put(c, 2, True).get_json()["points"] == 200                  # complete a second
    assert put(c, 1, False).get_json()["points"] == 100                 # uncheck one
    assert put(c, 2, False).get_json()["points"] == 0                   # uncheck the last
    assert score(c) == 0


def test_repeated_toggles_never_duplicate(student):
    c, _ = student
    for _ in range(3):
        assert put(c, 3, True).get_json()["points"] == 100   # re-checking a done step adds nothing
    for _ in range(3):
        assert put(c, 3, False).get_json()["points"] == 0    # re-unchecking never goes negative
    for _ in range(5):
        put(c, 3, True)
        put(c, 3, False)
    assert score(c) == 0
    put(c, 3, True)
    assert score(c) == 100


def test_persists_across_sessions_and_categories(app):
    c = app.test_client()
    email = "persist-" + __import__("uuid").uuid4().hex[:8] + "@test.invalid"
    signup(c, email=email)
    for item in (1, 5, 7):   # FAFSA, TAP, SAT/ACT
        put(c, item, True)
    c.post("/api/logout")
    c2 = app.test_client()
    login(c2, email)
    rows = {r["id"]: r for r in c2.get("/api/checklist").get_json()}
    assert [i for i, r in rows.items() if r["done"]] == [1, 5, 7]
    assert {rows[i]["tag"] for i in (1, 5, 7)} == {"FAFSA", "TAP", "SAT/ACT"}
    assert all(r["points"] == 100 for r in rows.values())
    me = c2.get("/api/me").get_json()["user"]
    assert me["points"] == 300 and me["pointsDetail"]["completed"] == 3


def test_client_supplied_points_ignored(student):
    c, _ = student
    r = c.put("/api/checklist/1", json={"done": True, "points": 99999})
    assert r.get_json()["points"] == 100


def test_validation_and_auth(app, student):
    c, _ = student
    assert c.put("/api/checklist/1", json={"done": "yes"}).status_code == 400
    assert c.put("/api/checklist/9999", json={"done": True}).status_code == 404
    anon = app.test_client()
    assert anon.put("/api/checklist/1", json={"done": True}).status_code == 401


def test_reset(student):
    c, _ = student
    put(c, 1, True); put(c, 2, True)
    r = c.post("/api/checklist/reset").get_json()
    assert r["points"] == 0 and score(c) == 0


def test_no_duplicate_progress_rows(student, conn):
    c, user = student
    for _ in range(4):
        put(c, 4, True); put(c, 4, False)
    n = conn.execute("SELECT count(*) FROM user_checklist_progress WHERE user_id=%s AND checklist_item_id=4",
                     (user["id"],)).fetchone()[0]
    assert n == 1


def test_redemption_uses_derived_balance(student):
    c, _ = student
    assert c.post("/api/redemptions", json={"reward_id": "rew-1", "address": "x@test.invalid"}).status_code == 409
    for i in (1, 2, 3):
        put(c, i, True)
    r = c.post("/api/redemptions", json={"reward_id": "rew-1", "address": "x@test.invalid"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["points"] == 300 and body["balance"] == 0          # score stays, balance spent
    assert c.post("/api/redemptions", json={"reward_id": "rew-1", "address": "x"}).status_code == 409  # no double spend
    put(c, 3, False)                                                 # unchecking still removes points from score
    me = c.get("/api/me").get_json()["user"]
    assert me["points"] == 200 and me["balance"] == 0 and me["pointsDetail"]["overspent"] == 100


def test_tutor_request_and_saving_award_no_points(student):
    c, _ = student
    c.post("/api/mentor-requests", json={"mentor_id": "tut-1", "type": "act_sat_tutoring"})
    sch = c.get("/api/scholarships").get_json()["scholarships"][0]["id"]
    c.post(f"/api/scholarships/{sch}/save")
    assert score(c) == 0


def test_legacy_points_migrated(conn):
    # conftest seeded users with the old mutable counter = 400 and no completed steps
    row = conn.execute("SELECT legacy_points_balance, points_balance FROM users WHERE email='alex@test.invalid'").fetchone()
    assert row == (400, 0)

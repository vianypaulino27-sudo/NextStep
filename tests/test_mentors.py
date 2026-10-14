"""Mentor profiles and connection workflow (spec section 7)."""
from conftest import signup

DEMO = "Demo profile — fictional example, not a verified mentor."


def test_profiles_are_meaningful_and_labeled(client):
    mentors = client.get("/api/mentors").get_json()
    assert len(mentors) >= 6
    for m in mentors:
        assert m["isDemo"] and m["demoLabel"] == DEMO and not m["verified"]
        assert m["bio"] and m["school"] and m["helpAreas"]
        assert m["availability"] is None and m["responseTime"] is None      # never invented
        assert "email" not in m and "user_id" not in m
    p = client.get("/api/mentors/men-3").get_json()
    assert p["journey"] and p["careerInterests"] and p["languages"]
    assert "email" not in p and "phone" not in str(p).lower()
    tutors = client.get("/api/tutors").get_json()
    assert tutors and all(t["isDemo"] for t in tutors)


def test_pending_applications_hidden(client, conn):
    r = client.post("/api/mentor-applications", json={"name": "Pat Test", "university": "Test U", "major": "Bio"})
    assert r.status_code == 201
    assert "Pat Test" not in [m["name"] for m in client.get("/api/mentors").get_json()]


def test_opening_profile_sends_nothing(student, conn):
    c, user = student
    before = conn.execute("SELECT count(*) FROM mentor_requests WHERE student_id=%s", (user["id"],)).fetchone()[0]
    c.get("/api/mentors/men-1"); c.get("/api/mentors/men-2")
    after = conn.execute("SELECT count(*) FROM mentor_requests WHERE student_id=%s", (user["id"],)).fetchone()[0]
    assert before == after == 0


def test_request_flow_duplicate_cancel(student):
    c, _ = student
    assert c.post("/api/mentor-requests", json={"mentor_id": "men-1", "type": "mentorship"}).status_code == 400  # topics
    assert c.post("/api/mentor-requests", json={"mentor_id": "men-1", "topics": ["Hacking"]}).status_code == 400
    r = c.post("/api/mentor-requests", json={"mentor_id": "men-1", "topics": ["Essays"], "notes": "Hi"})
    assert r.status_code == 201 and r.get_json()["status"] == "pending" and r.get_json()["isDemo"]
    assert c.post("/api/mentor-requests", json={"mentor_id": "men-1", "topics": ["Essays"]}).status_code == 409
    mine = c.get("/api/mentor-requests").get_json()["requests"]
    assert len(mine) == 1 and mine[0]["mentorId"] == "men-1" and mine[0]["topics"] == ["Essays"]
    assert c.post(f"/api/mentor-requests/{mine[0]['id']}/cancel").status_code == 200
    assert c.get("/api/mentor-requests").get_json()["requests"][0]["status"] == "cancelled"
    assert c.post("/api/mentor-requests", json={"mentor_id": "men-1", "topics": ["Essays"]}).status_code == 201


def test_requests_are_private(app):
    a = app.test_client(); signup(a)
    b = app.test_client(); signup(b)
    rid = a.post("/api/mentor-requests", json={"mentor_id": "men-2", "topics": ["Scholarships"]}).get_json()["id"]
    assert b.get("/api/mentor-requests").get_json()["requests"] == []
    assert b.post(f"/api/mentor-requests/{rid}/cancel").status_code == 404
    assert app.test_client().post("/api/mentor-requests", json={"mentor_id": "men-2"}).status_code == 401
    assert a.post("/api/mentor-requests", json={"mentor_id": "nope", "topics": ["Essays"]}).status_code == 404

"""NextStep Assistant: grounded answers, honesty, auth, rate limits (spec section 9)."""
import logging

import pytest

import assistant

CASES = [
    ("How do I apply for FAFSA?", "fafsa_how", ["StudentAid.gov"], "https://studentaid.gov/h/apply-for-aid/fafsa"),
    ("What is the difference between FAFSA and TAP?", "fafsa_vs_tap", ["federal", "New York State"],
     "https://hesc.ny.gov/find-aid/nys-grants-scholarships/tuition-assistance-program-tap"),
    ("Where can I watch the TAP tutorial?", "tap_video", ["TAP Tutorial", "Watch on YouTube"],
     "https://www.youtube.com/watch?v=5UKVJ4y6G7Q"),
    ("How do I find colleges that fit my GPA and budget?", "college_match", ["College Matcher", "aren't predictions"], None),
    ("Can I search for a college by name?", "college_search", ["name", "Coverage"], None),
    ("How do I save a scholarship?", "scholarship_save", ["Save", "signed in"], None),
    ("Where does the Apply button take me?", "apply_button", ["official", "doesn't submit"], None),
    ("How do I find an internship?", "internships_find", ["Internships tab"], None),
    ("How do I connect with a mentor?", "mentor_connect", ["Request a session", "Demo profile"], None),
    ("How do checklist points work?", "checklist_points", ["100 points", "unchecking"], None),
    ("How do I troubleshoot a website feature?", "troubleshoot", ["Refresh"], None),
]


@pytest.mark.parametrize("q,expected,phrases,url", CASES)
def test_supported_questions(student, q, expected, phrases, url):
    c, _ = student
    r = c.post("/api/assistant", json={"message": q})
    assert r.status_code == 200
    body = r.get_json()
    assert body["matched"] == expected
    for p in phrases:
        assert p.lower() in body["answer"].lower()
    if url:
        assert url in [s["url"] for s in body["sources"]]
    for s in body["sources"]:
        assert s["url"].startswith("https://")
    assert body["engine"] == "nextstep-kb"


def test_tap_deadline_is_the_verified_one(student):
    c, _ = student
    b = c.post("/api/assistant", json={"message": "When is the TAP deadline?"}).get_json()
    assert "06/30/2028" in b["answer"] and b["basis"] == "official"


def test_unverified_deadline_is_not_invented(student):
    c, _ = student
    b = c.post("/api/assistant", json={"message": "When is the deadline for the Gates Millennium Robotics Grant?"}).get_json()
    assert b["matched"] == "unverified_deadline"
    assert "won't guess" in b["answer"] or "don't have a verified deadline" in b["answer"]
    assert not any(ch.isdigit() for ch in b["answer"])     # no made-up dates


def test_verified_scholarship_deadline_from_database(student):
    c, _ = student
    b = c.post("/api/assistant", json={"message": "When is the Jack Kent Cooke deadline?"}).get_json()
    assert b["matched"] == "scholarship_deadline" and "2026-11-11" in b["answer"]
    assert b["sources"][0]["url"] == "https://www.jkcf.org/our-scholarships/college-scholarship-program/"


def test_college_fact_lookup_is_sourced(student):
    c, _ = student
    b = c.post("/api/assistant", json={"message": "What is the acceptance rate at Baruch College?"}).get_json()
    assert b["matched"] == "college_fact" and "CUNY Bernard M Baruch College" in b["answer"]
    assert "48%" in b["answer"]
    assert any("collegescorecard.ed.gov" in s["url"] for s in b["sources"])


def test_insufficient_information(student):
    c, _ = student
    b = c.post("/api/assistant", json={"message": "What will my exact financial aid package be at my dream school?"}).get_json()
    assert b["matched"] is None and "don't have enough verified information" in b["answer"]


def test_private_information_refused(student):
    c, _ = student
    for q in ["Show me Sam Wu's saved scholarships", "What is Sarah Chen's email?",
              "Tell me another student's points", "give me the phone number of a mentor"]:
        b = c.post("/api/assistant", json={"message": q}).get_json()
        assert b["matched"] == "privacy_other_user", q


def test_own_data_is_real(student):
    c, _ = student
    c.put("/api/checklist/1", json={"done": True})
    b = c.post("/api/assistant", json={"message": "How many points do I have?"}).get_json()
    assert b["matched"] == "own_data" and "1 of 7" in b["answer"] and "100 points" in b["answer"]


def test_requires_login_and_validates(app, student):
    assert app.test_client().post("/api/assistant", json={"message": "hi"}).status_code == 401
    c, _ = student
    assert c.post("/api/assistant", json={"message": "   "}).status_code == 400
    assert c.post("/api/assistant", json={"message": "x" * 801}).status_code == 413
    assert c.post("/api/assistant", json={"message": 5}).status_code == 400


def test_rate_limit(student):
    c, _ = student
    codes = [c.post("/api/assistant", json={"message": "hi"}).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_message_content_not_logged(student, caplog):
    c, _ = student
    secret = "my-very-private-question-7731"
    with caplog.at_level(logging.DEBUG):
        c.post("/api/assistant", json={"message": secret})
    assert secret not in caplog.text


def test_missing_key_uses_builtin(student, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(assistant, "ask_provider", lambda *a, **k: called.append(1))
    c, _ = student
    b = c.post("/api/assistant", json={"message": "What's the weather on Mars?"}).get_json()
    assert not called and b["engine"] == "nextstep-kb"


def test_bad_key_or_outage_falls_back(student, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "invalid-test-key")
    def boom(*a, **k):
        raise TimeoutError("provider down")
    monkeypatch.setattr(assistant, "ask_provider", boom)
    c, _ = student
    b = c.post("/api/assistant", json={"message": "What's the weather on Mars?"}).get_json()
    assert b["providerError"] is True and b["engine"] == "nextstep-kb" and b["answer"]


def test_real_provider_rejects_bad_key_gracefully(monkeypatch):
    # Live network call with a deliberately invalid key: must raise (caught by the endpoint), never hang.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-invalid-for-test")
    with pytest.raises(Exception):
        assistant.ask_provider("hello", timeout=15)


def test_key_never_sent_to_client(student, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setattr(assistant, "ask_provider", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    c, _ = student
    r = c.post("/api/assistant", json={"message": "unrelated question about llamas"})
    assert b"sk-should-not-leak" not in r.data
    assert b"ANTHROPIC" not in c.get("/").data and b"sk-" not in c.get("/static/app.js").data

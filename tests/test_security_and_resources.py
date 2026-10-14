"""Authorization, request hardening, XSS hygiene, FAFSA/TAP resources (spec sections 3, 10-13)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text()
PAGE = (ROOT / "Index.html.html").read_text()

PROTECTED = [("put", "/api/me"), ("get", "/api/points"), ("put", "/api/checklist/1"), ("post", "/api/checklist/reset"),
             ("post", "/api/scholarships"), ("post", "/api/scholarships/gates-scholarship/save"),
             ("delete", "/api/scholarships/gates-scholarship/save"), ("post", "/api/redemptions"),
             ("get", "/api/mentor-requests"), ("post", "/api/mentor-requests"), ("get", "/api/colleges/saved"),
             ("post", "/api/colleges/190512/save"), ("post", "/api/assistant")]


def test_protected_endpoints_require_login(client):
    for method, path in PROTECTED:
        assert getattr(client, method)(path, json={}).status_code == 401, path


def test_cross_site_writes_blocked(student):
    c, _ = student
    r = c.put("/api/checklist/1", json={"done": True}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.put("/api/checklist/1", json={"done": True}, headers={"Origin": "http://localhost"}).status_code == 200


def test_security_headers_and_cookie(app):
    c = app.test_client()
    r = c.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "SAMEORIGIN"
    from conftest import signup
    resp = c.post("/api/signup", json={"name": "Cookie T", "email": "cookie-" + __import__("uuid").uuid4().hex[:6] +
                                       "@test.invalid", "password": "long-enough-1", "gpa": 3, "budget": 1})
    cookie = resp.headers["Set-Cookie"]
    assert "nextstep_session=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert c.get("/api/me").headers["Cache-Control"] == "no-store"


def test_login_rate_limited(client):
    codes = [client.post("/api/login", json={"email": "nobody@test.invalid", "password": "x"}).status_code
             for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_profile_update_only_affects_self(app):
    from conftest import signup
    a = app.test_client(); ua = signup(a)
    b = app.test_client(); ub = signup(b)
    a.put("/api/me", json={"name": "Changed", "gpa": 2.5, "budget": 1000, "id": ub["id"]})
    assert b.get("/api/me").get_json()["user"]["name"] == "Test Student"
    assert a.get("/api/me").get_json()["user"]["name"] == "Changed"


def test_innerhtml_templates_escape_db_text():
    # Every DB/user text field interpolated into HTML must go through esc().
    risky = re.findall(r"\$\{(?:sch|s|int|p|c|r|m|t|item|l|tut|men)\.(?:title|name|desc|bio|provider|org|school|"
                       r"major|eligibility|notes|journey|mentorName|city|deadlineText)\}",
                       "\n".join(l for l in APP_JS.splitlines() if "toast(" not in l and "announce(" not in l))
    # (toast/announce write with textContent, which cannot inject HTML)
    assert risky == []
    assert "function esc(" in APP_JS and "innerHTML" in APP_JS


def test_resources_videos_are_correct_and_distinct(client):
    r = client.get("/api/resources").get_json()
    f, t = r["videos"]["fafsa"], r["videos"]["tap"]
    assert f["youtube_id"] == "XZDiNdIgPUE" and f["watch_url"] == "https://www.youtube.com/watch?v=XZDiNdIgPUE"
    assert t["youtube_id"] == "5UKVJ4y6G7Q" and t["watch_url"] == "https://www.youtube.com/watch?v=5UKVJ4y6G7Q"
    assert "FAFSA" in f["verified_title"] and "TAP" in t["verified_title"]
    assert f["embeddable"] is False and t["embeddable"] is True     # verified with YouTube on 2026-10-14
    links = r["links"]
    assert links["studentaid"]["url"] == "https://studentaid.gov/"
    assert links["fafsa_apply"]["url"] == "https://studentaid.gov/h/apply-for-aid/fafsa"
    assert links["understand_aid"]["url"] == "https://studentaid.gov/understand-aid"
    assert links["hesc"]["url"] == "https://hesc.ny.gov/"
    assert links["nys_grants"]["url"] == "https://hesc.ny.gov/find-aid/nys-grants-scholarships"


def test_page_has_two_labeled_tutorial_cards():
    assert 'id="card-fafsa-tutorial"' in PAGE and 'id="card-tap-tutorial"' in PAGE
    assert ">FAFSA Tutorial<" in PAGE and ">New York TAP Tutorial<" in PAGE
    fafsa = PAGE[PAGE.index('id="card-fafsa-tutorial"'):PAGE.index('id="card-tap-tutorial"')]
    assert 'id="fafsa-video"' in fafsa and "tap-video" not in fafsa
    assert "aspect-ratio: 16 / 9" in PAGE
    js = APP_JS[APP_JS.index("function videoEmbed"):APP_JS.index("function linkChip")]
    assert "youtube-nocookie.com/embed/" in js and 'loading="lazy"' in js and "title=" in js
    assert "Watch on YouTube" in js                       # fallback when embedding is disabled
    res = APP_JS[APP_JS.index("function renderResources"):APP_JS.index("// ---", APP_JS.index("function renderResources"))]
    assert "V.fafsa.watch_url" in res and "V.tap.watch_url" in res
    assert "could not be loaded" in res                    # graceful when resources fail


def test_no_secrets_or_credentials_in_repo():
    for p in list(ROOT.glob("*.py")) + list(ROOT.glob("scripts/*.py")) + [ROOT / "static" / "app.js"]:
        text = p.read_text()
        assert "postgresql://" not in text.replace("postgresql://...", ""), p
        assert not re.search(r"sk-ant-[A-Za-z0-9]", text), p

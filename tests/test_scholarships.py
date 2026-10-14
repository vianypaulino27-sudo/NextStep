"""Scholarships: Apply and Save are separate; saved list persists (spec section 5)."""
import re
from pathlib import Path

from conftest import login, signup
from urlcheck import allowed_listing_url, safe_external_url

APP_JS = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text()


def listing(c):
    return c.get("/api/scholarships").get_json()


def test_verified_listings_have_official_https_links(client):
    data = listing(client)["scholarships"]
    assert len(data) >= 10
    for s in data:
        assert s["verified"] and not s["isCustom"]
        assert s["applyUrl"].startswith("https://") and allowed_listing_url(s["applyUrl"]) == s["applyUrl"]
        assert s["lastVerified"] and s["sourceUrl"]
        assert s["status"] in ("open", "upcoming", "closed", "unknown")
        assert s["amountText"] is None or isinstance(s["amountText"], str)   # exactly as published, or "See official site"
    titles = {s["title"] for s in data}
    assert "Future STEM Leaders Award" not in titles          # fictional legacy row retired
    jkc = next(s for s in data if s["id"] == "jack-kent-cooke-college-scholarship")
    assert jkc["applyUrl"] == "https://www.jkcf.org/our-scholarships/college-scholarship-program/"


def test_save_adds_persists_and_removes(app):
    c = app.test_client()
    email = signup(c)["email"]
    sid = listing(c)["scholarships"][0]["id"]
    r = c.post(f"/api/scholarships/{sid}/save")
    assert r.status_code == 200 and r.get_json()["saved"] is True
    assert "Location" not in r.headers                       # Save never redirects to the application site
    assert c.post(f"/api/scholarships/{sid}/save").status_code == 200   # idempotent
    data = listing(c)
    assert data["saved"] == [sid] and len(data["savedItems"]) == 1      # no duplicates
    c2 = app.test_client()
    login(c2, email)                                          # persists across sessions
    assert listing(c2)["saved"] == [sid]
    assert c2.delete(f"/api/scholarships/{sid}/save").get_json()["saved"] is False
    assert listing(c2)["saved"] == [] and listing(c2)["savedItems"] == []


def test_viewing_or_applying_does_not_save(student):
    c, _ = student
    listing(c)
    listing(c)
    assert listing(c)["saved"] == []
    # Apply is a plain outbound link; there is no server endpoint that saves on apply
    assert c.post("/api/scholarships/jack-kent-cooke-college-scholarship/apply").status_code in (404, 405)


def test_frontend_apply_and_save_are_separate_controls():
    card = APP_JS[APP_JS.index("function scholarshipCard"):APP_JS.index("function renderSavedSidebar")]
    apply_link = re.search(r'<a href="\$\{esc\(sch\.applyUrl\)\}" target="_blank" rel="noopener noreferrer"', card)
    assert apply_link, "Apply must be an <a> to the official URL opened safely in a new tab"
    save_btn = re.search(r'<button type="button" onclick="toggleSave\(', card)
    assert save_btn and "href" not in card[save_btn.start():card.index("</button>", save_btn.start())]
    toggle = APP_JS[APP_JS.index("async function toggleSave"):APP_JS.index("async function saveCustomScholarship")]
    assert "window.open" not in toggle and "location" not in toggle   # Save never navigates
    assert "Application link unavailable" in card                    # honest when no link


def test_save_requires_login(client):
    assert client.post("/api/scholarships/gates-scholarship/save").status_code == 401


def test_custom_scholarship_private_and_linkless(app):
    a = app.test_client(); signup(a)
    b = app.test_client(); signup(b)
    r = a.post("/api/scholarships", json={"title": "<script>alert(1)</script> Local Award", "provider": "PTA",
                                           "amount": 500, "deadline": "2027-03-01", "desc": "Local"})
    assert r.status_code == 201
    s = r.get_json()["scholarship"]
    assert s["isCustom"] and s["applyUrl"] is None and s["status"] == "custom"
    assert s["id"] not in [x["id"] for x in listing(b)["scholarships"]]          # invisible to others
    assert b.post(f"/api/scholarships/{s['id']}/save").status_code == 404
    assert b.delete(f"/api/scholarships/{s['id']}").status_code == 404           # can't delete others'
    assert a.delete(f"/api/scholarships/{s['id']}").status_code == 200


def test_custom_validation(student):
    c, _ = student
    assert c.post("/api/scholarships", json={"title": "x"}).status_code == 400
    assert c.post("/api/scholarships", json={"title": "x", "provider": "y", "amount": -5, "deadline": "2027-01-01",
                                             "desc": "z"}).status_code == 400


def test_closed_status_when_deadline_passed():
    import server
    from datetime import date
    assert server.deadline_state(date(2020, 1, 1), "open") == "closed"
    assert server.deadline_state(None, "upcoming") == "upcoming"
    assert server.deadline_state(None, None) == "unknown"


def test_url_allowlist():
    assert safe_external_url("javascript:alert(1)") is None
    assert safe_external_url("http://example.org") is None
    assert safe_external_url("https://user:pw@studentaid.gov/") is None
    assert safe_external_url("https://127.0.0.1/") is None
    assert allowed_listing_url("https://evil.example.com/apply") is None
    assert allowed_listing_url("https://studentaid.gov/h/apply-for-aid/fafsa")

"""Internships: verified official links, honest status (spec section 8)."""
from urlcheck import allowed_listing_url


def test_listings_are_verified(client):
    data = client.get("/api/internships").get_json()
    assert len(data) >= 10
    ids = {i["id"] for i in data}
    assert not ids & {"int-1", "int-2", "int-3"}      # fictional examples retired
    for i in data:
        assert i["applyUrl"] and allowed_listing_url(i["applyUrl"]) == i["applyUrl"]
        assert i["lastVerified"] and i["sourceUrl"]
        assert i["status"] in ("open", "upcoming", "closed", "unknown")
        assert i["compensation"] in ("Paid", "Stipend", "Unpaid", "Not specified")
        assert i["target"] in ("High School", "College")
    syep = next(i for i in data if i["id"] == "nyc-syep")
    assert syep["applyUrl"].startswith("https://www.nyc.gov/site/dycd/")
    assert syep["status"] == "closed"                  # 2026 cycle ended; shown honestly


def test_expired_deadline_is_closed(conn, client):
    conn.execute("UPDATE internships SET deadline='2020-01-01', status='open' WHERE slug='rockefeller-ssrp'")
    conn.commit()
    try:
        r = next(i for i in client.get("/api/internships").get_json() if i["id"] == "rockefeller-ssrp")
        assert r["status"] == "closed"
    finally:
        conn.execute("UPDATE internships SET deadline='2027-01-04', status='open' WHERE slug='rockefeller-ssrp'")
        conn.commit()


def test_no_fake_apply_submission(client, student):
    c, _ = student
    # Apply is only an outbound link; NextStep never records or claims a submission.
    assert c.post("/api/internships/nyc-syep/apply").status_code in (404, 405)
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text()
    block = js[js.index("function renderInternships"):js.index("// ---", js.index("function renderInternships"))]
    assert "doesn't submit applications" in block and 'rel="noopener noreferrer"' in block

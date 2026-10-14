"""College search + matcher over real College Scorecard rows (spec section 6)."""
import pytest

import college_match as cm

BASE = {"gpa": 3.4, "budget": 18000}


def search(c, q, **kw):
    params = {"q": q, **kw}
    return c.get("/api/colleges/search", query_string=params)


def names(r):
    return [x["name"] for x in r.get_json()["results"]]


def test_full_partial_case_insensitive(client):
    assert names(search(client, "Cornell University"))[0] == "Cornell University"
    assert "CUNY Bernard M Baruch College" in names(search(client, "baruch"))
    assert names(search(client, "CORNELL"))[0] == "Cornell University"
    assert "Columbia University in the City of New York" in names(search(client, "columbia"))


def test_variations_and_misspellings(client):
    assert "St. John's University-New York" in names(search(client, "Saint Johns"))
    assert "St. John's University-New York" in names(search(client, "st johns"))
    assert "University at Albany" in names(search(client, "SUNY Albany"))       # alias from Scorecard
    r = search(client, "Cornel Univercity").get_json()                          # typo
    assert "Cornell University" in [x["name"] for x in r["results"]]


def test_empty_and_short_queries(client):
    r = search(client, "zzzqqqxx")
    assert r.status_code == 200 and r.get_json()["results"] == [] and r.get_json()["total"] == 0
    assert search(client, "a").status_code == 400
    assert search(client, "").status_code == 400


def test_pagination(client):
    p1 = search(client, "cuny", per_page=2).get_json()
    p2 = search(client, "cuny", per_page=2, page=2).get_json()
    assert p1["hasMore"] and p1["total"] >= 4
    assert not set(n["name"] for n in p1["results"]) & set(n["name"] for n in p2["results"])


def test_state_filter(client):
    assert all(x["state"] == "NY" for x in search(client, "university", state="NY").get_json()["results"])


def test_detail_is_correct_institution_with_sources(client):
    d = client.get("/api/colleges/190512").get_json()
    assert d["name"] == "CUNY Bernard M Baruch College" and d["city"] == "New York" and d["state"] == "NY"
    assert d["type"] == "Public" and d["website"] == "https://www.baruch.cuny.edu/"
    assert d["cost"]["tuitionInState"] < d["cost"]["tuitionOutOfState"]
    assert any("collegescorecard.ed.gov" in s["url"] for s in d["sources"])
    assert any(s["url"] == "https://nces.ed.gov/collegenavigator/?id=190512" for s in d["sources"])
    assert "Title IV" in d["dataSource"]["coverage"] and d["dataSource"]["years"]["admissions"].startswith("Fall 2024")
    assert client.get("/api/colleges/999999999").status_code == 404


def test_match_validation(client):
    r = client.post("/api/colleges/match", json={})
    assert r.status_code == 400 and set(r.get_json()["fields"]) >= {"gpa", "budget"}
    bad = client.post("/api/colleges/match", json={"gpa": 5, "budget": -1, "sat": 2000, "states": "New York",
                                                   "major": "Wizardry", "control": []}).get_json()["fields"]
    assert set(bad) >= {"gpa", "budget", "sat", "states", "major", "control"}


def test_recommendations_reflect_preferences(client):
    r = client.post("/api/colleges/match", json={**BASE, "states": "NY", "control": ["1"]}).get_json()
    assert r["total"] > 0
    assert all(x["state"] == "NY" and x["type"] == "Public" for x in r["results"])
    r2 = client.post("/api/colleges/match", json={**BASE, "major": "Engineering"}).get_json()
    rows = {row["unitid"]: row for row in (client.get(f"/api/colleges/{x['unitid']}").get_json() for x in r2["results"])}
    assert all("Engineering" in d["programs"]["bachelor"] for d in rows.values())
    for x in r["results"]:
        assert x["fit"] in ("Strong academic fit", "Possible academic fit", "More competitive", "Fit uncertain")
        assert x["reasons"] or x["cautions"]
        assert x["verify"]


def test_never_excluded_for_sticker_price(client):
    r = client.post("/api/colleges/match", json={"gpa": 3.9, "budget": 0, "states": "NY"}).get_json()
    assert r["total"] > 5                                                  # budget 0 still returns colleges
    assert all(x["affordability"]["within_budget"] in (False, None) for x in r["results"])
    names_ = [x["name"] for x in client.post("/api/colleges/match", json={"gpa": 3.9, "budget": 1000, "states": "NY",
                                                                          "control": ["2"]}).get_json()["results"]]
    assert names_  # expensive private colleges are still listed


def test_selective_school_labeled_competitive(client):
    r = client.post("/api/colleges/match", json={"gpa": 4.0, "budget": 90000, "sat": 1600, "states": "NY",
                                                 "control": ["2"]}).get_json()
    while True:
        col = [x for x in r["results"] if x["name"] == "Columbia University in the City of New York"]
        if col or r["page"] * r["perPage"] >= r["total"]:
            break
        r = client.post("/api/colleges/match", json={"gpa": 4.0, "budget": 90000, "sat": 1600, "states": "NY",
                                                     "control": ["2"], "page": r["page"] + 1}).get_json()
    assert col and col[0]["fit"] == "More competitive"     # <15% admit rate: never "strong", even with 1600


def test_fit_unit_rules():
    prefs = cm.validate_prefs({"gpa": 3.0, "budget": 10000, "sat": 1450})
    base = {"admission_rate": 0.6, "sat_rw_25": 500, "sat_math_25": 500, "sat_rw_75": 650, "sat_math_75": 650}
    assert cm.academic_fit(base, prefs)[0] == cm.FIT_STRONG          # above range -> shifted up
    low = cm.validate_prefs({"gpa": 3.0, "budget": 10000, "sat": 900})
    assert cm.academic_fit(base, low)[0] == cm.FIT_COMPETITIVE       # below range -> shifted down
    assert cm.academic_fit({"admission_rate": None}, prefs)[0] == cm.FIT_UNKNOWN
    assert cm.academic_fit({"open_admission": True}, prefs)[0] == cm.FIT_STRONG
    aff = cm.affordability({"cost_of_attendance": 80000, "avg_net_price": 15000,
                            "net_price_by_income": {"0-30000": 5000}}, {"budget": 10000, "income": "0-30000"})
    assert aff["within_budget"] and aff["net"] == 5000 and aff["sticker"] == 80000


def test_saved_colleges_are_per_user(app):
    from conftest import signup
    a = app.test_client(); signup(a)
    b = app.test_client(); signup(b)
    assert a.post("/api/colleges/190512/save").status_code == 200
    assert [x["unitid"] for x in a.get("/api/colleges/saved").get_json()] == [190512]
    assert b.get("/api/colleges/saved").get_json() == []
    assert a.get("/api/colleges/190512").get_json()["saved"] is True
    assert b.get("/api/colleges/190512").get_json()["saved"] is False


def test_matcher_prefs_persist(app):
    from conftest import signup
    a = app.test_client(); signup(a)
    a.post("/api/colleges/match", json={**BASE, "states": "NY"})
    assert a.get("/api/me").get_json()["user"]["collegePrefs"]["states"] == ["NY"]


def test_rate_limit_does_not_crash(client):
    import server
    for _ in range(90):
        server.limiter.allow("college-search:127.0.0.1", 90, 60)
    r = search(client, "baruch")
    assert r.status_code == 429 and "Too many requests" in r.get_json()["error"]

"""College search + explainable matcher over College Scorecard rows.

Pure functions (no database access) so they can be unit-tested; server.py
loads rows from the `college_scorecard` table and passes them in.

Honesty rules baked in:
  * Labels come only from published admissions evidence (admission rate,
    open-admission flag, SAT/ACT ranges). Scorecard publishes no GPA data, so
    GPA is used only as a coarse, clearly-labelled adjustment.
  * Nothing is excluded because of sticker price; affordability is reported
    using average net price (by income band when the student gives one).
  * Missing data is reported as missing, never filled in.
"""
import math
import re

CONTROL_LABELS = {1: "Public", 2: "Private nonprofit", 3: "Private for-profit"}
DEGREE_LABELS = {0: "Non-degree-granting", 1: "Certificate", 2: "Associate degree", 3: "Bachelor's degree", 4: "Graduate degree"}
TEST_POLICY = {1: "Required", 2: "Recommended", 3: "Neither required nor recommended", 4: "Not known",
               5: "Considered but not required"}
LOCALE_LABELS = {11: "Large city", 12: "Midsize city", 13: "Small city", 21: "Large suburb", 22: "Midsize suburb",
                 23: "Small suburb", 31: "Town (fringe)", 32: "Town (distant)", 33: "Town (remote)",
                 41: "Rural (fringe)", 42: "Rural (distant)", 43: "Rural (remote)"}
INCOME_BANDS = {"0-30000": "$0-$30,000", "30001-48000": "$30,001-$48,000", "48001-75000": "$48,001-$75,000",
                "75001-110000": "$75,001-$110,000", "110001+": "$110,001 and up"}
STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
          "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI AS MP FM MH PW").split()

# CIP 2-digit families (labels from the Scorecard data dictionary, shortened).
CIP = {
    "01": "Agriculture", "03": "Natural Resources & Conservation", "04": "Architecture",
    "05": "Area, Ethnic, Cultural & Gender Studies", "09": "Communication & Journalism",
    "10": "Communications Technologies", "11": "Computer & Information Sciences",
    "12": "Personal & Culinary Services", "13": "Education", "14": "Engineering",
    "15": "Engineering Technologies", "16": "Foreign Languages & Linguistics",
    "19": "Family & Consumer Sciences", "22": "Legal Professions", "23": "English Language & Literature",
    "24": "Liberal Arts & Humanities", "25": "Library Science", "26": "Biological & Biomedical Sciences",
    "27": "Mathematics & Statistics", "29": "Military Technologies", "30": "Multi/Interdisciplinary Studies",
    "31": "Parks, Recreation & Fitness", "38": "Philosophy & Religious Studies",
    "39": "Theology & Religious Vocations", "40": "Physical Sciences", "41": "Science Technologies",
    "42": "Psychology", "43": "Homeland Security, Law Enforcement & Firefighting",
    "44": "Public Administration & Social Service", "45": "Social Sciences", "46": "Construction Trades",
    "47": "Mechanic & Repair Technologies", "48": "Precision Production", "49": "Transportation",
    "50": "Visual & Performing Arts", "51": "Health Professions", "52": "Business & Management",
    "54": "History",
}
MAJORS = sorted(CIP.values())
RELIGIOUS = {"Theology & Religious Vocations", "Philosophy & Religious Studies"}

FIT_STRONG, FIT_POSSIBLE, FIT_COMPETITIVE, FIT_UNKNOWN = (
    "Strong academic fit", "Possible academic fit", "More competitive", "Fit uncertain")
_FIT_ORDER = [FIT_COMPETITIVE, FIT_POSSIBLE, FIT_STRONG]


def normalize(text):
    """Lower-case, '&'->'and', 'st.'->'saint' variants handled by caller, punctuation removed."""
    t = (text or "").lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


SYNONYMS = {"univ": "university", "u": "university", "coll": "college", "st": "saint", "mt": "mount",
            "inst": "institute", "tech": "technology"}


def query_variants(q):
    """The normalised query plus a version with common abbreviations expanded."""
    base = normalize(q)
    expanded = " ".join(SYNONYMS.get(w, w) for w in base.split())
    return [v for v in dict.fromkeys([base, expanded]) if v]


# ------------------------------------------------------------------ validation
class InputError(ValueError):
    def __init__(self, errors):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return "bad"


def validate_prefs(d):
    """Validate matcher input. Raises InputError({field: message})."""
    d = d or {}
    e, out = {}, {}
    gpa = _num(d.get("gpa"))
    if gpa is None:
        e["gpa"] = "GPA is required (unweighted, 0.0-4.0)."
    elif gpa == "bad" or not 0 <= gpa <= 4:
        e["gpa"] = "GPA must be a number from 0.0 to 4.0 (unweighted)."
    else:
        out["gpa"] = round(gpa, 2)
    budget = _num(d.get("budget"))
    if budget is None:
        e["budget"] = "Yearly budget is required (what your family can pay per year, in dollars)."
    elif budget == "bad" or not 0 <= budget <= 200000:
        e["budget"] = "Budget must be a dollar amount from 0 to 200,000."
    else:
        out["budget"] = int(budget)
    for key, lo, hi, label in (("sat", 400, 1600, "SAT total"), ("act", 1, 36, "ACT composite")):
        v = _num(d.get(key))
        if v == "bad" or (v is not None and not lo <= v <= hi):
            e[key] = f"{label} must be between {lo} and {hi}, or left blank."
        elif v is not None:
            out[key] = int(v)
    band = d.get("income") or None
    if band is not None and band not in INCOME_BANDS:
        e["income"] = "Choose one of the listed family income ranges, or leave it blank."
    out["income"] = band
    control = d.get("control")
    if control is None or control == "":
        control = ["1", "2"]  # default: public + private nonprofit
    if isinstance(control, str):
        control = [c for c in control.split(",") if c]
    try:
        control = sorted({int(c) for c in control})
    except (TypeError, ValueError):
        control = []
    if not control or any(c not in CONTROL_LABELS for c in control):
        e["control"] = "Choose at least one institution type."
    out["control"] = control
    states = d.get("states") or []
    if isinstance(states, str):
        states = [s for s in re.split(r"[\s,]+", states) if s]
    states = sorted({s.strip().upper() for s in states})
    if any(s not in STATES for s in states):
        e["states"] = "Use two-letter state codes such as NY or NJ."
    out["states"] = states
    major = d.get("major") or None
    if major is not None and major not in MAJORS:
        e["major"] = "Choose a field of study from the list."
    out["major"] = major
    degree = d.get("degree") or "bachelor"
    if degree not in ("bachelor", "any"):
        e["degree"] = "Degree must be 'bachelor' or 'any'."
    out["degree"] = degree
    if e:
        raise InputError(e)
    return out


# ------------------------------------------------------------------ evaluation
def _shift(label, steps):
    if label not in _FIT_ORDER:
        return label
    i = max(0, min(len(_FIT_ORDER) - 1, _FIT_ORDER.index(label) + steps))
    return _FIT_ORDER[i]


def sat_range(c):
    if None in (c.get("sat_rw_25"), c.get("sat_math_25"), c.get("sat_rw_75"), c.get("sat_math_75")):
        return None
    return c["sat_rw_25"] + c["sat_math_25"], c["sat_rw_75"] + c["sat_math_75"]


def academic_fit(c, prefs):
    """Return (label, reasons[], cautions[])."""
    reasons, cautions = [], []
    rate = c.get("admission_rate")
    rate = float(rate) if rate is not None else None
    if c.get("open_admission"):
        label = FIT_STRONG
        reasons.append("Open admission policy reported to the Department of Education.")
    elif rate is None:
        cautions.append("This college did not report an admission rate, so NextStep can't judge academic fit.")
        return FIT_UNKNOWN, reasons, cautions
    else:
        pct = round(rate * 100)
        if rate >= 0.75:
            label = FIT_STRONG
        elif rate >= 0.40:
            label = FIT_POSSIBLE
        else:
            label = FIT_COMPETITIVE
        reasons.append(f"Admits about {pct}% of applicants (fall 2024).")
    used_test = False
    if prefs.get("sat") and sat_range(c):
        lo, hi = sat_range(c)
        used_test = True
        if prefs["sat"] > hi:
            label = _shift(label, +1)
            reasons.append(f"Your SAT ({prefs['sat']}) is above the typical range of admitted students (about {lo}-{hi}).")
        elif prefs["sat"] < lo:
            label = _shift(label, -1)
            cautions.append(f"Your SAT ({prefs['sat']}) is below the typical range (about {lo}-{hi}).")
        else:
            reasons.append(f"Your SAT ({prefs['sat']}) is within the typical range (about {lo}-{hi}).")
    if prefs.get("act") and c.get("act_25") and c.get("act_75") and not used_test:
        lo, hi = c["act_25"], c["act_75"]
        used_test = True
        if prefs["act"] > hi:
            label = _shift(label, +1)
            reasons.append(f"Your ACT ({prefs['act']}) is above the middle 50% of admitted students ({lo}-{hi}).")
        elif prefs["act"] < lo:
            label = _shift(label, -1)
            cautions.append(f"Your ACT ({prefs['act']}) is below the middle 50% ({lo}-{hi}).")
        else:
            reasons.append(f"Your ACT ({prefs['act']}) is within the middle 50% ({lo}-{hi}).")
    if rate is not None and rate < 0.15 and label != FIT_COMPETITIVE:
        label = FIT_COMPETITIVE
        cautions.append("Admits fewer than 15% of applicants, so admission is uncertain for everyone.")
    gpa = prefs.get("gpa")
    if gpa is not None and not c.get("open_admission"):
        if gpa < 2.5 and label == FIT_STRONG:
            label = FIT_POSSIBLE
            cautions.append("Your GPA is below 2.5. Even colleges that admit most applicants review grades.")
        elif gpa >= 3.5 and label == FIT_POSSIBLE and not used_test and rate is not None and rate >= 0.5:
            reasons.append("Your GPA is strong, but Scorecard does not publish GPA data to compare against.")
    if not used_test and not c.get("open_admission"):
        cautions.append("No SAT/ACT comparison was made (no score entered or none published). The label is based on admission rate only.")
    return label, reasons, cautions


def affordability(c, prefs):
    """Return dict with sticker, net price, which net price was used and a plain-language note."""
    budget = prefs.get("budget")
    band = prefs.get("income")
    bands = c.get("net_price_by_income") or {}
    net, net_basis = None, None
    if band and bands.get(band) is not None:
        net, net_basis = bands[band], f"average net price for families earning {INCOME_BANDS[band]}"
    elif c.get("avg_net_price") is not None:
        net, net_basis = c["avg_net_price"], "average net price for all aided students"
    sticker = c.get("cost_of_attendance")
    out = {"sticker": sticker, "net": net, "net_basis": net_basis, "budget": budget, "within_budget": None}
    if net is None:
        out["note"] = "Net price not reported. Use the college's Net Price Calculator."
    elif budget is not None and net <= budget:
        out["within_budget"] = True
        if sticker and sticker > budget:
            out["note"] = (f"The published cost (${sticker:,}) is above your ${budget:,} budget, but the {net_basis} "
                           f"(${net:,}) is within it.")
        else:
            out["note"] = f"The {net_basis} (${net:,}) is within your ${budget:,} budget."
    else:
        out["within_budget"] = False
        out["note"] = (f"The {net_basis} (${net:,}) is above your ${budget:,} budget. Your own aid could be different, "
                       "so check the Net Price Calculator before you rule it out.")
    return out


def evaluate(c, prefs):
    fit, reasons, cautions = academic_fit(c, prefs)
    aff = affordability(c, prefs)
    matches = []
    if prefs.get("states"):
        matches.append(f"Located in {c.get('state')}, one of your preferred states.")
    if prefs.get("major"):
        matches.append(f"Offers {prefs['major']} ({'bachelor' if prefs['degree'] == 'bachelor' else 'associate or bachelor'}'s level, per IPEDS).")
    if c.get("control") in prefs.get("control", []):
        matches.append(f"{CONTROL_LABELS.get(c.get('control'))} institution, which matches your type preference.")
    score = {FIT_STRONG: 3, FIT_POSSIBLE: 2, FIT_UNKNOWN: 1.5, FIT_COMPETITIVE: 1}[fit] * 2
    score += {True: 2, None: 1, False: 0}[aff["within_budget"]]
    if c.get("completion_rate") is not None:
        score += float(c["completion_rate"]) * 3        # students who start there tend to finish
    else:
        score -= 0.5
        cautions.append("Graduation rate not reported.")
    ugds = c.get("undergrad_enrollment") or 0
    score += min(1.0, math.log10(ugds + 1) / 4)          # small tie-breaker toward established colleges
    if c.get("men_only") or c.get("women_only"):
        cautions.append("Admits " + ("men" if c.get("men_only") else "women") + " only (IPEDS).")
        score -= 1.5
    progs = set(c.get("bachelor_programs") or [])
    if progs and progs <= RELIGIOUS and prefs.get("major") not in RELIGIOUS:
        cautions.append("Offers degrees only in religious studies or theology (IPEDS).")
        score -= 2
    if c.get("hbcu"):
        matches.append("Historically Black College or University (HBCU).")
    if c.get("hsi"):
        matches.append("Hispanic-Serving Institution (HSI).")
    verify = ["Confirm admission requirements on the college's admissions website."]
    if c.get("net_price_calc_url"):
        verify.append("Estimate your own cost with the college's Net Price Calculator.")
    else:
        verify.append("Ask the college's financial aid office for a cost estimate.")
    if prefs.get("major"):
        verify.append("Check that the specific program you want is offered and open to first-year students.")
    return {"fit": fit, "reasons": reasons, "cautions": cautions, "affordability": aff, "matches": matches,
            "verify": verify, "score": round(score, 3)}


def candidates(rows, prefs):
    for c in rows:
        if c.get("control") not in prefs["control"]:
            continue
        if prefs["states"] and c.get("state") not in prefs["states"]:
            continue
        progs = c.get("bachelor_programs") or []
        if prefs["degree"] == "bachelor":
            if not progs:
                continue
        else:
            progs = list(progs) + list(c.get("associate_programs") or [])
        if prefs["major"] and prefs["major"] not in progs:
            continue
        yield c


def recommend(rows, prefs, page=1, per_page=12):
    """Rank all candidate rows. Returns dict with results page and label counts."""
    evaluated = []
    for c in candidates(rows, prefs):
        ev = evaluate(c, prefs)
        evaluated.append((ev["score"], c.get("undergrad_enrollment") or 0, c, ev))
    evaluated.sort(key=lambda t: (-t[0], -t[1], t[2]["name"]))
    counts = {}
    for _, _, _, ev in evaluated:
        counts[ev["fit"]] = counts.get(ev["fit"], 0) + 1
    start = (page - 1) * per_page
    return {"total": len(evaluated), "counts": counts, "page": page, "per_page": per_page,
            "results": [(c, ev) for _, _, c, ev in evaluated[start:start + per_page]]}

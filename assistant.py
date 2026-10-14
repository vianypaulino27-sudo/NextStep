"""NextStep Assistant: a grounded, retrieval-based responder.

It answers only from:
  * knowledge/assistant_kb.json   - curated answers (NextStep how-tos, official facts, general guidance)
  * knowledge/resources.json      - verified official links and facts
  * the database, through callbacks the server passes in (the signed-in student's
    OWN progress, verified scholarship listings, College Scorecard rows)

It never invents deadlines, amounts, schools, people or URLs: every link it
returns comes from resources.json or a verified database row, and when nothing
matches it says so.

An optional external model (Anthropic Messages API) is DORMANT: it is used only
if ANTHROPIC_API_KEY is set, and any failure falls back to this responder.
NextStep does not currently configure a key (decision: built-in assistant only).
"""
import json
import os
import re
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_QUESTION_CHARS = 800

TAB_LABELS = {
    "fafsa": "FAFSA & TAP Aid tab", "rewards": "Gift Shop & Merch tab", "scholarships": "Scholarships tab",
    "testprep": "ACT / SAT Prep tab", "matcher": "College Matcher tab", "mentors": "Connect Mentors tab",
    "internships": "Internships tab", "advisor": "Assistant tab",
}

STOP = set("""a an the i me my we our you your is are am was were be been to of in on for and or with at by it this that
do does did can could would should will how what where when which who why please tell about there here just get
im i'm its it's from as if so not no yes hi hello hey""".split())

BASIS_NOTE = {
    "official": "Source: official pages linked below (checked {date}). Rules can change, so confirm on the official site.",
    "general": "This is general guidance, not a verified rule. Check with your counselor or the official site for your situation.",
    "nextstep": "This is how the NextStep site works.",
}


@lru_cache(maxsize=1)
def kb():
    return json.loads((ROOT / "knowledge" / "assistant_kb.json").read_text(encoding="utf-8"))["entries"]


@lru_cache(maxsize=1)
def resources():
    return json.loads((ROOT / "knowledge" / "resources.json").read_text(encoding="utf-8"))


def norm(text):
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9'.\- ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _link(key):
    """Resolve a KB link key to {title, url} or a NextStep tab reference."""
    if key.startswith("tab:"):
        tab = key[4:]
        return {"tab": tab, "title": TAB_LABELS.get(tab, tab)}
    if key.startswith("video:"):
        v = resources()["videos"][key[6:]]
        return {"title": "Watch on YouTube: " + v["verified_title"], "url": v["watch_url"]}
    link = resources()["links"].get(key)
    return {"title": link["title"], "url": link["url"]} if link else None


def _links(keys):
    out = []
    for k in keys:
        item = _link(k)
        if item and item not in out:
            out.append(item)
    return out


def score_entry(q, words, entry):
    s = 0.0
    for kw, w in entry["keywords"].items():
        kw = kw.lower()
        if " " in kw or "-" in kw or "." in kw:
            if kw in q:
                s += w
        elif kw in words or (kw + "s") in words or (kw.endswith("s") and kw[:-1] in words):
            s += w
    for p in entry.get("patterns", []):
        if re.search(p, q):
            s += 3
    return s


def rank(question):
    q = norm(question)
    words = set(re.findall(r"[a-z0-9']+", q)) - STOP
    scored = sorted(((score_entry(q, words, e), e) for e in kb()), key=lambda t: -t[0])
    return q, [(s, e) for s, e in scored if s > 0]


def _reply(text, basis, links=(), matched=None, extra_sources=()):
    sources = list(extra_sources) + [l for l in links if "url" in l]
    tabs = [l for l in links if "tab" in l]
    note = BASIS_NOTE.get(basis, "")
    if basis == "official":
        note = note.format(date=resources()["last_verified"])
    return {"answer": text, "basis": basis, "basis_note": note, "sources": sources, "tabs": tabs,
            "matched": matched, "engine": "nextstep-kb"}


# --------------------------------------------------------------- special intents
_DATA_WORDS = r"(points|progress|account|email|password|saved|grades?|gpa|requests?|data|info|information|scholarships|checklist|address|phone)"
OTHER_PERSON_RE = re.compile(
    r"\b(another (student|user|person|kid)|other (students?|users?|people)|someone else|somebody else|classmates?|friends?"
    r"|(?!(what|it|that|there|here|who|where|let|he|she|how|nextstep|today|everyone)'s)[a-z0-9.@-]+'s)\b.{0,40}\b" + _DATA_WORDS + r"\b"
    r"|\b(email|phone|address|password|contact info|number) (of|for) (a |the |my )?(mentor|tutor|student|user|[a-z]+ [a-z]+)\b"
    r"|\b(mentor|tutor)'?s? (email|phone|number|address|contact)"
)
OWN_DATA_RE = re.compile(
    r"\b(my|i have|have i|i've)\b.{0,40}\b(points|progress|saved|checklist|score|requests?|balance)\b"
    r"|\b(points|progress|saved|scholarships|requests?|balance|score)\b.{0,30}\b(do i have|have i|i have|did i)\b")
DEADLINE_RE = re.compile(r"\b(deadline|due|last day|close[sd]?|when is)\b")
COLLEGE_FACT_RE = re.compile(
    r"\b(acceptance|admission|admit) rate\b|\b(net price|cost of attendance|tuition|graduation rate|retention|"
    r"sat (scores?|range)|act (scores?|range)|how (hard|selective)|how much does)\b")


def answer(question, ctx=None):
    """Return a grounded reply dict. ctx may provide:
       own_summary() -> dict|None, find_scholarship(text) -> list[dict], find_college(text) -> dict|None"""
    ctx = ctx or {}
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    q, ranked = rank(question)

    if OTHER_PERSON_RE.search(q):
        return _reply(
            "I can't share anyone else's information, including other students' points, saved items, grades or "
            "account details, and mentors' or tutors' private contact information. NextStep keeps each "
            "student's data private to their own account, and mentor contact details are never shown. "
            "To reach a mentor, send a session request from their profile on the Connect Mentors tab.",
            "nextstep", _links(["tab:mentors"]), "privacy_other_user")

    if OWN_DATA_RE.search(q) and ctx.get("own_summary"):
        s = ctx["own_summary"]()
        if s:
            return _reply(
                f"Here's what your NextStep account shows right now:\n"
                f"- Checklist: {s['completed']} of {s['total']} steps complete, so your checklist score is {s['score']} points.\n"
                f"- Gift Shop balance: {s['available']} points ({s['spent']} already redeemed).\n"
                f"- Saved scholarships: {s['saved']}.\n"
                f"- Mentor/tutor requests: {s['requests']} ({s['pending']} pending).\n"
                "I can only see your own account, not anyone else's.",
                "nextstep", _links(["tab:fafsa", "tab:scholarships"]), "own_data")

    if DEADLINE_RE.search(q) and ctx.get("find_scholarship"):
        hits = ctx["find_scholarship"](question)
        if hits:
            lines, srcs = [], []
            for h in hits[:3]:
                if h.get("deadline"):
                    d = f"deadline {h['deadline']}" + (f" (published as: \"{h['deadline_text']}\")" if h.get("deadline_text") else "")
                elif h.get("deadline_text"):
                    d = f"no single date in our data. The official page says: \"{h['deadline_text']}\""
                else:
                    d = "the official page didn't list a deadline when we checked, so I won't guess"
                lines.append(f"- {h['title']}: {d}. Status: {h.get('status') or 'unknown'}. Last checked {h.get('last_verified') or 'unknown'}.")
                if h.get("apply_url"):
                    srcs.append({"title": h["title"] + " (official page)", "url": h["apply_url"]})
            return _reply("Here's what NextStep has on file from the official pages:\n" + "\n".join(lines) +
                          "\nDeadlines can change, so confirm on the official page before you rely on them.",
                          "official", _links(["tab:scholarships"]), "scholarship_deadline", srcs)

    if COLLEGE_FACT_RE.search(q) and ctx.get("find_college"):
        c = ctx["find_college"](question)
        if c:
            return _reply(c["text"], "official", _links(["tab:matcher"]), "college_fact",
                          [{"title": "College Scorecard data (U.S. Dept. of Education)", "url": c["source_url"]}] +
                          ([{"title": c["name"] + " official website", "url": c["website"]}] if c.get("website") else []))

    if DEADLINE_RE.search(q):
        if re.search(r"\b(fafsa|tap|financial aid|state aid|federal aid)\b", q):
            best = next(e for e in kb() if e["id"] == "aid_deadlines")
            return _reply(best["answer"], best["basis"], _links(best["links"]), best["id"])
        return _reply(
            "I don't have a verified deadline for that, and I won't guess one. NextStep only gives dates it has "
            "checked on an official page. Check the official website of the college, scholarship or program, "
            "or ask your school counselor. Scholarships listed on NextStep's Scholarships tab show the deadline "
            "published on their official page, if there is one.",
            "nextstep", _links(["tab:scholarships", "fafsa_deadlines"]), "unverified_deadline")

    if ranked and ranked[0][0] >= 3:
        best = ranked[0][1]
        text = best["answer"]
        related = [e for s, e in ranked[1:3] if s >= 4 and e["id"] != best["id"]]
        if related:
            text += "\nRelated: " + "; ".join(e["title"] for e in related) + " (ask me about these too)."
        return _reply(text, best["basis"], _links(best.get("links", [])), best["id"])

    return _reply(
        "I don't have enough verified information to answer that well, and I don't want to guess. I can help with "
        "the FAFSA and New York TAP, scholarships, college search and matching, applications and essays, "
        "internships, mentors, and how NextStep works (checklist points, saving, Apply buttons). Could you "
        "rephrase or add a detail, such as which college, scholarship or step you mean? For anything about "
        "your own situation, your school counselor is a great next step.",
        "nextstep", [], None)


# --------------------------------------------------------------- dormant external model
def provider_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ask_provider(question, timeout=20):
    """Dormant path: only runs when ANTHROPIC_API_KEY is set. Returns text or raises."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("provider not configured")
    system = ("You are the NextStep help assistant for high-school students (college access, FAFSA, New York TAP, "
              "scholarships, internships, mentors). Answer ONLY from the reference material below. Never invent "
              "deadlines, amounts, schools, people, or URLs. If the material does not cover it, say you don't know "
              "and suggest the official site or a counselor. You cannot see or change the student's account.\n\n"
              "REFERENCE:\n" + json.dumps({"kb": kb(), "resources": resources()})[:60000])
    body = json.dumps({"model": os.environ.get("ASSISTANT_MODEL", "claude-haiku-4-5-20251001"), "max_tokens": 700,
                       "system": system, "messages": [{"role": "user", "content": question}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

"""Re-verify every curated outbound link (data/*.json + knowledge/resources.json)
and the two tutorial videos. Read-only: prints a report, changes nothing.

  python3 scripts/check_links.py            # all links
  python3 scripts/check_links.py --videos   # just the YouTube checks

Some sites block scripts (HTTP 403/429) while working fine in a browser; those
are reported as "blocked-to-scripts" and need a manual check, not removal.
After checking, update `last_verified` in the JSON and re-run the importer.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (NextStep link checker)"


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read(5_000_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:  # noqa: BLE001 - report any network failure
        return None, url, str(e)


def collect():
    out = []
    for p in sorted((ROOT / "data").glob("*.json")):
        doc = json.loads(p.read_text())
        for key, items in doc.items():
            if isinstance(items, list):
                for it in items:
                    for f in ("apply_url", "source_url"):
                        if it.get(f):
                            out.append((p.name, it.get("slug"), it[f]))
    res = json.loads((ROOT / "knowledge" / "resources.json").read_text())
    for k, v in res["links"].items():
        out.append(("resources.json", k, v["url"]))
    return sorted(set(out), key=lambda t: (t[0], t[1] or "", t[2]))


def check_videos():
    res = json.loads((ROOT / "knowledge" / "resources.json").read_text())
    ok = True
    for k, v in res["videos"].items():
        status, _, body = fetch("https://www.youtube.com/oembed?format=json&url=" + v["watch_url"])
        title = None
        if status == 200:
            title = json.loads(body).get("title")
        page_status, _, page = fetch(v["watch_url"])
        embeddable = '"playableInEmbed":true' in page
        if title is None and "<title>" in page:  # oEmbed refuses non-embeddable videos; use the watch page
            title = page.split("<title>", 1)[1].split("</title>", 1)[0].removesuffix(" - YouTube").strip()
        print(f"video {k}: oembed={status} title={title!r} watch={page_status} embeddable={embeddable}")
        if title is not None and title != v["verified_title"]:
            print(f"   ! title changed (stored: {v['verified_title']!r})")
            ok = False
        if embeddable != v["embeddable"]:
            print(f"   ! embeddable flag changed (stored: {v['embeddable']})")
            ok = False
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", action="store_true")
    args = ap.parse_args(argv)
    ok = check_videos()
    if args.videos:
        return 0 if ok else 1
    for src, slug, url in collect():
        status, final, _ = fetch(url)
        tag = "ok" if status == 200 else ("blocked-to-scripts" if status in (403, 429) else "CHECK")
        moved = "" if final.rstrip("/") == url.rstrip("/") else f" -> {final}"
        print(f"{tag:18} {status!s:5} {src:20} {slug:40} {url}{moved}")
        ok = ok and tag != "CHECK"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Outbound-link safety helpers shared by server.py and the import scripts.

NextStep never redirects through itself (there is no /go?url= endpoint), so
there is no open redirect. Links are handed to the browser only if they pass
safe_external_url(), and listing links must also be on the allowlist built
from the curated data files (data/*.json) and knowledge/resources.json.
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def safe_external_url(url):
    """Return the URL if it is a plain https URL to a public-looking hostname, else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2000 or any(c.isspace() for c in url) or any(c in url for c in "<>\"'`\\"):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return None
    host = (parts.hostname or "").lower()
    if not _HOST_RE.match(host) or host in ("localhost",) or host.endswith(".local"):
        return None
    return url


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


@lru_cache(maxsize=1)
def allowed_hosts():
    """Hosts of every curated link (scholarships, internships, official resources)."""
    hosts = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o.startswith("https://") and safe_external_url(o):
            hosts.add(host_of(o))

    for p in list((ROOT / "data").glob("*.json")) + [ROOT / "knowledge" / "resources.json"]:
        if p.exists():
            walk(json.loads(p.read_text(encoding="utf-8")))
    return frozenset(hosts)


def allowed_listing_url(url):
    """safe_external_url + host must be on the curated allowlist."""
    u = safe_external_url(url)
    return u if u and host_of(u) in allowed_hosts() else None

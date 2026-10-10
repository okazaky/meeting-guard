# -*- coding: utf-8 -*-
"""Meeting-link extraction. Only canonical, rebuilt https URLs are ever returned."""
import html
import re
import urllib.parse

CANDIDATE_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
# ASCII-only classes, always used with fullmatch.
ZOOM_PATH_RE = re.compile(r"/[jw]/([0-9]{1,20})")
MEET_PATH_RE = re.compile(r"/([a-z]{3}-[a-z]{4}-[a-z]{3})")
HOST_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
PWD_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
TRAILING_PUNCT = ").,;:!?]}>"


def _split_https(url):
    """urlsplit result for a plain https URL without userinfo/port, else None."""
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        return None
    if p.scheme.lower() != "https" or "@" in p.netloc or port is not None:
        return None
    if not p.hostname:
        return None
    return p


def _zoom_host_ok(host):
    if host == "zoom.us":
        return True
    if not host.endswith(".zoom.us"):
        return False
    labels = host[: -len(".zoom.us")].split(".")
    return all(HOST_LABEL_RE.fullmatch(label) for label in labels)


def canonical_zoom(url):
    """https://<host>/j/<digits>[?pwd=..] rebuilt from parts, or None."""
    p = _split_https(url)
    if not p or not _zoom_host_ok(p.hostname.lower()):
        return None
    m = ZOOM_PATH_RE.fullmatch(p.path)
    if not m:
        return None
    out = f"https://{p.hostname.lower()}/j/{m.group(1)}"
    pwd = urllib.parse.parse_qs(p.query).get("pwd", [""])[0]
    if pwd and PWD_RE.fullmatch(pwd):
        out += "?pwd=" + pwd
    return out


def canonical_meet(url):
    """https://meet.google.com/<xxx-yyyy-zzz> rebuilt from parts, or None."""
    p = _split_https(url)
    if not p or p.hostname.lower() != "meet.google.com":
        return None
    m = MEET_PATH_RE.fullmatch(p.path)
    return f"https://meet.google.com/{m.group(1)}" if m else None


def canonicalize(kind, url):
    if kind == "zoom":
        return canonical_zoom(url)
    if kind == "meet":
        return canonical_meet(url)
    return None


def link_sources(ev):
    """Texts in trust order: conferenceData video entry points > hangoutLink >
    location > description (anyone who can edit the description cannot override
    the real conference link)."""
    entry_points = (ev.get("conferenceData") or {}).get("entryPoints") or []
    video = " ".join(str(ep.get("uri") or "") for ep in entry_points
                     if isinstance(ep, dict) and ep.get("entryPointType") == "video")
    # Descriptions are often HTML: decode entities (&amp; -> &) before matching.
    description = html.unescape(str(ev.get("description") or ""))
    return [video, ev.get("hangoutLink") or "", ev.get("location") or "", description]


def link_in_text(text):
    """First canonical link in one text, Zoom preferred over Meet."""
    meet = None
    for match in CANDIDATE_RE.finditer(str(text)):
        cand = match.group(0).rstrip(TRAILING_PUNCT)
        zoom = canonical_zoom(cand)
        if zoom:
            return "zoom", zoom
        meet = meet or canonical_meet(cand)
    return ("meet", meet) if meet else (None, None)


def extract_link(ev):
    """Return ("zoom"|"meet", canonical_url) from the most trusted source, or (None, None)."""
    for text in link_sources(ev):
        kind, url = link_in_text(text)
        if kind:
            return kind, url
    return None, None


def zoom_native_url(canonical):
    """zoommtg:// URL for an already-canonical Zoom URL."""
    p = urllib.parse.urlsplit(canonical)
    confno = p.path.rsplit("/", 1)[-1]
    native = f"zoommtg://{p.hostname}/join?confno={confno}"
    pwd = urllib.parse.parse_qs(p.query).get("pwd", [""])[0]
    return native + ("&pwd=" + pwd if pwd else "")

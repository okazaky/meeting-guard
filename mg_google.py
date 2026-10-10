# -*- coding: utf-8 -*-
"""Google Calendar (read-only): token refresh, paginated listing, target filter."""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import mg_config as C
import mg_http
from mg_text import sanitize_summary

CAL_API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
MAX_PAGES = 5


def load_client():
    keys = json.loads(C.read_private_file(Path(C.CONFIG["oauth_keys"]).expanduser()))
    cred = keys.get("installed") or keys.get("web") or next(iter(keys.values()))
    return cred["client_id"], cred.get("client_secret", "")


def read_refresh_token():
    """Accepts {refresh_token: ...} or {"<profile>": {refresh_token: ...}}."""
    raw = json.loads(C.read_private_file(Path(C.CONFIG["tokens"]).expanduser()))
    profile = C.CONFIG.get("token_profile")
    if profile:
        return raw[profile]["refresh_token"]
    if "refresh_token" in raw:
        return raw["refresh_token"]
    for v in raw.values():
        if isinstance(v, dict) and v.get("refresh_token"):
            return v["refresh_token"]
    raise KeyError("refresh_token not found in tokens file")


class InvalidGrantError(Exception):
    """Google rejected the refresh token (revoked, or expired after 7 days while
    the OAuth app is in Testing status). Fix: run --auth again."""


def _oauth_error(http_error):
    """The OAuth `error` field of an HTTP error body, or ''. Never raises."""
    try:
        body = http_error.read(4096) if http_error.fp else b""
        data = json.loads(body.decode("utf-8", "replace"))
        err = data.get("error") if isinstance(data, dict) else ""
        return err if isinstance(err, str) else ""
    except Exception:
        return ""
    finally:
        try:
            http_error.close()
        except Exception:
            pass


def post_token(fields):
    data = urllib.parse.urlencode({k: v for k, v in fields.items() if v}).encode()
    try:
        return mg_http.request_json(urllib.request.Request(TOKEN_URL, data=data), 15)
    except urllib.error.HTTPError as e:
        oauth_error = _oauth_error(e)  # reads at most 4 KB, then closes
        if e.code in (400, 401) and oauth_error == "invalid_grant":
            raise InvalidGrantError("invalid_grant") from None
        raise


def get_access_token():
    client_id, client_secret = load_client()
    return post_token({"client_id": client_id, "client_secret": client_secret,
                       "refresh_token": read_refresh_token(),
                       "grant_type": "refresh_token"})["access_token"]


def api_get(path, token, params):
    url = CAL_API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    return mg_http.request_json(req, 20)


def list_events_paged(token, cal, time_min, time_max, max_results):
    """Follow nextPageToken for at most MAX_PAGES pages.
    Returns (items, truncated): truncated = a page token was still left."""
    path = f"/calendars/{urllib.parse.quote(cal, safe='')}/events"
    params = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
              "singleEvents": "true", "orderBy": "startTime",
              "maxResults": str(max_results)}
    items = []
    for _ in range(MAX_PAGES):
        res = api_get(path, token, params)
        items.extend(res.get("items", []))
        page = res.get("nextPageToken")
        if not page:
            return items, False
        params = {**params, "pageToken": page}
    return items, True


def list_events(token, cal, time_min, time_max, max_results):
    items, truncated = list_events_paged(token, cal, time_min, time_max, max_results)
    if truncated:
        C.log(f"WARNING: more than {MAX_PAGES} pages of events; the rest was not read")
    return items


class FetchResult:
    """pairs = [(calendar_id, event)]; failed = [(index, exception)];
    truncated = indexes of calendars with unread pages."""

    def __init__(self):
        self.pairs, self.failed, self.truncated = [], [], []


def fetch_events(token, now):
    """Per-calendar isolation: one failing calendar never hides the others.
    Raises the first error only when every calendar failed."""
    result = FetchResult()
    cals = C.CONFIG["calendars"]
    t_min = now - timedelta(minutes=C.CONFIG["nag_minutes"] + 20)
    for i, cal in enumerate(cals):
        try:
            items, truncated = list_events_paged(token, cal, t_min,
                                                 now + timedelta(minutes=25), 30)
        except Exception as e:
            C.log(f"CALENDAR ERROR: calendar #{i + 1}: {C.err_desc(e)}")
            result.failed.append((i, e))
            continue
        if truncated:
            C.log(f"WARNING: calendar #{i + 1}: more than {MAX_PAGES} pages; "
                  "the rest was not read")
            result.truncated.append(i)
        result.pairs.extend((cal, ev) for ev in items)
    if cals and len(result.failed) == len(cals):
        raise result.failed[0][1]
    return result


def parse_dt(s, tz_name=None):
    """Aware datetime. A naive value gets `tz_name` (IANA) or, if that is
    missing / unknown, the local time zone. ValueError for non-strings."""
    if not isinstance(s, str):
        raise ValueError("dateTime is not a string")
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        return dt
    try:
        tz = ZoneInfo(tz_name) if isinstance(tz_name, str) and tz_name else None
    except Exception:
        tz = None
    return dt.replace(tzinfo=tz) if tz else dt.astimezone()  # naive = local time


def event_start(ev):
    start = ev["start"]
    return parse_dt(start["dateTime"], start.get("timeZone"))


def self_attendee(ev):
    return next((a for a in ev.get("attendees") or [] if a.get("self")), None)


def attendance_ok(ev):
    """H1: invitations are untrusted until accepted (unless include_unaccepted)."""
    me = self_attendee(ev)
    if me and me.get("responseStatus") == "declined":
        return False
    if C.CONFIG.get("include_unaccepted"):
        return True
    if (ev.get("organizer") or {}).get("self") or (ev.get("creator") or {}).get("self"):
        return True
    if not ev.get("attendees"):
        return True
    return bool(me and me.get("responseStatus") == "accepted")


def is_target(ev):
    if ev.get("status") == "cancelled":
        return False
    if "dateTime" not in (ev.get("start") or {}):
        return False  # all-day events are not meetings
    if not attendance_ok(ev):
        return False
    if excluded(ev.get("summary")):
        return False
    return True


def excluded(raw_summary):
    """exclude_summary_contains: case-insensitive substrings of the sanitized title."""
    words = C.CONFIG.get("exclude_summary_contains") or []
    title = sanitize_summary(raw_summary, "").casefold()
    return any(w.casefold() in title for w in words if isinstance(w, str) and w)

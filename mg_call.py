# -*- coding: utf-8 -*-
"""Optional Twilio phone call, gated by a persistent call ledger (calls.json)."""
import base64
import json
import math
import re
import time
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape as xml_escape

import mg_config as C
import mg_http
import mg_secrets
from mg_text import mask_phone

LEDGER = "calls.json"
LEDGER_RETENTION_SEC = 30 * 86400
DAY_SEC = 86400
MIN_PLAUSIBLE_TS = 946684800  # 2000-01-01T00:00:00Z
# ASCII-only classes + fullmatch: no Unicode digits, no trailing newline.
TWILIO_SID_RE = re.compile(r"AC[0-9A-Za-z]{10,64}")
TWILIO_KEY_SID_RE = re.compile(r"SK[0-9A-Za-z]{10,64}")
E164_RE = re.compile(r"\+[1-9][0-9]{7,14}")

# place_call results. The event is marked "called" only for PLACED / FAILED /
# ALREADY (an attempt exists in the ledger); REFUSED may be retried next run.
PLACED, FAILED, ALREADY, REFUSED = "placed", "failed", "already", "refused"
ATTEMPTED = (PLACED, FAILED, ALREADY)


class LedgerError(Exception):
    """calls.json exists but is not in the expected shape."""


# ---------- ledger ----------

def _plausible_ts(ts, now_ts):
    """Finite epoch seconds between 2000-01-01 and one day after now."""
    return (isinstance(ts, (int, float)) and not isinstance(ts, bool)
            and math.isfinite(ts) and MIN_PLAUSIBLE_TS <= ts <= now_ts + DAY_SEC)


def _valid_attempt(a, now_ts):
    return (isinstance(a, dict) and isinstance(a.get("key"), str)
            and _plausible_ts(a.get("ts"), now_ts))


def load_ledger(now_ts=None):
    """Missing file -> empty ledger. Unreadable / insecure / malformed (incl.
    NaN / Infinity / implausible timestamps) -> exception (caller fails closed)."""
    now_ts = time.time() if now_ts is None else now_ts
    try:
        text = C.read_private_file(C.home_path(LEDGER))
    except FileNotFoundError:
        return {"attempts": []}
    data = json.loads(text, parse_constant=C.reject_constant)
    attempts = data.get("attempts") if isinstance(data, dict) else None
    if (not isinstance(attempts, list)
            or not all(_valid_attempt(a, now_ts) for a in attempts)):
        raise LedgerError("malformed call ledger")
    return {"attempts": attempts}


def ledger_refusal(ledger, key, now_ts):
    """(status, reason) when the call must not be placed, or None.
    ALREADY = this event has an attempt; REFUSED = a global limit (retry later)."""
    call_cfg = C.CONFIG["call"]
    attempts = ledger["attempts"]
    if any(a["key"] == key for a in attempts):
        return ALREADY, "a call was already attempted for this event (never retried)"
    recent = [a for a in attempts if now_ts - a["ts"] < DAY_SEC]
    if len(recent) >= int(call_cfg.get("max_per_day", 2)):
        return REFUSED, "call.max_per_day reached"
    last = max((a["ts"] for a in attempts), default=None)
    if last is not None and now_ts - last < float(call_cfg.get("min_interval_min", 30)) * 60:
        return REFUSED, "call.min_interval_min not elapsed"
    return None


def reserve_call(key, now_ts):
    """Persist the attempt BEFORE calling. Returns None when reserved, else
    ALREADY / REFUSED. Any ledger problem fails closed (REFUSED)."""
    with C.file_lock(C.home_path("calls.lock")):
        try:
            ledger = load_ledger(now_ts)
        except (OSError, ValueError, C.InsecureFileError, LedgerError) as e:
            C.log(f"call REFUSED: call ledger unusable ({C.err_desc(e)}); "
                  f"fix or remove {C.home_path(LEDGER)}")
            return REFUSED
        refusal = ledger_refusal(ledger, key, now_ts)
        if refusal:
            C.log(f"call REFUSED: {refusal[1]}")
            return refusal[0]
        kept = [a for a in ledger["attempts"] if now_ts - a["ts"] < LEDGER_RETENTION_SEC]
        payload = {"attempts": kept + [{"key": key, "ts": now_ts}]}
        try:
            C.atomic_write(C.home_path(LEDGER), json.dumps(payload, indent=1).encode())
        except OSError as e:
            C.log(f"call REFUSED: cannot write call ledger ({C.err_desc(e)})")
            return REFUSED
    return None


# ---------- Twilio ----------

def build_twiml(text):
    attr = {'"': "&quot;"}
    voice = xml_escape(str(C.CONFIG.get("twilio_voice") or "Polly.Mizuki"), attr)
    lang = xml_escape(str(C.CONFIG.get("twilio_language") or "ja-JP"), attr)
    return (f'<Response><Say voice="{voice}" language="{lang}" loop="3">'
            f'{xml_escape(text)}</Say></Response>')


def call_text(summary):
    """Titles are omitted unless call_include_summary (Twilio keeps TwiML in its logs)."""
    if C.CONFIG.get("call_include_summary"):
        return C.msg("call_say", summary=summary)
    return C.msg("call_say_generic")


def _auth_pair(s):
    """(user, password) for Basic auth; API key preferred over the account token."""
    key_sid, key_secret = s.get("TWILIO_API_KEY_SID"), s.get("TWILIO_API_KEY_SECRET")
    if key_sid and key_secret:
        return (key_sid, key_secret) if TWILIO_KEY_SID_RE.fullmatch(key_sid) else None
    token = s.get("TWILIO_AUTH_TOKEN")
    return (s.get("TWILIO_ACCOUNT_SID"), token) if token else None


def _number_problem(frm, to):
    call_cfg = C.CONFIG["call"]
    if not (E164_RE.fullmatch(to) and E164_RE.fullmatch(frm)):
        return "numbers must be E.164 (+<country><number>)"
    if frm == to:
        return "from and to are the same number"
    if not to.startswith(call_cfg.get("allowed_to_prefix") or ""):
        return f"destination {mask_phone(to)} does not match call.allowed_to_prefix"
    if not frm.startswith(call_cfg.get("allowed_from_prefix") or ""):
        return f"caller ID {mask_phone(frm)} does not match call.allowed_from_prefix"
    return None


def call_params():
    """Return (sid, (user, password), from, to) or None after logging why."""
    s = mg_secrets.load_secrets()
    sid, frm, to = (s.get("TWILIO_ACCOUNT_SID"), s.get("TWILIO_FROM_NUMBER"),
                    s.get("MEETING_GUARD_CALL_TO"))
    auth = _auth_pair(s)
    if not (sid and auth and frm and to):
        C.log("call SKIP: Twilio settings missing or invalid (env or secrets file)")
        return None
    if mg_secrets.PLACEHOLDERS & {sid, auth[1], frm, to}:
        C.log("call SKIP: example placeholder values are still set")
        return None
    if not TWILIO_SID_RE.fullmatch(sid):
        C.log("call SKIP: TWILIO_ACCOUNT_SID has an unexpected format")
        return None
    problem = _number_problem(frm, to)
    if problem:
        C.log(f"call SKIP: {problem}")
        return None
    return sid, auth, frm, to


def prepare_call(key, now_ts):
    """Settings + ledger reservation. Returns (params, None) when the call may be
    placed, else (None, status). ANY exception here (lock file, secrets file,
    unexpected data) fails closed as REFUSED: nothing was sent."""
    try:
        params = call_params()
        if not params:
            return None, REFUSED
        refused = reserve_call(key, now_ts)
        return (None, refused) if refused else (params, None)
    except Exception as e:
        C.log(f"call REFUSED: preparation failed ({C.err_desc(e)})")
        return None, REFUSED


def place_call(key, summary, now_ts=None):
    """At most one attempt per key, within global limits; never auto-retried.
    Returns PLACED / FAILED / ALREADY / REFUSED."""
    params, status = prepare_call(key, time.time() if now_ts is None else now_ts)
    if status:
        return status
    sid, (user, password), frm, to = params
    data = urllib.parse.urlencode(
        {"To": to, "From": frm, "Twiml": build_twiml(call_text(summary))}).encode()
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", data=data)
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    try:
        res = mg_http.request_json(req, 15)
        C.log(f"call placed: status={res.get('status')} to={mask_phone(to)}")
        return PLACED
    except Exception as e:
        C.log(f"call FAILED (not retried): {C.err_desc(e)}")
        return FAILED

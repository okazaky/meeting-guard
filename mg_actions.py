# -*- coding: utf-8 -*-
"""Local and push alert actions: ntfy, say, dialogs, opening links, Zoom detection."""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import mg_config as C
import mg_http
import mg_secrets
import mg_state
from mg_links import canonicalize, zoom_native_url
from mg_text import applescript_string, clean_speech

MAIN_SCRIPT = C.APP_DIR / "meeting_guard.py"
LOCAL_HTTP_HOSTS = ("127.0.0.1", "localhost")
ZOOM_START_SLACK_SEC = 2  # lstart has 1-second resolution


def ntfy_url():
    """https only; plain http is allowed for a server on this Mac."""
    server = C.CONFIG.get("ntfy_server") or "https://ntfy.sh"
    try:
        p = urllib.parse.urlsplit(server)
        host = p.hostname
    except ValueError:
        return None
    if not host or "@" in p.netloc:
        return None
    if p.scheme == "https" or (p.scheme == "http" and host in LOCAL_HTTP_HOSTS):
        return server
    return None


def push(title, message, priority):
    topic = mg_secrets.ntfy_topic()
    if not topic:
        C.log("push disabled (no ntfy topic)")
        return
    url = ntfy_url()
    if not url:
        C.log("push REFUSED: ntfy_server must be https (http only for localhost)")
        return
    headers = {"Content-Type": "application/json"}
    token = mg_secrets.ntfy_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps({"topic": topic, "title": title, "message": message,
                       "priority": priority, "tags": ["rotating_light"]}).encode()
    try:
        with mg_http.open_request(urllib.request.Request(url, data=body, headers=headers),
                                  10) as r:
            C.log(f"push {r.status}: {title}")
    except Exception as e:
        C.log(f"push FAILED: {C.err_desc(e)}")


def speak(text):
    clean = clean_speech(text)
    voice = C.CONFIG.get("say_voice")
    cmd = ["/usr/bin/say", *(["-v", str(voice)] if voice else []), "--", clean]
    subprocess.Popen(cmd, start_new_session=True)
    C.log(f"say: {clean}")


def dialog(key, text, timeout_sec, ack=True):
    """ack=False is the pre-start info dialog: clicking it does NOT stop alerts
    (a reflexive click must not disable the escalation)."""
    subprocess.Popen(
        [sys.executable, str(MAIN_SCRIPT), "--dialog",
         key, text, str(timeout_sec), "1" if ack else "0"],
        start_new_session=True)
    C.log(f"dialog(ack={ack}): {text.splitlines()[0] if text else ''}")


def run_dialog(key, text, timeout_sec, write_ack):
    """--dialog child process. Only write_ack dialogs record an ack on click."""
    btn = applescript_string(C.msg("button_ack") if write_ack else C.msg("button_ok"))
    title = applescript_string(C.msg("dialog_title"))
    script = (f'display dialog "{applescript_string(text)}" with title "{title}" '
              f'buttons {{"{btn}"}} default button 1 '
              f'with icon caution giving up after {int(timeout_sec)}')
    res = subprocess.run(["/usr/bin/osascript", "-e", script],
                         capture_output=True, text=True)
    if write_ack and "gave up:false" in res.stdout:
        mg_state.append_ack(key)


def _run_open(target):
    try:
        return subprocess.run(["/usr/bin/open", target], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_link(kind, url):
    """Open only the canonical rebuilt URL (Zoom via the native app when possible).
    Returns True only when /usr/bin/open succeeded."""
    canonical = canonicalize(kind, url)
    if not canonical:
        C.log(f"open REFUSED: {kind} link is not canonical")
        return False
    target = zoom_native_url(canonical) if kind == "zoom" else canonical
    ok = _run_open(target)
    if not ok and target != canonical:
        ok = _run_open(canonical)
    C.log(f"opened: {kind} link" if ok else f"open FAILED: {kind} link")
    return ok


MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
WEEKDAYS = "Mon Tue Wed Thu Fri Sat Sun".split()


def parse_ps_line(line):
    """'<pid> <lstart>' printed with LC_ALL=C and TZ=UTC (e.g.
    "123 Sat Oct 10 09:32:40 2026") -> (pid, start Unix time), or None.
    lstart is the absolute start time kept by the OS; printing it in UTC avoids
    the repeated hour at the end of daylight saving time. Impossible dates,
    times or weekdays are rejected (None), never normalised."""
    parts = line.split()
    if len(parts) != 6 or not all(p.isascii() for p in parts):
        return None
    pid, wday, mon, day, hms, year = parts
    hms_parts = hms.split(":")
    nums = [pid, day, year] + hms_parts
    if mon not in MONTHS or len(hms_parts) != 3 or not all(n.isdigit() for n in nums):
        return None
    try:
        dt = datetime(int(year), MONTHS[mon], int(day), *(int(x) for x in hms_parts),
                      tzinfo=timezone.utc)
    except (OverflowError, ValueError):
        return None
    if WEEKDAYS[dt.weekday()] != wday:
        return None
    return int(pid), dt.timestamp()


def _cpthost_procs():
    """(pid, start time) of each CptHost process of this user; [] if none.
    Raises ValueError when pgrep / ps fail or print something unexpected, so a
    failure is never mistaken for "no Zoom running"."""
    found = subprocess.run(["/usr/bin/pgrep", "-u", str(os.getuid()), "-x", "CptHost"],
                           capture_output=True, text=True, timeout=10)
    if found.returncode == 1 and not found.stdout.strip():
        return []  # pgrep: no process matched
    pids = found.stdout.split()
    if found.returncode != 0 or not pids or not all(p.isascii() and p.isdigit() for p in pids):
        raise ValueError(f"pgrep failed (rc={found.returncode})")
    ps = subprocess.run(["/bin/ps", "-o", "pid=,lstart=", "-p", ",".join(pids)],
                        capture_output=True, text=True, timeout=10,
                        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    lines = [ln for ln in ps.stdout.splitlines() if ln.strip()]
    procs = [parse_ps_line(ln) for ln in lines]
    if (ps.returncode != 0 or not procs or None in procs
            or not {pid for pid, _ in procs} <= {int(p) for p in pids}):
        raise ValueError(f"ps failed (rc={ps.returncode})")
    return procs


def cpthost_snapshot():
    """[[pid, start_ts], ...] of the CptHost processes running now; None on failure."""
    try:
        return [[pid, start] for pid, start in _cpthost_procs()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _in_snapshot(pid, start_ts, snapshot):
    """Same pid and the same start time: the process existed before the open."""
    return any(pid == s_pid and abs(start_ts - s_start) <= ZOOM_START_SLACK_SEC
               for s_pid, s_start in snapshot)


def zoom_in_meeting(since_ts, exclude=None):
    """True only if a Zoom in-meeting process (CptHost, this user) started at or
    after `since_ts` and is not in `exclude` (a cpthost_snapshot taken before the
    link was opened). A CptHost left over from an earlier meeting does not count.
    Any failure = False (keep nagging)."""
    try:
        procs = _cpthost_procs()
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    for pid, start_ts in procs:
        if start_ts < since_ts - ZOOM_START_SLACK_SEC:
            continue
        if exclude and _in_snapshot(pid, start_ts, exclude):
            continue
        return True
    return False

# -*- coding: utf-8 -*-
"""Per-event state (key = calendarId|eventId) and acknowledgements."""
import json
import math
import os
from datetime import timedelta

import mg_config as C
from mg_google import parse_dt


def event_key(cal, ev):
    return f"{cal}|{ev['id']}"


def retention():
    return timedelta(minutes=C.CONFIG["nag_minutes"] + 30)


def load_state():
    try:
        data = json.loads(C.read_nofollow(C.home_path("state.json")),
                          parse_constant=C.reject_constant)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state):
    C.atomic_write(C.home_path("state.json"),
                   json.dumps(state, ensure_ascii=False, indent=1).encode())


def prune(state, now):
    """Keep `_meta` and entries whose stored start is within the retention window."""
    keep = {}
    for k, v in state.items():
        if k.startswith("_"):
            keep[k] = v
            continue
        try:
            start = parse_dt(v["start"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if now - start < retention():
            keep[k] = v
    return keep


def load_acks():
    try:
        text = C.read_nofollow(C.home_path("acks.txt"))
    except (OSError, ValueError):
        return set()
    return {line for line in text.splitlines() if line}


def append_ack(key):
    if not key or "\n" in key or "\r" in key:
        return
    problem = C.home_problem()  # --dialog child: re-validate before writing
    if problem:
        C.LOG_TO_FILE = False
        C.log(f"ack NOT recorded: unsafe MEETING_GUARD_HOME ({problem})")
        return
    with C.file_lock(C.home_path("acks.lock")):
        with C.open_nofollow(C.home_path("acks.txt"),
                             os.O_WRONLY | os.O_APPEND | os.O_CREAT, "a") as f:
            f.write(key + "\n")


def prune_acks(keep_keys):
    """Acks share the state retention: drop acks whose state entry was pruned."""
    with C.file_lock(C.home_path("acks.lock")):
        acks = load_acks()
        kept = sorted(acks & set(keep_keys))
        if len(kept) == len(acks):
            return
        text = "".join(k + "\n" for k in kept)
        C.atomic_write(C.home_path("acks.txt"), text.encode("utf-8"))


def alert_due(name, now_ts, interval_sec):
    """Rate limit for local warnings: one tiny file `.alert-<name>` in
    MEETING_GUARD_HOME holds the last alert time. Returns True (and records now)
    when an alert may be shown. Unreadable / invalid file = alert is due."""
    path = C.home_path(f".alert-{name}")
    try:
        last = float(C.read_nofollow(path).strip())
        if math.isfinite(last) and 0 <= now_ts - last < interval_sec:
            return False
    except (OSError, ValueError):
        pass
    try:
        C.atomic_write(path, repr(float(now_ts)).encode())
    except OSError as e:
        C.log(f"WARNING: cannot record alert time ({C.err_desc(e)})")
    return True

# -*- coding: utf-8 -*-
"""Alert stages, post-start escalation and the per-minute polling pass.

Rule: local alarms (voice / dialog / push) always run before, and never depend
on, the optional phone-call path; a failure in one event or one calendar never
silences the others.
"""
import fcntl
import os
from datetime import datetime, timezone

import mg_actions as A
import mg_call
import mg_config as C
import mg_google
import mg_state
from mg_links import extract_link
from mg_text import sanitize_summary

STAGE_ORDER = C.STAGE_ORDER
WINDOW_AHEAD_MIN = 11
# A CptHost older than these thresholds is a leftover from an earlier meeting.
ZOOM_OPEN_SLACK_SEC = 60    # open0 opened the link: CptHost >= open0 fire time - 60s
ZOOM_START_LEAD_SEC = 120   # otherwise: CptHost >= event start - 2 min
ERROR_ALERT_INTERVAL_SEC = 3600
ERROR_DIALOG_KEY = "error|meeting-guard"


def stage_minutes(stage):
    return int(C.CONFIG["stages"][stage])


def push_fields(f):
    """push_include_summary=false keeps titles off the ntfy server."""
    if C.CONFIG.get("push_include_summary", True):
        return f
    return {**f, "summary": C.msg("push_generic_summary")}


def push_msg(title_key, body_key, f, priority):
    pf = push_fields(f)
    A.push(C.msg(title_key, **pf), C.msg(body_key, **pf), priority)


def safe_open(kind, url):
    try:
        return bool(A.open_link(kind, url))
    except Exception as e:
        C.log(f"open ERROR: {C.err_desc(e)}")
        return False


def fire_open(ctx):
    """Returns True only when the meeting link was actually opened."""
    f = {"summary": ctx["summary"], "hhmm": ctx["hhmm"], "minutes": 0}
    kind = ctx["kind"]
    opened = False
    if kind and C.CONFIG.get("auto_open", True):
        opened = safe_open(kind, ctx["url"])
        if opened:
            A.speak(C.msg("open_say_opened", **f))
        else:
            A.speak(C.msg("open_failed_say", **f))
            A.dialog(ctx["key"], C.msg("open_dialog_manual", **f), 240)
    elif kind:
        A.speak(C.msg("open_say", **f))
        A.dialog(ctx["key"], C.msg("open_dialog_manual", **f), 240)
    else:
        # No link = in-person event: voice only, no post-start escalation.
        A.speak(C.msg("open_say", **f))
    push_msg("open_title", "open_body", f, 5)
    return opened


def fire_stage(stage, ctx):
    """Returns True only when open0 actually opened the meeting link."""
    C.log(f"stage {stage}: {ctx['summary']} ({ctx['hhmm']})")
    f = {"summary": ctx["summary"], "hhmm": ctx["hhmm"], "minutes": stage_minutes(stage)}
    if stage == "push10":
        push_msg("push10_title", "push10_body", f, 4)
    elif stage == "voice5":
        A.speak(C.msg("voice5_say", **f))
        push_msg("voice5_title", "voice5_body", f, 4)
    elif stage == "dialog1":
        A.speak(C.msg("dialog1_say", **f))
        A.dialog(ctx["key"], C.msg("dialog1_text", **f), 240, ack=False)
    elif stage == "open0":
        return fire_open(ctx)
    return False


def nag(ctx, elapsed):
    f = {"summary": ctx["summary"], "hhmm": ctx["hhmm"], "minutes": elapsed}
    C.log(f"nag: {ctx['summary']} ({elapsed} min after start)")
    A.speak(C.msg("nag_say", **f))
    A.dialog(ctx["key"], C.msg("nag_dialog", **f), 100)
    push_msg("nag_title", "nag_body", f, 5)


def zoom_since(ctx, st):
    """Earliest CptHost start time that counts as joining THIS meeting:
    open0 opened the link -> open0 fire time - 60s; else event start - 2 min."""
    opened_at = st.get("open0_at")
    if (st.get("open0_opened") is True and isinstance(opened_at, (int, float))
            and not isinstance(opened_at, bool)):
        return opened_at - ZOOM_OPEN_SLACK_SEC
    return ctx["start_ts"] - ZOOM_START_LEAD_SEC


PID_MAX = 2 ** 31
TS_MAX = 1e11  # far beyond any real Unix time; also rejects inf / nan


def _snapshot_entry_ok(x):
    """[pid, start_ts] from state.json. Range checks compare without converting
    huge integers to float (no OverflowError)."""
    if not (isinstance(x, list) and len(x) == 2):
        return False
    pid, start = x
    return (isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < PID_MAX
            and isinstance(start, (int, float)) and not isinstance(start, bool)
            and 0 <= start <= TS_MAX)


def zoom_exclude(st):
    """CptHost processes that were already running when open0 opened the link
    (they belong to an earlier meeting). None = unknown / not opened by open0."""
    snap = st.get("open0_zoom_pre")
    if st.get("open0_opened") is not True or not isinstance(snap, list):
        return None
    if not all(_snapshot_entry_ok(x) for x in snap):
        return None
    return snap


def in_zoom(ctx, st, now):
    """Joined Zoom? When open0 opened the link but the record of processes that
    were already running is missing or broken, we cannot tell an old meeting
    from this one: keep nagging (False)."""
    if ctx["kind"] != "zoom":
        return False
    if st.get("open0_opened") is True:
        exclude = zoom_exclude(st)
        if exclude is None:
            return False
        return A.zoom_in_meeting(zoom_since(ctx, st), exclude=exclude)
    return A.zoom_in_meeting(zoom_since(ctx, st))


def zoom_snapshot():
    """Never raises: a failure only means "keep nagging" later (see in_zoom)."""
    try:
        return A.cpthost_snapshot()
    except Exception as e:
        C.log(f"zoom snapshot ERROR: {C.err_desc(e)}")
        return None


def escalation_allowed(ctx):
    return bool(ctx["kind"]) or bool(C.CONFIG.get("escalate_linkless", False))


def maybe_call(st, mins, now, ctx):
    """Call once after after_min. The event is marked only when an attempt exists
    in the ledger (placed / failed / already); a limit refusal may retry next run.
    Never raises: the call path can not suppress local alarms."""
    call_cfg = C.CONFIG["call"]
    if (not call_cfg.get("enabled") or st.get("called")
            or -mins < call_cfg.get("after_min", 2)):
        return False
    try:
        result = mg_call.place_call(ctx["key"], ctx["summary"], now.timestamp())
    except Exception as e:
        C.log(f"call ERROR (local alarms unaffected): {C.err_desc(e)}")
        return False
    if result in mg_call.ATTEMPTED:
        st["called"] = True  # a real attempt exists: never retried
    return result != mg_call.REFUSED


def escalate(st, mins, now, ctx):
    """Post-start: periodic nags (first) + one phone call until joined/acked."""
    if in_zoom(ctx, st, now):
        return False
    acted = False
    if now.timestamp() - st["last_alert"] >= C.CONFIG["nag_interval_sec"]:
        st["last_alert"] = now.timestamp()
        acted = True
        try:
            nag(ctx, int(-mins))
        except Exception as e:
            C.log(f"nag ERROR: {C.err_desc(e)}")
    return maybe_call(st, mins, now, ctx) or acted


def in_window(mins):
    return -C.CONFIG["nag_minutes"] <= mins <= WINDOW_AHEAD_MIN


def event_context(cal, ev, start):
    kind, url = extract_link(ev)
    return {"key": mg_state.event_key(cal, ev),
            "summary": sanitize_summary(ev.get("summary"), C.msg("untitled")),
            "hhmm": start.astimezone().strftime("%H:%M"), "kind": kind, "url": url,
            "start_ts": start.timestamp()}


def note_skipped(cal, ev, start, mins, state):
    """First seen after the nag window (e.g. long sleep): log once, never alert.
    `skipped_at_start` remembers which start was skipped (see rearm_if_moved)."""
    key = mg_state.event_key(cal, ev)
    if mins >= -C.CONFIG["nag_minutes"] or key in state:
        return
    state[key] = {"start": start.isoformat(), "fired": list(STAGE_ORDER),
                  "last_alert": 0, "called": False,
                  "skipped_at_start": start.isoformat()}
    C.log(f"skipped: event first seen {int(-mins)} min after start "
          f"(beyond nag_minutes={C.CONFIG['nag_minutes']})")


def _same_instant(iso, start):
    try:
        return mg_google.parse_dt(iso) == start
    except (TypeError, ValueError):
        return False


REARM_DROP = ("fired", "last_alert", "skipped_at_start", "open0_at", "open0_opened",
              "open0_zoom_pre")


def rearm_if_moved(prev, start):
    """A skipped (never alerted) event whose start changed gets its stages back.
    Called only inside the alert window, so a new start that already passed but
    is within nag_minutes runs the normal catch-up (open0, then nag / call).
    call / ack records are kept. (A legacy `skipped: true` flag is not honoured.)"""
    skipped = prev.get("skipped_at_start")
    if not skipped or _same_instant(skipped, start):
        return prev
    C.log("skipped event was rescheduled into the alert window: alerts re-armed")
    return {k: v for k, v in prev.items() if k not in REARM_DROP}


def catch_up(applicable, st, mins, now, ctx, acks):
    """Fire only the most urgent pending stage. If the meeting already started
    (e.g. wake after sleep), the call path runs in this same pass, after it."""
    if "open0" in applicable and ctx["kind"] == "zoom":
        # before opening: Zoom processes running now are not this meeting
        st["open0_zoom_pre"] = zoom_snapshot()
    opened = fire_stage(applicable[-1], ctx)
    st["fired"] = st["fired"] + applicable
    st["last_alert"] = now.timestamp()
    if "open0" in applicable:
        st["open0_at"] = now.timestamp()
        st["open0_opened"] = opened is True
    if (mins <= 0 and "open0" in applicable and escalation_allowed(ctx)
            and ctx["key"] not in acks and not in_zoom(ctx, st, now)):
        maybe_call(st, mins, now, ctx)
    return True


def process_event(cal, ev, now, state, acks, progress=None):
    """Run one event. State survives rescheduling (fired/called/ack keep). True if
    acted. progress["started"] is set before any action may run (cap counting)."""
    progress = {} if progress is None else progress
    start = mg_google.event_start(ev)
    mins = (start - now).total_seconds() / 60
    if not in_window(mins):
        note_skipped(cal, ev, start, mins, state)
        return False
    ctx = event_context(cal, ev, start)
    prev = rearm_if_moved(state.get(ctx["key"]) or {}, start)
    st = {"fired": [], "last_alert": 0, "called": False, **prev, "start": start.isoformat()}
    state[ctx["key"]] = st
    applicable = [s for s in STAGE_ORDER
                  if mins <= C.CONFIG["stages"][s] and s not in st["fired"]]
    acted = False
    if applicable:
        progress["started"] = True
        acted = catch_up(applicable, st, mins, now, ctx, acks)
    elif (escalation_allowed(ctx) and mins <= 0 and "open0" in st["fired"]
          and ctx["key"] not in acks):
        progress["started"] = True
        acted = escalate(st, mins, now, ctx)
    if acted:
        st.pop("skipped_at_start", None)
    return acted


def _distance(pair, now):
    return abs((mg_google.event_start(pair[1]) - now).total_seconds())


def target_pairs(pairs, now):
    """Filter (per-event try) and sort nearest start first."""
    out = []
    for cal, ev in pairs:
        try:
            if mg_google.is_target(ev):
                out.append(((cal, ev), _distance((cal, ev), now)))
        except Exception as e:
            C.log(f"event ERROR (filter): {C.err_desc(e)}")
    return [p for p, _ in sorted(out, key=lambda x: x[1])]


def run_events(pairs, now, state, acks):
    """At most max_events_per_run events produce actions in one pass. An event
    whose actions started counts even if it then raised."""
    cap = int(C.CONFIG.get("max_events_per_run", 3))
    acted = 0
    for cal, ev in target_pairs(pairs, now):
        if acted >= cap:
            C.log(f"max_events_per_run={cap} reached; remaining events deferred")
            break
        progress = {"started": False}
        try:
            did = process_event(cal, ev, now, state, acks, progress)
        except Exception as e:
            C.log(f"event ERROR: {C.err_desc(e)}")
            did = progress["started"]
        acted += 1 if did else 0
    return acted


# ---------- local warnings (alarms are not fully working) ----------

def local_alarm(dialog_id, say_key, dialog_key, timeout=240, **fields):
    """Voice + OK-only dialog. Never raises (logged instead)."""
    try:
        A.speak(C.msg(say_key, **fields))
        A.dialog(dialog_id, C.msg(dialog_key, **fields), timeout, ack=False)
    except Exception as e:
        C.log(f"local alarm ERROR: {C.err_desc(e)}")


def alert_api_error(state, now, err):
    """Calendar / auth / token failure: alarms are not working. Tell the user
    locally (voice + non-ack dialog) and by push if configured, at most hourly.
    invalid_grant gets its own "run --auth" message."""
    meta = state.setdefault("_meta", {})
    if now.timestamp() - meta.get("last_error_alert", 0) < ERROR_ALERT_INTERVAL_SEC:
        return
    meta["last_error_alert"] = now.timestamp()
    if isinstance(err, mg_google.InvalidGrantError):
        error = "invalid_grant"
        local_alarm(ERROR_DIALOG_KEY, "auth_expired_say", "auth_expired_dialog")
    else:
        error = err.__class__.__name__
        local_alarm(ERROR_DIALOG_KEY, "error_say", "error_dialog", error=error)
    try:
        A.push(C.msg("error_title"), C.msg("error_body", error=error), 4)
    except Exception as e:
        C.log(f"push ERROR: {C.err_desc(e)}")


def alert_config_problems(now):
    if C.CONFIG_PROBLEMS and mg_state.alert_due("config", now.timestamp(),
                                                ERROR_ALERT_INTERVAL_SEC):
        local_alarm("error|config", "config_say", "config_dialog",
                    error=", ".join(C.CONFIG_PROBLEMS)[:200])


def alert_fetch_warnings(result, now):
    """Some calendars failed / were truncated: the rest is still processed."""
    ts = now.timestamp()
    if result.failed and mg_state.alert_due("calendar", ts, ERROR_ALERT_INTERVAL_SEC):
        local_alarm("error|calendar", "calendar_error_say", "calendar_error_dialog",
                    count=len(result.failed),
                    error=result.failed[0][1].__class__.__name__)
    if result.truncated and mg_state.alert_due("truncated", ts, ERROR_ALERT_INTERVAL_SEC):
        local_alarm("error|truncated", "truncated_say", "truncated_dialog")


def alert_unsafe_home(problem, home_untrusted=True):
    """MEETING_GUARD_HOME (or its run lock) failed the safety check. When the
    directory itself is untrusted nothing more is written there (log -> stderr)."""
    if home_untrusted:
        C.LOG_TO_FILE = False
    C.log(f"UNSAFE MEETING_GUARD_HOME: {problem}; exiting")
    local_alarm("error|home", "home_say", "home_dialog", timeout=50, error=problem)


# ---------- polling pass ----------

def poll(now):
    """Body of run_once, called with the run lock held."""
    state, acks = mg_state.load_state(), mg_state.load_acks()
    alert_config_problems(now)
    try:
        result = mg_google.fetch_events(mg_google.get_access_token(), now)
    except Exception as e:
        C.log(f"API ERROR: {C.err_desc(e)}")
        alert_api_error(state, now, e)
        mg_state.save_state(state)
        return
    alert_fetch_warnings(result, now)
    run_events(result.pairs, now, state, acks)
    pruned = mg_state.prune(state, now)
    mg_state.save_state(pruned)
    mg_state.prune_acks(k for k in pruned if not k.startswith("_"))


def run_once(now=None):
    """One polling pass (launchd). Skips if the previous pass is still running.
    Returns 1 (after a local alarm) when MEETING_GUARD_HOME is unsafe."""
    now = now or datetime.now(timezone.utc)
    problem = C.home_problem()
    if problem:
        alert_unsafe_home(problem)
        return 1
    try:
        lock = C.open_nofollow(C.home_path(".lock"), os.O_WRONLY | os.O_CREAT, "w")
    except OSError as e:
        alert_unsafe_home(f".lock cannot be opened ({C.err_desc(e)})", home_untrusted=False)
        return 1
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        poll(now)
    return 0

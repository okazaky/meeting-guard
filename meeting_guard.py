#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""meeting-guard: escalating meeting alarms for macOS (Zoom / Google Meet).

launchd runs this script every minute. For the nearest Google Calendar
events it fires escalating alerts:

  10 min before   push notification (ntfy, opt-in)
   5 min before   macOS voice (say) + push
   1 min before   information dialog (clicking it does NOT stop alerts)
   start          open the Zoom / Meet link + voice + push
   after start    nag (voice + dialog + push) every nag_interval_sec until
                  the "joined" dialog button is clicked or Zoom's in-meeting
                  process (CptHost) is detected, for at most nag_minutes
   +2 min         one Twilio phone call (optional, limited by calls.json)

Events without a Zoom / Meet link only get the pre-start reminders.
Invitations you have not accepted are ignored unless include_unaccepted=true.

Usage:
  meeting_guard.py               one polling pass (launchd)
  meeting_guard.py --auth        Google OAuth (loopback + PKCE, read-only scope)
  meeting_guard.py --check       verify auth and list meetings in the next 24h
  meeting_guard.py --simulate    fire every stage locally for a fake event
                                 (voice + dialogs; push only if configured; no call)
  meeting_guard.py --test-call   place one test phone call (subject to call limits)

Python 3.9+, standard library only.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import mg_call
import mg_config as C
import mg_engine
import mg_google
import mg_oauth
import mg_secrets
from mg_actions import run_dialog
from mg_links import extract_link
from mg_text import sanitize_summary

SIM_SUMMARY = "meeting-guard simulation"
SIM_KEY = "simulation|meeting-guard"


def check():
    now = datetime.now(timezone.utc)
    for problem in C.CONFIG_PROBLEMS:
        print(f"config ERROR: {problem} (default used, phone call disabled)")
    token = mg_google.get_access_token()
    print("token: OK")
    print(f"push: {'enabled' if mg_secrets.ntfy_topic() else 'disabled (no ntfy topic)'}")
    for cal in C.CONFIG["calendars"]:
        items = [e for e in mg_google.list_events(token, cal, now,
                                                  now + timedelta(hours=24), 50)
                 if mg_google.is_target(e)]
        print(f"calendar '{cal}': {len(items)} candidate(s) in the next 24h")
        for ev in items:
            kind, _ = extract_link(ev)
            print(f"  - {ev['start']['dateTime']}  "
                  f"{sanitize_summary(ev.get('summary'), C.msg('untitled'))}"
                  f"  link={kind or 'none'}")


def simulate(pause_sec=4.0, sleep=time.sleep):
    """Fire each stage locally for a fake event. Never places a phone call."""
    now = datetime.now(timezone.utc)
    ctx = {"key": SIM_KEY, "summary": SIM_SUMMARY, "kind": None, "url": None,
           "hhmm": now.astimezone().strftime("%H:%M")}
    for stage in mg_engine.STAGE_ORDER:
        print(f"simulate: {stage}")
        mg_engine.fire_stage(stage, ctx)
        sleep(pause_sec)
    print("simulate: nag")
    mg_engine.nag(ctx, 1)
    print("simulate: done (no phone call is placed)")


def test_call():
    key = f"test-call|{int(time.time())}"
    result = mg_call.place_call(key, C.msg("test_call_summary"))
    print("call: placed" if result == mg_call.PLACED
          else f"call: not placed ({result}; see logs/guard.log)")


COMMANDS = {"--auth": mg_oauth.run_auth, "--check": check,
            "--simulate": simulate, "--test-call": test_call}
# commands that write into MEETING_GUARD_HOME (state / calls.json / acks)
HOME_WRITERS = ("--simulate", "--test-call")


def cli(argv):
    os.umask(0o077)
    C.setup()  # validates MEETING_GUARD_HOME before any write
    if len(argv) > 5 and argv[1] == "--dialog":
        run_dialog(argv[2], argv[3], int(argv[4]), argv[5] == "1")
        return
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd in HOME_WRITERS and C.HOME_PROBLEM:
        mg_engine.alert_unsafe_home(C.HOME_PROBLEM)
        return 1
    if cmd in COMMANDS:
        COMMANDS[cmd]()
    elif cmd:
        print(__doc__)
        sys.exit(2)
    else:
        return mg_engine.run_once()
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv) or 0)

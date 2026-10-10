"""Stages, escalation, H1 invite filter, H3 state keys, M4 per-run cap, --simulate."""
import json
from datetime import timedelta
from unittest import mock

from mgtest_helpers import CAL, MEET_URL, NOW, ZOOM_URL, Base, make_event

import meeting_guard
import mg_call
import mg_config
import mg_engine
import mg_google
import mg_state


def run(ev, minutes_offset, state, acks=(), cal=CAL):
    now = NOW + timedelta(minutes=minutes_offset)
    return mg_engine.process_event(cal, ev, now, state, set(acks))


class StageTests(Base):
    def test_ten_minutes_fires_push_only(self):
        self.assertTrue(run(make_event(10), 0, {}))
        self.assertEqual(self.names(), ["push"])

    def test_catch_up_fires_only_most_urgent(self):
        state = {}
        run(make_event(0.5), 0, state)
        self.assertIn("open_link", self.names())
        self.assertEqual(self.names().count("push"), 1)
        self.assertEqual(state[CAL + "|ev1"]["fired"], mg_engine.STAGE_ORDER)
        self.actions.clear()
        self.assertFalse(run(make_event(0.5), 0, state))
        self.assertEqual(self.names(), [])

    def test_out_of_window_ignored(self):
        run(make_event(30), 0, {})
        run(make_event(-30), 0, {})
        self.assertEqual(self.names(), [])

    def test_pre_start_dialog_is_not_ack(self):
        run(make_event(1), 0, {})
        dialogs = [a for a in self.actions if a[0] == "dialog"]
        self.assertEqual(len(dialogs), 1)
        self.assertFalse(dialogs[0][2].get("ack", True))

    def test_open_passes_canonical_url(self):
        run(make_event(0, location="Join: https://zoom.us/j/0000000000?pwd=abc&x=1 now"), 0, {})
        opened = [a for a in self.actions if a[0] == "open_link"][0]
        self.assertEqual(opened[1], ("zoom", "https://zoom.us/j/0000000000?pwd=abc"))

    def test_push_include_summary_false_hides_title(self):
        self.configure({"push_include_summary": False})
        run(make_event(10, summary="Secret merger"), 0, {})
        title, body = self.actions[0][1][:2]
        self.assertNotIn("Secret merger", title + body)
        self.assertIn("会議", title)

    def test_push_include_summary_default_true(self):
        run(make_event(10, summary="Weekly sync"), 0, {})
        self.assertIn("Weekly sync", self.actions[0][1][0])


class EscalationTests(Base):
    def start_meeting(self, location=ZOOM_URL):
        ev = make_event(0, location=location)
        state = {}
        run(ev, 0, state)
        self.actions.clear()
        return ev, state

    def test_nag_after_start(self):
        ev, state = self.start_meeting()
        run(ev, 2, state)
        for name in ("speak", "dialog", "push"):
            self.assertIn(name, self.names())
        nag_dialog = [a for a in self.actions if a[0] == "dialog"][0]
        self.assertEqual(len(nag_dialog[1]), 3)  # ack defaults to True
        self.assertEqual(nag_dialog[1][0], CAL + "|ev1")

    def test_nag_respects_interval(self):
        ev, state = self.start_meeting()
        run(ev, 2, state)
        self.actions.clear()
        run(ev, 2.5, state)
        self.assertNotIn("speak", self.names())

    def test_ack_stops_nag(self):
        ev, state = self.start_meeting()
        run(ev, 3, state, acks=[CAL + "|ev1"])
        self.assertEqual(self.names(), [])

    def test_zoom_detection_stops_nag(self):
        ev, state = self.start_meeting()
        self.zoom_check.return_value = True
        run(ev, 3, state)
        self.assertEqual(self.names(), [])

    def test_call_once_only_with_event_key(self):
        ev, state = self.start_meeting(location=MEET_URL)
        for t in (2, 4, 6, 8):
            run(ev, t, state)
        calls = [a for a in self.actions if a[0] == "place_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1][0], CAL + "|ev1")

    def test_no_call_before_after_min(self):
        ev, state = self.start_meeting()
        run(ev, 1, state)
        self.assertNotIn("place_call", self.names())

    def test_call_disabled(self):
        self.configure({"call": {"enabled": False}})
        ev, state = self.start_meeting()
        run(ev, 3, state)
        self.assertNotIn("place_call", self.names())

    def test_linkless_no_escalation(self):
        ev, state = self.start_meeting(location="Conference room B")
        for t in (2, 4, 6):
            run(ev, t, state)
        self.assertEqual(self.names(), [])

    def test_linkless_still_gets_reminders(self):
        run(make_event(5, location=""), 0, {})
        self.assertEqual(self.names(), ["speak", "push"])

    def test_linkless_start_does_not_open(self):
        run(make_event(0, location=""), 0, {})
        self.assertNotIn("open_link", self.names())
        self.assertNotIn("dialog", self.names())


class InviteFilterTests(Base):
    """H1: untrusted invitations are ignored unless accepted."""

    def ev(self, **extra):
        return make_event(5, **extra)

    def test_no_attendees_is_target(self):
        self.assertTrue(mg_google.is_target(self.ev()))

    def test_unaccepted_invite_ignored(self):
        for status in ("needsAction", "tentative"):
            ev = self.ev(attendees=[{"self": True, "responseStatus": status},
                                    {"responseStatus": "accepted"}])
            self.assertFalse(mg_google.is_target(ev), status)

    def test_invite_without_self_entry_ignored(self):
        self.assertFalse(mg_google.is_target(self.ev(attendees=[{"responseStatus": "accepted"}])))

    def test_accepted_invite_is_target(self):
        ev = self.ev(attendees=[{"self": True, "responseStatus": "accepted"}])
        self.assertTrue(mg_google.is_target(ev))

    def test_organizer_or_creator_is_target(self):
        att = [{"self": True, "responseStatus": "needsAction"}, {"responseStatus": "accepted"}]
        self.assertTrue(mg_google.is_target(self.ev(attendees=att, organizer={"self": True})))
        self.assertTrue(mg_google.is_target(self.ev(attendees=att, creator={"self": True})))

    def test_declined_always_ignored(self):
        self.configure({"include_unaccepted": True})
        ev = self.ev(attendees=[{"self": True, "responseStatus": "declined"}],
                     organizer={"self": True})
        self.assertFalse(mg_google.is_target(ev))

    def test_include_unaccepted_opt_in(self):
        self.configure({"include_unaccepted": True})
        ev = self.ev(attendees=[{"self": True, "responseStatus": "needsAction"}])
        self.assertTrue(mg_google.is_target(ev))

    def test_default_include_unaccepted_false(self):
        self.assertFalse(mg_config.DEFAULT_CONFIG["include_unaccepted"])

    def test_other_filters(self):
        self.assertFalse(mg_google.is_target({"status": "cancelled", "start": {"dateTime": "x"}}))
        self.assertFalse(mg_google.is_target({"start": {"date": "2026-01-05"}}))

    def test_exclude_contains_case_insensitive_on_sanitized_summary(self):
        """R7: list of case-insensitive substrings; no regex anywhere."""
        self.configure({"exclude_summary_contains": ["lunch", "Focus Time"]})
        self.assertFalse(mg_google.is_target(self.ev(summary="--" + chr(0x200B) + "LUNCH")))
        self.assertFalse(mg_google.is_target(self.ev(summary="Team lunch")))
        self.assertFalse(mg_google.is_target(self.ev(summary="focus time (auto)")))
        self.assertTrue(mg_google.is_target(self.ev(summary="Weekly sync")))

    def test_exclude_contains_treats_regex_chars_literally(self):
        self.configure({"exclude_summary_contains": ["(a+)+$", ".*"]})
        self.assertTrue(mg_google.is_target(self.ev(summary="aaaaaaaaaaaaaaaaaaaaaaaa!")))
        self.assertFalse(mg_google.is_target(self.ev(summary="x .* y")))

    def test_regex_option_removed(self):
        self.assertNotIn("exclude_summary_regex", mg_config.DEFAULT_CONFIG)
        self.assertFalse(hasattr(mg_config, "EXCLUDE_RE"))
        self.configure({"exclude_summary_regex": "^Weekly"})
        self.assertTrue(mg_google.is_target(self.ev()))
        self.assertIn("exclude_summary_regex is no longer supported", self.log_text())

    def test_invalid_exclude_list_is_config_problem(self):
        self.configure({"exclude_summary_contains": "lunch"})
        self.assertIn("exclude_summary_contains", mg_config.CONFIG_PROBLEMS)
        self.assertTrue(mg_google.is_target(self.ev(summary="lunch")))


class StateKeyTests(Base):
    """H3: key = calendarId|eventId; start stored in value; rescheduling keeps state."""

    def test_key_and_start_stored(self):
        state = {}
        run(make_event(10), 0, state, cal="work-cal")
        self.assertEqual(list(state), ["work-cal|ev1"])
        self.assertEqual(state["work-cal|ev1"]["start"],
                         (NOW + timedelta(minutes=10)).isoformat())

    def test_same_event_id_other_calendar_is_separate(self):
        state = {}
        run(make_event(10), 0, state, cal="a")
        run(make_event(10), 0, state, cal="b")
        self.assertEqual(set(state), {"a|ev1", "b|ev1"})

    def test_reschedule_does_not_reset_fired_called_ack(self):
        ev, state = make_event(0, location=MEET_URL), {}
        run(ev, 0, state)
        run(ev, 3, state)  # nag + call
        self.assertTrue(state[CAL + "|ev1"]["called"])
        moved = make_event(5, location=MEET_URL)  # start moved 5 minutes later
        self.actions.clear()
        for t in (0.5, 4.6, 5.0, 9, 11):
            run(moved, t, state, acks=[CAL + "|ev1"])
        self.assertEqual(self.names(), [])
        self.assertEqual(state[CAL + "|ev1"]["start"], moved["start"]["dateTime"])

    def test_reschedule_without_ack_never_second_call(self):
        ev, state = make_event(0, location=MEET_URL), {}
        run(ev, 0, state)
        run(ev, 3, state)
        moved = make_event(4, location=MEET_URL)
        for t in (7, 9, 12):
            run(moved, t, state)
        self.assertEqual(self.names().count("place_call"), 1)

    def test_prune_by_stored_start(self):
        old = (NOW - timedelta(hours=2)).isoformat()
        state = {"_meta": {}, "c|a": {"start": old}, "c|b": {"start": NOW.isoformat()},
                 "c|broken": {}, "legacy@x": {"fired": []}}
        self.assertEqual(set(mg_state.prune(state, NOW)), {"_meta", "c|b"})

    def test_state_roundtrip_atomic_private(self):
        mg_state.save_state({"x": 1})
        self.assertEqual(mg_state.load_state(), {"x": 1})
        self.assertEqual((self.home / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.home.glob("*.tmp")), [])

    def test_acks_pruned_with_state(self):
        mg_state.append_ack("c|keep")
        mg_state.append_ack("c|old")
        mg_state.append_ack("bad\nkey")
        mg_state.prune_acks({"c|keep"})
        self.assertEqual(mg_state.load_acks(), {"c|keep"})


class RunOnceTests(Base):
    """M4 cap + full pass with fetch mocked."""

    def pairs(self, n):
        return [(CAL, make_event(9 - i, ev_id=f"e{i}")) for i in range(n)]

    def test_max_events_per_run_default_3_nearest_first(self):
        far = (CAL, make_event(10.4, ev_id="far"))
        acted = mg_engine.run_events([far] + self.pairs(4), NOW, {}, set())
        self.assertEqual(acted, 3)
        self.assertEqual(self.names().count("push"), 3)

    def test_max_events_configurable(self):
        self.configure({"max_events_per_run": 1})
        state = {}
        mg_engine.run_events(self.pairs(3), NOW, state, set())
        self.assertEqual(list(state), [CAL + "|e2"])  # nearest (start in 7 min)

    def test_bad_event_does_not_stop_others(self):
        bad = (CAL, {"id": "x", "start": {"dateTime": "not-a-date"}})
        attendees_bad = (CAL, make_event(9, ev_id="y", attendees="oops"))
        acted = mg_engine.run_events([bad, attendees_bad] + self.pairs(1), NOW, {}, set())
        self.assertEqual(acted, 1)
        self.assertIn("event ERROR", self.log_text())

    def test_run_once_saves_pruned_state_and_acks(self):
        (self.home / "state.json").write_text(json.dumps(
            {"c|old": {"start": (NOW - timedelta(hours=3)).isoformat()}}))
        mg_state.append_ack("c|old")
        result = mg_google.FetchResult()
        result.pairs = self.pairs(1)
        with mock.patch.object(mg_google, "get_access_token", return_value="t"), \
                mock.patch.object(mg_google, "fetch_events", return_value=result):
            mg_engine.run_once(NOW)
        state = json.loads((self.home / "state.json").read_text())
        self.assertEqual(list(state), [CAL + "|e0"])
        self.assertEqual(mg_state.load_acks(), set())

    def test_run_once_api_error_logs_class_only(self):
        err = RuntimeError("https://example.invalid/?token=SECRET123456")
        with mock.patch.object(mg_google, "get_access_token", side_effect=err):
            mg_engine.run_once(NOW)
        log = self.log_text()
        self.assertIn("API ERROR: RuntimeError", log)
        self.assertNotIn("SECRET", log)


class ApiErrorAlertTests(Base):
    """F2: auth / network / token failures alert locally, at most hourly."""

    def run_with(self, err, minutes=0):
        with mock.patch.object(mg_google, "get_access_token", side_effect=err):
            mg_engine.run_once(NOW + timedelta(minutes=minutes))

    def test_local_alert_without_push(self):
        self.run_with(OSError("net down"))
        self.assertEqual(sorted(self.names()), ["dialog", "push", "speak"])
        dialog = [a for a in self.actions if a[0] == "dialog"][0]
        self.assertFalse(dialog[2]["ack"])
        self.assertIn("OSError", dialog[1][1])

    def test_at_most_hourly_and_persisted(self):
        self.run_with(OSError("x"))
        self.actions.clear()
        self.run_with(OSError("x"), minutes=30)
        self.assertEqual(self.names(), [])
        meta = json.loads((self.home / "state.json").read_text())["_meta"]
        self.assertEqual(meta["last_error_alert"], NOW.timestamp())
        self.run_with(OSError("x"), minutes=61)
        self.assertIn("speak", self.names())

    def test_missing_or_insecure_tokens_alert(self):
        self.configure({"tokens": str(self.home / "none.json"),
                        "oauth_keys": str(self.home / "keys.json")})
        self.write_private(self.home / "keys.json", '{"installed": {"client_id": "c"}}')
        mg_engine.run_once(NOW)
        self.assertIn("speak", self.names())
        self.assertIn("API ERROR: FileNotFoundError", self.log_text())
        self.actions.clear()
        (self.home / "state.json").unlink()
        self.write_private(self.home / "none.json", '{"refresh_token": "r"}', 0o644)
        mg_engine.run_once(NOW)
        self.assertIn("dialog", self.names())
        self.assertIn("API ERROR: InsecureFileError", self.log_text())


class CallMarkingTests(Base):
    """F4: only a real attempt marks the event as called."""

    def started(self):
        ev, state = make_event(0, location=MEET_URL), {}
        run(ev, 0, state)
        self.actions.clear()
        return ev, state

    def test_limit_refusal_retried_next_run(self):
        ev, state = self.started()
        self.call_result = mg_call.REFUSED
        run(ev, 3, state)
        self.assertFalse(state[CAL + "|ev1"]["called"])
        self.call_result = mg_call.PLACED
        run(ev, 4, state)
        self.assertTrue(state[CAL + "|ev1"]["called"])
        run(ev, 6, state)
        self.assertEqual(self.names().count("place_call"), 2)

    def test_attempt_statuses_mark_called(self):
        for result in (mg_call.PLACED, mg_call.FAILED, mg_call.ALREADY):
            self.actions.clear()
            ev, state = self.started()
            self.call_result = result
            run(ev, 3, state)
            run(ev, 6, state)
            self.assertTrue(state[CAL + "|ev1"]["called"], result)
            self.assertEqual(self.names().count("place_call"), 1, result)


class WakeAfterSleepTests(Base):
    """F5: first seen after start but inside the nag window."""

    def test_first_seen_late_opens_and_calls_same_run(self):
        state = {}
        for late in (9.5, 10.0):
            self.actions.clear()
            ev = make_event(0, location=MEET_URL, ev_id=f"late{late}")
            run(ev, late, state)
            self.assertIn("open_link", self.names())
            self.assertIn("place_call", self.names())

    def test_catch_up_respects_after_min(self):
        ev, state = make_event(0, location=MEET_URL), {}
        run(ev, 1, state)
        self.assertIn("open_link", self.names())
        self.assertNotIn("place_call", self.names())
        run(ev, 2.5, state)
        self.assertIn("place_call", self.names())

    def test_catch_up_skips_call_when_acked_or_in_zoom(self):
        run(make_event(0, ev_id="a"), 5, {}, acks=[CAL + "|a"])
        self.zoom_check.return_value = True
        run(make_event(0, ev_id="z"), 5, {})
        self.assertNotIn("place_call", self.names())

    def test_catch_up_linkless_no_call(self):
        run(make_event(0, location=""), 5, {})
        self.assertNotIn("place_call", self.names())

    def test_first_seen_beyond_window_logged_once(self):
        state, ev = {}, make_event(0)
        run(ev, 11, state)
        run(ev, 12, state)
        self.assertEqual(self.names(), [])
        self.assertEqual(self.log_text().count("skipped: event first seen"), 1)
        self.assertEqual(state[CAL + "|ev1"]["skipped_at_start"], ev["start"]["dateTime"])


class SimulateTests(Base):
    def test_simulate_fires_every_stage_without_call(self):
        out = []
        with mock.patch("builtins.print", side_effect=out.append):
            meeting_guard.simulate(pause_sec=0, sleep=lambda s: None)
        names = self.names()
        self.assertNotIn("place_call", names)
        self.assertNotIn("open_link", names)
        self.assertGreaterEqual(names.count("speak"), 4)
        self.assertEqual(names.count("dialog"), 2)
        said = [a[1][0] for a in self.actions if a[0] == "speak"]
        self.assertTrue(all("meeting-guard simulation" in s for s in said))

    def test_removed_calendar_write_commands(self):
        self.assertNotIn("--test", meeting_guard.COMMANDS)
        self.assertNotIn("--test-cleanup", meeting_guard.COMMANDS)
        self.assertIn("--simulate", meeting_guard.COMMANDS)
        self.assertIn("--test-call", meeting_guard.COMMANDS)
        self.assertFalse(hasattr(mg_google, "api"))

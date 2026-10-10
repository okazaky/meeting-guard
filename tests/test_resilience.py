"""Round 3: nothing may silently stop the alarms (R4 R6 R8 R9 R11-R13 R16-R18)."""
import json
import os
from datetime import timedelta
from unittest import mock

from mgtest_helpers import CAL, MEET_URL, NOW, ZOOM_URL, Base, make_event

import mg_call
import mg_config
import mg_engine
import mg_google
import mg_state


def run(ev, minutes_offset, state, acks=(), progress=None):
    now = NOW + timedelta(minutes=minutes_offset)
    return mg_engine.process_event(CAL, ev, now, state, set(acks), progress)


def fetch_result(pairs=(), failed=(), truncated=()):
    r = mg_google.FetchResult()
    r.pairs, r.failed, r.truncated = list(pairs), list(failed), list(truncated)
    return r


class CallPathIsolationTests(Base):
    """R4: local alarms run first; the call path can never suppress them."""

    def started(self):
        ev, state = make_event(0, location=MEET_URL), {}
        run(ev, 0, state)
        self.actions.clear()
        return ev, state

    def test_nag_runs_before_call(self):
        ev, state = self.started()
        run(ev, 3, state)
        names = self.names()
        self.assertLess(names.index("speak"), names.index("place_call"))
        self.assertLess(names.index("dialog"), names.index("place_call"))
        self.assertLess(names.index("push"), names.index("place_call"))

    def test_call_exception_does_not_suppress_nag(self):
        ev, state = self.started()
        with mock.patch.object(mg_call, "place_call", side_effect=OSError("lock")):
            self.assertTrue(run(ev, 3, state))
        for name in ("speak", "dialog", "push"):
            self.assertIn(name, self.names())
        self.assertFalse(state[CAL + "|ev1"]["called"])
        self.assertIn("call ERROR (local alarms unaffected)", self.log_text())

    def test_catch_up_call_exception_keeps_open_alarm(self):
        with mock.patch.object(mg_call, "place_call", side_effect=RuntimeError("x")):
            self.assertTrue(run(make_event(0, location=MEET_URL), 5, {}))
        self.assertIn("open_link", self.names())
        self.assertIn("speak", self.names())


class CallPrepFailClosedTests(Base):
    """R4: any exception while preparing a call -> REFUSED (nothing sent)."""

    def test_lock_failure_refused(self):
        with mock.patch.object(mg_call, "call_params",
                               return_value=("AC" + "0" * 32, ("u", "p"), "+1", "+2")), \
                mock.patch.object(mg_config, "file_lock", side_effect=OSError("lock")), \
                mock.patch.object(mg_call.mg_http, "request_json") as req:
            self.assertEqual(self.real_place_call(), mg_call.REFUSED)
        req.assert_not_called()
        self.assertIn("preparation failed", self.log_text())

    def test_secrets_failure_refused(self):
        with mock.patch.object(mg_call.mg_secrets, "load_secrets",
                               side_effect=ValueError("x")), \
                mock.patch.object(mg_call.mg_http, "request_json") as req:
            self.assertEqual(self.real_place_call(), mg_call.REFUSED)
        req.assert_not_called()

    def real_place_call(self):
        from mgtest_helpers import REAL_PLACE_CALL
        return REAL_PLACE_CALL("c|a", "s", NOW.timestamp())


class OpenFailureTests(Base):
    """R18: a failed open says/dialogs the manual-join message, not "opened"."""

    def test_open_failure_manual_message(self):
        self.open_ok = False
        run(make_event(0), 0, {})
        said = [a[1][0] for a in self.actions if a[0] == "speak"]
        self.assertTrue(any("開けませんでした" in s for s in said), said)
        self.assertFalse(any("開きました" in s for s in said))
        dialogs = [a for a in self.actions if a[0] == "dialog"]
        self.assertEqual(len(dialogs), 1)
        self.assertIn("手動で参加", dialogs[0][1][1])

    def test_open_exception_treated_as_failure(self):
        with mock.patch.object(mg_engine.A, "open_link", side_effect=OSError("x")):
            run(make_event(0), 0, {})
        self.assertIn("dialog", self.names())
        self.assertIn("open ERROR", self.log_text())

    def test_open_success_says_opened(self):
        run(make_event(0), 0, {})
        said = [a[1][0] for a in self.actions if a[0] == "speak"]
        self.assertTrue(any("開きました" in s for s in said))
        self.assertNotIn("dialog", self.names())


class ZoomStaleTests(Base):
    """R13: opened by open0 -> CptHost >= open0 fire time - 60s;
    otherwise CptHost >= event start - 2 min."""

    def test_opened_threshold_is_open0_minus_60(self):
        ev, state = make_event(0), {}
        run(ev, 0, state)
        run(ev, 3, state)
        start_ts = NOW.timestamp()
        self.assertEqual(self.zoom_check.call_args[0][0], start_ts - 60)
        self.assertEqual(state[CAL + "|ev1"]["open0_at"], start_ts)
        self.assertIs(state[CAL + "|ev1"]["open0_opened"], True)

    def test_early_open0_threshold_follows_open_time(self):
        self.configure({"stages": {"voice5": 9, "dialog1": 8.5, "open0": 8}})
        ev, state = make_event(8), {}
        run(ev, 0, state)
        run(ev, 10, state)
        self.assertEqual(self.zoom_check.call_args[0][0], NOW.timestamp() - 60)

    def test_late_open_does_not_accept_older_cpthost(self):
        # catch-up opened the link 4 min after start: CptHost must be >= open - 60s
        ev, state = make_event(0), {}
        run(ev, 4, state)
        run(ev, 7, state)
        self.assertEqual(self.zoom_check.call_args[0][0],
                         NOW.timestamp() + 4 * 60 - 60)

    def test_open_failed_uses_start_minus_2min(self):
        self.open_ok = False
        ev, state = make_event(0), {}
        run(ev, 0, state)
        run(ev, 3, state)
        self.assertIs(state[CAL + "|ev1"]["open0_opened"], False)
        self.assertEqual(self.zoom_check.call_args[0][0], NOW.timestamp() - 120)

    def test_auto_open_off_uses_start_minus_2min(self):
        self.configure({"auto_open": False})
        ev, state = make_event(0), {}
        run(ev, 0, state)
        run(ev, 3, state)
        self.assertNotIn("open_link", self.names())
        self.assertEqual(self.zoom_check.call_args[0][0], NOW.timestamp() - 120)

    def test_open0_snapshot_taken_before_open_and_passed_as_exclude(self):
        # R13: a CptHost already running at open0 belongs to the previous meeting
        snap = [[101, NOW.timestamp() - 30]]
        self.zoom_snapshot.side_effect = (
            lambda *_: self.actions.append(("snapshot", (), {})) or snap)
        ev, state = make_event(0), {}
        run(ev, 0, state)
        names = self.names()
        self.assertLess(names.index("snapshot"), names.index("open_link"))
        self.assertEqual(state[CAL + "|ev1"]["open0_zoom_pre"], snap)
        run(ev, 3, state)
        self.assertEqual(self.zoom_check.call_args[1]["exclude"], snap)

    def test_missing_snapshot_after_open_keeps_nagging_and_calls(self):
        # R13: snapshot failed (None) -> cannot tell old meeting from this one
        self.zoom_snapshot.return_value = None
        self.zoom_check.return_value = True
        ev, state = make_event(0), {}
        run(ev, 0, state)
        self.assertIn("open_link", self.names())
        before = len(self.actions)
        run(ev, 3, state)
        self.zoom_check.assert_not_called()
        self.assertIn("speak", [a[0] for a in self.actions[before:]])  # nagged
        self.assertTrue(state[CAL + "|ev1"]["called"])

    def test_snapshot_exception_never_blocks_open(self):
        self.zoom_snapshot.side_effect = RuntimeError("boom")
        ev, state = make_event(0), {}
        run(ev, 0, state)
        self.assertIn("open_link", self.names())
        self.assertIsNone(state[CAL + "|ev1"]["open0_zoom_pre"])
        self.assertIn("zoom snapshot ERROR", self.log_text())

    def test_snapshot_ignored_when_not_opened_or_malformed(self):
        self.assertIsNone(mg_engine.zoom_exclude(
            {"open0_opened": False, "open0_zoom_pre": [[1, 2.0]]}))
        for bad in ("x", [[1]], [["1", 2.0]], [[True, 2.0]], [1, 2],
                    [[101, 10 ** 400]], [[10 ** 400, 2.0]], [[101, float("inf")]],
                    [[101, float("nan")]], [[101, -1.0]], [[0, 2.0]], [[1.5, 2.0]]):
            self.assertIsNone(mg_engine.zoom_exclude(
                {"open0_opened": True, "open0_zoom_pre": bad}), bad)
        self.assertEqual(mg_engine.zoom_exclude(
            {"open0_opened": True, "open0_zoom_pre": [[1, 2.0]]}), [[1, 2.0]])

    def test_no_snapshot_for_meet_links(self):
        run(make_event(0, location=MEET_URL), 0, {})
        self.zoom_snapshot.assert_not_called()

    def test_zoom_since_ignores_non_true_opened_flag(self):
        ctx = {"start_ts": 1000.0}
        self.assertEqual(mg_engine.zoom_since(ctx, {"open0_at": 900.0}), 880.0)
        self.assertEqual(mg_engine.zoom_since(
            ctx, {"open0_at": 900.0, "open0_opened": 1}), 880.0)
        self.assertEqual(mg_engine.zoom_since(
            ctx, {"open0_at": True, "open0_opened": True}), 880.0)
        self.assertEqual(mg_engine.zoom_since(
            ctx, {"open0_at": 900.0, "open0_opened": True}), 840.0)

    def test_stale_process_keeps_nagging(self):
        ev, state = make_event(0), {}
        run(ev, 0, state)
        self.actions.clear()
        self.zoom_check.return_value = False  # e.g. CptHost older than threshold
        run(ev, 3, state)
        self.assertIn("speak", self.names())


class RescheduleAfterSkipTests(Base):
    """R11: a skipped event moved into the future alerts again."""

    def test_skipped_then_moved_to_future_alerts(self):
        state, ev = {}, make_event(0, location=MEET_URL)
        run(ev, 30, state)  # first seen 30 min after start: skipped
        self.assertEqual(self.names(), [])
        moved = make_event(40, location=MEET_URL)  # now 10 min ahead of offset 30
        run(moved, 30, state)
        self.assertEqual(self.names(), ["push"])
        self.assertNotIn("skipped_at_start", state[CAL + "|ev1"])
        self.assertIn("alerts re-armed", self.log_text())

    def test_skipped_same_start_never_alerts(self):
        state, ev = {}, make_event(0)
        run(ev, 30, state)
        run(ev, 31, state)
        self.assertEqual(self.names(), [])

    def test_alerted_event_is_not_rearmed(self):
        ev, state = make_event(0, location=MEET_URL), {}
        run(ev, 0, state)
        run(ev, 3, state)
        self.actions.clear()
        moved = make_event(10, location=MEET_URL)
        run(moved, 5, state, acks=[CAL + "|ev1"])
        self.assertEqual(self.names(), [])

    def test_rearm_keeps_call_record(self):
        prev = {"skipped_at_start": NOW.isoformat(), "fired": ["open0"], "called": True,
                "last_alert": 5, "open0_at": 1.0, "open0_opened": True}
        out = mg_engine.rearm_if_moved(prev, NOW + timedelta(minutes=5))
        self.assertEqual(out, {"called": True})
        same = mg_engine.rearm_if_moved(prev, NOW)
        self.assertIs(same, prev)
        past = mg_engine.rearm_if_moved(prev, NOW - timedelta(minutes=5))
        self.assertEqual(past, {"called": True})

    def test_moved_to_passed_start_within_nag_window_catches_up(self):
        """R11: new start already passed but within nag_minutes (10): open0 +
        call path run in this pass instead of being silently skipped."""
        state, ev = {}, make_event(0, location=MEET_URL)
        run(ev, 30, state)
        self.assertEqual(self.names(), [])
        moved = make_event(25, location=MEET_URL)  # 5 min ago at offset 30
        self.assertTrue(run(moved, 30, state))
        names = self.names()
        self.assertIn("open_link", names)
        self.assertIn("speak", names)
        self.assertIn("place_call", names)  # 5 min >= after_min 2
        self.assertLess(names.index("speak"), names.index("place_call"))
        st = state[CAL + "|ev1"]
        self.assertNotIn("skipped_at_start", st)
        self.assertIn("open0", st["fired"])
        self.actions.clear()
        run(moved, 32, state)  # then the normal nag path continues
        self.assertIn("speak", self.names())

    def test_linkless_moved_to_passed_start_gets_start_voice(self):
        state, ev = {}, make_event(0, location="")
        run(ev, 30, state)
        moved = make_event(26, location="")
        self.assertTrue(run(moved, 30, state))
        speaks = [a[1][0] for a in self.actions if a[0] == "speak"]
        self.assertEqual(len(speaks), 1)
        self.assertIn("Weekly sync", speaks[0])
        self.assertNotIn("open_link", self.names())
        self.assertNotIn("place_call", self.names())  # escalate_linkless off

    def test_moved_but_still_beyond_window_stays_silent(self):
        state, ev = {}, make_event(0)
        run(ev, 30, state)
        run(make_event(5), 30, state)  # 25 min ago > nag_minutes
        self.assertEqual(self.names(), [])

    def test_legacy_skipped_true_flag_is_ignored(self):
        """Legacy `skipped: true` (pre-release format, no public users) is dropped:
        the entry behaves exactly like the same entry without the flag (no re-arm)."""
        def entry(**extra):
            return {CAL + "|ev1": {"start": NOW.isoformat(), "last_alert": 0,
                                   "fired": list(mg_engine.STAGE_ORDER),
                                   "called": False, **extra}}
        moved = make_event(25, location=MEET_URL)
        run(moved, 30, entry(skipped=True))
        legacy = self.names()
        self.actions.clear()
        run(moved, 30, entry())
        self.assertEqual(legacy, self.names())
        self.assertNotIn("open_link", legacy)


class CapCountsFailuresTests(Base):
    """R12: an event whose actions started counts toward the cap even if it raised."""

    def test_raising_event_counts(self):
        self.configure({"max_events_per_run": 1})
        pairs = [(CAL, make_event(1, ev_id="a")), (CAL, make_event(9, ev_id="b"))]
        with mock.patch.object(mg_engine, "fire_stage", side_effect=OSError("say")):
            acted = mg_engine.run_events(pairs, NOW, {}, set())
        self.assertEqual(acted, 1)
        self.assertIn("max_events_per_run=1 reached", self.log_text())

    def test_failure_before_any_action_does_not_count(self):
        self.configure({"max_events_per_run": 1})
        pairs = [(CAL, make_event(1, ev_id="a")), (CAL, make_event(9, ev_id="b"))]
        real = mg_engine.event_context

        def broken_for_a(cal, ev, start):
            if ev["id"] == "a":
                raise KeyError("x")  # fails before any action
            return real(cal, ev, start)

        with mock.patch.object(mg_engine, "event_context", side_effect=broken_for_a):
            acted = mg_engine.run_events(pairs, NOW, {}, set())
        self.assertEqual(acted, 1)
        self.assertIn("push", self.names())

    def test_progress_flag(self):
        progress = {"started": False}
        run(make_event(10), 0, {}, progress=progress)
        self.assertTrue(progress["started"])


class TimeZoneTests(Base):
    """R9: dateTime without offset uses start.timeZone, else local time."""

    def test_naive_with_timezone(self):
        dt = mg_google.parse_dt("2026-01-05T19:00:00", "Asia/Tokyo")
        self.assertEqual(dt, NOW)

    def test_unknown_or_bad_timezone_is_local(self):
        for tz in ("Mars/Olympus", "../../etc/passwd", "", None, 5, "A" * 300):
            dt = mg_google.parse_dt("2026-01-05T10:00:00", tz)
            self.assertIsNotNone(dt.tzinfo, tz)
            self.assertEqual(dt.replace(tzinfo=None), mg_google.parse_dt(
                "2026-01-05T10:00:00").replace(tzinfo=None))

    def test_never_type_error(self):
        ev = make_event(5)
        ev["start"] = {"dateTime": "2026-01-05T19:05:00", "timeZone": "Asia/Tokyo"}
        self.assertTrue(run(ev, 0, {}))
        self.assertEqual(self.names(), ["speak", "push"])
        with self.assertRaises(ValueError):
            mg_google.parse_dt(12345)
        bad = (CAL, {"id": "x", "start": {"dateTime": 5}})
        self.assertEqual(mg_engine.run_events([bad], NOW, {}, set()), 0)


class RunOnceAlertTests(Base):
    """R6 R8 R10 R16 R17 in the polling pass."""

    def poll(self, result=None, minutes=0, token_err=None):
        result = result or fetch_result()
        with mock.patch.object(mg_google, "get_access_token", return_value="t",
                               side_effect=token_err), \
                mock.patch.object(mg_google, "fetch_events", return_value=result):
            return mg_engine.run_once(NOW + timedelta(minutes=minutes))

    def test_partial_calendar_failure_processes_healthy_and_alerts(self):
        result = fetch_result(pairs=[(CAL, make_event(10))], failed=[(1, OSError("x"))])
        self.poll(result)
        self.assertIn("push", self.names())  # the healthy calendar's event
        dialogs = [a for a in self.actions if a[0] == "dialog"]
        self.assertEqual(len(dialogs), 1)
        self.assertIn("1件のカレンダー", dialogs[0][1][1])
        self.assertFalse(dialogs[0][2]["ack"])
        self.actions.clear()
        self.poll(result, minutes=30)
        self.assertNotIn("dialog", self.names())  # rate-limited to 1/h
        self.poll(result, minutes=61)
        self.assertIn("dialog", self.names())

    def test_truncation_warning_rate_limited(self):
        self.poll(fetch_result(truncated=[0]))
        self.assertEqual(sorted(self.names()), ["dialog", "speak"])
        self.actions.clear()
        self.poll(fetch_result(truncated=[0]), minutes=1)
        self.assertEqual(self.names(), [])

    def test_invalid_grant_alarm(self):
        self.poll(token_err=mg_google.InvalidGrantError("invalid_grant"))
        said = [a[1][0] for a in self.actions if a[0] == "speak"]
        self.assertTrue(any("--auth" in s for s in said))
        dialog = [a for a in self.actions if a[0] == "dialog"][0]
        self.assertIn("--auth", dialog[1][1])
        self.actions.clear()
        self.poll(token_err=mg_google.InvalidGrantError("invalid_grant"), minutes=10)
        self.assertEqual(self.names(), [])

    def test_config_problem_alarm_rate_limited(self):
        self.configure({"nag_minutes": float("nan"), "call": {"enabled": "yes"}})
        self.poll()
        dialog = [a for a in self.actions if a[0] == "dialog"][0]
        self.assertIn("nag_minutes", dialog[1][1])
        self.assertIn("call.enabled", dialog[1][1])
        self.assertTrue((self.home / ".alert-config").is_file())
        self.actions.clear()
        self.poll(minutes=5)
        self.assertEqual(self.names(), [])

    def test_unsafe_home_alarms_and_exits(self):
        os.chmod(self.home, 0o777)
        self.addCleanup(os.chmod, self.home, 0o700)
        with mock.patch.object(mg_google, "get_access_token") as tok, \
                mock.patch.object(mg_config.sys, "stderr") as err:
            self.assertEqual(mg_engine.run_once(NOW), 1)
        self.assertIn("UNSAFE MEETING_GUARD_HOME", err.write.call_args[0][0])
        tok.assert_not_called()
        self.assertEqual(sorted(self.names()), ["dialog", "speak"])
        self.assertFalse((self.home / "state.json").exists())
        self.assertFalse((self.home / "logs" / "guard.log").exists())

    def test_symlinked_lock_alarms(self):
        target = self.home / "elsewhere"
        target.write_text("keep")
        os.symlink(target, self.home / ".lock")
        self.assertEqual(self.poll(), 1)
        self.assertIn("dialog", self.names())
        self.assertEqual(target.read_text(), "keep")


class HomeProblemTests(Base):
    """R17: StrictModes-style MEETING_GUARD_HOME check."""

    def test_ok(self):
        self.assertIsNone(mg_config.home_problem())

    def test_group_writable(self):
        os.chmod(self.home, 0o770)
        self.addCleanup(os.chmod, self.home, 0o700)
        self.assertIn("writable", mg_config.home_problem())

    def test_symlink_and_missing(self):
        link = self.home.parent / (self.home.name + "-link")
        os.symlink(self.home, link)
        self.addCleanup(os.unlink, link)
        with mock.patch.object(mg_config, "HOME", link):
            self.assertEqual(mg_config.home_problem(), "is a symlink")
        with mock.patch.object(mg_config, "HOME", self.home / "nope"):
            self.assertIn("cannot stat", mg_config.home_problem())

    def test_other_owner(self):
        real = os.lstat(self.home)
        fake = mock.Mock(st_mode=real.st_mode, st_uid=real.st_uid + 1)
        with mock.patch.object(mg_config.os, "lstat", return_value=fake):
            self.assertIn("not owned", mg_config.home_problem())


class NoFollowTests(Base):
    """R17: state / acks / log / locks are opened with O_NOFOLLOW."""

    def plant(self, name):
        victim = self.home / ("victim-" + name.replace("/", "_"))
        victim.write_text("keep\n")
        os.symlink(victim, self.home / name)
        return victim

    def test_symlinked_state_and_acks_not_followed(self):
        v1 = self.plant("state.json")
        v2 = self.plant("acks.txt")
        self.assertEqual(mg_state.load_state(), {})
        self.assertEqual(mg_state.load_acks(), set())
        with self.assertRaises(OSError):
            mg_state.append_ack("c|x")
        self.assertEqual((v1.read_text(), v2.read_text()), ("keep\n", "keep\n"))

    def test_symlinked_lock_not_followed(self):
        victim = self.plant("acks.lock")
        with self.assertRaises(OSError):
            with mg_config.file_lock(self.home / "acks.lock"):
                pass
        self.assertEqual(victim.read_text(), "keep\n")

    def test_symlinked_log_falls_back_to_stderr(self):
        (self.home / "logs").mkdir(exist_ok=True)
        victim = self.plant("logs/guard.log")
        with mock.patch.object(mg_config.sys, "stderr") as err:
            mg_config.log("hello")
        self.assertEqual(victim.read_text(), "keep\n")
        self.assertIn("hello", err.write.call_args[0][0])


class AlertDueTests(Base):
    def test_rate_limit_file(self):
        ts = NOW.timestamp()
        self.assertTrue(mg_state.alert_due("x", ts, 3600))
        self.assertFalse(mg_state.alert_due("x", ts + 10, 3600))
        self.assertTrue(mg_state.alert_due("x", ts + 3601, 3600))
        self.assertEqual((self.home / ".alert-x").stat().st_mode & 0o777, 0o600)

    def test_garbage_file_means_due(self):
        (self.home / ".alert-y").write_text("nan")
        self.assertTrue(mg_state.alert_due("y", NOW.timestamp(), 3600))
        self.assertEqual(json.loads((self.home / ".alert-y").read_text()), NOW.timestamp())

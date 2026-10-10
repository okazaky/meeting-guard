"""Final round: huge JSON ints (N1), explicit null (R6), OAuth absolute deadline
(R14) and MEETING_GUARD_HOME validation before any write (R17)."""
import http.client
import io
import os
import socket
import threading
import time
from pathlib import Path
from unittest import mock

from mgtest_helpers import TempHome

import meeting_guard
import mg_actions
import mg_call
import mg_config
import mg_engine
import mg_oauth
import mg_schema

HUGE = int("9" * 401)


class HugeIntTests(TempHome):
    """N1: a 401-digit JSON int is a normal config error, never OverflowError."""

    def test_is_num_huge_int_out_of_range(self):
        for spec in (mg_schema.NUM_KEYS["nag_minutes"], mg_schema.STAGE_RANGE,
                     mg_schema.CALL_NUM_KEYS["max_per_day"]):
            self.assertFalse(mg_schema.is_num(HUGE, spec))
            self.assertFalse(mg_schema.is_num(-HUGE, spec))
        self.assertTrue(mg_schema.is_num(5, mg_schema.NUM_KEYS["nag_minutes"]))
        self.assertFalse(mg_schema.is_num(float("nan"), mg_schema.STAGE_RANGE))

    def test_huge_ints_from_config_file_fall_back(self):
        text = ('{"nag_minutes": %s, "max_events_per_run": %s, '
                '"call": {"enabled": true, "max_per_day": %s}, '
                '"stages": {"open0": %s}}' % (HUGE, HUGE, HUGE, -HUGE))
        cfg = self.write_private(self.home / "c.json", text)
        with mock.patch.dict(os.environ, {"MEETING_GUARD_CONFIG": str(cfg)}):
            mg_config.setup(home=self.home)
        self.assertEqual(sorted(mg_config.CONFIG_PROBLEMS),
                         ["call.max_per_day", "max_events_per_run", "nag_minutes",
                          "stages.open0"])
        defaults = mg_config.DEFAULT_CONFIG
        self.assertEqual(mg_config.CONFIG["nag_minutes"], defaults["nag_minutes"])
        self.assertEqual(mg_config.CONFIG["stages"]["open0"], defaults["stages"]["open0"])
        self.assertFalse(mg_config.CONFIG["call"]["enabled"])
        self.assertIn("CONFIG ERROR: nag_minutes", self.log_text())

    def test_any_validation_exception_means_all_defaults(self):
        with mock.patch.object(mg_schema, "validate", side_effect=RuntimeError("boom")):
            self.configure({"nag_minutes": 3, "call": {"enabled": True}})
        cfg = mg_config.CONFIG
        self.assertEqual(cfg["nag_minutes"], mg_config.DEFAULT_CONFIG["nag_minutes"])
        self.assertFalse(cfg["call"]["enabled"])
        self.assertEqual(mg_config.CONFIG_PROBLEMS,
                         ["config validation failed (RuntimeError)"])
        self.assertIn("config validation failed", self.log_text())


class ExplicitNullTests(TempHome):
    """R6: an explicit null for any known key is invalid -> default + warning."""

    def test_null_known_keys_flagged(self):
        for key in ("call", "stages", "messages", "calendars", "auto_open",
                    "ntfy_topic", "nag_minutes", "exclude_summary_contains"):
            self.assertEqual(mg_schema.validate({key: None})[1], [key], key)
        self.assertEqual(mg_schema.validate({"call": {"enabled": None}})[1],
                         ["call.enabled"])
        self.assertEqual(mg_schema.validate({"stages": {"open0": None}})[1],
                         ["stages.open0"])
        self.assertEqual(mg_schema.validate({})[1], [])

    def test_null_sections_use_defaults_and_disable_call(self):
        self.configure({"stages": None, "messages": None})
        cfg = mg_config.CONFIG
        self.assertEqual(cfg["stages"], mg_config.DEFAULT_CONFIG["stages"])
        self.assertEqual(cfg["messages"], {})
        self.assertFalse(cfg["call"]["enabled"])
        self.assertEqual(mg_config.CONFIG_PROBLEMS, ["stages", "messages"])
        mg_config.setup({"call": None}, self.home)
        self.assertEqual(mg_config.CONFIG["call"], {
            **mg_config.DEFAULT_CONFIG["call"], "enabled": False})
        self.assertIn("CONFIG ERROR: call;", self.log_text())


class OAuthDeadlineTests(TempHome):
    """R14: per-connection timeout = min(10, remaining); handler ignores a code
    after the deadline; the loop exits at the deadline (mocked clock)."""

    def handler_timeout(self, remaining):
        server = mock.Mock(auth_deadline=100.0, auth_clock=lambda: 100.0 - remaining)
        handler = mg_oauth.AuthHandler.__new__(mg_oauth.AuthHandler)
        handler.server, handler.request = server, mock.Mock()
        handler.setup()
        handler._deadline_timer.cancel()
        return handler.request.settimeout.call_args[0][0]

    def test_per_connection_timeout(self):
        self.assertEqual(self.handler_timeout(50.0), 10)
        self.assertEqual(self.handler_timeout(3.0), 3.0)
        self.assertEqual(self.handler_timeout(-1.0), 0.001)

    def serve_once(self, now):
        server = mg_oauth.http.server.HTTPServer(("127.0.0.1", 0), mg_oauth.AuthHandler)
        self.addCleanup(server.server_close)
        server.auth_deadline, server.auth_clock = 100.0, lambda: now
        server.auth_result = None
        t = threading.Thread(target=server.handle_request)
        t.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            conn.request("GET", "/?code=abc&state=good")
            resp = conn.getresponse()
            resp.read()
            status = resp.status
        finally:
            conn.close()
            t.join(5)
        return status, server.auth_result

    def test_handler_ignores_code_after_deadline(self):
        self.assertEqual(self.serve_once(101.0), (408, None))
        status, result = self.serve_once(99.0)
        self.assertEqual((status, result["code"]), (200, "abc"))

    def test_slow_drip_client_cut_at_absolute_deadline(self):
        # R14: one header byte every 50 ms never trips the 10 s idle timeout;
        # the connection must still be cut at the absolute deadline (0.3 s).
        server = mg_oauth.http.server.HTTPServer(("127.0.0.1", 0), mg_oauth.AuthHandler)
        self.addCleanup(server.server_close)
        stop = threading.Event()
        self.addCleanup(stop.set)
        sent, errors, connected = [], [], threading.Event()

        def drip():
            try:
                with socket.create_connection(server.server_address, timeout=5) as s:
                    connected.set()
                    s.sendall(b"GET /?code=abc&state=good HTTP/1.1\r\n")
                    give_up = time.monotonic() + 3.0  # a regression fails, never hangs
                    while not stop.is_set() and time.monotonic() < give_up:
                        try:
                            s.sendall(b"X")
                        except OSError:
                            return  # cut by the server: expected
                        sent.append(1)
                        time.sleep(0.05)
            except Exception as e:  # surfaced below, never swallowed
                errors.append(e)

        t = threading.Thread(target=drip, daemon=True)
        t.start()
        self.assertTrue(connected.wait(5), "client could not connect")
        began = time.monotonic()
        with self.assertRaises(TimeoutError):
            mg_oauth.wait_for_code(server, "good", 0.3)
        elapsed = time.monotonic() - began
        stop.set()
        t.join(5)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(sent), 3)  # the client really was dripping
        self.assertLess(elapsed, 2.0)

    def test_timer_start_failure_closes_streams(self):
        # N2: setup() raising means finish() is never called by socketserver
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        handler = mg_oauth.AuthHandler.__new__(mg_oauth.AuthHandler)
        handler.server = mock.Mock(auth_deadline=100.0, auth_clock=lambda: 50.0)
        handler.request, handler.connection = a, a
        with mock.patch.object(mg_oauth.threading.Timer, "start",
                               side_effect=RuntimeError("no thread")):
            with self.assertRaises(RuntimeError):
                handler.setup()
        self.assertTrue(handler.rfile.closed)
        self.assertTrue(handler.wfile.closed)

    def test_loop_timeouts_bounded_and_exit_at_deadline(self):
        clock, timeouts = {"t": 0.0}, []

        class Server:
            auth_result = None

            def handle_request(self):
                timeouts.append(self.timeout)
                clock["t"] += self.timeout  # nobody connects

        server = Server()
        with self.assertRaises(TimeoutError):
            mg_oauth.wait_for_code(server, "good", 12, lambda: clock["t"])
        self.assertEqual(timeouts, [5.0, 5.0, 2.0])
        self.assertEqual(server.auth_deadline, 12)


class HomeBeforeWriteTests(TempHome):
    """R17: MEETING_GUARD_HOME is validated before ANY write."""

    def setUp(self):
        super().setUp()
        self.bad = self.home / "shared"
        self.bad.mkdir()
        os.chmod(self.bad, 0o777)
        os.environ["MEETING_GUARD_HOME"] = str(self.bad)
        os.environ["MEETING_GUARD_CONFIG"] = str(self.home / "absent.json")
        for name in ("local_alarm", "fire_stage", "nag"):
            p = mock.patch.object(mg_engine, name)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        p = mock.patch.object(meeting_guard.os, "umask")
        p.start()
        self.addCleanup(p.stop)

    def cli(self, *args):
        with mock.patch.object(mg_config.sys, "stderr", io.StringIO()) as err:
            code = meeting_guard.cli(["meeting_guard.py", *args])
        return code, err.getvalue()

    def test_setup_logs_config_error_to_stderr_only(self):
        with mock.patch.object(mg_config.sys, "stderr", io.StringIO()) as err:
            mg_config.setup({"nag_minutes": "x"}, self.bad)
        self.assertIn("unsafe MEETING_GUARD_HOME", err.getvalue())
        self.assertIn("CONFIG ERROR: nag_minutes", err.getvalue())
        self.assertFalse(mg_config.LOG_TO_FILE)
        self.assertEqual(os.listdir(self.bad), [])

    def test_symlinked_home_not_written(self):
        link = self.home / "link"
        link.symlink_to(self.home / "target", target_is_directory=True)
        with mock.patch.object(mg_config.sys, "stderr", io.StringIO()):
            mg_config.setup({"nag_minutes": "x"}, link)
        self.assertEqual(mg_config.HOME_PROBLEM, "is a symlink")
        self.assertFalse((self.home / "target").exists())

    def test_missing_home_created_private(self):
        new = self.home / "a" / "b"
        mg_config.setup({}, new)
        self.assertIsNone(mg_config.HOME_PROBLEM)
        self.assertEqual(new.stat().st_mode & 0o777, 0o700)

    def test_simulate_refused(self):
        code, err = self.cli("--simulate")
        self.assertEqual(code, 1)
        self.fire_stage.assert_not_called()
        self.local_alarm.assert_called_once()
        self.assertIn("UNSAFE MEETING_GUARD_HOME", err)
        self.assertEqual(os.listdir(self.bad), [])

    def test_test_call_refused(self):
        with mock.patch.object(mg_call, "place_call") as call:
            code, _ = self.cli("--test-call")
        self.assertEqual(code, 1)
        call.assert_not_called()
        self.local_alarm.assert_called_once()
        self.assertEqual(os.listdir(self.bad), [])

    def dialog(self):
        clicked = mock.Mock(stdout="button returned:OK, gave up:false")
        with mock.patch.object(mg_actions.subprocess, "run", return_value=clicked):
            return self.cli("--dialog", "primary|ev1", "text", "5", "1")

    def test_dialog_ack_not_written_to_unsafe_home(self):
        _, err = self.dialog()
        self.assertIn("ack NOT recorded", err)
        self.assertEqual(os.listdir(self.bad), [])

    def test_dialog_ack_written_to_safe_home(self):
        os.chmod(self.bad, 0o700)
        self.dialog()
        acks = Path(self.bad) / "acks.txt"
        self.assertEqual(acks.read_text(), "primary|ev1\n")

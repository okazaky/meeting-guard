"""Round 3: ledger values (R3), log (R5), config validation (R6), OAuth state /
deadline (R14), token merge lock (R15), invalid_grant (R16), empty env (R20)."""
import io
import json
import os
import tempfile
import threading
import urllib.error
from pathlib import Path
from unittest import mock

from mgtest_helpers import FAKE_SID, REAL_PLACE_CALL, TempHome

import mg_call
import mg_config
import mg_google
import mg_http
import mg_oauth
import mg_schema
import mg_secrets

T0 = 1_800_000_000.0


class LedgerValueTests(TempHome):
    """R3: NaN / Infinity / implausible timestamps fail closed."""

    def setUp(self):
        super().setUp()
        self.write_private(self.env_file, "".join(
            f"{k}={v}\n" for k, v in (("TWILIO_ACCOUNT_SID", FAKE_SID),
                                      ("TWILIO_AUTH_TOKEN", "tok"),
                                      ("TWILIO_FROM_NUMBER", "+15555550188"),
                                      ("MEETING_GUARD_CALL_TO", "+15555550199"))))
        p = mock.patch.object(mg_http, "request_json", return_value={"status": "queued"})
        self.req = p.start()
        self.addCleanup(p.stop)

    def test_non_finite_and_implausible_refused(self):
        for ts in ("NaN", "Infinity", "-Infinity", "1e400", "5", "-1",
                   str(T0 + 2 * 86400), "true", '"1800000000"'):
            content = '{"attempts": [{"key": "c|old", "ts": %s}]}' % ts
            self.write_private(self.home / "calls.json", content)
            self.assertEqual(REAL_PLACE_CALL("c|new", "s", T0), mg_call.REFUSED, ts)
        self.req.assert_not_called()
        self.assertIn("call ledger unusable", self.log_text())

    def test_plausible_ledger_ok(self):
        self.write_private(self.home / "calls.json",
                           json.dumps({"attempts": [{"key": "c|old", "ts": T0 - 86400 * 2}]}))
        self.assertEqual(REAL_PLACE_CALL("c|new", "s", T0), mg_call.PLACED)


class LogNeverRaisesTests(TempHome):
    """R5: a log write failure never aborts an alarm."""

    def test_unwritable_log_falls_back_to_stderr(self):
        with mock.patch.object(mg_config, "_append_log", side_effect=OSError("disk full")), \
                mock.patch.object(mg_config.sys, "stderr") as err:
            mg_config.log("alarm text")
        self.assertIn("alarm text", err.write.call_args[0][0])

    def test_stderr_failure_swallowed(self):
        with mock.patch.object(mg_config, "_append_log", side_effect=OSError("x")), \
                mock.patch.object(mg_config.sys, "stderr", None):
            mg_config.log("x")  # no exception

    def test_logs_dir_is_file(self):
        (self.home / "logs").write_text("not a dir")
        with mock.patch.object(mg_config.sys, "stderr", io.StringIO()) as err:
            mg_config.log("still fine")
        self.assertIn("still fine", err.getvalue())

    def test_rotation_keeps_tail(self):
        mg_config.log("first")
        path = self.home / "logs" / "guard.log"
        with mock.patch.object(mg_config, "LOG_MAX_BYTES", 10):
            mg_config.log("second")
            mg_config.log("third")
        self.assertIn("third", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class SchemaTests(TempHome):
    """R6: type / range checks of known keys."""

    def problems(self, user):
        return mg_schema.validate(user)[1]

    def test_valid_example_config(self):
        example = json.loads((Path(mg_config.APP_DIR) / "config.example.json").read_text())
        self.assertEqual(self.problems(example), [])

    def test_bad_types_and_ranges(self):
        cases = {"auto_open": "false", "include_unaccepted": 1, "max_events_per_run": 2.5,
                 "nag_minutes": float("inf"), "nag_interval_sec": 0, "ntfy_topic": 5,
                 "calendars": [], "exclude_summary_contains": [1]}
        for key, value in cases.items():
            self.assertEqual(self.problems({key: value}), [key], key)
        self.assertEqual(self.problems({"calendars": "primary"}), ["calendars"])
        self.assertEqual(self.problems({"call": {"enabled": "true", "max_per_day": True}}),
                         ["call.enabled", "call.max_per_day"])
        self.assertEqual(self.problems({"stages": {"open0": "now"}}), ["stages.open0"])
        self.assertEqual(self.problems({"messages": {"open_say": 3}}), ["messages.open_say"])
        self.assertEqual(self.problems({"call": []}), ["call"])
        self.assertEqual(self.problems([]), ["(top level is not an object)"])

    def test_invalid_value_replaced_by_default_and_call_disabled(self):
        self.configure({"nag_minutes": -5, "calendars": ["work"],
                        "call": {"enabled": True, "max_per_day": 99}})
        cfg = mg_config.CONFIG
        self.assertEqual(cfg["nag_minutes"], mg_config.DEFAULT_CONFIG["nag_minutes"])
        self.assertEqual(cfg["calendars"], ["work"])  # valid keys are kept
        self.assertEqual(cfg["call"]["max_per_day"], 2)
        self.assertFalse(cfg["call"]["enabled"])
        self.assertEqual(mg_config.CONFIG_PROBLEMS, ["nag_minutes", "call.max_per_day"])
        self.assertIn("CONFIG ERROR: nag_minutes", self.log_text())

    def test_valid_config_keeps_call_enabled(self):
        self.configure({"call": {"enabled": True}})
        self.assertTrue(mg_config.CONFIG["call"]["enabled"])
        self.assertEqual(mg_config.CONFIG_PROBLEMS, [])

    def test_invalid_json_or_nan_falls_back(self):
        for text in ("{broken", '{"nag_minutes": NaN}', "[1]"):
            with tempfile.TemporaryDirectory() as d:
                cfg = Path(d) / "c.json"
                cfg.write_text(text)
                with mock.patch.dict(os.environ, {"MEETING_GUARD_CONFIG": str(cfg)}):
                    mg_config.setup(home=self.home)
            self.assertTrue(mg_config.CONFIG_PROBLEMS, text)
            self.assertFalse(mg_config.CONFIG["call"]["enabled"])
            self.assertEqual(mg_config.CONFIG["calendars"], ["primary"])


class OAuthHardeningTests(TempHome):
    """R14: malformed state never crashes; absolute deadline after each request."""

    def wait(self, results, timeout=300, step=1.0):
        clock = {"t": 0.0}

        class Server:
            auth_result = None

            def handle_request(self):
                clock["t"] += step
                self.auth_result = results.pop(0) if results else None

        with mock.patch("builtins.print"):
            return mg_oauth.wait_for_code(Server(), "good", timeout, lambda: clock["t"])

    def test_non_ascii_overlong_and_wrong_type_state_ignored(self):
        bad = [{"code": "x", "state": "g" + chr(0xE9) + "od"},
               {"code": "x", "state": "a" * 5000},
               {"code": "x", "state": ["good"]},
               {"code": "x"},
               {"code": "x", "state": ""}]
        self.assertEqual(self.wait(bad + [{"code": "ok", "state": "good"}]), "ok")

    def test_malformed_code_ignored(self):
        res = [{"code": "a b", "state": "good"}, {"code": "c" * 5000, "state": "good"},
               {"code": "4/0Ab-_.~", "state": "good"}]
        self.assertEqual(self.wait(res), "4/0Ab-_.~")

    def test_deadline_checked_after_handle_request(self):
        with self.assertRaises(TimeoutError):
            self.wait([{"code": "late", "state": "good"}], timeout=5, step=6)

    def test_non_ascii_error_does_not_crash(self):
        with self.assertRaises(RuntimeError) as cm:
            self.wait([{"error": chr(0x1F600), "state": "good"}])
        self.assertIn("(malformed)", str(cm.exception))


class TokenMergeLockTests(TempHome):
    """R15: re-read the latest file under the lock before merging."""

    def test_reread_under_lock(self):
        path = self.home / "cfg" / "tokens.json"
        path.parent.mkdir(mode=0o700)
        self.write_private(path, json.dumps({"a": {"refresh_token": "1"}}))
        self.configure({"tokens": str(path), "token_profile": "b"})
        seen = []
        real_lock = mg_config.file_lock

        def lock_then_concurrent_write(p):
            seen.append(Path(p).name)
            # another --auth finished just before we got the lock
            self.write_private(path, json.dumps({"a": {"refresh_token": "1"},
                                                 "c": {"refresh_token": "3"}}))
            return real_lock(p)

        with mock.patch.object(mg_config, "file_lock", side_effect=lock_then_concurrent_write):
            mg_oauth.write_tokens({"refresh_token": "2"})
        data = json.loads(path.read_text())
        self.assertEqual(set(data), {"a", "b", "c"})
        self.assertEqual(seen, ["tokens.json.lock"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_successive_profiles_all_kept(self):
        path = self.home / "cfg" / "tokens.json"
        for i in range(3):
            self.configure({"tokens": str(path), "token_profile": f"p{i}"})
            mg_oauth.write_tokens({"refresh_token": f"r{i}"})
        self.assertEqual(set(json.loads(path.read_text())), {"p0", "p1", "p2"})

    def test_lock_is_exclusive(self):
        """A second holder blocks until the first releases (same lock path)."""
        lock = self.home / "t.lock"
        order = []
        with mg_config.file_lock(lock):
            t = threading.Thread(target=self._take, args=(lock, order))
            t.start()
            t.join(0.3)
            order.append("first-released")
        t.join(5)
        self.assertEqual(order, ["first-released", "second"])

    @staticmethod
    def _take(lock, order):
        with mg_config.file_lock(lock):
            order.append("second")


class InvalidGrantTests(TempHome):
    """R16: Google invalid_grant becomes InvalidGrantError."""

    def http_error(self, code, body):
        return urllib.error.HTTPError("https://oauth2.example.invalid/token", code, "x", {},
                                      io.BytesIO(body))

    def test_invalid_grant_detected(self):
        err = self.http_error(400, b'{"error": "invalid_grant", "error_description": "x"}')
        with mock.patch.object(mg_http, "request_json", side_effect=err):
            with self.assertRaises(mg_google.InvalidGrantError):
                mg_google.post_token({"a": "b"})

    def test_other_errors_unchanged(self):
        for code, body in ((400, b'{"error": "invalid_client"}'), (500, b"oops"),
                           (400, b"\xff\xfe not json")):
            with mock.patch.object(mg_http, "request_json",
                                   side_effect=self.http_error(code, body)):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    mg_google.post_token({"a": "b"})
            self.assertNotIsInstance(cm.exception, mg_google.InvalidGrantError)


class EmptyEnvDisablesTests(TempHome):
    """R20: an env var set to "" disables the value (unset = not overridden)."""

    def test_empty_ntfy_topic_disables_push(self):
        self.write_private(self.env_file, "NTFY_TOPIC=from-file\nNTFY_TOKEN=tk\n")
        self.configure({"ntfy_topic": "from-config"})
        self.assertEqual(mg_secrets.ntfy_topic(), "from-file")
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "", "NTFY_TOKEN": ""}):
            self.assertEqual(mg_secrets.ntfy_topic(), "")
            self.assertEqual(mg_secrets.ntfy_token(), "")

    def test_unset_does_not_override(self):
        self.configure({"ntfy_topic": "from-config"})
        self.assertNotIn("NTFY_TOPIC", os.environ)
        self.assertEqual(mg_secrets.ntfy_topic(), "from-config")

    def test_empty_call_to_disables_call(self):
        self.write_private(self.env_file, "".join(
            f"{k}={v}\n" for k, v in (("TWILIO_ACCOUNT_SID", FAKE_SID),
                                      ("TWILIO_AUTH_TOKEN", "tok"),
                                      ("TWILIO_FROM_NUMBER", "+15555550188"),
                                      ("MEETING_GUARD_CALL_TO", "+15555550199"))))
        with mock.patch.dict(os.environ, {"MEETING_GUARD_CALL_TO": ""}), \
                mock.patch.object(mg_http, "request_json") as req:
            self.assertEqual(REAL_PLACE_CALL("c|a", "s", T0), mg_call.REFUSED)
        req.assert_not_called()
        self.assertIn("Twilio settings missing", self.log_text())

"""H2 call ledger (fail closed, limits) and M2 Twilio settings."""
import base64
import json
import os
import urllib.parse
from unittest import mock

from mgtest_helpers import (FAKE_KEY_SID, FAKE_SID, REAL_PLACE_CALL, FakeResponse,
                            TempHome)

import mg_call
import mg_config
import mg_http

TO = "+15555550199"
FROM = "+15555550188"
T0 = 1_800_000_000.0


class CallBase(TempHome):
    def setUp(self):
        super().setUp()
        self.write_env()
        self.calls = []
        p = mock.patch.object(mg_http, "request_json", side_effect=self._fake_twilio)
        p.start()
        self.addCleanup(p.stop)

    def _fake_twilio(self, req, timeout):
        self.ledger_at_request = self.ledger()
        self.calls.append(req)
        return {"sid": "CA" + "0" * 32, "status": "queued"}

    def write_env(self, **over):
        values = {"TWILIO_ACCOUNT_SID": FAKE_SID, "TWILIO_AUTH_TOKEN": "tok",
                  "TWILIO_FROM_NUMBER": FROM, "MEETING_GUARD_CALL_TO": TO}
        values.update(over)
        text = "".join(f"{k}={v}\n" for k, v in values.items())
        self.write_private(self.env_file, text)

    def ledger(self):
        path = self.home / "calls.json"
        return json.loads(path.read_text()) if path.exists() else None

    def status(self, key="primary|ev1", ts=T0):
        return REAL_PLACE_CALL(key, "Weekly sync", ts)

    def call(self, key="primary|ev1", ts=T0):
        return self.status(key, ts) == mg_call.PLACED


class LedgerTests(CallBase):
    def test_attempt_persisted_before_request(self):
        self.assertTrue(self.call())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger_at_request["attempts"],
                         [{"key": "primary|ev1", "ts": T0}])
        self.assertEqual((self.home / "calls.json").stat().st_mode & 0o777, 0o600)

    def test_same_event_never_called_twice(self):
        self.call()
        self.assertFalse(self.call(ts=T0 + 7200))
        self.assertEqual(len(self.calls), 1)
        self.assertIn("already attempted", self.log_text())

    def test_failed_call_not_retried(self):
        with mock.patch.object(mg_http, "request_json", side_effect=OSError("boom")):
            self.assertFalse(self.call())
        self.assertFalse(self.call(ts=T0 + 7200))
        self.assertEqual(len(self.ledger()["attempts"]), 1)
        self.assertIn("not retried", self.log_text())

    def test_min_interval(self):
        self.call("c|a", T0)
        self.assertFalse(self.call("c|b", T0 + 29 * 60))
        self.assertTrue(self.call("c|b", T0 + 31 * 60))

    def test_max_per_day_default_2(self):
        self.assertTrue(self.call("c|a", T0))
        self.assertTrue(self.call("c|b", T0 + 3600))
        self.assertFalse(self.call("c|c", T0 + 7200))
        self.assertIn("max_per_day", self.log_text())
        self.assertTrue(self.call("c|d", T0 + 86400 + 1))

    def test_limits_configurable(self):
        self.configure({"call": {"max_per_day": 1, "min_interval_min": 0}})
        self.assertTrue(self.call("c|a", T0))
        self.assertFalse(self.call("c|b", T0 + 60))

    def test_corrupt_ledger_fails_closed(self):
        for content in ("{not json", '{"attempts": "x"}', '[1, 2]',
                        '{"attempts": [{"key": 1, "ts": 2}]}'):
            self.write_private(self.home / "calls.json", content)
            self.assertFalse(self.call(), content)
        self.assertEqual(self.calls, [])
        self.assertIn("call ledger unusable", self.log_text())

    def test_insecure_ledger_fails_closed(self):
        self.write_private(self.home / "calls.json", '{"attempts": []}', 0o644)
        self.assertFalse(self.call())
        self.assertEqual(self.calls, [])

    def test_symlink_ledger_fails_closed(self):
        real = self.write_private(self.home / "other.json", '{"attempts": []}')
        os.symlink(real, self.home / "calls.json")
        self.assertFalse(self.call())
        self.assertEqual(self.calls, [])

    def test_unwritable_ledger_fails_closed(self):
        with mock.patch.object(mg_config, "atomic_write", side_effect=OSError("ro")):
            self.assertFalse(self.call())
        self.assertEqual(self.calls, [])
        self.assertIn("cannot write call ledger", self.log_text())

    def test_statuses(self):
        self.assertEqual(self.status("c|a", T0), mg_call.PLACED)
        self.assertEqual(self.status("c|a", T0 + 7200), mg_call.ALREADY)
        self.assertEqual(self.status("c|b", T0 + 60), mg_call.REFUSED)  # min interval
        with mock.patch.object(mg_http, "request_json", side_effect=OSError("x")):
            self.assertEqual(self.status("c|c", T0 + 3600), mg_call.FAILED)
        self.assertEqual(self.status("c|d", T0 + 7200), mg_call.REFUSED)  # max_per_day
        self.write_private(self.home / "calls.json", "{broken")
        self.assertEqual(self.status("c|e", T0 + 99999), mg_call.REFUSED)

    def test_limit_refusal_does_not_record_event(self):
        self.status("c|a", T0)
        self.assertEqual(self.status("c|b", T0 + 60), mg_call.REFUSED)
        self.assertEqual([a["key"] for a in self.ledger()["attempts"]], ["c|a"])
        self.assertEqual(self.status("c|b", T0 + 31 * 60), mg_call.PLACED)

    def test_old_entries_pruned_on_write(self):
        self.write_private(self.home / "calls.json", json.dumps(
            {"attempts": [{"key": "c|old", "ts": T0 - 40 * 86400}]}))
        self.call()
        self.assertEqual([a["key"] for a in self.ledger()["attempts"]], ["primary|ev1"])


class TwilioSettingsTests(CallBase):
    def body(self):
        return urllib.parse.parse_qs(self.calls[0].data.decode())

    def assert_refused(self, why):
        self.assertFalse(self.call())
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.ledger())
        self.assertIn(why, self.log_text())

    def test_generic_message_by_default(self):
        self.call()
        twiml = self.body()["Twiml"][0]
        self.assertNotIn("Weekly sync", twiml)
        self.assertIn("今すぐ参加してください", twiml)

    def test_include_summary_opt_in(self):
        self.configure({"call_include_summary": True})
        self.call()
        self.assertIn("Weekly sync", self.body()["Twiml"][0])

    def test_to_must_be_e164(self):
        arabic_indic = "".join(chr(0x0660 + int(d)) for d in "5555550199")
        fullwidth = "".join(chr(0xFF10 + int(d)) for d in "5555550199")
        for bad in ("0901234", "+0123456789", "+1234", "+1" + "2" * 15, "1555550199",
                    "+1" + arabic_indic, "+1" + fullwidth):
            self.write_env(MEETING_GUARD_CALL_TO=bad)
            self.assert_refused("E.164")

    def test_trailing_newline_from_environment_refused(self):
        # secrets.env strips lines; the process environment does not.
        for name, value in (("MEETING_GUARD_CALL_TO", TO + "\n"),
                            ("TWILIO_FROM_NUMBER", FROM + "\n")):
            with mock.patch.dict(os.environ, {name: value}):
                self.assert_refused("E.164")

    def test_e164_regex_ascii_fullmatch(self):
        self.assertTrue(mg_call.E164_RE.fullmatch(TO))
        self.assertIsNone(mg_call.E164_RE.fullmatch(TO + "\n"))
        self.assertIsNone(mg_call.E164_RE.fullmatch("+1" + chr(0x0661) * 9))

    def test_sid_regex_fullmatch(self):
        self.assertIsNone(mg_call.TWILIO_SID_RE.fullmatch(FAKE_SID + "\n"))
        self.assertIsNone(mg_call.TWILIO_KEY_SID_RE.fullmatch(FAKE_KEY_SID + "\n"))
        self.assertIsNone(mg_call.TWILIO_SID_RE.fullmatch("AC" + chr(0x0661) * 32))

    def test_from_must_be_e164(self):
        for bad in ("+1" + chr(0xFF15) * 10, "+1" + chr(0x0665) * 10):
            self.write_env(TWILIO_FROM_NUMBER=bad)
            self.assert_refused("E.164")

    def test_allowed_to_prefix(self):
        self.configure({"call": {"allowed_to_prefix": "+44"}})
        self.assert_refused("allowed_to_prefix")

    def test_allowed_from_prefix(self):
        self.configure({"call": {"allowed_from_prefix": "+44"}})
        self.assert_refused("allowed_from_prefix")

    def test_placeholders_refused(self):
        self.write_env(MEETING_GUARD_CALL_TO="+810000000000")
        self.assert_refused("placeholder")
        self.write_env(TWILIO_AUTH_TOKEN="your_auth_token_here")
        self.assert_refused("placeholder")

    def test_missing_values_refused(self):
        self.write_env(MEETING_GUARD_CALL_TO="")
        self.assert_refused("missing")

    def test_same_number_refused(self):
        self.write_env(MEETING_GUARD_CALL_TO=FROM)
        self.assert_refused("same number")

    def test_auth_token_basic_auth(self):
        self.call()
        req = self.calls[0]
        self.assertIn(f"/Accounts/{FAKE_SID}/Calls.json", req.full_url)
        expected = base64.b64encode(f"{FAKE_SID}:tok".encode()).decode()
        self.assertEqual(req.get_header("Authorization"), "Basic " + expected)

    def test_api_key_preferred(self):
        self.write_env(TWILIO_API_KEY_SID=FAKE_KEY_SID, TWILIO_API_KEY_SECRET="ksecret")
        self.call()
        req = self.calls[0]
        self.assertIn(f"/Accounts/{FAKE_SID}/Calls.json", req.full_url)
        expected = base64.b64encode(f"{FAKE_KEY_SID}:ksecret".encode()).decode()
        self.assertEqual(req.get_header("Authorization"), "Basic " + expected)

    def test_api_key_bad_format_refused(self):
        self.write_env(TWILIO_API_KEY_SID="XX123", TWILIO_API_KEY_SECRET="s")
        self.assert_refused("missing or invalid")

    def test_secrets_not_logged(self):
        self.call()
        log = self.log_text()
        for secret in ("tok", TO, FROM, FAKE_SID):
            self.assertNotIn(secret, log)
        self.assertIn("0199", log)

    def test_test_call_goes_through_ledger(self):
        import meeting_guard
        with mock.patch("builtins.print"), \
                mock.patch.object(mg_call, "place_call", wraps=REAL_PLACE_CALL) as pc:
            meeting_guard.test_call()
            meeting_guard.test_call()
        self.assertEqual(pc.call_count, 2)
        self.assertEqual(len(self.calls), 1)  # second blocked by min_interval_min
        self.assertTrue(self.ledger()["attempts"][0]["key"].startswith("test-call|"))

    def test_mask_phone(self):
        from mg_text import mask_phone
        self.assertEqual(mask_phone(TO), "********0199")
        self.assertEqual(mask_phone("123"), "***")


class TwimlTests(TempHome):
    def test_twiml_escaped_and_configurable(self):
        self.configure({"twilio_voice": 'Polly."x', "twilio_language": "en-US"})
        twiml = mg_call.build_twiml("Join <b>&")
        self.assertIn("Join &lt;b&gt;&amp;", twiml)
        self.assertIn('voice="Polly.&quot;x"', twiml)
        self.assertIn('language="en-US"', twiml)

    def test_fake_response_helper(self):
        self.assertEqual(FakeResponse(b"x").read(), b"x")

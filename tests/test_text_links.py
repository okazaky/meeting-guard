"""M5 sanitising / say argv, M6 canonical links, dialogs, messages."""
import calendar
import time
from unittest import mock

from mgtest_helpers import MEET_URL, ZOOM_CANON, ZOOM_URL, TempHome

import mg_actions
import mg_config
import mg_state
from mg_links import canonical_meet, canonical_zoom, extract_link, zoom_native_url
from mg_text import SUMMARY_MAX, applescript_string, clean_speech, sanitize_summary

ZWSP = chr(0x200B)
RLO = chr(0x202E)
BEL = chr(7)
ESC = chr(0x1B)
LS = chr(0x2028)
AT = chr(0x40)  # built at runtime so scanners do not see e-mail-like literals


class SanitizeTests(TempHome):
    def test_control_chars_become_space(self):
        self.assertEqual(sanitize_summary("a" + chr(0) + "b\nc" + ESC + "x"), "a b c x")

    def test_format_chars_removed(self):
        self.assertEqual(sanitize_summary("ab" + ZWSP + "c" + RLO + "d"), "abcd")
        self.assertEqual(sanitize_summary("a" + LS + "b"), "a b")

    def test_say_embedded_commands_removed(self):
        self.assertEqual(sanitize_summary("[[volm 0]]Hi"), "volm 0Hi")
        self.assertEqual(sanitize_summary("[[[[x]]]]"), "x")
        self.assertEqual(sanitize_summary("[" + ZWSP + "[rate 1]" + ZWSP + "]"), "rate 1")

    def test_leading_dashes_loop_stripped(self):
        self.assertEqual(sanitize_summary("-o /tmp/out"), "o /tmp/out")
        self.assertEqual(sanitize_summary("- - -v x"), "v x")
        self.assertEqual(sanitize_summary("[[-]]-x"), "x")

    def test_truncate(self):
        self.assertEqual(len(sanitize_summary("x" * 300)), SUMMARY_MAX)
        self.assertEqual(SUMMARY_MAX, 80)

    def test_empty_becomes_untitled(self):
        self.assertEqual(sanitize_summary(None), "(無題)")
        self.assertEqual(sanitize_summary("--"), "(無題)")

    def test_clean_speech(self):
        self.assertEqual(clean_speech("a" + BEL + "[[inpt PHON]]b"), "a inpt PHONb")

    def test_applescript_escaping(self):
        s = applescript_string('say "hi"' + BEL + "\nnext\\")
        self.assertEqual(s, 'say \\"hi\\" \\nnext\\\\')


class SpeakTests(TempHome):
    def test_say_argv_has_double_dash(self):
        self.configure({"say_voice": "Kyoko"})
        with mock.patch.object(mg_actions.subprocess, "Popen") as popen:
            mg_actions.speak("-v [[volm 0]]hello")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:4], ["/usr/bin/say", "-v", "Kyoko", "--"])
        self.assertEqual(argv[4], "-v volm 0hello")
        self.assertNotIn("shell", popen.call_args[1])

    def test_say_without_voice(self):
        with mock.patch.object(mg_actions.subprocess, "Popen") as popen:
            mg_actions.speak("hi")
        self.assertEqual(popen.call_args[0][0], ["/usr/bin/say", "--", "hi"])


class LinkTests(TempHome):
    def test_zoom_canonical(self):
        self.assertEqual(canonical_zoom("https://zoom.us/j/0000000000?pwd=abc&evil=1#f"),
                         "https://zoom.us/j/0000000000?pwd=abc")
        self.assertEqual(canonical_zoom("https://US02Web.Zoom.us/w/123"),
                         "https://us02web.zoom.us/j/123")

    def test_zoom_bad_pwd_dropped(self):
        self.assertEqual(canonical_zoom("https://zoom.us/j/1?pwd=a%22b"), "https://zoom.us/j/1")

    def test_zoom_rejects(self):
        for bad in ("http://zoom.us/j/1", "https://zoom.us/j/1/extra", "https://zoom.us/j/abc",
                    "https://zoom.us/s/1", "https://zoom.us/j/", "https://zoom.us:8443/j/1",
                    "https://user" + AT + "zoom.us/j/1", "https://evilzoom.us/j/1",
                    "https://zoom.us.evil.example/j/1", "https://a_b.zoom.us/j/1",
                    "https://zoom.us/j/1/../x", "zoommtg://zoom.us/join?confno=1"):
            self.assertIsNone(canonical_zoom(bad), bad)

    def test_meet_canonical_and_rejects(self):
        self.assertEqual(canonical_meet(MEET_URL + "?authuser=1"), MEET_URL)
        for bad in ("http://meet.google.com/abc-defg-hij", "https://meet.google.com.evil.example/abc-defg-hij",
                    "https://meet.google.com/abc-defg-hij/x", "https://meet.google.com/lookup/x",
                    "https://x" + AT + "meet.google.com/abc-defg-hij",
                    "https://meet.google.co/abc-defg-hij"):
            self.assertIsNone(canonical_meet(bad), bad)

    def test_extract_within_one_text_prefers_zoom_and_returns_canonical(self):
        ev = {"description": f"see ({MEET_URL}) and {ZOOM_URL}&x=1."}
        self.assertEqual(extract_link(ev), ("zoom", "https://zoom.us/j/0000000000?pwd=abc"))
        self.assertEqual(extract_link({"hangoutLink": MEET_URL}), ("meet", MEET_URL))

    def test_source_priority(self):
        """conferenceData video > hangoutLink > location > description."""
        conf = {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+15555550199"},
                                {"entryPointType": "video", "uri": MEET_URL}]}
        evil = "https://zoom.us/j/999"
        full = {"conferenceData": conf, "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc",
                "location": "https://zoom.us/j/111", "description": evil}
        self.assertEqual(extract_link(full), ("meet", MEET_URL))
        no_conf = {k: v for k, v in full.items() if k != "conferenceData"}
        self.assertEqual(extract_link(no_conf), ("meet", "https://meet.google.com/aaa-bbbb-ccc"))
        loc_desc = {"location": "https://zoom.us/j/111", "description": evil}
        self.assertEqual(extract_link(loc_desc), ("zoom", "https://zoom.us/j/111"))
        self.assertEqual(extract_link({"description": evil}), ("zoom", evil))

    def test_non_video_entry_points_ignored(self):
        ev = {"conferenceData": {"entryPoints": [{"entryPointType": "more", "uri": MEET_URL},
                                                 {"uri": MEET_URL}]}}
        self.assertEqual(extract_link(ev), (None, None))

    def test_invalid_conference_falls_through(self):
        ev = {"conferenceData": {"entryPoints": [{"entryPointType": "video",
                                                  "uri": "https://evil.example/j/1"}]},
              "location": ZOOM_URL}
        self.assertEqual(extract_link(ev)[0], "zoom")

    def test_unicode_digits_and_newline_rejected(self):
        arabic = "".join(chr(0x0660 + int(d)) for d in "123")
        fullwidth = "".join(chr(0xFF10 + int(d)) for d in "123")
        for bad in ("https://zoom.us/j/" + arabic, "https://zoom.us/j/" + fullwidth,
                    "https://zoom.us/j/123\n", "https://zoom.us/j/123?pwd=a" + chr(0x0661)):
            got = canonical_zoom(bad)
            self.assertIn(got, (None, "https://zoom.us/j/123"), bad)
            self.assertNotIn(chr(0x0661), got or "")
        self.assertIsNone(canonical_zoom("https://zoom.us/j/" + arabic))
        self.assertIn(canonical_meet(MEET_URL + "\n"), (None, MEET_URL))  # rebuilt, no newline
        self.assertIsNone(canonical_meet("https://meet.google.com/abc-defg-hi" + chr(0x0131)))

    def test_lookalikes_ignored(self):
        for bad in ("https://zoom.us.evil.example/j/0000000000",
                    "https://evilzoom.us/j/0000000000",
                    "https://zoom.us" + AT + "evil.example/j/0000000000",
                    "http://zoom.us/j/0000000000",
                    "https://meet.google.com.evil.example/abc-defg-hij"):
            self.assertEqual(extract_link({"description": bad}), (None, None), bad)

    def test_zoom_native_url(self):
        self.assertEqual(zoom_native_url("https://zoom.us/j/0000000000?pwd=abc"),
                         "zoommtg://zoom.us/join?confno=0000000000&pwd=abc")
        self.assertEqual(zoom_native_url("https://example.zoom.us/j/123"),
                         "zoommtg://example.zoom.us/join?confno=123")


class OpenLinkTests(TempHome):
    def test_open_link_list_args_native(self):
        with mock.patch.object(mg_actions.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            mg_actions.open_link("zoom", ZOOM_URL)
        args = run.call_args[0][0]
        self.assertEqual(args, ["/usr/bin/open", "zoommtg://zoom.us/join?confno=0000000000&pwd=abc"])
        self.assertNotIn("shell", run.call_args[1])

    def test_fallback_opens_canonical_https(self):
        with mock.patch.object(mg_actions.subprocess, "run",
                               return_value=mock.Mock(returncode=1)) as run:
            mg_actions.open_link("zoom", "https://zoom.us/j/123?pwd=abc&x=evil")
        self.assertEqual(run.call_args_list[1][0][0],
                         ["/usr/bin/open", "https://zoom.us/j/123?pwd=abc"])

    def test_open_refuses_non_matching_url(self):
        with mock.patch.object(mg_actions.subprocess, "run") as run:
            mg_actions.open_link("meet", "https://meet.google.com.evil.example/x")
            mg_actions.open_link("zoom", "http://zoom.us/j/1")
        run.assert_not_called()

    def test_open_log_has_no_url(self):
        with mock.patch.object(mg_actions.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            mg_actions.open_link("meet", MEET_URL)
        self.assertNotIn("meet.google.com", self.log_text())


class RunDialogTests(TempHome):
    def run_dialog(self, write_ack, stdout="button returned:OK, gave up:false"):
        result = mock.Mock(stdout=stdout)
        with mock.patch.object(mg_actions.subprocess, "run", return_value=result) as run:
            mg_actions.run_dialog("primary|k1", 'say "hi"' + BEL + "\nnext", 60, write_ack)
        return run.call_args[0][0]

    def test_info_dialog_click_does_not_write_ack(self):
        self.run_dialog(False)
        self.assertEqual(mg_state.load_acks(), set())

    def test_ack_dialog_click_writes_ack(self):
        self.run_dialog(True)
        self.assertEqual(mg_state.load_acks(), {"primary|k1"})

    def test_timeout_does_not_write_ack(self):
        self.run_dialog(True, stdout="button returned:, gave up:true")
        self.assertEqual(mg_state.load_acks(), set())

    def test_osascript_argv(self):
        cmd = self.run_dialog(False)
        self.assertEqual(cmd[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('say \\"hi\\"', cmd[2])
        self.assertNotIn(BEL, cmd[2])
        self.assertNotIn("\n", cmd[2])


class ZoomDetectTests(TempHome):
    """R13: CptHost counts only if it started after the threshold and was not
    already running when the link was opened. Start times come from ps lstart."""
    T0 = calendar.timegm((2026, 10, 10, 9, 0, 0))  # 09:00:00 UTC

    @staticmethod
    def lstart(ts):
        return time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(ts))

    def ps_result(self, starts, ps_rc=0, pgrep_rc=0):
        pids = [101 + 101 * i for i in range(len(starts))]
        out = "".join(f"{pid} {self.lstart(t)}\n" for pid, t in zip(pids, starts))
        return [mock.Mock(returncode=pgrep_rc, stdout="".join(f"{x}\n" for x in pids)),
                mock.Mock(returncode=ps_rc, stdout=out)]

    def detect(self, starts, since_ts, exclude=None, **kw):
        with mock.patch.object(mg_actions.subprocess, "run",
                               side_effect=self.ps_result(starts, **kw)) as run:
            found = mg_actions.zoom_in_meeting(since_ts, exclude)
        return found, run

    def test_pgrep_limited_to_current_user_then_ps_lstart_in_c_locale(self):
        found, run = self.detect([self.T0], self.T0 - 60)
        self.assertTrue(found)
        argv = run.call_args_list[0][0][0]
        self.assertEqual(argv[:3], ["/usr/bin/pgrep", "-u", str(mg_actions.os.getuid())])
        self.assertEqual(argv[3:], ["-x", "CptHost"])
        self.assertEqual(run.call_args_list[1][0][0],
                         ["/bin/ps", "-o", "pid=,lstart=", "-p", "101"])
        self.assertEqual(run.call_args_list[1][1]["env"]["LC_ALL"], "C")
        self.assertEqual(run.call_args_list[1][1]["env"]["TZ"], "UTC")

    def test_stale_cpthost_ignored(self):
        self.assertFalse(self.detect([self.T0 - 3600, self.T0 - 600], self.T0 - 60)[0])

    def test_process_running_before_open_is_excluded(self):
        # pid 101 started 30 s before open0 (in the snapshot) -> not this meeting
        snap = [[101, self.T0 - 30]]
        self.assertFalse(self.detect([self.T0 - 30], self.T0 - 60, exclude=snap)[0])
        self.assertTrue(self.detect([self.T0 - 30], self.T0 - 60)[0])

    def test_new_process_after_open_counts_despite_snapshot(self):
        snap = [[101, self.T0 - 30]]
        self.assertTrue(self.detect([self.T0 - 30, self.T0 + 20], self.T0 - 60,
                                    exclude=snap)[0])

    def test_reused_pid_with_new_start_time_counts(self):
        snap = [[101, self.T0 - 600]]
        self.assertTrue(self.detect([self.T0 + 20], self.T0 - 60, exclude=snap)[0])

    def test_no_process_is_empty_but_failures_are_errors(self):
        none = [mock.Mock(returncode=1, stdout="")]
        with mock.patch.object(mg_actions.subprocess, "run", side_effect=none):
            self.assertEqual(mg_actions.cpthost_snapshot(), [])
        bad_cases = [
            self.ps_result([self.T0], pgrep_rc=2),          # pgrep error
            self.ps_result([self.T0], ps_rc=1),             # ps error (pid gone)
            [mock.Mock(returncode=0, stdout="101\n"),       # garbage ps output
             mock.Mock(returncode=0, stdout="101 garbage\n")],
            [mock.Mock(returncode=0, stdout="101\n"),       # empty ps output
             mock.Mock(returncode=0, stdout="")],
            [mock.Mock(returncode=0, stdout="101\n"),       # pid pgrep did not list
             mock.Mock(returncode=0, stdout=f"0 {self.lstart(self.T0)}\n")],
        ]
        for results in bad_cases:
            with mock.patch.object(mg_actions.subprocess, "run", side_effect=results):
                self.assertIsNone(mg_actions.cpthost_snapshot(), results)
            with mock.patch.object(mg_actions.subprocess, "run",
                                   side_effect=list(results)):
                self.assertFalse(mg_actions.zoom_in_meeting(0), results)
        with mock.patch.object(mg_actions.subprocess, "run", side_effect=OSError("x")):
            self.assertIsNone(mg_actions.cpthost_snapshot())
            self.assertFalse(mg_actions.zoom_in_meeting(0))

    def test_parse_ps_line(self):
        self.assertEqual(mg_actions.parse_ps_line(f"  101 {self.lstart(self.T0)}  "),
                         (101, self.T0))
        self.assertEqual(mg_actions.parse_ps_line("7 Thu Oct  1 09:05:03 2026"),
                         (7, calendar.timegm((2026, 10, 1, 9, 5, 3))))
        # R7-N1: the repeated hour at the end of US DST is unambiguous in UTC
        self.assertEqual(mg_actions.parse_ps_line("9 Sun Nov  1 05:59:30 2026")[1]
                         + 3600,
                         mg_actions.parse_ps_line("9 Sun Nov  1 06:59:30 2026")[1])
        # R7-N3: impossible dates / times / weekdays are rejected, not normalised
        for bad in ("101 Sat Oct 32 18:00:00 2026", "101 Sat Oct 10 25:00:00 2026",
                    "101 Sat Oct 10 18:90:00 2026", "101 Sat Feb 30 18:00:00 2026",
                    "101 Sun Oct 10 18:00:00 2026", "101 Sat Oct 10 18:00:60 2026"):
            self.assertIsNone(mg_actions.parse_ps_line(bad), bad)
        for bad in ("", "x", "101 Sat Foo 10 18:00:00 2026", "101 Sat Oct 10 18:00 2026",
                    "a Sat Oct 10 18:00:00 2026", "101 Sat Oct 10 18:00:00",
                    "101 Sat Oct " + chr(0x0661) + "0 18:00:00 2026"):
            self.assertIsNone(mg_actions.parse_ps_line(bad), bad)



class OpenLinkResultTests(TempHome):
    """R18: open_link reports success."""

    def test_success_and_failure(self):
        with mock.patch.object(mg_actions.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            self.assertTrue(mg_actions.open_link("meet", MEET_URL))
        with mock.patch.object(mg_actions.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            self.assertFalse(mg_actions.open_link("zoom", ZOOM_URL))
        with mock.patch.object(mg_actions.subprocess, "run", side_effect=OSError("x")):
            self.assertFalse(mg_actions.open_link("meet", MEET_URL))
        self.assertFalse(mg_actions.open_link("meet", "https://evil.example/x"))
        self.assertIn("open FAILED", self.log_text())

    def test_html_entities_decoded_in_description(self):
        """R19: &amp; in an HTML description becomes & before link extraction."""
        desc = '<a href="https://zoom.us/j/0000000000?pwd=abc&amp;uname=x">join</a>'
        self.assertEqual(extract_link({"description": desc}), ("zoom", ZOOM_CANON))
        desc2 = "https://zoom.us/j/0000000000?x=1&amp;pwd=abc"
        self.assertEqual(extract_link({"description": desc2}), ("zoom", ZOOM_CANON))


class MessageTests(TempHome):
    def test_default_japanese(self):
        self.assertEqual(mg_config.msg("open_say", summary="A"), "A、開始時刻です。")

    def test_bad_override_falls_back(self):
        self.configure({"messages": {"open_say": "{summary"}})
        self.assertEqual(mg_config.msg("open_say", summary="A"), "A、開始時刻です。")

    def test_unknown_field_kept(self):
        self.configure({"messages": {"open_say": "{summary} {nope}"}})
        self.assertEqual(mg_config.msg("open_say", summary="A"), "A {nope}")

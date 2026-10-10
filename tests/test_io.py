"""M1 ntfy, M4 redirects/pagination, M7 private files/tokens, M8 OAuth, M9 logs, plist."""
import http.server
import json
import os
import plistlib
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from mgtest_helpers import NOW, REAL_PUSH, FakeResponse, TempHome

import meeting_guard
import mg_config
import mg_google
import mg_http
import mg_oauth
import mg_plist
import mg_secrets


def close_err(e):
    """HTTPError.close() fails on Python 3.9 when fp is None."""
    if "file" in e.__dict__:
        e.close()


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        _RedirectHandler.hits.append(self.path)
        self.send_response(302)
        self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class RedirectTests(TempHome):
    def test_handler_raises(self):
        req = urllib.request.Request("https://example.invalid/a")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            mg_http.NoRedirect().redirect_request(req, None, 302, "Found", {},
                                                  "https://evil.example/")
        self.assertEqual(cm.exception.code, 302)
        close_err(cm.exception)

    def test_opener_has_no_default_redirect_handler(self):
        kinds = [type(h) for h in mg_http.OPENER.handlers]
        self.assertIn(mg_http.NoRedirect, kinds)
        self.assertNotIn(urllib.request.HTTPRedirectHandler, kinds)

    def test_loopback_redirect_not_followed(self):
        _RedirectHandler.hits = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/start"
            req = urllib.request.Request(url, headers={"Authorization": "Bearer x"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                mg_http.request_json(req, 5)
            close_err(cm.exception)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(_RedirectHandler.hits, ["/start"])

    def test_env_proxies_ignored(self):
        # ProxyHandler({}) replaces the default env-reading ProxyHandler and,
        # having no proxies, registers no *_open methods at all.
        proxies = [h for h in mg_http.OPENER.handlers
                   if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(all(h.proxies == {} for h in proxies))
        with mock.patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:9"}):
            default = urllib.request.build_opener()
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies
                            for h in default.handlers))

    def test_loopback_request_bypasses_env_proxy(self):
        _RedirectHandler.hits = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
               "https_proxy": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
        try:
            url = f"http://localhost:{server.server_address[1]}/direct"
            with mock.patch.dict(os.environ, env):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    mg_http.request_json(urllib.request.Request(url), 5)
                close_err(cm.exception)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(_RedirectHandler.hits, ["/direct"])

    def test_all_authenticated_calls_use_opener(self):
        with mock.patch.object(mg_http, "open_request",
                               return_value=FakeResponse(b'{"access_token": "a"}')) as op:
            mg_google.post_token({"x": "y"})
            mg_google.api_get("/p", "tok", {"a": "b"})
        self.assertEqual(op.call_count, 2)


class PaginationTests(TempHome):
    def test_follows_next_page_token_max_5(self):
        pages = [{"items": [{"id": str(i)}], "nextPageToken": f"p{i}"} for i in range(9)]
        with mock.patch.object(mg_google, "api_get", side_effect=pages) as get:
            items = mg_google.list_events("t", "primary", NOW, NOW, 30)
        self.assertEqual(len(items), 5)
        self.assertEqual(get.call_count, 5)
        self.assertNotIn("pageToken", get.call_args_list[0][0][2])
        self.assertEqual(get.call_args_list[1][0][2]["pageToken"], "p0")

    def test_stops_without_token(self):
        pages = [{"items": [{"id": "a"}], "nextPageToken": "n"}, {"items": [{"id": "b"}]}]
        with mock.patch.object(mg_google, "api_get", side_effect=pages) as get:
            items = mg_google.list_events("t", "primary", NOW, NOW, 30)
        self.assertEqual([i["id"] for i in items], ["a", "b"])
        self.assertEqual(get.call_count, 2)

    def test_fetch_returns_calendar_pairs(self):
        self.configure({"calendars": ["a", "b"]})
        with mock.patch.object(mg_google, "list_events_paged",
                               return_value=([{"id": "x"}], False)):
            result = mg_google.fetch_events("t", NOW)
        self.assertEqual(result.pairs, [("a", {"id": "x"}), ("b", {"id": "x"})])
        self.assertEqual((result.failed, result.truncated), ([], []))

    def test_truncation_reported(self):
        """R10: a page token left after MAX_PAGES is reported, not silent."""
        pages = [{"items": [], "nextPageToken": f"p{i}"} for i in range(9)]
        with mock.patch.object(mg_google, "api_get", side_effect=pages):
            items, truncated = mg_google.list_events_paged("t", "primary", NOW, NOW, 30)
        self.assertTrue(truncated)
        with mock.patch.object(mg_google, "api_get", side_effect=list(pages)):
            result = mg_google.fetch_events("t", NOW)
        self.assertEqual(result.truncated, [0])
        self.assertIn("more than 5 pages", self.log_text())


class PrivateFileTests(TempHome):
    def test_ok_0600(self):
        p = self.write_private(self.home / "s", "A=1\n")
        self.assertEqual(mg_config.read_private_file(p), "A=1\n")

    def test_group_readable_refused(self):
        p = self.write_private(self.home / "s", "A=1\n", 0o640)
        with self.assertRaises(mg_config.InsecureFileError):
            mg_config.read_private_file(p)

    def test_symlink_refused(self):
        real = self.write_private(self.home / "real", "A=1\n")
        link = self.home / "link"
        os.symlink(real, link)
        with self.assertRaises(mg_config.InsecureFileError):
            mg_config.read_private_file(link)

    def test_other_owner_refused(self):
        p = self.write_private(self.home / "s", "A=1\n")
        with mock.patch.object(mg_config.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(mg_config.InsecureFileError):
                mg_config.read_private_file(p)

    def test_directory_refused(self):
        d = self.home / "d"
        d.mkdir(mode=0o700)
        with self.assertRaises(mg_config.InsecureFileError):
            mg_config.read_private_file(d)

    def test_secrets_env_refused_and_logged(self):
        self.write_private(self.env_file, "NTFY_TOPIC=t\n", 0o644)
        self.assertEqual(mg_secrets.load_env_file(self.env_file), {})
        self.assertIn("chmod 600", self.log_text())

    def test_secrets_env_symlink_refused(self):
        real = self.write_private(self.home / "real.env", "NTFY_TOPIC=t\n")
        os.symlink(real, self.env_file)
        self.assertEqual(mg_secrets.load_env_file(self.env_file), {})
        self.assertIn("symlink", self.log_text())

    def test_tokens_and_oauth_keys_checked(self):
        tok = self.write_private(self.home / "tokens.json", '{"refresh_token": "r"}', 0o644)
        keys = self.write_private(self.home / "keys.json",
                                  '{"installed": {"client_id": "c"}}', 0o644)
        self.configure({"tokens": str(tok), "oauth_keys": str(keys)})
        with self.assertRaises(mg_config.InsecureFileError):
            mg_google.read_refresh_token()
        with self.assertRaises(mg_config.InsecureFileError):
            mg_google.load_client()
        os.chmod(tok, 0o600)
        os.chmod(keys, 0o600)
        self.assertEqual(mg_google.read_refresh_token(), "r")
        self.assertEqual(mg_google.load_client(), ("c", ""))

    def test_env_file_parsing_and_env_override(self):
        self.write_private(self.env_file, "export NTFY_TOPIC='from-file'\n# c\nX\n")
        self.assertEqual(mg_secrets.ntfy_topic(), "from-file")
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "from-env"}):
            self.assertEqual(mg_secrets.ntfy_topic(), "from-env")


class TokenWriteTests(TempHome):
    def setUp(self):
        super().setUp()
        self.path = self.home / "cfg" / "tokens.json"

    def test_only_refresh_token_and_scope_stored(self):
        self.configure({"tokens": str(self.path)})
        mg_oauth.write_tokens({"refresh_token": "r", "access_token": "a", "scope": "s",
                               "id_token": "i", "expires_in": 1})
        self.assertEqual(json.loads(self.path.read_text()), {"refresh_token": "r", "scope": "s"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["tokens.json", "tokens.json.lock"])  # no temp leftovers

    def test_profile_merges(self):
        self.path.parent.mkdir(mode=0o700)
        self.write_private(self.path, json.dumps({"other": {"refresh_token": "o"}}))
        self.configure({"tokens": str(self.path), "token_profile": "work"})
        mg_oauth.write_tokens({"refresh_token": "w"})
        data = json.loads(self.path.read_text())
        self.assertEqual(data, {"other": {"refresh_token": "o"}, "work": {"refresh_token": "w"}})
        self.assertEqual(mg_google.read_refresh_token(), "w")

    def test_profile_merge_refuses_insecure_existing(self):
        self.path.parent.mkdir(mode=0o700)
        self.write_private(self.path, "{}", 0o644)
        self.configure({"tokens": str(self.path), "token_profile": "work"})
        with self.assertRaises(mg_config.InsecureFileError):
            mg_oauth.write_tokens({"refresh_token": "w"})

    def test_parent_chmod_0700(self):
        self.path.parent.mkdir(mode=0o755)
        os.chmod(self.path.parent, 0o755)
        self.configure({"tokens": str(self.path)})
        mg_oauth.write_tokens({"refresh_token": "r"})
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_missing_refresh_token(self):
        self.configure({"tokens": str(self.path)})
        with self.assertRaises(RuntimeError):
            mg_oauth.write_tokens({"access_token": "a"})

    def test_flat_and_nested_read(self):
        for data, profile, want in (({"refresh_token": "r1"}, "", "r1"),
                                    ({"work": {"refresh_token": "r2"}}, "work", "r2"),
                                    ({"any": {"refresh_token": "r3"}}, "", "r3")):
            self.write_private(self.home / "t.json", json.dumps(data))
            self.configure({"tokens": str(self.home / "t.json"), "token_profile": profile})
            self.assertEqual(mg_google.read_refresh_token(), want)


class FakeServer:
    """Feeds pre-baked auth results to wait_for_code (no sockets)."""

    def __init__(self, results, clock):
        self.results, self.clock, self.timeout = list(results), clock, None
        self.auth_result = None

    def handle_request(self):
        self.clock.t += 1
        self.auth_result = self.results.pop(0) if self.results else None


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class OAuthTests(TempHome):
    def wait(self, results, timeout=300):
        clock = Clock()
        server = FakeServer(results, clock)
        with mock.patch("builtins.print"):
            return mg_oauth.wait_for_code(server, "good", timeout, clock), server

    def test_state_mismatch_ignored(self):
        code, _ = self.wait([{"code": "evil", "state": "bad"}, {"code": "ok", "state": "good"}])
        self.assertEqual(code, "ok")

    def test_overall_deadline(self):
        with self.assertRaises(TimeoutError):
            self.wait([{"code": "evil", "state": "bad"}] * 50, timeout=10)

    def test_server_timeout_capped_by_deadline(self):
        _, server = self.wait([None, {"code": "ok", "state": "good"}], timeout=5.5)
        self.assertLessEqual(server.timeout, 5.0)

    def test_error_reported(self):
        with self.assertRaises(RuntimeError):
            self.wait([{"error": "access_denied", "state": "good"}])

    def test_handler_timeout_and_path(self):
        self.assertEqual(mg_oauth.AuthHandler.timeout, 10)
        self.assertEqual(mg_oauth.AUTH_DEADLINE_SEC, 300)
        server = http.server.HTTPServer(("127.0.0.1", 0), mg_oauth.AuthHandler)
        port = server.server_address[1]
        try:
            for path, want in (("/favicon.ico?code=x&state=good", None),
                               ("/?code=c1&state=good", {"code": "c1", "state": "good"})):
                server.auth_result = None
                t = threading.Thread(target=server.handle_request)
                t.start()
                try:
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        f"http://127.0.0.1:{port}{path}", timeout=5).read()
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 404)
                    close_err(e)
                t.join(5)
                self.assertEqual(server.auth_result, want)
        finally:
            server.server_close()

    def test_readonly_scope(self):
        _, challenge = mg_oauth.pkce_pair()
        url = mg_oauth.build_auth_url("cid", "http://127.0.0.1:1", "st", challenge)
        self.assertIn("calendar.events.readonly", url)
        self.assertIn("code_challenge_method=S256", url)


class LogRedactionTests(TempHome):
    def test_urls_and_long_digits_redacted(self):
        mg_config.log("stage: Call https://zoom.us/j/12345678901?pwd=x tel 00011122233 id 12345")
        log = self.log_text()
        self.assertNotIn("zoom.us", log)
        self.assertNotIn("12345678901", log)
        self.assertNotIn("00011122233", log)
        self.assertIn("id 12345", log)

    def test_err_desc_class_and_status_only(self):
        e = urllib.error.HTTPError("https://x.example/?k=secret", 403, "secret", {}, None)
        self.assertEqual(mg_config.err_desc(e), "HTTPError HTTP 403")
        close_err(e)
        self.assertEqual(mg_config.err_desc(ValueError("secret")), "ValueError")

    def test_log_file_private_under_umask(self):
        old = os.umask(0o077)
        try:
            mg_config.log("x")
        finally:
            os.umask(old)
        self.assertEqual((self.home / "logs" / "guard.log").stat().st_mode & 0o777, 0o600)


class NtfyTests(TempHome):
    def push(self):
        with mock.patch.object(mg_http, "open_request",
                               return_value=FakeResponse(b"")) as op:
            REAL_PUSH("t", "m", 4)
        return op

    def test_disabled_without_topic(self):
        self.assertFalse(self.push().called)

    def test_placeholder_topic_disabled(self):
        self.write_private(self.env_file, "NTFY_TOPIC=change-me-to-random\n")
        self.assertFalse(self.push().called)

    def test_https_server_and_bearer_token(self):
        self.write_private(self.env_file, "NTFY_TOPIC=topic1\nNTFY_TOKEN=tk\n")
        self.configure({"ntfy_server": "https://ntfy.example.org"})
        req = self.push().call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.example.org")
        self.assertEqual(req.get_header("Authorization"), "Bearer tk")
        self.assertEqual(json.loads(req.data)["topic"], "topic1")

    def test_no_auth_header_without_token(self):
        self.write_private(self.env_file, "NTFY_TOPIC=topic1\n")
        req = self.push().call_args[0][0]
        self.assertIsNone(req.get_header("Authorization"))

    def test_http_refused_except_localhost(self):
        self.write_private(self.env_file, "NTFY_TOPIC=topic1\n")
        for server, allowed in (("http://ntfy.example.org", False), ("ftp://x", False),
                                ("http://127.0.0.1:8080", True), ("http://localhost", True),
                                ("http://localhost.evil.example", False),
                                ("https://u" + chr(0x40) + "ntfy.example.org", False)):
            self.configure({"ntfy_server": server})
            self.assertEqual(self.push().called, allowed, server)
        self.assertIn("must be https", self.log_text())


class PlistTests(TempHome):
    def test_render_plist(self):
        data = plistlib.loads(mg_plist.render_plist("io.example", "/py", "/h/m.py", "/h<&"))
        self.assertEqual(data["Label"], "io.example")
        self.assertEqual(data["ProgramArguments"], ["/py", "/h/m.py"])
        self.assertEqual(data["Umask"], 63)
        self.assertEqual(data["EnvironmentVariables"], {"MEETING_GUARD_HOME": "/h<&"})
        self.assertEqual(data["WorkingDirectory"], "/h<&")
        self.assertEqual(data["StartInterval"], 60)

    def test_main_writes_file(self):
        out = self.home / "p.plist"
        self.assertEqual(mg_plist.main(["x", str(out), "l", "/py", "/s", "/h"]), 0)
        self.assertEqual(plistlib.loads(out.read_bytes())["Label"], "l")
        with mock.patch("sys.stderr"):
            self.assertEqual(mg_plist.main(["x"]), 2)


class ConfigTests(TempHome):
    def test_env_overrides_paths(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            cfg.write_text(json.dumps({"nag_minutes": 3, "call": {"after_min": 5}}))
            with mock.patch.dict(os.environ, {"MEETING_GUARD_CONFIG": str(cfg),
                                              "MEETING_GUARD_HOME": str(Path(d) / "h")}):
                mg_config.setup()
            self.assertEqual(mg_config.CONFIG["nag_minutes"], 3)
            self.assertEqual(mg_config.CONFIG["call"]["after_min"], 5)
            self.assertEqual(mg_config.CONFIG["call"]["max_per_day"], 2)
            self.assertFalse(mg_config.CONFIG["call"]["enabled"])
            self.assertEqual(mg_config.HOME, Path(d) / "h")

    def test_defaults(self):
        d = mg_config.DEFAULT_CONFIG
        self.assertEqual((d["call"]["max_per_day"], d["call"]["min_interval_min"]), (2, 30))
        self.assertEqual(d["max_events_per_run"], 3)
        self.assertFalse(d["call_include_summary"])
        self.assertTrue(d["push_include_summary"])
        self.assertEqual(d["call"]["allowed_to_prefix"], "")

    def test_cli_sets_umask(self):
        seen = []
        with mock.patch.object(meeting_guard.os, "umask",
                               side_effect=lambda m: seen.append(m) or 0o022), \
                mock.patch.object(meeting_guard.C, "setup"), \
                mock.patch.object(meeting_guard.mg_engine, "run_once"):
            meeting_guard.cli(["meeting_guard.py"])
        self.assertEqual(seen, [0o077])

    def test_atomic_write_no_leftovers(self):
        target = self.home / "f.json"
        mg_config.atomic_write(target, b"1")
        mg_config.atomic_write(target, b"2")
        self.assertEqual(target.read_bytes(), b"2")
        self.assertEqual(sorted(p.name for p in self.home.iterdir() if p.name.startswith(".f")), [])

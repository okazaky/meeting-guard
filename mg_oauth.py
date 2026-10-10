# -*- coding: utf-8 -*-
"""--auth: installed-app loopback OAuth with PKCE (read-only calendar scope)."""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import mg_config as C
import mg_google

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTH_DEADLINE_SEC = 300
HANDLER_TIMEOUT_SEC = 10
# Google codes look like "4/0Ab..."; states are ours (token_urlsafe).
TOKEN_CHARS_RE = re.compile(r"[A-Za-z0-9._~/+=-]+")
STATE_MAX = 128
CODE_MAX = 2048


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id, redirect_uri, state, challenge):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": OAUTH_SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent"})


def remaining_sec(server):
    """Seconds left until the absolute OAuth deadline set by wait_for_code."""
    deadline = getattr(server, "auth_deadline", None)
    if deadline is None:
        return float(HANDLER_TIMEOUT_SEC)
    return deadline - getattr(server, "auth_clock", time.monotonic)()


class AuthHandler(http.server.BaseHTTPRequestHandler):
    timeout = HANDLER_TIMEOUT_SEC  # a silent client cannot hold the server

    def setup(self):
        # per-connection socket timeout = min(10 s, time left to the deadline)
        left = max(0.001, remaining_sec(self.server))
        self.timeout = min(HANDLER_TIMEOUT_SEC, left)
        super().setup()
        # The socket timeout is per read; a client dripping one byte at a time
        # would pass it. Cut the connection at the absolute deadline.
        self._deadline_timer = threading.Timer(left, self._cut_connection)
        self._deadline_timer.daemon = True
        try:
            self._deadline_timer.start()
        except BaseException:
            super().finish()  # finish() is not called when setup() raises
            raise

    def _cut_connection(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self._deadline_timer.cancel()
        super().finish()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self.send_error(404)
            return
        if remaining_sec(self.server) <= 0:
            self.send_error(408, "OAuth deadline passed")  # code ignored
            return
        query = urllib.parse.parse_qs(parsed.query)
        self.server.auth_result = {k: v[0] for k, v in query.items()}
        body = "meeting-guard: you can close this tab.".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _token_like(value, max_len):
    """str of 1..max_len URL-safe ASCII characters (state / code / error)."""
    return (isinstance(value, str) and 0 < len(value) <= max_len
            and TOKEN_CHARS_RE.fullmatch(value) is not None)


def _state_ok(res, state):
    got = res.get("state")
    if not _token_like(got, STATE_MAX):
        return False  # non-ASCII / overlong / missing: ignored, never a crash
    return secrets.compare_digest(got.encode("ascii"), state.encode("ascii"))


def wait_for_code(server, state, timeout_sec=AUTH_DEADLINE_SEC, clock=time.monotonic):
    """Wait for the redirect. A wrong / malformed `state` is ignored (not fatal).
    The absolute deadline bounds the loop, each connection's socket timeout and
    the handler (a code arriving after it is ignored)."""
    deadline = clock() + timeout_sec
    server.auth_deadline, server.auth_clock = deadline, clock
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("OAuth timed out")
        server.timeout = min(5.0, remaining)
        server.auth_result = None
        server.handle_request()
        if clock() >= deadline:
            raise TimeoutError("OAuth timed out")
        res = server.auth_result
        if not isinstance(res, dict) or ("code" not in res and "error" not in res):
            continue
        if not _state_ok(res, state):
            print("ignored a request with a wrong or malformed OAuth state")
            continue
        if "error" in res:
            err = res["error"] if _token_like(res["error"], 64) else "(malformed)"
            raise RuntimeError("OAuth error: " + err)
        if not _token_like(res.get("code"), CODE_MAX):
            print("ignored a request with a malformed authorization code")
            continue
        return res["code"]


def token_payload(token_data, existing, profile):
    """Only refresh_token (+scope) is stored; token_profile merges into the file."""
    entry = {"refresh_token": token_data["refresh_token"]}
    if token_data.get("scope"):
        entry["scope"] = token_data["scope"]
    if not profile:
        return entry
    base = existing if isinstance(existing, dict) else {}
    return {**base, profile: entry}


def _existing_tokens(path):
    try:
        return json.loads(C.read_private_file(path))
    except FileNotFoundError:
        return {}


def write_tokens(token_data):
    """token_profile merge: under `<tokens>.lock`, re-read the latest file, merge,
    then write via tmp + fsync + os.replace (concurrent --auth runs never lose a
    profile)."""
    if not token_data.get("refresh_token"):
        raise RuntimeError("no refresh_token returned; revoke access and retry")
    path = Path(C.CONFIG["tokens"]).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    profile = C.CONFIG.get("token_profile")
    with C.file_lock(path.parent / (path.name + ".lock")):
        existing = _existing_tokens(path) if profile else {}
        payload = token_payload(token_data, existing, profile)
        C.atomic_write(path, json.dumps(payload, indent=1).encode())
    return path


def run_auth():
    client_id, client_secret = mg_google.load_client()
    server = http.server.HTTPServer(("127.0.0.1", 0), AuthHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    url = build_auth_url(client_id, redirect_uri, state, challenge)
    print("Open this URL in your browser to authorize meeting-guard:\n" + url)
    subprocess.run(["/usr/bin/open", "-g", url], check=False)
    try:
        code = wait_for_code(server, state)
    finally:
        server.server_close()
    data = mg_google.post_token({"client_id": client_id, "client_secret": client_secret,
                                 "code": code, "code_verifier": verifier,
                                 "redirect_uri": redirect_uri,
                                 "grant_type": "authorization_code"})
    print(f"saved: {write_tokens(data)} (mode 0600)")

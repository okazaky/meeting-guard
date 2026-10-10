"""Shared fixtures for meeting-guard tests (stdlib only, no network, no real subprocesses)."""
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import mg_actions  # noqa: E402
import mg_call  # noqa: E402
import mg_config  # noqa: E402
import mg_secrets  # noqa: E402

NOW = datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
ZOOM_URL = "https://zoom.us/j/0000000000?pwd=abc"
ZOOM_CANON = "https://zoom.us/j/0000000000?pwd=abc"
MEET_URL = "https://meet.google.com/abc-defg-hij"
CAL = "primary"
FAKE_SID = "AC" + "0" * 32
FAKE_KEY_SID = "SK" + "0" * 32
CLEAN_ENV = {k: "" for k in mg_secrets.SECRET_KEYS}
REAL_PUSH = mg_actions.push
REAL_PLACE_CALL = mg_call.place_call


def make_event(minutes_from_now, location=ZOOM_URL, summary="Weekly sync", ev_id="ev1",
               **extra):
    start = NOW + timedelta(minutes=minutes_from_now)
    ev = {"id": ev_id, "summary": summary, "location": location,
          "start": {"dateTime": start.isoformat()}}
    ev.update(extra)
    return ev


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TempHome(unittest.TestCase):
    """Temp MEETING_GUARD_HOME with clean secret env vars."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env_file = self.home / "secrets.env"
        patch = mock.patch.dict(os.environ)  # restored after the test
        patch.start()
        self.addCleanup(patch.stop)
        for k in CLEAN_ENV:  # unset (an empty value would mean "disabled")
            os.environ.pop(k, None)
        self.addCleanup(self.tmp.cleanup)
        self.configure({})

    def configure(self, user):
        cfg = {"call": {"enabled": True, "after_min": 2, "env_file": str(self.env_file)}}
        for k, v in user.items():
            cfg[k] = {**cfg[k], **v} if k == "call" else v
        mg_config.setup(cfg, self.home)

    def log_text(self):
        path = self.home / "logs" / "guard.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_private(self, path, text, mode=0o600):
        path = Path(path)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path


class Base(TempHome):
    """TempHome plus recorded (mocked) side-effect actions."""

    def setUp(self):
        super().setUp()
        self.actions = []
        self.open_ok = True
        for name in ("push", "speak", "dialog", "open_link"):
            p = mock.patch.object(mg_actions, name, side_effect=self._recorder(name))
            p.start()
            self.addCleanup(p.stop)
        self.call_result = mg_call.PLACED
        p = mock.patch.object(mg_call, "place_call", side_effect=self._call_recorder)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(mg_actions, "zoom_in_meeting", return_value=False)
        self.zoom_check = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(mg_actions, "cpthost_snapshot", return_value=[])
        self.zoom_snapshot = p.start()
        self.addCleanup(p.stop)

    def _recorder(self, name):
        def rec(*args, **kwargs):
            self.actions.append((name, args, kwargs))
            return self.open_ok if name == "open_link" else None
        return rec

    def _call_recorder(self, *args, **kwargs):
        self.actions.append(("place_call", args, kwargs))
        return self.call_result

    def names(self):
        return [a[0] for a in self.actions]

# -*- coding: utf-8 -*-
"""Configuration, paths, logging and private-file helpers for meeting-guard."""
import copy
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import mg_schema

APP_DIR = Path(__file__).resolve().parent
DEFAULT_HOME = "~/.local/share/meeting-guard"
STAGE_ORDER = ["push10", "voice5", "dialog1", "open0"]

DEFAULT_CONFIG = {
    "calendars": ["primary"],
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "push_include_summary": True,
    "oauth_keys": "~/.config/meeting-guard/gcp-oauth.keys.json",
    "tokens": "~/.config/meeting-guard/tokens.json",
    "token_profile": "",
    "say_voice": "",
    "twilio_voice": "Polly.Mizuki",
    "twilio_language": "ja-JP",
    "call_include_summary": False,
    "auto_open": True,
    "escalate_linkless": False,
    "include_unaccepted": False,
    "exclude_summary_contains": [],
    "max_events_per_run": 3,
    "nag_minutes": 10,
    "nag_interval_sec": 110,
    "call": {
        "enabled": False,
        "after_min": 2,
        "env_file": "~/.config/meeting-guard/secrets.env",
        "allowed_from_prefix": "",
        "allowed_to_prefix": "",
        "max_per_day": 2,
        "min_interval_min": 30,
    },
    "stages": {"push10": 10.5, "voice5": 5.5, "dialog1": 1.5, "open0": 0.6},
    "messages": {},
}

DEFAULT_MESSAGES = {
    "untitled": "(無題)",
    "push_generic_summary": "会議",
    "push10_title": "📅 {minutes}分後: {summary}",
    "push10_body": "{hhmm} 開始。準備してください。",
    "voice5_say": "あと{minutes}分で、{summary}、が始まります。",
    "voice5_title": "⏰ {minutes}分前: {summary}",
    "voice5_body": "{hhmm} 開始。",
    "dialog1_say": "まもなく、{summary}、が始まります。",
    "dialog1_text": "「{summary}」が {hhmm} に始まります。",
    "open_say_opened": "{summary}、開始時刻です。会議を開きました。参加してください。",
    "open_say": "{summary}、開始時刻です。",
    "open_dialog_manual": "「{summary}」開始時刻です！\n手動で参加してください。",
    "open_title": "🚨 開始: {summary}",
    "open_body": "{hhmm} 開始。",
    "nag_say": "{summary}、会議が始まっています。まだ参加していません。",
    "nag_dialog": "「{summary}」({hhmm}開始) がもう始まっています！\n参加したらこのボタンを押してください。",
    "nag_title": "⚠️ 未参加: {summary}",
    "nag_body": "{hhmm} 開始済み（{minutes}分経過）。今すぐ参加を！",
    "call_say": "会議が始まっています。{summary}。今すぐ参加してください。",
    "call_say_generic": "会議が始まっています。今すぐ参加してください。",
    "dialog_title": "meeting-guard",
    "button_ack": "参加します（警報停止）",
    "button_ok": "OK",
    "error_title": "meeting-guard エラー",
    "error_body": "カレンダー取得に失敗: {error}",
    "error_say": "meeting-guard が、カレンダーを読めません。会議の警報が止まっています。",
    "error_dialog": "カレンダーを読めません（{error}）。\n会議の警報が止まっています。"
                    "logs/guard.log を確認し、--check を実行してください。",
    "test_call_summary": "テスト発信",
    "open_failed_say": "{summary}、開始時刻です。会議を開けませんでした。手動で参加してください。",
    "auth_expired_say": "meeting-guard の、Google の認可が切れました。会議の警報が止まっています。"
                        "--auth を実行してください。",
    "auth_expired_dialog": "Google の認可が無効になりました（invalid_grant）。\n"
                           "会議の警報が止まっています。ターミナルで meeting_guard.py --auth を"
                           "実行してください。",
    "calendar_error_say": "meeting-guard が、一部のカレンダーを読めません。"
                          "そのカレンダーの会議は警報が鳴りません。",
    "calendar_error_dialog": "{count}件のカレンダーを読めません（{error}）。\n"
                             "そのカレンダーの会議は警報が鳴りません。"
                             "logs/guard.log を確認し、--check を実行してください。",
    "truncated_say": "meeting-guard が、予定を全部は読み切れていません。",
    "truncated_dialog": "予定が多すぎて、一部のカレンダーを最後まで読めていません。\n"
                        "一部の会議で警報が鳴らない可能性があります。",
    "config_say": "meeting-guard の設定ファイルに誤りがあります。既定の設定で動いています。"
                  "電話の発信は止めています。",
    "config_dialog": "設定ファイル config.json に誤りがあります（{error}）。\n"
                     "既定の設定で動いています（電話の発信は無効）。"
                     "logs/guard.log を確認してください。",
    "home_say": "meeting-guard のデータフォルダが安全ではないため、停止しました。"
                "会議の警報は止まっています。",
    "home_dialog": "データフォルダが安全ではないため停止しました（{error}）。\n"
                   "会議の警報は止まっています。README の説明に沿って直してください。",
}

CONFIG = copy.deepcopy(DEFAULT_CONFIG)
HOME = Path(DEFAULT_HOME).expanduser()
CONFIG_PROBLEMS = []
LOG_TO_FILE = True
HOME_PROBLEM = None  # set by setup(): reason MEETING_GUARD_HOME is unsafe, or None
LOG_MAX_BYTES = 512 * 1024

URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")
DIGITS_RE = re.compile(r"\d{6,}")


class InsecureFileError(Exception):
    """A private file failed the ownership / permission / symlink checks."""


# ---------- setup ----------

def guard_home():
    return Path(os.environ.get("MEETING_GUARD_HOME") or DEFAULT_HOME).expanduser()


def config_path():
    env = os.environ.get("MEETING_GUARD_CONFIG")
    return Path(env).expanduser() if env else APP_DIR / "config.json"


def merge_config(user):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for k, v in (user or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return cfg


def reject_constant(name):
    """json parse_constant hook: NaN / Infinity are never valid."""
    raise ValueError(f"non-finite number {name} is not allowed")


def read_user_config():
    """Return (user_dict, problems). Missing file = {}; unreadable / invalid JSON
    = {} plus a problem (the caller falls back to defaults, call disabled)."""
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeDecodeError) as e:
        return {}, [f"config unreadable ({err_desc(e)})"]
    try:
        return json.loads(text, parse_constant=reject_constant), []
    except ValueError:
        return {}, ["config.json is not valid JSON"]


def checked_config(user, problems):
    """Validate known keys; any problem forces call.enabled = False. Any
    unexpected exception during validation = all defaults (call disabled)."""
    try:
        clean, bad = mg_schema.validate(user)
        problems = list(problems) + bad
        cfg = merge_config(clean)
    except Exception as e:
        problems = list(problems) + [f"config validation failed ({err_desc(e)})"]
        cfg = merge_config({})
    if problems:
        cfg["call"] = {**cfg["call"], "enabled": False}
    return cfg, problems


def prepare_home():
    """Create MEETING_GUARD_HOME only if it does not exist, then validate it
    before anything is written there. An unsafe home disables file logging."""
    global LOG_TO_FILE, HOME_PROBLEM
    try:
        os.lstat(HOME)
    except FileNotFoundError:
        try:
            HOME.mkdir(parents=True, mode=0o700)
        except OSError:
            pass  # reported by home_problem() below
    except OSError:
        pass
    HOME_PROBLEM = home_problem()
    LOG_TO_FILE = HOME_PROBLEM is None
    if HOME_PROBLEM:
        try:
            sys.stderr.write(f"meeting-guard: unsafe MEETING_GUARD_HOME: {HOME_PROBLEM}\n")
        except Exception:
            pass
    return HOME_PROBLEM


def setup(user_config=None, home=None):
    """Initialise CONFIG, HOME and CONFIG_PROBLEMS. Tests pass both explicitly.
    Never raises on a bad config: alarms must keep working with defaults."""
    global CONFIG, HOME, CONFIG_PROBLEMS
    problems = []
    if user_config is None:
        user_config, problems = read_user_config()
    CONFIG, CONFIG_PROBLEMS = checked_config(user_config, problems)
    HOME = Path(home) if home else guard_home()
    prepare_home()
    for p in CONFIG_PROBLEMS:
        log(f"CONFIG ERROR: {p}; using the default for it, phone call disabled")
    if isinstance(user_config, dict) and "exclude_summary_regex" in user_config:
        log("WARNING: exclude_summary_regex is no longer supported and is ignored; "
            "use exclude_summary_contains (list of words)")


def home_path(name):
    return HOME / name


class _SafeFields(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def msg(key, **fields):
    """Format a user-facing string; config `messages` overrides defaults."""
    template = CONFIG.get("messages", {}).get(key, DEFAULT_MESSAGES[key])
    try:
        return str(template).format_map(_SafeFields(fields))
    except (ValueError, IndexError, AttributeError):
        return DEFAULT_MESSAGES[key].format_map(_SafeFields(fields))


# ---------- logging ----------

def redact(text):
    """Logs never contain URLs or long digit runs (meeting IDs, phone numbers)."""
    return DIGITS_RE.sub("[num]", URL_RE.sub("[url]", str(text)))


def err_desc(exc):
    """Exception class plus HTTP status only (messages may echo secrets / URLs)."""
    code = getattr(exc, "code", None)
    return exc.__class__.__name__ + (f" HTTP {code}" if isinstance(code, int) else "")


def _log_dir():
    d = home_path("logs")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stat.S_ISDIR(os.lstat(d).st_mode):
        raise OSError("logs is not a real directory")
    return d


def _rotate_log(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_size <= LOG_MAX_BYTES:
        return
    with open_nofollow(path, os.O_RDONLY, "rb") as f:
        tail = f.read().decode("utf-8", "replace").splitlines()[-200:]
    atomic_write(path, ("\n".join(tail) + "\n").encode("utf-8"))


def _append_log(line):
    if not LOG_TO_FILE:
        raise OSError("file logging disabled (unsafe MEETING_GUARD_HOME)")
    path = _log_dir() / "guard.log"
    _rotate_log(path)
    with open_nofollow(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, "a") as f:
        f.write(line)


def log(text):
    """Never raises: a log problem must not stop an alarm (falls back to stderr)."""
    try:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {redact(text)}\n"
    except Exception:
        line = "[log] (unformattable message)\n"
    try:
        _append_log(line)
    except Exception:
        try:
            sys.stderr.write(line)
        except Exception:
            pass


# ---------- private files ----------

def private_problem(st):
    """Return why a stat result is not an acceptable private file, or None."""
    if stat.S_ISLNK(st.st_mode):
        return "is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_uid != os.getuid():
        return "is not owned by the current user"
    if st.st_mode & 0o077:
        return f"mode {oct(st.st_mode & 0o777)} allows group/other access; run chmod 600"
    return None


def read_private_file(path):
    """Read a secret file. FileNotFoundError if missing, InsecureFileError if unsafe."""
    path = Path(path)
    st = os.lstat(path)
    problem = private_problem(st)
    if problem:
        raise InsecureFileError(f"{path} {problem}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        now = os.fstat(f.fileno())
        if (now.st_ino, now.st_dev) != (st.st_ino, st.st_dev):
            raise InsecureFileError(f"{path} changed while opening")
        return f.read()


def atomic_write(path, data):
    """Write bytes via mkstemp in the same directory + fsync + os.replace (mode 0600)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory):
    try:
        dfd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def open_nofollow(path, flags, mode_str, perm=0o600):
    """os.open with O_NOFOLLOW (a symlink raises OSError), wrapped as a file object."""
    fd = os.open(str(path), flags | os.O_NOFOLLOW | os.O_CLOEXEC, perm)
    try:
        return os.fdopen(fd, mode_str, **({} if "b" in mode_str else {"encoding": "utf-8"}))
    except BaseException:
        os.close(fd)
        raise


def read_nofollow(path):
    """Read a text file without following a symlink at the final component."""
    with open_nofollow(path, os.O_RDONLY, "r") as f:
        return f.read()


@contextmanager
def file_lock(path):
    """Exclusive advisory lock held for the duration of the block (O_NOFOLLOW)."""
    with open_nofollow(path, os.O_WRONLY | os.O_CREAT, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def home_problem():
    """ssh StrictModes-style check of MEETING_GUARD_HOME: a real directory owned
    by this user, not group/world writable, not a symlink. Returns reason or None."""
    try:
        st = os.lstat(HOME)
    except OSError as e:
        return f"cannot stat ({err_desc(e)})"
    if stat.S_ISLNK(st.st_mode):
        return "is a symlink"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if st.st_uid != os.getuid():
        return "is not owned by the current user"
    if st.st_mode & 0o022:
        return f"mode {oct(st.st_mode & 0o777)} is group/world writable; run chmod 700"
    return None

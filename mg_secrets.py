# -*- coding: utf-8 -*-
"""Secrets: secrets.env (private file) overridden by the process environment."""
import os
from pathlib import Path

import mg_config as C

SECRET_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
               "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET",
               "TWILIO_FROM_NUMBER", "MEETING_GUARD_CALL_TO",
               "NTFY_TOPIC", "NTFY_TOKEN")

# Values from current and earlier secrets.env.example files: never used for real.
PLACEHOLDERS = frozenset({
    "change-me-to-random", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "your_auth_token_here", "+15555550100", "+810000000000",
})


def env_file_path():
    return Path(C.CONFIG["call"].get("env_file")
                or C.DEFAULT_CONFIG["call"]["env_file"]).expanduser()


def parse_env_lines(text):
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        env[k] = v.strip().strip('"').strip("'")
    return env


def load_env_file(path):
    """KEY=VALUE file; refused unless a regular 0600 file owned by us. Values never logged."""
    try:
        return parse_env_lines(C.read_private_file(path))
    except FileNotFoundError:
        return {}
    except C.InsecureFileError as e:
        C.log(f"WARNING: secrets file refused: {e}")
        return {}
    except (OSError, UnicodeDecodeError) as e:
        C.log(f"WARNING: cannot read secrets file {path}: {C.err_desc(e)}")
        return {}


def load_secrets():
    """secrets.env values overridden by the environment. An env var that is SET
    but empty (e.g. NTFY_TOPIC="") disables that value: it is kept as "" so it
    also overrides config.json. Unset env vars do not override."""
    merged = {k: v for k, v in load_env_file(env_file_path()).items() if v}
    for k in SECRET_KEYS:
        if k in os.environ:
            merged[k] = os.environ[k]
    return merged


def ntfy_topic():
    """Push is opt-in: empty or placeholder topic disables it."""
    s = load_secrets()
    topic = s["NTFY_TOPIC"] if "NTFY_TOPIC" in s else (C.CONFIG.get("ntfy_topic") or "")
    return "" if topic in PLACEHOLDERS else topic


def ntfy_token():
    return load_secrets().get("NTFY_TOKEN") or ""

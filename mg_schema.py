# -*- coding: utf-8 -*-
"""Type / range validation of known config.json keys (pure functions, no I/O).

An invalid value is dropped (its default is used instead) and reported; the
caller then forces call.enabled = False and alerts the user locally.
"""
import math

BOOL_KEYS = ("push_include_summary", "call_include_summary", "auto_open",
             "escalate_linkless", "include_unaccepted")
STR_KEYS = ("ntfy_server", "ntfy_topic", "oauth_keys", "tokens", "token_profile",
            "say_voice", "twilio_voice", "twilio_language")
# key: (min, max, integer_only)
NUM_KEYS = {"max_events_per_run": (1, 20, True), "nag_minutes": (0, 60, False),
            "nag_interval_sec": (10, 3600, False)}
CALL_BOOL_KEYS = ("enabled",)
CALL_STR_KEYS = ("env_file", "allowed_from_prefix", "allowed_to_prefix")
CALL_NUM_KEYS = {"after_min": (0, 60, False), "max_per_day": (0, 10, True),
                 "min_interval_min": (0, 1440, False)}
STAGE_RANGE = (0, 60, False)
STR_MAX = 1024
LIST_MAX = 50


def is_bool(v):
    return isinstance(v, bool)


def is_str(v):
    return isinstance(v, str) and len(v) <= STR_MAX


def is_num(v, spec):
    """ints are range-compared as ints (no float conversion: a 400-digit JSON
    int must not raise OverflowError); floats must be finite."""
    lo, hi, integer = spec
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if isinstance(v, int):
        return lo <= v <= hi
    return not integer and math.isfinite(v) and lo <= v <= hi


def is_str_list(v, allow_empty):
    if not isinstance(v, list) or len(v) > LIST_MAX or (not v and not allow_empty):
        return False
    return all(isinstance(s, str) and 0 < len(s) <= STR_MAX for s in v)


def _check_flat(user, bools, strs, nums):
    """Yield the names of invalid keys among those present in `user`."""
    for k in bools:
        if k in user and not is_bool(user[k]):
            yield k
    for k in strs:
        if k in user and not is_str(user[k]):
            yield k
    for k, spec in nums.items():
        if k in user and not is_num(user[k], spec):
            yield k


def _clean_section(user, name, check):
    """Validate a nested dict (call / stages / messages). Returns (clean, problems).
    Absent = fine; present but not an object (including null) = invalid."""
    if name not in user:
        return None, []
    section = user[name]
    if not isinstance(section, dict):
        return None, [name]
    bad = list(check(section))
    clean = {k: v for k, v in section.items() if k not in bad}
    return clean, [f"{name}.{k}" for k in bad]


def _check_call(section):
    return _check_flat(section, CALL_BOOL_KEYS, CALL_STR_KEYS, CALL_NUM_KEYS)


def _check_stages(section):
    return (k for k, v in section.items() if not is_num(v, STAGE_RANGE))


def _check_messages(section):
    return (k for k, v in section.items() if not is_str(v))


def validate(user):
    """Return (clean_user, problems). problems lists key names only (no values)."""
    if not isinstance(user, dict):
        return {}, ["(top level is not an object)"]
    problems = list(_check_flat(user, BOOL_KEYS, STR_KEYS, NUM_KEYS))
    if "calendars" in user and not is_str_list(user["calendars"], allow_empty=False):
        problems.append("calendars")
    if ("exclude_summary_contains" in user
            and not is_str_list(user["exclude_summary_contains"], allow_empty=True)):
        problems.append("exclude_summary_contains")
    clean = {k: v for k, v in user.items() if k not in problems}
    for name, check in (("call", _check_call), ("stages", _check_stages),
                        ("messages", _check_messages)):
        section, bad = _clean_section(user, name, check)
        problems.extend(bad)
        if section is None:
            clean.pop(name, None)
        else:
            clean[name] = section
    return clean, problems

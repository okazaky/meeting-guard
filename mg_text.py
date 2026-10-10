# -*- coding: utf-8 -*-
"""Sanitising untrusted text (event titles) for speech, dialogs and logs."""
import re
import unicodedata

SUMMARY_MAX = 80
_SPACE_RE = re.compile(r"\s+")


def strip_unsafe(text):
    """Control chars (Cc, line/paragraph separators) -> space; format chars (Cf) removed."""
    out = []
    for ch in str(text or ""):
        cat = unicodedata.category(ch)
        if cat == "Cf":
            continue
        out.append(" " if cat in ("Cc", "Zl", "Zp") else ch)
    return "".join(out)


def remove_say_commands(text):
    """`say` interprets [[...]] as embedded commands; remove every [[ and ]]."""
    while "[[" in text or "]]" in text:
        text = text.replace("[[", "").replace("]]", "")
    return text


def sanitize_summary(raw, fallback="(無題)"):
    """Event titles are untrusted: strip control/format chars, [[ ]], leading '-', truncate."""
    text = remove_say_commands(strip_unsafe(raw))
    text = _SPACE_RE.sub(" ", text).strip()
    while text.startswith("-"):
        text = text[1:].lstrip()
    text = text[:SUMMARY_MAX].rstrip()
    return text or fallback


def clean_speech(text):
    """Final filter for any text handed to `say` (templates may be user-edited)."""
    return _SPACE_RE.sub(" ", remove_say_commands(strip_unsafe(text))).strip()


def applescript_string(text):
    """Escape for an AppleScript string literal; newlines kept as \\n."""
    lines = [strip_unsafe(line) for line in str(text).split("\n")]
    joined = "\n".join(lines)
    return joined.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def mask_phone(number):
    s = str(number or "")
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]

# -*- coding: utf-8 -*-
"""Render the launchd agent plist with plistlib (used by install.sh).

Usage: mg_plist.py <out_path> <label> <python> <script> <app_home>
"""
import plistlib
import sys


def render_plist(label, python, script, home):
    """Pure function: plist bytes for the per-user launchd agent."""
    data = {
        "Label": label,
        "ProgramArguments": [python, script],
        "StartInterval": 60,
        "RunAtLoad": True,
        "AbandonProcessGroup": True,
        "ProcessType": "Interactive",
        "Umask": 63,  # 0o077: files created by the agent are private
        "EnvironmentVariables": {"MEETING_GUARD_HOME": home},
        "StandardOutPath": f"{home}/logs/launchd.out.log",
        "StandardErrorPath": f"{home}/logs/launchd.err.log",
        "WorkingDirectory": home,
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML)


def main(argv):
    if len(argv) != 6:
        print(__doc__, file=sys.stderr)
        return 2
    out, label, python, script, home = argv[1:6]
    with open(out, "wb") as f:
        f.write(render_plist(label, python, script, home))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

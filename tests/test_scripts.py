"""H4 / L: install.sh and uninstall.sh guards, run with HOME=<tempdir> and fake
launchctl / plutil on PATH (never touches the real HOME or launchd)."""
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mgtest_helpers import REPO

LABEL = "io.github.meeting-guard"
FAKE_TOOL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
if [ "$1" = "bootout" ] && [ -n "${MG_TEST_UNLINK:-}" ]; then /bin/rm -f "$MG_TEST_UNLINK"; fi
exit 0
"""
# Records every rm call, then does the real removal.
FAKE_RM = """#!/bin/sh
echo "rm $*" >> "$FAKE_LOG"
exec /bin/rm "$@"
"""


class ScriptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.outside = root / "outside"
        self.bin = root / "bin"
        for d in (self.home, self.outside, self.bin):
            d.mkdir()
        self.fake_log = root / "fake.log"
        for tool, body in (("launchctl", FAKE_TOOL), ("plutil", FAKE_TOOL), ("rm", FAKE_RM)):
            path = self.bin / tool
            path.write_text(body)
            path.chmod(0o755)
        self.app_home = self.home / ".local" / "share" / "meeting-guard"
        self.conf = self.home / ".config" / "meeting-guard"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def run_script(self, name, *args, app_home=None, extra_env=None):
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
               "PYTHON": sys.executable, "FAKE_LOG": str(self.fake_log),
               **(extra_env or {})}
        if app_home is not None:
            env["MEETING_GUARD_HOME"] = str(app_home)
        return subprocess.run(["/bin/bash", str(REPO / name), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def fake_calls(self):
        return self.fake_log.read_text() if self.fake_log.exists() else ""

    def install(self, app_home=None):
        res = self.run_script("install.sh", app_home=app_home)
        return res


class InstallTests(ScriptBase):
    def test_fresh_install(self):
        res = self.install()
        self.assertEqual(res.returncode, 0, res.stderr)
        for d in (self.app_home, self.conf):
            self.assertTrue((d / ".meeting-guard-home").is_file())
            self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        for name in ("meeting_guard.py", "mg_engine.py", "mg_call.py", "mg_config.py"):
            self.assertTrue((self.app_home / name).is_file(), name)
        secrets_env = self.conf / "secrets.env"
        self.assertEqual(secrets_env.stat().st_mode & 0o777, 0o600)
        self.assertIn("\nNTFY_TOPIC=\n", secrets_env.read_text())
        self.assertRegex(res.stdout, r"NTFY_TOPIC=mg-[0-9a-f]{32}")
        data = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(data["Umask"], 63)
        self.assertEqual(data["EnvironmentVariables"]["MEETING_GUARD_HOME"], str(self.app_home))
        self.assertEqual(data["ProgramArguments"],
                         [sys.executable, str(self.app_home / "meeting_guard.py")])
        self.assertIn("launchctl bootstrap", self.fake_calls())
        self.assertIn("plutil -lint", self.fake_calls())
        leftovers = [p.name for p in self.plist.parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_reinstall_keeps_config_and_backs_up_plist(self):
        self.assertEqual(self.install().returncode, 0)
        (self.app_home / "config.json").write_text("{}")
        res = self.install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.app_home / "config.json").read_text(), "{}")
        self.assertTrue(Path(str(self.plist) + ".bak").is_file())
        leftovers = [p.name for p in self.plist.parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_symlinked_logs_refused(self):
        """R2: chmod on logs/ must not follow a planted symlink."""
        self.app_home.mkdir(parents=True)
        (self.app_home / ".meeting-guard-home").write_text("x")  # earlier install
        self.outside.chmod(0o755)
        os.symlink(self.outside, self.app_home / "logs")
        self.assert_refused(self.app_home, "symlink")
        self.assertEqual(self.outside.stat().st_mode & 0o777, 0o755)

    def test_symlinked_plist_backup_refused(self):
        """R2: the backup must not be written through a symlink."""
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("old")
        victim = self.outside / "victim.txt"
        victim.write_text("keep")
        os.symlink(victim, str(self.plist) + ".bak")
        res = self.install()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("symlink", res.stderr)
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(self.fake_calls(), "")

    def test_control_chars_and_dash_refused(self):
        """R1: newline / tab / leading '-' in the target path."""
        for name, why in (("mg\nx", "control character"), ("mg\tx", "control character"),
                          ("-rf", "starting with '-'")):
            self.assert_refused(self.home / name, why)
            self.assertFalse((self.home / name).exists())

    def assert_refused(self, app_home, why):
        res = self.install(app_home)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("refused", res.stderr)
        self.assertIn(why, res.stderr)
        self.assertEqual(self.fake_calls(), "")
        self.assertFalse(self.plist.exists())
        return res

    def test_home_itself_refused(self):
        self.assert_refused(self.home, "HOME itself")

    def test_root_refused(self):
        self.assert_refused("/", "is /")

    def test_outside_home_refused(self):
        self.assert_refused(self.outside / "mg", "outside")

    def test_relative_and_dotdot_refused(self):
        self.assert_refused("rel/mg", "absolute")
        self.assert_refused(f"{self.home}/x/../..", ". / ..")

    def test_symlink_refused(self):
        os.symlink(self.outside, self.home / "mg")
        self.assert_refused(self.home / "mg", "symlink")

    def test_symlinked_ancestor_refused(self):
        os.symlink(self.outside, self.home / "link")
        self.assert_refused(self.home / "link" / "mg", "symlink")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_non_empty_without_marker_refused(self):
        target = self.home / "Documents"
        target.mkdir()
        (target / "keep.txt").write_text("mine")
        target.chmod(0o755)
        self.assert_refused(target, "not created by meeting-guard")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual([p.name for p in target.iterdir()], ["keep.txt"])

    def test_empty_custom_home_ok(self):
        target = self.home / "mg-data"
        target.mkdir()
        res = self.install(target)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((target / ".meeting-guard-home").is_file())


class UninstallTests(ScriptBase):
    def setUp(self):
        super().setUp()
        res = self.install()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.fake_log.unlink()

    def test_plain_uninstall_keeps_data(self):
        res = self.run_script("uninstall.sh")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(self.plist.exists())
        self.assertTrue(self.app_home.is_dir())
        self.assertIn("myaccount.google.com/connections", res.stdout)

    def test_purge_without_yes_only_lists(self):
        res = self.run_script("uninstall.sh", "--purge")
        self.assertEqual(res.returncode, 1)
        self.assertIn(str(self.app_home), res.stdout)
        self.assertIn(str(self.conf), res.stdout)
        self.assertTrue(self.app_home.is_dir())
        self.assertTrue(self.plist.exists())
        self.assertEqual(self.fake_calls(), "")

    def test_purge_yes_deletes(self):
        res = self.run_script("uninstall.sh", "--purge", "--yes")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(self.app_home.exists())
        self.assertFalse(self.conf.exists())
        self.assertFalse(self.plist.exists())
        self.assertTrue(self.home.is_dir())
        self.assertIn("myaccount.google.com/connections", res.stdout)

    def test_yes_alone_is_usage_error(self):
        self.assertEqual(self.run_script("uninstall.sh", "--yes").returncode, 2)
        self.assertTrue(self.plist.exists())

    def assert_purge_refused(self, app_home, why):
        res = self.run_script("uninstall.sh", "--purge", "--yes", app_home=app_home)
        self.assertEqual(res.returncode, 1)
        self.assertIn(why, res.stderr)
        self.assertTrue(self.plist.exists())
        self.assertTrue(self.home.is_dir())
        self.assertNotIn("rm ", self.fake_calls())  # rm is never called
        return res

    def marked_dir(self, path):
        path.mkdir(parents=True)
        (path / ".meeting-guard-home").write_text("x")
        return path

    def test_newline_path_never_split(self):
        """R1: "$HOME/x<newline>$HOME/victim" used to be split into two rm targets."""
        victim = self.home / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("mine")
        evil = self.marked_dir(Path(f"{self.home}/x\n{self.home}/victim"))
        self.assert_purge_refused(evil, "control character")
        self.assertEqual((victim / "keep.txt").read_text(), "mine")
        self.assertTrue(evil.is_dir())

    def test_tab_and_control_char_refused(self):
        for name in ("mg\tx", "mg" + chr(0x7F) + "x", "mg" + chr(0x1B) + "x"):
            target = self.marked_dir(self.home / name)
            self.assert_purge_refused(target, "control character")
            self.assertTrue(target.is_dir())

    def test_leading_dash_refused(self):
        target = self.marked_dir(self.home / "-rf")
        self.assert_purge_refused(target, "starting with '-'")
        self.assertTrue(target.is_dir())

    def test_revalidated_right_before_rm(self):
        """R1: a target that stops passing the checks after listing is kept."""
        marker = self.app_home / ".meeting-guard-home"
        res = self.run_script("uninstall.sh", "--purge", "--yes",
                              extra_env={"MG_TEST_UNLINK": str(marker)})
        self.assertEqual(res.returncode, 1)
        self.assertIn("failed re-check", res.stderr)
        self.assertTrue(self.app_home.is_dir())
        self.assertFalse(self.conf.exists())  # still valid -> deleted
        self.assertNotIn(f"rm -rf -- {self.app_home}\n", self.fake_calls())
        self.assertIn(f"rm -rf -- {self.conf}\n", self.fake_calls())

    def test_purge_yes_records_rm_targets(self):
        res = self.run_script("uninstall.sh", "--purge", "--yes")
        self.assertEqual(res.returncode, 0, res.stderr)
        rm_calls = [ln for ln in self.fake_calls().splitlines() if ln.startswith("rm -rf")]
        self.assertEqual(rm_calls, [f"rm -rf -- {self.app_home}", f"rm -rf -- {self.conf}"])

    def test_missing_marker_refused(self):
        (self.app_home / ".meeting-guard-home").unlink()
        self.assert_purge_refused(self.app_home, "missing")
        self.assertTrue(self.app_home.is_dir())

    def test_home_refused(self):
        (self.home / ".meeting-guard-home").write_text("x")
        self.assert_purge_refused(self.home, "HOME itself")

    def test_root_refused(self):
        self.assert_purge_refused("/", "is /")

    def test_outside_home_refused(self):
        (self.outside / ".meeting-guard-home").write_text("x")
        self.assert_purge_refused(self.outside, "outside")
        self.assertTrue(self.outside.is_dir())

    def test_symlink_refused(self):
        (self.outside / ".meeting-guard-home").write_text("x")
        os.symlink(self.outside, self.home / "mg-link")
        self.assert_purge_refused(self.home / "mg-link", "symlink")
        self.assertTrue((self.outside / ".meeting-guard-home").exists())

    def test_symlinked_ancestor_refused(self):
        self.marked_dir(self.outside / "mg")
        os.symlink(self.outside, self.home / "anc")
        self.assert_purge_refused(self.home / "anc" / "mg", "symlink")
        self.assertTrue((self.outside / "mg").is_dir())

    def test_realpath_check_refuses_home_and_outside(self):
        """R1: the resolved-path check alone (shape checks bypassed)."""
        script = (f'. "{REPO}/lib/paths.sh"; mg_check_realpath "$1"')
        cases = ((self.home, "HOME itself"), (self.outside, "outside"),
                 (self.app_home, None))
        for path, why in cases:
            res = subprocess.run(["/bin/bash", "-c", script, "x", str(path)],
                                 env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                                 capture_output=True, text=True)
            if why is None:
                self.assertEqual(res.returncode, 0, res.stderr)
            else:
                self.assertEqual(res.returncode, 1)
                self.assertIn(why, res.stderr)


class ShellSyntaxTests(unittest.TestCase):
    def test_bash_n(self):
        for name in ("install.sh", "uninstall.sh", "lib/paths.sh"):
            res = subprocess.run(["/bin/bash", "-n", str(REPO / name)],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, name + res.stderr)

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from sns_comment import launcher


class LauncherTests(unittest.TestCase):
    def test_finder_launch_survives_script_edit_while_collector_runs(self):
        with tempfile.TemporaryDirectory(prefix="sns launcher ") as folder:
            root = Path(folder).resolve()
            entry = root / "run.command"
            entry.write_bytes((launcher.ROOT / "run.command").read_bytes())
            executable = root / "python3"
            ready, release = root / "ready", root / "release"
            executable.write_text('#!/bin/zsh\n'
                'print -r -- "$PWD" > "$SNS_TEST_CWD"\n'
                'print -rl -- "$@" > "$SNS_TEST_ARGS"\n'
                ': > "$SNS_TEST_READY"\n'
                'while [[ ! -e "$SNS_TEST_RELEASE" ]]; do /bin/sleep 0.02; done\n', encoding="utf-8")
            executable.chmod(0o700)
            env = {**os.environ, "PATH": str(root) + ":" + os.environ.get("PATH", ""),
                   "SNS_TEST_CWD": str(root / "cwd"), "SNS_TEST_ARGS": str(root / "args"),
                   "SNS_TEST_READY": str(ready), "SNS_TEST_RELEASE": str(release)}
            child = subprocess.Popen(["/bin/zsh", str(entry), "--max-total-requests", "40"],
                cwd="/", env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                # The old launcher resumed reading a shifted file offset and
                # raised the exact unmatched-quote error seen in Terminal.
                entry.write_text('#!/bin/zsh\nread "unfinished\n', encoding="utf-8")
                release.touch()
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertNotIn("unmatched", stderr)
                self.assertEqual((root / "cwd").read_text(encoding="utf-8").strip(), str(root))
                self.assertEqual((root / "args").read_text(encoding="utf-8").splitlines(),
                    ["-m", "sns_comment.launcher", "--max-total-requests", "40"])
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_partial_collection_is_displayed_and_logged_without_credentials(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(launcher, "ROOT", Path(folder)), \
                mock.patch.object(launcher, "collect_main", return_value=1) as collect, \
                mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch.dict(os.environ, {"SNS_IG_ACCESS_TOKEN": "synthetic-secret"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(launcher.main([]), 1)
            collect.assert_called_once_with(["run", "--until-complete"])
            path = Path(folder) / ".local" / "last-launch.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "incomplete")
            self.assertEqual(record["exit_code"], 1)
            self.assertTrue(record["ended_at"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("synthetic-secret", path.read_text(encoding="utf-8") + output.getvalue())
            self.assertIn("수집 미완료", output.getvalue())

    def test_launch_records_early_failure_before_a_collection_run_exists(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(launcher, "ROOT", Path(folder)), \
                mock.patch.object(launcher, "collect_main", side_effect=SystemExit(2)), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main([]), 2)
            record = json.loads((Path(folder) / ".local" / "last-launch.json").read_text(encoding="utf-8"))
            self.assertEqual(record["exit_code"], 2)
            self.assertEqual(record["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()

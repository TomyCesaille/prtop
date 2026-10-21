"""`prtop --demo`, the report a screenshot is taken from."""

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from prtop.cli import main
from prtop.demo import demo_report
from prtop.report import MARK_ORDER, plain


class Demo(unittest.TestCase):
    def test_it_shows_every_severity_mark(self):
        marks = {line.split("|")[1].strip() for line in plain(demo_report().text).splitlines()[2:]}
        self.assertLessEqual(set(MARK_ORDER), marks)

    def test_it_shows_open_and_merged_rows_with_their_tasks(self):
        table = plain(demo_report().text)
        self.assertIn("mergeable, approved", table)
        self.assertIn("awaiting prod trigger", table)
        self.assertIn("PROJ-150 Single sign-on", table)

    def test_it_runs_without_any_configuration(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--demo"]), 0)
        self.assertIn("PR title", out.getvalue())

    def test_its_emoji_survive_a_pipe_on_a_windows_code_page(self):
        run = subprocess.run(
            [sys.executable, "-m", "prtop", "--demo"],
            capture_output=True,
            cwd=Path(__file__).parents[1],
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors="replace"))
        self.assertIn("👀 awaiting review", run.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()

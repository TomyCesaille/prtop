"""`prtop --demo`, the report a screenshot is taken from."""

import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from prtop.cli import main
from prtop.demo import OPEN_MRS, TITLE, demo_report, demo_story
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

    def test_each_fetch_of_the_live_demo_moves_something_until_the_story_ends(self):
        fetch = demo_story(pause=lambda seconds: None)
        tables = [plain(fetch().text) for _ in range(6)]
        self.assertEqual(tables[0], plain(demo_report().text))
        self.assertEqual(len(set(tables[:4])), 4)
        self.assertEqual(tables[3:], [tables[3]] * 3)

    def test_the_live_demo_takes_a_moment_to_fetch_like_a_forge(self):
        pauses = []
        fetch = demo_story(pause=pauses.append)
        for _ in range(3):
            fetch()
        self.assertEqual(len(pauses), 2)

    def test_it_reads_a_gitlab_instance_and_github_together(self):
        self.assertEqual(TITLE, "gitlab.example.com + github.com")
        self.assertIn("https://github.com/acme/worker/pull/812", {mr["web_url"] for mr in OPEN_MRS.values()})

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

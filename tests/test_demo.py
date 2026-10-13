"""`prtop --demo`, the report a screenshot is taken from."""

import io
import unittest
from contextlib import redirect_stdout

from prtop.cli import main
from prtop.demo import demo_report
from prtop.report import MARK_ORDER, plain


class Demo(unittest.TestCase):
    def test_it_shows_every_severity_mark(self):
        marks = {line.split("|")[1].strip() for line in plain(demo_report()).splitlines()[2:]}
        self.assertLessEqual(set(MARK_ORDER), marks)

    def test_it_shows_open_and_merged_rows_with_their_tasks(self):
        table = plain(demo_report())
        self.assertIn("mergeable, approved", table)
        self.assertIn("awaiting prod trigger", table)
        self.assertIn("PROJ-150 Single sign-on", table)

    def test_it_runs_without_any_configuration(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--demo"]), 0)
        self.assertIn("PR title", out.getvalue())


if __name__ == "__main__":
    unittest.main()

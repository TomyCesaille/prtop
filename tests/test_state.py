"""The state directory: what the previous fetch saw, and the latest fetch's report."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from prtop.config import Config
from prtop.report import Report, Row, hyperlink
from prtop.state import load_report, load_state, save_report, save_state

FRESH = {"open_mrs": {}, "deployments": {}, "last_checked": None}


class StateFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "prtop" / "github.com.json"

    def test_the_first_fetch_starts_from_nothing(self):
        self.assertEqual(load_state(self.path), FRESH)

    def test_a_malformed_file_is_reported_and_started_afresh(self):
        self.path.parent.mkdir()
        self.path.write_text("{not json")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(load_state(self.path), FRESH)
        self.assertIn("is malformed; starting fresh", stderr.getvalue())

    def test_a_saved_state_reads_back_whole_and_leaves_no_temporary_file(self):
        save_state(self.path, {"open_mrs": {"web!1": {"title": "équipe"}}, "deployments": {}})
        state = load_state(self.path)
        self.assertEqual(state["open_mrs"], {"web!1": {"title": "équipe"}})
        self.assertIsNotNone(state["last_checked"])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["github.com.json"])


FETCHED_AT = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)
REPORT = Report(
    [
        Row("!", "feat: équipe", "PROJ-1 the task", hyperlink("`web!1`", "https://example.com/1"),
            "✅ mergeable, approved", "https://example.com/PROJ-1"),
        None,
        Row("~", "feat: other", "—", "`web!2`", "👀 awaiting review"),
    ],
    True,
    True,
)


class LatestReport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config(state_dir=Path(tmp.name) / "prtop", forges=("gitlab", "github"))

    def read(self, seconds_later, config=None):
        later = FETCHED_AT + timedelta(seconds=seconds_later)
        return load_report(config or self.config, later, timedelta(seconds=10))

    def test_a_report_fetched_under_the_window_reads_back_whole(self):
        save_report(self.config, REPORT, FETCHED_AT)
        self.assertEqual(self.read(9), REPORT)
        self.assertEqual(self.read(9).text, REPORT.text)

    def test_a_report_as_old_as_the_window_is_not_read(self):
        save_report(self.config, REPORT, FETCHED_AT)
        self.assertIsNone(self.read(10))

    def test_a_report_dated_in_the_future_is_not_read(self):
        save_report(self.config, REPORT, FETCHED_AT)
        self.assertIsNone(self.read(-1))

    def test_a_report_fetched_under_other_settings_is_not_read(self):
        save_report(self.config, REPORT, FETCHED_AT)
        self.assertIsNone(self.read(0, replace(self.config, task_source="issues")))

    def test_before_any_fetch_there_is_no_report(self):
        self.assertIsNone(self.read(0))

    def test_a_malformed_report_is_not_read(self):
        self.config.state_dir.mkdir()
        self.config.report_file.write_text('{"fetched_at": "yesterday"}')
        self.assertIsNone(self.read(0))

if __name__ == "__main__":
    unittest.main()

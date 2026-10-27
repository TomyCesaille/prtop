"""The state directory: what the previous fetch saw, and the live view's latest report."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


HOSTS = ("gitlab.example.com", "github.com")
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


class LiveReport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "prtop" / "report.json"

    def read(self, seconds_later, hosts=HOSTS):
        return load_report(self.path, hosts, FETCHED_AT + timedelta(seconds=seconds_later), timedelta(minutes=5))

    def test_a_report_fetched_within_the_delay_reads_back_whole(self):
        save_report(self.path, HOSTS, REPORT, FETCHED_AT)
        self.assertEqual(self.read(299), REPORT)
        self.assertEqual(self.read(299).text, REPORT.text)

    def test_a_report_as_old_as_the_delay_is_too_old_to_stand_in_for_a_fetch(self):
        save_report(self.path, HOSTS, REPORT, FETCHED_AT)
        self.assertIsNone(self.read(300))

    def test_a_report_dated_in_the_future_is_not_read(self):
        save_report(self.path, HOSTS, REPORT, FETCHED_AT)
        self.assertIsNone(self.read(-1))

    def test_a_report_of_other_forges_is_not_read(self):
        save_report(self.path, HOSTS, REPORT, FETCHED_AT)
        self.assertIsNone(self.read(0, hosts=("gitlab.example.com",)))

    def test_without_a_live_view_there_is_no_report(self):
        self.assertIsNone(self.read(0))

    def test_a_malformed_report_is_not_read(self):
        self.path.parent.mkdir()
        self.path.write_text('{"fetched_at": "yesterday"}')
        self.assertIsNone(self.read(0))


if __name__ == "__main__":
    unittest.main()

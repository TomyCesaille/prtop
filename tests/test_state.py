"""The state directory: what the previous fetch saw, and the latest fetch's report."""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from prtop.config import Config
from prtop.report import Report, Row, hyperlink
from prtop.state import WRITE_ATTEMPTS, load_report, load_state, save_report, save_state

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

    def replace_after(self, before_replacing):
        """`os.replace`, with `before_replacing` run before each call."""
        calls = []

        def replacing(src, dst):
            calls.append(src)
            before_replacing(len(calls))
            real_replace(src, dst)

        real_replace = os.replace
        return patch("os.replace", side_effect=replacing), calls

    def test_two_prtops_saving_at_the_same_moment_both_save(self):
        def another_prtop_saves(call):
            if call == 1:
                save_state(self.path, {"open_mrs": {"web!2": {}}, "deployments": {}})

        patched, _ = self.replace_after(another_prtop_saves)
        with patched:
            save_state(self.path, {"open_mrs": {"web!1": {}}, "deployments": {}})
        self.assertEqual(load_state(self.path)["open_mrs"], {"web!1": {}})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["github.com.json"])

    def test_a_state_directory_removed_during_the_save_is_made_again(self):
        def directory_removed(call):
            if call == 1:
                shutil.rmtree(self.path.parent)

        patched, calls = self.replace_after(directory_removed)
        with patched:
            save_state(self.path, {"open_mrs": {"web!1": {}}, "deployments": {}})
        self.assertEqual(load_state(self.path)["open_mrs"], {"web!1": {}})
        self.assertEqual(len(calls), 2)

    def test_a_state_directory_that_keeps_disappearing_fails_the_save(self):
        patched, calls = self.replace_after(lambda call: shutil.rmtree(self.path.parent))
        with patched, self.assertRaises(FileNotFoundError):
            save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertEqual(len(calls), WRITE_ATTEMPTS)

    def test_a_save_that_fails_leaves_no_temporary_file(self):
        no_space = OSError("No space left on device")
        with patch("os.replace", side_effect=no_space), self.assertRaises(OSError):
            save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertEqual(list(self.path.parent.iterdir()), [])


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

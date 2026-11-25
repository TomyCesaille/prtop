"""The state directory: what the previous fetch saw, and the latest fetch's report."""

import io
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from prtop.config import Config
from prtop.report import Report, Row, hyperlink
from prtop.state import (
    ORPHANED_AFTER_SECONDS,
    RETRY_PAUSE_SECONDS,
    WRITE_ATTEMPTS,
    load_report,
    load_state,
    save_report,
    save_state,
)

FRESH = {"open_mrs": {}, "deployments": {}, "last_checked": None}


class StateFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "prtop" / "github.com.json"
        pause = patch("prtop.state.time.sleep")
        self.pauses = pause.start()
        self.addCleanup(pause.stop)

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

    def patch_replace(self, run_before):
        calls = []

        def replacing(src, dst):
            calls.append(src)
            run_before(len(calls))
            real_replace(src, dst)

        real_replace = os.replace
        return patch("os.replace", side_effect=replacing), calls

    def test_two_prtops_saving_at_the_same_moment_both_save(self):
        def another_prtop_saves(call):
            if call == 1:
                save_state(self.path, {"open_mrs": {"web!2": {}}, "deployments": {}})

        patched, _ = self.patch_replace(another_prtop_saves)
        with patched:
            save_state(self.path, {"open_mrs": {"web!1": {}}, "deployments": {}})
        self.assertEqual(load_state(self.path)["open_mrs"], {"web!1": {}})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["github.com.json"])

    def test_a_state_directory_removed_during_the_save_is_made_again(self):
        def directory_removed(call):
            if call == 1:
                shutil.rmtree(self.path.parent)

        patched, calls = self.patch_replace(directory_removed)
        with patched:
            save_state(self.path, {"open_mrs": {"web!1": {}}, "deployments": {}})
        self.assertEqual(load_state(self.path)["open_mrs"], {"web!1": {}})
        self.assertEqual(len(calls), 2)

    def test_a_state_directory_that_keeps_disappearing_fails_the_save(self):
        patched, calls = self.patch_replace(lambda call: shutil.rmtree(self.path.parent))
        with patched, self.assertRaises(FileNotFoundError):
            save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertEqual(len(calls), WRITE_ATTEMPTS)

    def test_a_file_another_prtop_reads_on_windows_is_replaced_once_it_is_let_go(self):
        def read_by_another_prtop(call):
            if call == 1:
                raise PermissionError(13, "Access is denied")

        patched, calls = self.patch_replace(read_by_another_prtop)
        with patched:
            save_state(self.path, {"open_mrs": {"web!1": {}}, "deployments": {}})
        self.assertEqual(load_state(self.path)["open_mrs"], {"web!1": {}})
        self.assertEqual(len(calls), 2)
        self.pauses.assert_called_once_with(RETRY_PAUSE_SECONDS)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["github.com.json"])

    def test_a_file_that_stays_locked_fails_the_save(self):
        def locked(call):
            raise PermissionError(13, "Access is denied")

        patched, calls = self.patch_replace(locked)
        with patched, self.assertRaises(PermissionError):
            save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertEqual(len(calls), WRITE_ATTEMPTS)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_a_save_that_fails_leaves_no_temporary_file(self):
        no_space = OSError("No space left on device")
        with patch("os.replace", side_effect=no_space), self.assertRaises(OSError):
            save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def temporary_file(self, name, seconds_old):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.parent / name
        tmp.write_text("{")
        written = time.time() - seconds_old
        os.utime(tmp, (written, written))
        return tmp

    def test_temporary_files_left_by_a_killed_prtop_are_removed_by_the_next_save(self):
        self.temporary_file("github.com.json.k1ll3d.tmp", ORPHANED_AFTER_SECONDS + 1)
        self.temporary_file("github.com.json.0ld3r.tmp", 3600)
        self.temporary_file("github.com.json.tmp", 3600)
        save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["github.com.json"])

    def test_a_temporary_file_another_prtop_is_writing_stays(self):
        writing = self.temporary_file("github.com.json.wr1t3.tmp", 1)
        save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertTrue(writing.exists())

    def test_the_temporary_files_of_other_files_stay(self):
        report = self.temporary_file("report.json.k1ll3d.tmp", 3600)
        save_state(self.path, {"open_mrs": {}, "deployments": {}})
        self.assertTrue(report.exists())

    def test_an_orphan_another_prtop_removes_first_does_not_fail_the_save(self):
        orphan = self.temporary_file("github.com.json.k1ll3d.tmp", 3600)
        real_stat = Path.stat

        def removed_by_another_prtop(path, *args, **kwargs):
            if path == orphan:
                path.unlink(missing_ok=True)
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", removed_by_another_prtop):
            save_state(self.path, {"open_mrs": {"web!1": {}}, "deployments": {}})
        self.assertEqual(load_state(self.path)["open_mrs"], {"web!1": {}})

    def test_an_orphan_that_cannot_be_removed_does_not_fail_the_save(self):
        orphan = self.temporary_file("github.com.json.k1ll3d.tmp", 3600)
        real_unlink = Path.unlink

        def access_denied_on_the_orphan(path, *args, **kwargs):
            if path == orphan:
                raise PermissionError(13, "Access is denied")
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", access_denied_on_the_orphan):
            save_state(self.path, {"open_mrs": {"web!1": {}}, "deployments": {}})
        self.assertEqual(load_state(self.path)["open_mrs"], {"web!1": {}})
        self.assertTrue(orphan.exists())


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

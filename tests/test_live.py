"""The live view, driven by a terminal and a clock that answer from memory."""

import re
import sys
import time
import unittest
from concurrent.futures import Future
from contextlib import redirect_stderr
from datetime import datetime, timedelta

from prtop import live
from prtop.live import REVERSE, Fetcher, Messages, fit
from prtop.model import ForgeError
from prtop.report import (
    ACT,
    NOTHING_OPEN,
    PEOPLE,
    TITLE_MAX,
    Report,
    Row,
    display_width,
    hyperlink,
    unreachable_row,
)

STARTED = datetime(2026, 10, 21, 14, 0, 0)


def row(iid, status="👀 awaiting review", mark=PEOPLE, title=None, task="PROJ-1 the task"):
    return Row(mark, title or f"feat: change {iid}", task, f"`web!{iid}`", status)


def report(*rows):
    return Report(list(rows), True, any(r.mark == ACT for r in rows))


def inline(attempt):
    future = Future()
    future.set_result(attempt())
    return future


def shown(line):
    return re.sub("\033\\[[0-9;]*m", "", line)


class FakeTerminal:
    def __init__(self, clock, keys, size):
        self.clock = clock
        self.keys = sorted(keys)
        self.columns, self.lines = size
        self.frames = []
        self.cleared = 0

    def size(self):
        return self.columns, self.lines

    def read_keys(self, timeout):
        self.clock.now += timeout
        if self.clock.now > 3600:
            raise AssertionError("the view never stopped")
        due = [key for at, key in self.keys if at <= self.clock.now]
        self.keys = [(at, key) for at, key in self.keys if at > self.clock.now]
        return due

    def draw(self, lines):
        self.frames.append(lines)

    def redraw(self):
        self.cleared += 1


class LiveView(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.reports = [report(row(1))]
        self.fetched_at = []
        self.seen = []

    def fetch(self):
        self.fetched_at.append(self.now)
        outcome = self.reports[min(len(self.fetched_at), len(self.reports)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def run_view(self, keys=(), max_iterations=None, size=(160, 40), fetcher=None):
        self.terminal = FakeTerminal(self, keys, size)
        return live.run(
            fetcher or Fetcher(self.fetch, submit=inline),
            self.terminal,
            "gitlab.example.com",
            30,
            max_iterations,
            on_report=self.seen.append,
            clock=lambda: self.now,
            now=lambda: STARTED + timedelta(seconds=self.now),
        )

    def last_frame(self):
        return [shown(line) for line in self.terminal.frames[-1]]

    def test_it_fetches_at_once_then_every_30_seconds(self):
        self.run_view(keys=[(95, "q")])
        self.assertEqual(len(self.fetched_at), 4)
        self.assertEqual(self.fetched_at[0], 0)

    def test_q_ends_the_view_with_no_table_left_behind(self):
        view = self.run_view(keys=[(1, "q")])
        self.assertFalse(view.finished)

    def test_after_n_fetches_it_stops_and_hands_back_the_last_table(self):
        self.reports = [report(row(1)), report(row(2))]
        view = self.run_view(max_iterations=2)
        self.assertTrue(view.finished)
        self.assertIn("feat: change 2", view.report.text)
        self.assertEqual(len(self.fetched_at), 2)

    def test_ctrl_l_fetches_at_once_and_repaints_the_screen(self):
        self.run_view(keys=[(5, "Ctrl+L"), (6, "q")])
        self.assertEqual(len(self.fetched_at), 2)
        self.assertEqual(self.terminal.cleared, 1)

    def test_z_pauses_fetching_until_pressed_again(self):
        self.run_view(keys=[(1, "Z"), (100, "Z"), (101, "q")])
        self.assertEqual(len(self.fetched_at), 2)
        self.assertGreaterEqual(self.fetched_at[1], 100)

    def test_ctrl_l_still_fetches_while_paused(self):
        self.run_view(keys=[(1, "Z"), (5, "Ctrl+L"), (6, "q")])
        self.assertEqual(len(self.fetched_at), 2)
        self.assertIn("paused", self.last_frame()[0])

    def test_the_status_line_names_the_instance_and_counts_down_to_the_next_fetch(self):
        self.run_view(keys=[(5, "q")])
        status = self.last_frame()[0]
        self.assertTrue(status.startswith("gitlab.example.com · fetched 14:00:00 · next in 2"), status)
        self.assertIn("next in 30s", self.terminal.frames[1][0])

    def test_the_key_bar_sits_on_the_last_line_of_the_window(self):
        self.run_view(keys=[(1, "q")], size=(160, 20))
        frame = self.last_frame()
        self.assertEqual(len(frame), 20)
        self.assertTrue(frame[-1].startswith("q  quit"))

    def test_a_forge_that_fails_keeps_the_last_table_on_screen_and_says_why(self):
        self.reports = [report(row(1)), ForgeError("502 Bad Gateway")]
        self.run_view(keys=[(40, "q")])
        frame = "\n".join(self.last_frame())
        self.assertIn("feat: change 1", frame)
        self.assertIn("prtop: 502 Bad Gateway", frame)

    def test_an_error_over_several_lines_keeps_the_screen_one_line_per_row(self):
        self.reports = [ForgeError("Command failed: glab api\n401 Unauthorized")]
        self.run_view(keys=[(1, "q")])
        self.assertEqual(self.last_frame()[1:3], ["prtop: Command failed: glab api", "401 Unauthorized"])
        self.assertFalse(any("\n" in line for line in self.last_frame()))

    def test_what_a_fetch_prints_to_stderr_shows_under_the_status_line(self):
        def noisy():
            print("prtop: web!1: 502 Bad Gateway", file=sys.stderr)
            return report(row(1))

        messages = Messages()
        with redirect_stderr(messages):
            self.run_view(keys=[(1, "q")], fetcher=Fetcher(noisy, messages.take, inline))
        self.assertEqual(self.last_frame()[1], "prtop: web!1: 502 Bad Gateway")

    def test_a_row_that_changed_stays_in_reverse_video_until_the_next_change(self):
        self.reports = [
            report(row(1), row(2)),
            report(row(1, "✅ mergeable, approved", ACT), row(2)),
            report(row(1, "✅ mergeable, approved", ACT), row(2)),
        ]
        self.run_view(keys=[(70, "q")])
        lines = self.terminal.frames[-1]
        changed = [line for line in lines if "web!1" in line]
        unchanged = [line for line in lines if "web!2" in line]
        self.assertTrue(changed[0].startswith(REVERSE))
        self.assertFalse(unchanged[0].startswith(REVERSE))
        self.assertIn("last change 14:00:30", lines[0])

    def test_the_same_mr_id_on_two_forges_changes_on_its_own(self):
        def on(forge, status="👀 awaiting review", mark=PEOPLE):
            return row(1, status, mark)._replace(pr=hyperlink("`web!1`", f"https://{forge}/web/1"))

        self.reports = [
            report(on("gitlab.example.com"), on("github.com")),
            report(on("gitlab.example.com"), on("github.com", "✅ mergeable, approved", ACT)),
        ]
        self.run_view(keys=[(40, "q")])
        highlighted = [line for line in self.terminal.frames[-1] if line.startswith(REVERSE)]
        self.assertEqual(len(highlighted), 1)
        self.assertIn("mergeable", highlighted[0])

    def test_a_second_forge_going_unreachable_changes_on_its_own(self):
        self.reports = [
            report(unreachable_row("github.com"), row(1)),
            report(unreachable_row("github.com"), unreachable_row("gitlab.example.com")),
        ]
        self.run_view(keys=[(40, "q")])
        highlighted = [line for line in self.terminal.frames[-1] if line.startswith(REVERSE)]
        self.assertEqual(len(highlighted), 1)
        self.assertIn("gitlab.example.com", highlighted[0])

    def test_nothing_open_says_so_in_place_of_the_table(self):
        self.reports = [report()]
        self.run_view(keys=[(1, "q")])
        self.assertEqual(self.last_frame()[2], NOTHING_OPEN)

    def test_a_key_prtop_has_no_use_for_changes_nothing(self):
        view = self.run_view(keys=[(1, "x"), (2, "q")])
        self.assertEqual((len(self.fetched_at), view.help, view.paused, self.terminal.cleared), (1, False, False, 0))
        self.assertIn("feat: change 1", "\n".join(self.last_frame()))

    def test_the_first_table_has_nothing_in_reverse_video(self):
        self.run_view(keys=[(1, "q")])
        self.assertFalse(any(REVERSE in line for line in self.terminal.frames[-1]))

    def test_h_shows_the_keys_and_the_marks_and_esc_brings_the_table_back(self):
        self.run_view(keys=[(1, "h"), (2, "Esc"), (3, "q")])
        help_frame = next(f for f in self.terminal.frames if "  Ctrl+L           fetch now" in f)
        self.assertIn("  !   on you now", help_frame)
        self.assertIn("feat: change 1", "\n".join(self.last_frame()))

    def test_every_line_fits_a_narrow_window(self):
        self.reports = [report(row(1, title="x" * 80, task="y" * 80))]
        self.run_view(keys=[(1, "q")], size=(90, 40))
        self.assertLessEqual(max(map(display_width, self.last_frame())), 90)

    def test_rows_below_the_window_are_counted_rather_than_lost(self):
        self.reports = [report(*(row(i) for i in range(10)))]
        self.run_view(keys=[(1, "q")], size=(160, 10))
        self.assertIn("… 6 more lines below", "\n".join(self.last_frame()))

    def test_every_report_reaches_the_tab_colour(self):
        self.reports = [report(row(1)), ForgeError("down"), report(row(1))]
        self.run_view(keys=[(70, "q")])
        self.assertEqual(len(self.seen), 2)


class Fitting(unittest.TestCase):
    def test_the_task_column_gives_way_before_the_title(self):
        title = "t" * TITLE_MAX
        lines = fit(report(row(1, title=title, task="y" * 80)), 130)
        self.assertLessEqual(max(map(display_width, lines)), 130)
        self.assertIn(title, lines[2])

    def test_a_window_too_narrow_for_the_narrowest_columns_cuts_every_line_at_its_edge(self):
        lines = fit(report(row(1, title="x" * 80, task="y" * 80)), 50)
        self.assertEqual({display_width(line) for line in lines}, {50})
        self.assertTrue(all(line.endswith("…") for line in lines))

    def test_a_table_that_fits_is_left_whole(self):
        table = report(row(1))
        self.assertEqual(fit(table, 300), table.text.splitlines())


class Messages_(unittest.TestCase):
    def test_messages_stand_in_for_stderr_line_by_line(self):
        messages = Messages()
        self.assertTrue(messages.writable())
        print("prtop: one\n\nprtop: two", file=messages)
        self.assertEqual(messages.take(), ["prtop: one", "prtop: two"])
        self.assertEqual(messages.take(), [])


class FetchingInTheBackground(unittest.TestCase):
    def test_a_failure_other_than_the_forges_surfaces_when_the_fetch_is_collected(self):
        def broken():
            raise KeyError("ref")

        fetcher = Fetcher(broken)
        fetcher.start()
        with self.assertRaises(KeyError):
            while fetcher.poll() is None:
                time.sleep(0.01)

    def test_it_stays_busy_until_its_outcome_is_collected(self):
        fetcher = Fetcher(lambda: report(row(1)), submit=inline)
        fetcher.start()
        self.assertTrue(fetcher.busy)
        self.assertIsNotNone(fetcher.poll())
        self.assertFalse(fetcher.busy)


if __name__ == "__main__":
    unittest.main()

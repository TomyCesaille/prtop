"""One check end to end, against a forge that answers from memory."""

import io
import json
import runpy
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from prtop import model
from prtop.cli import batch, fetch_report, main, parse_args, run_check, show_live, tab_color_hook, watch
from prtop.config import Config, ConfigError
from prtop.forges import make_forges
from prtop.forges.github import GitHub
from prtop.forges.gitlab import GitLab
from prtop.live import View
from prtop.model import DeployState, ForgeError
from prtop.report import NOTHING_OPEN, Report, plain
from prtop.state import save_report
from prtop.tasks import NoTasks, Task

HOST = "gitlab.example.com"
EPOCH = datetime(2026, 10, 27, 14, 0, tzinfo=UTC)


def entry(iid, status=model.AWAITING_REVIEW, *, approved=False, title=None):
    return {
        "title": title or f"feat: change {iid}",
        "task": "",
        "status": status,
        "fully_approved": approved,
        "has_conflicts": False,
        "threads_await_user": True,
        "project_path": "acme/web/frontend",
        "iid": iid,
        "web_url": f"https://{HOST}/acme/web/frontend/-/merge_requests/{iid}",
    }


class FakeForge:
    def __init__(self, host=HOST):
        self.host = host
        self.open: dict = {}
        self.error: str | None = None
        self.merged: set[int] = set()
        self.states: dict[str, DeployState] = {}
        self.asked_about_merges: list[int] = []
        self.merge_check_error: str | None = None
        self.listings = 0

    def open_merge_requests(self, tasks):
        self.listings += 1
        if self.error:
            raise ForgeError(self.error)
        return dict(self.open)

    def merged_deployment(self, entry):
        self.asked_about_merges.append(entry["iid"])
        if self.merge_check_error:
            raise ForgeError(self.merge_check_error)
        if entry["iid"] not in self.merged:
            return None
        return {"title": entry["title"], "task": "", "web_url": entry["web_url"]}

    def deploy_states(self, deployments):
        return {k: self.states.get(k, DeployState(model.PIPELINE_RUNNING, "running")) for k in deployments}


class Check(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.state_file = self.state_dir / f"{HOST}.json"
        self.config = Config(state_dir=self.state_dir)
        self.forge = FakeForge()

    def check(self, config=None, tasks=None):
        return run_check(config or self.config, [self.forge], tasks or NoTasks())

    def saved(self):
        return json.loads(self.state_file.read_text())

    def test_with_nothing_open_the_report_says_so(self):
        self.assertEqual(self.check().text, NOTHING_OPEN)

    def test_the_first_run_records_a_baseline_without_reporting_merges(self):
        self.forge.open = {"frontend!1": entry(1)}
        self.check()
        self.assertEqual(self.forge.asked_about_merges, [])
        self.assertIn("frontend!1", self.saved()["open_mrs"])

    def test_an_mr_that_merged_moves_to_the_deploy_rows(self):
        self.forge.open = {"frontend!1": entry(1, title="feat: the merged one")}
        self.check()
        self.forge.open, self.forge.merged = {}, {1}
        report = self.check()
        self.assertIn("feat: the merged one", report.text)
        self.assertIn("pipeline running", report.text)
        self.assertIn("frontend!1", self.saved()["deployments"])

    def test_a_merge_with_no_production_job_leaves_once_its_pipeline_is_done(self):
        self.forge.open = {"frontend!1": entry(1, title="feat: nothing to deploy")}
        self.check()
        self.forge.open, self.forge.merged = {}, {1}
        self.forge.states["frontend!1"] = DeployState(model.NO_PROD_JOB)
        self.assertNotIn("feat: nothing to deploy", self.check().text)
        self.assertEqual(self.saved()["deployments"], {})

    def test_an_mr_closed_without_merging_drops_out(self):
        self.forge.open = {"frontend!1": entry(1)}
        self.check()
        self.forge.open = {}
        self.check()
        self.assertEqual(self.saved()["deployments"], {})

    def test_a_deployment_that_shipped_leaves_the_report(self):
        self.forge.open = {"frontend!1": entry(1, title="feat: shipped")}
        self.check()
        self.forge.open, self.forge.merged = {}, {1}
        self.forge.states["frontend!1"] = DeployState(model.SHIPPED)
        self.assertNotIn("feat: shipped", self.check().text)
        self.assertEqual(self.saved()["deployments"], {})

    def test_a_forge_that_does_not_answer_keeps_the_baseline_and_reads_as_no_merge(self):
        self.forge.open = {"frontend!1": entry(1)}
        self.check()
        self.forge.error = "connection refused"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            report = self.check()
        self.assertIn(f"{HOST}: connection refused", stderr.getvalue())
        self.assertIn("instance unreachable", report.text)
        self.assertTrue(report.action_needed)
        self.assertEqual(self.forge.asked_about_merges, [])
        self.assertIn("frontend!1", self.saved()["open_mrs"])

    def test_a_merge_the_forge_could_not_confirm_is_asked_about_again_on_the_next_run(self):
        self.forge.open = {"frontend!1": entry(1, title="feat: the merged one")}
        self.check()
        self.forge.open, self.forge.merged = {}, {1}
        self.forge.merge_check_error = "502 Bad Gateway"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            report = self.check()
        self.assertIn("frontend!1: 502 Bad Gateway", stderr.getvalue())
        self.assertNotIn("feat: the merged one", report.text)

        self.forge.merge_check_error = None
        self.assertIn("feat: the merged one", self.check().text)
        self.assertIn("frontend!1", self.saved()["deployments"])
        self.assertEqual(self.saved()["open_mrs"], {})

    def test_without_deploy_tracking_merges_are_not_followed(self):
        config = Config(state_dir=self.state_dir, deploy_tracking=False)
        self.forge.open = {"frontend!1": entry(1)}
        self.check(config)
        self.forge.open, self.forge.merged = {}, {1}
        self.check(config)
        self.assertEqual(self.forge.asked_about_merges, [])

    def test_without_a_task_source_the_task_column_is_left_out(self):
        self.forge.open = {"frontend!1": entry(1)}
        self.assertNotIn("Task", self.check().text.splitlines()[0])

    def test_a_task_source_brings_the_task_column(self):
        class EveryMrIsOneTask:
            shows_column = True

            def resolve(self, branch, closing_issues):
                return Task("PROJ-1 the task")

        self.forge.open = {"frontend!1": dict(entry(1), task="PROJ-1 the task")}
        report = self.check(tasks=EveryMrIsOneTask())
        self.assertIn("Task", report.text.splitlines()[0])
        self.assertIn("PROJ-1 the task", report.text)

    def test_an_mr_on_you_calls_for_action(self):
        self.forge.open = {"frontend!1": entry(1, model.MERGEABLE, approved=True)}
        self.assertTrue(self.check().action_needed)

    def test_two_repositories_of_the_same_name_show_their_full_paths(self):
        self.forge.open = {"acme/web!7": entry(7), "labs/web!7": entry(7), "acme/web!8": entry(8)}
        cells = [plain(row.pr) for row in self.check().rows if row]
        self.assertEqual(sorted(cells), ["`acme/web!7`", "`labs/web!7`", "`web!8`"])

    def test_a_merge_is_followed_when_a_namesake_repository_opens_the_same_number(self):
        self.forge.open = {"acme/web!7": entry(7, title="feat: the merged one")}
        self.check()
        self.forge.open, self.forge.merged = {"labs/web!7": entry(7, title="feat: the new one")}, {7}
        report = self.check()
        self.assertEqual(self.forge.asked_about_merges, [7])
        self.assertIn("acme/web!7", self.saved()["deployments"])
        self.assertIn("`acme/web!7`", plain(report.text))
        self.assertIn("`labs/web!7`", plain(report.text))

    def test_a_deploy_awaiting_its_trigger_never_calls_for_action(self):
        self.forge.open = {"frontend!1": entry(1)}
        self.check()
        self.forge.open, self.forge.merged = {}, {1}
        self.forge.states["frontend!1"] = DeployState(model.AWAITING_TRIGGER)
        report = self.check()
        self.assertIn("awaiting prod trigger", report.text)
        self.assertFalse(report.action_needed)


class SeveralForges(unittest.TestCase):
    """One GitLab instance and github.com, read together into one table."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.config = Config(state_dir=self.state_dir, forges=("gitlab", "github"))
        self.gitlab, self.github = FakeForge(HOST), FakeForge("github.com")

    def check(self):
        return run_check(self.config, [self.gitlab, self.github], NoTasks())

    def test_their_rows_sort_together_by_mark(self):
        self.gitlab.open = {"web!1": entry(1, title="feat: on gitlab")}
        self.github.open = {"cli!2": entry(2, model.MERGEABLE, approved=True, title="feat: on github")}
        lines = plain(self.check().text).splitlines()
        self.assertIn("feat: on github", lines[2])
        self.assertIn("feat: on gitlab", lines[4])

    def test_a_pull_request_reads_exactly_like_a_merge_request(self):
        self.gitlab.open = {"web!1": entry(1, title="feat: same")}
        on_gitlab = plain(self.check().text)
        on_github = dict(entry(1, title="feat: same"), web_url="https://github.com/acme/web/pull/1")
        self.gitlab.open, self.github.open = {}, {"web!1": on_github}
        self.assertEqual(plain(self.check().text), on_gitlab)

    def test_each_forge_keeps_a_state_file_of_its_own(self):
        self.gitlab.open = {"web!1": entry(1)}
        self.github.open = {"web!1": entry(1, title="the github one")}
        self.check()
        saved = {path.name: json.loads(path.read_text()) for path in self.state_dir.iterdir()}
        self.assertEqual(set(saved), {f"{HOST}.json", "github.com.json"})
        self.assertEqual(saved["github.com.json"]["open_mrs"]["web!1"]["title"], "the github one")

    def test_a_forge_that_does_not_answer_leaves_the_others_rows_in_place(self):
        self.gitlab.open = {"web!1": entry(1, title="feat: still here")}
        self.github.error = "connection refused"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            report = self.check()
        self.assertIn("feat: still here", report.text)
        self.assertIn("github.com", report.text)
        self.assertIn("instance unreachable", report.text)
        self.assertTrue(report.action_needed)
        self.assertIn("prtop: github.com: connection refused", stderr.getvalue())

    def test_watch_sees_two_forges_identical_ids_trade_states(self):
        ready, waiting = entry(1, model.MERGEABLE, approved=True, title="feat: same"), entry(1, title="feat: same")
        on_github = {"web_url": "https://github.com/acme/web/pull/1"}
        self.gitlab.open, self.github.open = {"web!1": ready}, {"web!1": waiting | on_github}
        clock = [0.0]

        def trade(seconds):
            clock[0] += seconds
            self.gitlab.open, self.github.open = {"web!1": waiting}, {"web!1": ready | on_github}

        out = io.StringIO()
        with redirect_stdout(out):
            code = watch(self.config, [self.gitlab, self.github], NoTasks(), 300, 900,
                         sleep=trade, clock=lambda: clock[0], now=lambda: EPOCH + timedelta(seconds=clock[0]))
        self.assertEqual(code, 0)
        self.assertNotIn("TIMEOUT", out.getvalue())
        self.assertEqual(out.getvalue().count("no change"), 1)

    def test_a_merge_is_asked_of_the_forge_it_happened_on(self):
        self.github.open = {"cli!2": entry(2, title="feat: merged on github")}
        self.check()
        self.github.open, self.github.merged = {}, {2}
        report = self.check()
        self.assertEqual((self.gitlab.asked_about_merges, self.github.asked_about_merges), ([], [2]))
        self.assertIn("feat: merged on github", report.text)

    def test_every_forge_is_made_in_the_order_the_setting_names_them(self):
        forges = make_forges(Config(state_dir=self.state_dir, forges=("github", "gitlab")))
        self.assertEqual([type(f) for f in forges], [GitHub, GitLab])
        self.assertEqual([f.host for f in forges], ["github.com", "gitlab.com"])


class Watch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config(state_dir=Path(tmp.name))
        self.forge = FakeForge()
        self.now = 0.0
        self.checks = 0

    def sleep(self, seconds):
        self.now += seconds
        self.checks += 1
        if self.checks == self.change_after:
            self.forge.open = {"frontend!1": entry(1, model.MERGEABLE, approved=True, title="feat: ready")}

    def watch(self, change_after=None, cap=3600):
        self.change_after = change_after
        out = io.StringIO()
        with redirect_stdout(out):
            code = watch(self.config, [self.forge], NoTasks(), 300, cap,
                         sleep=self.sleep, clock=lambda: self.now, now=self.wall_clock)
        return code, out.getvalue()

    def wall_clock(self):
        return EPOCH + timedelta(seconds=self.now)

    def test_it_exits_with_the_report_as_soon_as_something_changes(self):
        code, out = self.watch(change_after=2)
        self.assertEqual(code, 0)
        self.assertIn("feat: ready", out)
        self.assertEqual(out.count("no change"), 2)

    def test_it_gives_up_with_a_timeout_line_after_the_cap(self):
        code, out = self.watch(cap=900)
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("TIMEOUT after 900s with no PR activity"))

    def test_a_check_that_fails_stops_it_with_an_error(self):
        def broken(deployments):
            raise ForgeError("boom")

        self.forge.deploy_states = broken
        with redirect_stderr(io.StringIO()):
            code, out = self.watch()
        self.assertEqual(code, 1)
        self.assertIn("check failed", out)

    def test_a_check_within_10_seconds_of_another_prtops_fetch_reads_that_fetch(self):
        self.now = -9
        fetch_report(self.config, [self.forge], NoTasks(), self.wall_clock)
        self.now = 0
        self.watch(cap=300)
        self.assertEqual(self.forge.listings, 1 + 1)


class SharedFetch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config(state_dir=Path(tmp.name))
        self.forge = FakeForge()
        self.forge.open = {"frontend!1": entry(1)}

    def fetch(self, seconds):
        return fetch_report(self.config, [self.forge], NoTasks(), lambda: EPOCH + timedelta(seconds=seconds))

    def test_a_fetch_under_10_seconds_old_stands_in_for_a_new_one(self):
        first = self.fetch(0)
        self.forge.open = {}
        self.assertEqual(self.fetch(9.9), first)
        self.assertEqual(self.forge.listings, 1)

    def test_a_fetch_10_seconds_old_is_made_again(self):
        self.fetch(0)
        self.forge.open = {}
        self.assertEqual(self.fetch(10).text, NOTHING_OPEN)
        self.assertEqual(self.forge.listings, 2)

    def test_a_fetch_under_other_settings_is_made_again(self):
        save_report(Config(state_dir=self.config.state_dir, deploy_tracking=False), Report([], False, False), EPOCH)
        self.fetch(0)
        self.assertEqual(self.forge.listings, 1)


class Batch(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        return Report([], False, False)

    def printed(self, iterations):
        out = io.StringIO()
        with redirect_stdout(out):
            code = batch(self.fetch, 30, iterations, sleep=self.sleeps.append)
        return code, out.getvalue()

    def test_output_that_is_not_a_terminal_gets_one_table_and_the_command_ends(self):
        code, out = self.printed(1)
        self.assertEqual((code, out.count(NOTHING_OPEN), self.sleeps), (0, 1, []))

    def test_n_fetches_print_n_tables_the_delay_apart(self):
        _, out = self.printed(3)
        self.assertEqual((out.count(NOTHING_OPEN), self.sleeps), (3, [30, 30]))

    def test_a_forge_that_does_not_answer_ends_it_with_an_error(self):
        def down():
            raise ForgeError("connection refused")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(batch(down, 30, 1), 1)
        self.assertIn("prtop: connection refused", stderr.getvalue())


class Arguments(unittest.TestCase):
    def test_the_view_fetches_every_30_seconds_and_watch_checks_every_5_minutes(self):
        self.assertEqual(parse_args([]).delay, 30)
        self.assertEqual(parse_args(["watch"]).delay, 300)

    def test_the_delay_can_follow_or_precede_watch(self):
        self.assertEqual(parse_args(["watch", "-d", "60"]).delay, 60)
        self.assertEqual(parse_args(["-d", "60", "watch"]).delay, 60)

    def test_a_fetch_count_means_nothing_to_watch(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["-n", "2", "watch"])

    def test_a_delay_under_one_second_is_refused(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["-d", "0"])

    def test_the_demo_has_nothing_to_watch(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--demo", "watch"])


class Terminal(io.StringIO):
    def isatty(self):
        return True


class Main(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config(state_dir=Path(tmp.name), forges=("gitlab", "github"), gitlab_host=HOST)
        self.forges = [FakeForge(HOST), FakeForge("github.com")]
        self.forges[0].open = {"web!1": entry(1, title="feat: on gitlab")}
        self.forges[1].open = {"cli!2": entry(2, title="feat: on github")}
        for target, value in (("load_config", self.config), ("make_forges", self.forges)):
            patcher = mock.patch(f"prtop.cli.{target}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_setting_prtop_cannot_read_ends_it_before_any_fetch(self):
        stderr = io.StringIO()
        refused = ConfigError("PRTOP_FORGES='svn': expected …")
        with mock.patch("prtop.cli.load_config", side_effect=refused), redirect_stderr(stderr):
            self.assertEqual(main([]), 2)
        self.assertIn("prtop: PRTOP_FORGES='svn'", stderr.getvalue())

    def test_output_that_is_not_a_terminal_gets_one_table_of_every_forge(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main([]), 0)
        self.assertIn("feat: on gitlab", out.getvalue())
        self.assertIn("feat: on github", out.getvalue())

    def test_two_tables_asked_for_seconds_apart_share_one_fetch(self):
        with redirect_stdout(io.StringIO()):
            main([])
            main([])
        self.assertEqual([forge.listings for forge in self.forges], [1, 1])

    def test_watch_checks_every_forge(self):
        with mock.patch("prtop.cli.watch", return_value=0) as watching:
            self.assertEqual(main(["watch"]), 0)
        config, forges, _, delay, cap = watching.call_args.args
        self.assertEqual((config, forges, delay, cap), (self.config, self.forges, 300, 7200))

    def test_in_a_terminal_the_live_view_names_every_forge_it_reads(self):
        with mock.patch("prtop.cli.show_live", return_value=0) as live_view, \
                mock.patch("sys.stdin", Terminal()), redirect_stdout(Terminal()):
            self.assertEqual(main(["-d", "10"]), 0)
        fetch, title, delay, max_iterations, on_report = live_view.call_args.args
        self.assertEqual((title, delay, max_iterations, on_report), (f"{HOST} + github.com", 10, None, None))
        self.assertIn("feat: on github", fetch().text)


class ModuleEntry(unittest.TestCase):
    def test_python_m_prtop_runs_the_command_and_exits_with_its_status(self):
        with mock.patch("prtop.cli.main", return_value=3), self.assertRaises(SystemExit) as caught:
            runpy.run_module("prtop", run_name="__main__")
        self.assertEqual(caught.exception.code, 3)


class LiveScreen(unittest.TestCase):
    def show(self, outcome, busy=False):
        exits = []
        run = mock.patch("prtop.cli.live.run", side_effect=[outcome])
        screen = mock.patch("prtop.cli.fullscreen", return_value=nullcontext())
        fetcher = mock.patch("prtop.cli.live.Fetcher")
        out, err = io.StringIO(), io.StringIO()
        with run, screen, fetcher as making, mock.patch("prtop.cli.os._exit", exits.append):
            making.return_value.busy = busy
            with redirect_stdout(out), redirect_stderr(err):
                code = show_live(lambda: None, HOST, 30, None, None)
        return code, out.getvalue(), err.getvalue(), exits

    def test_after_n_fetches_the_last_table_and_its_messages_stay_behind(self):
        view = View(HOST, report=Report([], False, False), messages=["prtop: web!1: 502"], finished=True)
        self.assertEqual(self.show(view)[:3], (0, NOTHING_OPEN + "\n", "prtop: web!1: 502\n"))

    def test_after_n_fetches_that_all_failed_only_the_errors_stay_behind(self):
        view = View(HOST, messages=["prtop: connection refused"], finished=True)
        self.assertEqual(self.show(view)[:3], (0, "", "prtop: connection refused\n"))

    def test_quitting_leaves_the_terminal_as_it_was(self):
        self.assertEqual(self.show(View(HOST, report=Report([], False, False)))[:3], (0, "", ""))

    def test_ctrl_c_quits_quietly(self):
        self.assertEqual(self.show(KeyboardInterrupt())[:3], (0, "", ""))

    def test_a_fetch_still_in_flight_does_not_hold_the_exit(self):
        self.assertEqual(self.show(View(HOST), busy=True)[3], [0])


class TabColor(unittest.TestCase):
    def printed(self, action_needed):
        out = io.StringIO()
        with redirect_stdout(out):
            tab_color_hook(Config(state_dir=Path("unused"), iterm_tab_color=True))(
                Report([], False, action_needed)
            )
        return out.getvalue()

    def test_the_tab_keeps_its_color_unless_asked(self):
        self.assertIsNone(tab_color_hook(Config(state_dir=Path("unused"))))

    def test_the_iterm_tab_turns_orange_while_an_mr_is_on_you(self):
        self.assertIn("\033]6;1;bg;red;brightness;255\a", self.printed(True))

    def test_the_iterm_tab_turns_green_when_nothing_is_on_you(self):
        self.assertIn("\033]6;1;bg;green;brightness;180\a", self.printed(False))


if __name__ == "__main__":
    unittest.main()

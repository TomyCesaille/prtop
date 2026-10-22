"""One check end to end, against a forge that answers from memory."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from prtop import model
from prtop.cli import batch, parse_args, run_check, tab_color_hook, watch
from prtop.config import Config
from prtop.model import DeployState, ForgeError
from prtop.report import NOTHING_OPEN, Report
from prtop.tasks import NoTasks, Task

HOST = "gitlab.example.com"


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
    host = HOST

    def __init__(self):
        self.open: dict = {}
        self.error: str | None = None
        self.merged: set[int] = set()
        self.states: dict[str, DeployState] = {}
        self.asked_about_merges: list[int] = []
        self.merge_check_error: str | None = None

    def open_merge_requests(self, tasks):
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
        self.state_file = Path(tmp.name) / "state.json"
        self.config = Config(state_file=self.state_file)
        self.forge = FakeForge()

    def check(self, config=None, tasks=None):
        return run_check(config or self.config, self.forge, tasks or NoTasks())

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
        config = Config(state_file=self.state_file, deploy_tracking=False)
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

    def test_a_deploy_awaiting_its_trigger_never_calls_for_action(self):
        self.forge.open = {"frontend!1": entry(1)}
        self.check()
        self.forge.open, self.forge.merged = {}, {1}
        self.forge.states["frontend!1"] = DeployState(model.AWAITING_TRIGGER)
        report = self.check()
        self.assertIn("awaiting prod trigger", report.text)
        self.assertFalse(report.action_needed)


class Watch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Config(state_file=Path(tmp.name) / "state.json")
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
            code = watch(self.config, self.forge, NoTasks(), 300, cap, sleep=self.sleep, clock=lambda: self.now)
        return code, out.getvalue()

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


class TabColor(unittest.TestCase):
    def printed(self, action_needed):
        out = io.StringIO()
        with redirect_stdout(out):
            tab_color_hook(Config(state_file=Path("unused"), iterm_tab_color=True))(
                Report([], False, action_needed)
            )
        return out.getvalue()

    def test_the_tab_keeps_its_color_unless_asked(self):
        self.assertIsNone(tab_color_hook(Config(state_file=Path("unused"))))

    def test_the_iterm_tab_turns_orange_while_an_mr_is_on_you(self):
        self.assertIn("\033]6;1;bg;red;brightness;255\a", self.printed(True))

    def test_the_iterm_tab_turns_green_when_nothing_is_on_you(self):
        self.assertIn("\033]6;1;bg;green;brightness;180\a", self.printed(False))


if __name__ == "__main__":
    unittest.main()

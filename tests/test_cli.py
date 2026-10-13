"""One check end to end, against a forge that answers from memory."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from prtop import model
from prtop.cli import run_check, set_tab_color
from prtop.config import Config
from prtop.model import DeployState, ForgeError

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

    def open_merge_requests(self):
        if self.error:
            raise ForgeError(self.error)
        return dict(self.open)

    def merged_deployment(self, entry):
        self.asked_about_merges.append(entry["iid"])
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

    def check(self, config=None):
        return run_check(config or self.config, self.forge)

    def saved(self):
        return json.loads(self.state_file.read_text())

    def test_with_nothing_open_the_report_says_so(self):
        self.assertIn("No open merge requests", self.check().text)

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

    def test_without_deploy_tracking_merges_are_not_followed(self):
        config = Config(state_file=self.state_file, deploy_tracking=False)
        self.forge.open = {"frontend!1": entry(1)}
        self.check(config)
        self.forge.open, self.forge.merged = {}, {1}
        self.check(config)
        self.assertEqual(self.forge.asked_about_merges, [])

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


class TabColor(unittest.TestCase):
    def written(self, mode, environ):
        out = io.StringIO()
        with redirect_stdout(out):
            set_tab_color(True, mode, environ)
        return out.getvalue()

    def test_auto_colors_the_tab_in_iterm(self):
        self.assertIn("\033]6;1;bg;red;brightness;255\a", self.written("auto", {"TERM_PROGRAM": "iTerm.app"}))

    def test_auto_writes_nothing_in_other_terminals(self):
        self.assertEqual(self.written("auto", {"TERM_PROGRAM": "Apple_Terminal"}), "")

    def test_off_writes_nothing_even_in_iterm(self):
        self.assertEqual(self.written("off", {"TERM_PROGRAM": "iTerm.app"}), "")


if __name__ == "__main__":
    unittest.main()

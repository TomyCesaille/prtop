"""How a merged change is followed to production, whichever forge reports it."""

import unittest
from pathlib import Path

from prtop import model
from prtop.config import Config
from prtop.forges.pipelines import ProdJobs, after_merge, classify_pending, judge_deployments
from prtop.model import DeployState, ForgeError

_defaults = Config(state_dir=Path("unused"))
PROD_JOBS = ProdJobs(_defaults.deploy_job_pattern, _defaults.primary_deploy_job_pattern)


def job(status, name="deploy_production", id=1):
    return {"name": name, "status": status, "id": id}


class PendingDeploys(unittest.TestCase):
    def classify(self, pipeline_status, *jobs):
        return classify_pending(pipeline_status, list(jobs), PROD_JOBS, "u")

    def test_a_running_pipeline_is_in_flight(self):
        self.assertEqual(self.classify("running"), DeployState(model.PIPELINE_RUNNING, "running"))

    def test_a_failed_pipeline_failed(self):
        self.assertEqual(self.classify("failed").kind, model.PIPELINE_FAILED)

    def test_a_project_without_a_prod_job_says_so(self):
        self.assertEqual(self.classify("success", job("success", "test")).kind, model.NO_PROD_JOB)

    def test_a_failed_production_job_failed(self):
        self.assertEqual(self.classify("success", job("failed")).kind, model.PROD_FAILED)

    def test_a_running_production_job_is_deploying(self):
        self.assertEqual(self.classify("success", job("running")).kind, model.PROD_DEPLOYING)

    def test_a_manual_production_job_awaits_its_trigger(self):
        self.assertEqual(self.classify("success", job("manual")), DeployState(model.AWAITING_TRIGGER, url="u"))

    def test_a_production_job_in_another_state_is_named(self):
        self.assertEqual(self.classify("success", job("canceled")), DeployState(model.PROD_OTHER, "canceled"))


class ProductionJobs(unittest.TestCase):
    def test_an_auxiliary_job_left_manual_does_not_hide_that_the_app_shipped(self):
        jobs = [job("success"), job("manual", "deploy_production_heavy_duty")]
        self.assertTrue(PROD_JOBS.shipped(jobs))

    def test_prod_test_and_migrate_jobs_are_not_deploys(self):
        jobs = [job("failed", "test_scenarios_production"), job("failed", "migrate_production")]
        self.assertEqual(PROD_JOBS.primary(jobs), [])

    def test_without_the_canonical_job_any_prod_deploy_job_decides(self):
        self.assertTrue(PROD_JOBS.shipped([job("success", "deploy-prod-eu")]))

    def test_a_job_named_as_the_primary_is_told_apart_from_the_fallback(self):
        self.assertTrue(PROD_JOBS.names_primary([job("manual"), job("success", "deploy_production_heavy_duty")]))
        self.assertFalse(PROD_JOBS.names_primary([job("success", "deploy-prod-eu")]))

    def test_the_job_to_act_on_is_the_one_waiting_to_be_started(self):
        jobs = [job("success", "deploy-prod-eu", id=1), job("manual", "deploy-prod-us", id=2)]
        self.assertEqual(PROD_JOBS.to_act_on(jobs)["id"], 2)

    def test_with_no_job_waiting_the_first_production_job_is_the_one_to_act_on(self):
        jobs = [job("failed", "deploy-prod-eu", id=1), job("running", "deploy-prod-us", id=2)]
        self.assertEqual(PROD_JOBS.to_act_on(jobs)["id"], 1)

    def test_without_a_production_job_there_is_none_to_act_on(self):
        self.assertIsNone(PROD_JOBS.to_act_on([job("manual", "test")]))


class AfterAMerge(unittest.TestCase):
    def test_a_pipeline_that_shows_up_late_is_waited_for(self):
        answers, sleeps = iter([None, None, "pipeline"]), []
        self.assertEqual(after_merge(lambda: next(answers), sleeps.append), "pipeline")
        self.assertEqual(sleeps, [2, 2])

    def test_it_gives_up_after_three_asks_without_a_last_wait(self):
        asks, sleeps = [], []
        self.assertIsNone(after_merge(lambda: asks.append(1), sleeps.append))
        self.assertEqual((len(asks), sleeps), (3, [2, 2]))


class Frontier(unittest.TestCase):
    """Deployments judged per branch, each against its branch's frontier."""

    def judge(self, deployments, frontier, evaluate=lambda dep, f: DeployState(model.SHIPPED, str(f))):
        return judge_deployments(deployments, lambda d: d["branch"], frontier, evaluate)

    def test_nothing_tracked_asks_nothing(self):
        self.assertEqual(self.judge({}, frontier=None), {})

    def test_each_deployment_is_judged_against_its_own_branchs_frontier(self):
        asked = []

        def frontier(branch):
            asked.append(branch)
            return f"top of {branch}"

        states = self.judge({"a!1": {"branch": "main"}, "a!2": {"branch": "main"}, "b!3": {"branch": "dev"}}, frontier)
        self.assertEqual(sorted(asked), ["dev", "main"])
        self.assertEqual(states["b!3"].detail, "top of dev")

    def test_a_frontier_the_forge_cannot_tell_leaves_each_deployment_to_its_own_pipeline(self):
        def unreachable(branch):
            raise ForgeError("502")

        self.assertEqual(self.judge({"a!1": {"branch": "main"}}, unreachable)["a!1"].detail, "None")

    def test_a_deployment_the_forge_cannot_judge_says_so_on_its_own_row(self):
        def evaluate(dep, frontier):
            raise ForgeError("timeout")

        states = self.judge({"a!1": {"branch": "main"}}, lambda b: 1, evaluate)
        self.assertEqual(states["a!1"], DeployState(model.CHECK_FAILED, "timeout"))


if __name__ == "__main__":
    unittest.main()

"""The GitLab forge, against canned API answers — no glab, no network."""

import unittest
from pathlib import Path

from prtop import model
from prtop.config import Config
from prtop.forges.gitlab import (
    REVIEW_STATUS,
    GitLab,
    ProdJobs,
    classify_pending,
    mr_short_id,
    project_path_from_summary,
    unresolved_threads_await_user,
)
from prtop.model import DeployState, ForgeError
from prtop.report import REVIEW_SIGNAL
from prtop.tasks import IssueTasks, NoTasks, task_source

HOST = "gitlab.example.com"
PROJECT = "acme/web/frontend"
ENCODED = "acme%2Fweb%2Ffrontend"
MR = f"projects/{ENCODED}/merge_requests/7"
MR_DETAIL = f"{MR}?include_diverged_commits_count=true"
ME = "john.doe"
QUERY = "scope=assigned_to_me&state=opened&non_archived=true&per_page=100"
LIST_ALL = f"merge_requests?{QUERY}"

_defaults = Config(state_file=Path("unused"))
PROD_JOBS = ProdJobs(_defaults.deploy_job_pattern, _defaults.primary_deploy_job_pattern)


class FakeGitLab(GitLab):
    """Serves `responses[path]`; a path it was not given fails like an outage."""

    def __init__(self, responses: dict, **settings):
        super().__init__(Config(state_file=Path("unused"), gitlab_host=HOST, **settings))
        self.responses = responses
        self.calls: list[str] = []

    def api(self, path):
        self.calls.append(path)
        if path not in self.responses:
            raise ForgeError(f"no answer for {path}")
        return self.responses[path]


def thread(*authors, resolved=False):
    """A resolvable discussion, written by `authors` in order."""
    return {"notes": [
        {"author": {"username": a}, "resolvable": True, "resolved": resolved}
        for a in authors
    ]}


def open_mr(detailed_status="not_approved", **fields):
    return {
        LIST_ALL: [{"iid": 7, "references": {"full": f"{PROJECT}!7"}}],
        MR_DETAIL: {"title": "feat: x", "detailed_merge_status": detailed_status,
             "web_url": f"https://{HOST}/{PROJECT}/-/merge_requests/7", **fields},
        f"{MR}/approvals": {"approved": False, "approved_by": []},
    }


def job(status, name="deploy_production", id=1):
    return {"name": name, "status": status, "id": id}


class OwnThreads(unittest.TestCase):
    def awaits(self, discussions):
        return unresolved_threads_await_user(discussions, ME)

    def test_a_thread_you_opened_and_nobody_answered_waits_on_no_reply(self):
        self.assertFalse(self.awaits([thread(ME)]))

    def test_a_reviewer_replying_in_your_thread_turns_it_into_feedback(self):
        self.assertTrue(self.awaits([thread(ME, "alice.martin")]))

    def test_a_thread_a_reviewer_opened_waits_on_your_reply(self):
        self.assertTrue(self.awaits([thread("bob.smith")]))

    def test_a_resolved_reviewer_thread_is_behind_you(self):
        self.assertFalse(self.awaits([thread("alice.martin", resolved=True)]))

    def test_one_reviewer_thread_among_your_own_is_enough(self):
        self.assertTrue(self.awaits([thread(ME), thread(ME), thread("carol.white")]))

    def test_a_plain_comment_is_not_a_thread_to_resolve(self):
        comment = {"individual_note": True, "notes": [
            {"author": {"username": "alice.martin"}, "resolvable": False},
        ]}
        self.assertFalse(self.awaits([comment]))

    def test_gitlabs_own_system_notes_never_make_a_thread_await_you(self):
        own = thread(ME)
        own["notes"].append({"author": {"username": "gitlab-bot"}, "system": True, "resolvable": False})
        self.assertFalse(self.awaits([own]))


class OpenMergeRequests(unittest.TestCase):
    def test_without_a_group_the_whole_instance_is_listed(self):
        forge = FakeGitLab({LIST_ALL: []})
        self.assertEqual(forge.open_merge_requests(NoTasks()), {})
        self.assertEqual(forge.calls, [LIST_ALL])

    def test_a_group_limits_the_listing_to_it_and_its_subgroups(self):
        listing = f"groups/acme%2Fweb/merge_requests?{QUERY}"
        forge = FakeGitLab({listing: []}, gitlab_group="acme/web")
        forge.open_merge_requests(NoTasks())
        self.assertEqual(forge.calls, [listing])

    def test_an_mr_is_keyed_by_repo_and_number_with_its_review_state(self):
        entry = FakeGitLab(open_mr()).open_merge_requests(NoTasks())["frontend!7"]
        self.assertEqual(entry["status"], model.AWAITING_REVIEW)
        self.assertEqual(entry["web_url"], f"https://{HOST}/{PROJECT}/-/merge_requests/7")

    def test_an_mr_in_review_that_fell_behind_its_target_needs_a_rebase(self):
        entry = FakeGitLab(open_mr(diverged_commits_count=1)).open_merge_requests(NoTasks())["frontend!7"]
        self.assertEqual(entry["status"], model.REBASE_NEEDED)

    def test_a_draft_behind_its_target_stays_a_draft(self):
        forge = FakeGitLab(open_mr("draft_status", draft=True, diverged_commits_count=3))
        entry = forge.open_merge_requests(NoTasks())["frontend!7"]
        self.assertEqual(entry["status"], model.DRAFT)

    def test_reviewer_feedback_outranks_a_branch_behind_its_target(self):
        forge = FakeGitLab(open_mr("requested_changes", diverged_commits_count=2))
        entry = forge.open_merge_requests(NoTasks())["frontend!7"]
        self.assertEqual(entry["status"], model.CHANGES_REQUESTED)

    def test_a_merge_status_gitlab_adds_later_passes_through_raw(self):
        entry = FakeGitLab(open_mr("some_status_from_the_future")).open_merge_requests(NoTasks())["frontend!7"]
        self.assertEqual(entry["status"], "some_status_from_the_future")

    def test_every_gitlab_status_maps_to_a_state_the_report_knows(self):
        self.assertLessEqual(set(REVIEW_STATUS.values()), set(REVIEW_SIGNAL) | {model.MERGEABLE})

    def test_threads_only_you_wrote_in_do_not_await_you(self):
        responses = open_mr("discussions_not_resolved")
        responses[f"{MR}/discussions?per_page=100&page=1"] = [thread(ME)]
        responses["user"] = {"username": ME}
        entry = FakeGitLab(responses).open_merge_requests(NoTasks())["frontend!7"]
        self.assertFalse(entry["threads_await_user"])

    def test_a_configured_username_is_not_asked_of_the_instance(self):
        responses = open_mr("discussions_not_resolved")
        responses[f"{MR}/discussions?per_page=100&page=1"] = [thread(ME)]
        forge = FakeGitLab(responses, gitlab_username=ME)
        forge.open_merge_requests(NoTasks())
        self.assertNotIn("user", forge.calls)

    def test_your_username_is_only_asked_for_when_threads_block_the_merge(self):
        forge = FakeGitLab(open_mr())
        forge.open_merge_requests(NoTasks())
        self.assertNotIn("user", forge.calls)

    def test_an_instance_that_does_not_answer_raises(self):
        with self.assertRaises(ForgeError):
            FakeGitLab({}).open_merge_requests(NoTasks())


class Tasks(unittest.TestCase):
    def test_an_mr_closing_an_issue_takes_it_as_its_task(self):
        responses = open_mr()
        responses[f"{MR}/closes_issues"] = [{"iid": 12, "title": "Login fails", "web_url": "https://x.example/12"}]
        entry = FakeGitLab(responses).open_merge_requests(IssueTasks())["frontend!7"]
        self.assertEqual((entry["task"], entry["task_url"]), ("#12 Login fails", "https://x.example/12"))

    def test_an_issue_from_an_external_tracker_goes_by_its_key(self):
        responses = open_mr()
        responses[f"{MR}/closes_issues"] = [{"id": "PROJ-9", "title": "Login fails"}]
        entry = FakeGitLab(responses).open_merge_requests(IssueTasks())["frontend!7"]
        self.assertEqual(entry["task"], "PROJ-9 Login fails")

    def test_a_branch_based_source_costs_no_issue_call(self):
        forge = FakeGitLab(open_mr(source_branch="PROJ-9-login"))
        entry = forge.open_merge_requests(task_source(Config(state_file=Path("u"), task_source="jira")))["frontend!7"]
        self.assertEqual(entry["task"], "PROJ-9 login")
        self.assertNotIn(f"{MR}/closes_issues", forge.calls)


class Merges(unittest.TestCase):
    def merged(self, **fields):
        return {MR: {"state": "merged", "target_branch": "develop", "merge_commit_sha": "abc", **fields}}

    def test_a_merged_mr_is_tracked_through_its_target_branch_pipeline(self):
        responses = self.merged()
        responses[f"projects/{ENCODED}/pipelines?ref=develop&per_page=10"] = [
            {"id": 8, "sha": "zzz"}, {"id": 9, "sha": "abc", "status": "running"},
        ]
        dep = FakeGitLab(responses).merged_deployment(
            {"project_path": PROJECT, "iid": 7, "title": "feat: x", "task": "t", "task_url": "tu", "web_url": "u"}
        )
        self.assertEqual((dep["ref"], dep["pipeline_id"], dep["title"]), ("develop", 9, "feat: x"))
        self.assertEqual((dep["task"], dep["task_url"]), ("t", "tu"))

    def test_an_mr_closed_without_merging_is_not_tracked(self):
        forge = FakeGitLab({MR: {"state": "closed"}})
        self.assertIsNone(forge.merged_deployment({"project_path": PROJECT, "iid": 7}))


class Deployments(unittest.TestCase):
    PIPELINES = f"projects/{ENCODED}/pipelines?ref=main&per_page=20"

    def dep(self, pipeline_id=10):
        return {"project_path": PROJECT, "iid": 7, "ref": "main", "pipeline_id": pipeline_id}

    def test_an_mr_carried_to_production_by_a_later_commit_has_shipped(self):
        forge = FakeGitLab({
            self.PIPELINES: [{"id": 12}],
            f"projects/{ENCODED}/pipelines/12/jobs?per_page=100": [job("success")],
        })
        self.assertEqual(forge.deploy_states({"frontend!7": self.dep(10)})["frontend!7"].kind, model.SHIPPED)

    def test_an_untriggered_production_job_links_to_the_branch_commits_page(self):
        dep = self.dep()
        forge = FakeGitLab({
            self.PIPELINES: [],
            f"projects/{ENCODED}/pipelines/10": {"status": "success"},
            f"projects/{ENCODED}/pipelines/10/jobs?per_page=100": [job("manual", id=77)],
        })
        state = forge.deploy_states({"frontend!7": dep})["frontend!7"]
        self.assertEqual(state.kind, model.AWAITING_TRIGGER)
        self.assertEqual(state.url, f"https://{HOST}/{PROJECT}/-/commits/main?ref_type=HEADS")
        self.assertEqual(dep["prod_job_id"], 77)

    def test_a_check_that_gets_no_answer_says_so_on_its_own_row(self):
        state = FakeGitLab({self.PIPELINES: []}).deploy_states({"frontend!7": self.dep()})["frontend!7"]
        self.assertEqual(state.kind, model.CHECK_FAILED)


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


class ProductionJobs(unittest.TestCase):
    def test_an_auxiliary_job_left_manual_does_not_hide_that_the_app_shipped(self):
        jobs = [job("success"), job("manual", "deploy_production_heavy_duty")]
        self.assertTrue(PROD_JOBS.shipped(jobs))

    def test_prod_test_and_migrate_jobs_are_not_deploys(self):
        jobs = [job("failed", "test_scenarios_production"), job("failed", "migrate_production")]
        self.assertEqual(PROD_JOBS.primary(jobs), [])

    def test_without_the_canonical_job_any_prod_deploy_job_decides(self):
        self.assertTrue(PROD_JOBS.shipped([job("success", "deploy-prod-eu")]))


class MrIds(unittest.TestCase):
    def test_the_table_names_the_mr_by_repo_and_number_alone(self):
        self.assertEqual(mr_short_id(PROJECT, 1904), "frontend!1904")

    def test_the_project_is_read_from_the_mr_reference(self):
        self.assertEqual(project_path_from_summary({"references": {"full": f"{PROJECT}!7"}}), PROJECT)

    def test_the_project_is_read_from_the_mr_url_without_a_reference(self):
        summary = {"web_url": f"https://{HOST}/{PROJECT}/-/merge_requests/7"}
        self.assertEqual(project_path_from_summary(summary), PROJECT)


if __name__ == "__main__":
    unittest.main()

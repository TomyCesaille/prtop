"""The GitHub forge, against canned API answers — no gh, no network."""

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from prtop import model
from prtop.config import Config
from prtop.forges.github import (
    CLOSING_ISSUES,
    MERGE_STATE,
    OPEN_PULL_REQUESTS,
    PULL_REQUESTS,
    GitHub,
    actions_status,
    review_state,
    unresolved_threads_await_user,
)
from prtop.model import ForgeError
from prtop.report import REVIEW_SIGNAL, fmt_status
from prtop.tasks import IssueTasks, NoTasks, task_source

ME = "octocat"
REPO = "acme/web"
KEY = f"{REPO}!7"
PULL = f"repos/{REPO}/pulls/7"


def compare(sha, deployed):
    return f"repos/{REPO}/compare/{sha}...{deployed}?per_page=1"


COMPARE = compare("abc", "def")


def pull_request(
    number=7, *, merge_state="BLOCKED", decision="REVIEW_REQUIRED", checks="SUCCESS", threads=(),
    more_threads=False, **fields
):
    """An open pull request as the listing query returns it. `threads` holds
    each review thread's resolution, and `more_threads` says the list runs past it."""
    return {
        "id": f"PR_{number}",
        "number": number,
        "title": f"feat: change {number}",
        "url": f"https://github.com/{REPO}/pull/{number}",
        "isDraft": False,
        "headRefName": "feature",
        "repository": {"nameWithOwner": REPO, "viewerPermission": "WRITE"},
        "reviewDecision": decision,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": merge_state,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]},
        "reviewThreads": {
            "pageInfo": {"hasNextPage": more_threads},
            "nodes": [{"isResolved": resolved} for resolved in threads],
        },
        "closingIssuesReferences": {"nodes": []},
        **fields,
    }


def thread(*authors, resolved=False, more_comments=False):
    """A review thread, commented on by `authors` in order."""
    return {
        "isResolved": resolved,
        "comments": {"pageInfo": {"hasNextPage": more_comments}, "nodes": [{"author": {"login": a}} for a in authors]},
    }


def page_of(items, variables, size=100):
    """One page of `items`, cursor-paginated the way GitHub's GraphQL connections are."""
    start = int(variables.get("after", 0))
    return {
        "pageInfo": {"hasNextPage": start + size < len(items), "endCursor": str(start + size)},
        "nodes": items[start:start + size],
    }


RECENT_RUNS = f"repos/{REPO}/actions/runs?branch=main&exclude_pull_requests=true&per_page=20"


def runs_for(sha, page=1):
    return f"repos/{REPO}/actions/runs?branch=main&exclude_pull_requests=true&head_sha={sha}&per_page=100&page={page}"


def run(id, sha="abc", status="completed", conclusion="success", attempt=1):
    return {"id": id, "head_sha": sha, "status": status, "conclusion": conclusion, "run_attempt": attempt}


def jobs_of(run_id, page=1):
    return f"repos/{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}"


def job(name="deploy-production", status="completed", conclusion="success", id=1):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion}


def listing(*runs):
    return {"workflow_runs": list(runs)}


def jobs(*items):
    return {"jobs": list(items)}


class FakeGitHub(GitHub):
    """Serves REST `rest[path]`, and the pull requests in `authored` and
    `assigned` to the queries; a path it was not given fails like an outage."""

    def __init__(
        self, rest=None, *, authored=(), assigned=(), threads=None, issues=None, hidden=(), now=None, **settings
    ):
        super().__init__(Config(state_dir=Path("unused"), **settings), now=now or (lambda: datetime.now(UTC)))
        self.rest = rest or {}
        self.authored, self.assigned = list(authored), list(assigned)
        self.threads = threads or {}
        self.issues = issues or {}
        # Pull requests the search finds but whose details come back empty.
        self.hidden = set(hidden)
        self.calls: list[str] = []
        self.queries: list[dict] = []

    def api(self, path):
        self.calls.append(path)
        if path not in self.rest:
            raise ForgeError(f"no answer for {path}")
        return self.rest[path]

    def query(self, document, **variables):
        self.queries.append(variables)
        if document == OPEN_PULL_REQUESTS:
            found = self.authored if variables["search"].endswith("author:@me") else self.assigned
            return {"viewer": {"login": ME}, "search": page_of([pr and {"id": pr["id"]} for pr in found], variables)}
        if document == PULL_REQUESTS:
            known = {pr["id"]: pr for pr in [*self.authored, *self.assigned] if pr and pr["id"] not in self.hidden}
            return {"nodes": [known.get(id) for id in variables["ids"]]}
        if document == CLOSING_ISSUES:
            issues = page_of(self.issues[variables["number"]], variables)
            return {"repository": {"pullRequest": {"closingIssuesReferences": issues}}}
        threads = page_of(self.threads[variables["number"]], variables)
        return {"repository": {"pullRequest": {"reviewThreads": threads}}}

    def asked(self, key):
        return [q for q in self.queries if key in q]


def state_of(pr, threads=()):
    """The review state and the Status cell the report shows for `pr`."""
    entry = FakeGitHub(authored=[pr], threads={pr["number"]: list(threads)}).open_merge_requests(NoTasks())
    (only,) = entry.values()
    return only["status"], fmt_status(
        only["status"], only["fully_approved"], only["has_conflicts"], only["threads_await_user"]
    )[1]


def never_asked():
    raise AssertionError("the threads were asked for although they decide nothing")


class ReviewStates(unittest.TestCase):
    def test_a_draft_is_a_draft_whatever_else_github_says(self):
        self.assertEqual(review_state(pull_request(isDraft=True, merge_state="DRAFT"), never_asked, ME)[0], model.DRAFT)

    def test_a_branch_that_conflicts_with_its_base_has_conflicts(self):
        self.assertEqual(state_of(pull_request(mergeable="CONFLICTING", merge_state="DIRTY")),
                         (model.CONFLICTS, "💥 conflicts, unapproved"))

    def test_conflicts_github_reports_only_through_the_merge_state_still_count(self):
        self.assertEqual(review_state(pull_request(merge_state="DIRTY"), never_asked, ME)[0], model.CONFLICTS)

    def test_requested_changes_wait_on_your_answer(self):
        pr = pull_request(decision="CHANGES_REQUESTED", merge_state="BEHIND")
        self.assertEqual(state_of(pr), (model.CHANGES_REQUESTED, "⛔ changes requested, unapproved"))

    def test_a_branch_behind_its_base_needs_a_rebase_even_in_review(self):
        self.assertEqual(review_state(pull_request(merge_state="BEHIND"), never_asked, ME)[0], model.REBASE_NEEDED)

    def test_an_approved_pull_request_github_would_merge_is_on_you(self):
        pr = pull_request(decision="APPROVED", merge_state="CLEAN")
        self.assertEqual(state_of(pr), (model.MERGEABLE, "✅ mergeable, approved"))

    def test_failing_checks_the_branch_does_not_require_leave_it_mergeable(self):
        pr = pull_request(decision="APPROVED", merge_state="UNSTABLE", checks="FAILURE")
        self.assertEqual(review_state(pr, never_asked, ME)[0], model.MERGEABLE)

    def test_a_repository_that_requires_no_review_counts_the_pull_request_approved(self):
        self.assertEqual(state_of(pull_request(decision=None, merge_state="HAS_HOOKS"))[1], "✅ mergeable, approved")

    def test_a_mergeable_pull_request_in_someone_elses_repository_waits_on_its_maintainers(self):
        for permission in ("READ", "TRIAGE", None):
            pr = pull_request(decision="APPROVED", merge_state="CLEAN")
            pr["repository"]["viewerPermission"] = permission
            with self.subTest(permission):
                self.assertEqual(state_of(pr), (model.AWAITING_REVIEW, "👀 awaiting review"))

    def test_a_repository_requiring_no_review_approves_nothing_for_who_cannot_merge(self):
        pr = pull_request(decision=None, merge_state="BLOCKED", checks="PENDING")
        pr["repository"]["viewerPermission"] = "READ"
        self.assertEqual(state_of(pr)[1], "⏳ CI running, unapproved")

    def test_unresolved_threads_do_not_hide_a_pull_request_github_would_merge(self):
        pr = pull_request(decision="APPROVED", merge_state="CLEAN", threads=[False])
        self.assertEqual(review_state(pr, never_asked, ME)[0], model.MERGEABLE)

    def test_a_merge_state_github_is_still_computing_is_a_pending_merge_check(self):
        self.assertEqual(review_state(pull_request(merge_state="UNKNOWN"), never_asked, ME)[0], model.MERGE_CHECK_PENDING)
        self.assertEqual(review_state(pull_request(merge_state=None), never_asked, ME)[0], model.MERGE_CHECK_PENDING)

    def test_a_merge_state_github_adds_later_passes_through_raw(self):
        self.assertEqual(review_state(pull_request(merge_state="QUEUED_SOMEHOW"), never_asked, ME)[0], "queued_somehow")

    def test_every_merge_state_maps_to_a_state_the_report_knows(self):
        self.assertLessEqual(set(MERGE_STATE.values()), set(REVIEW_SIGNAL) | {model.MERGEABLE})


class BlockedPullRequests(unittest.TestCase):
    """GitHub says a merge is blocked without saying by what."""

    def test_a_missing_review_leaves_it_with_the_reviewers(self):
        self.assertEqual(state_of(pull_request()), (model.AWAITING_REVIEW, "👀 awaiting review"))

    def test_a_reviewers_unresolved_thread_waits_on_your_answer(self):
        pr = pull_request(threads=[False, True])
        status, cell = state_of(pr, [thread(ME, "alice"), thread("bob", resolved=True)])
        self.assertEqual((status, cell), (model.UNRESOLVED_THREADS, "💬 unresolved threads, unapproved"))

    def test_threads_only_you_wrote_in_leave_it_with_the_reviewers(self):
        self.assertEqual(state_of(pull_request(threads=[False]), [thread(ME)])[1], "👀 awaiting review")

    def test_once_approved_your_own_threads_are_yours_to_resolve(self):
        pr = pull_request(decision="APPROVED", threads=[False])
        self.assertEqual(state_of(pr, [thread(ME)]), (model.UNRESOLVED_THREADS, "🧵 resolve own threads, approved"))

    def test_checks_still_running_are_on_the_machine(self):
        pr = pull_request(decision="APPROVED", checks="PENDING")
        self.assertEqual(state_of(pr), (model.CI_RUNNING, "⏳ CI running, approved"))

    def test_failing_required_checks_must_pass(self):
        pr = pull_request(decision="APPROVED", checks="ERROR")
        self.assertEqual(review_state(pr, never_asked, ME)[0], model.CI_MUST_PASS)

    def test_a_reviewers_thread_outranks_the_checks(self):
        pr = pull_request(decision="APPROVED", checks="FAILURE", threads=[False])
        self.assertEqual(review_state(pr, lambda: [thread("alice")], ME), (model.UNRESOLVED_THREADS, True))

    def test_a_block_with_nothing_to_explain_it_says_blocked(self):
        pr = pull_request(decision="APPROVED", checks=None)
        self.assertEqual(state_of(pr), (model.BLOCKED, "🚧 blocked, approved"))

    def test_an_unresolved_thread_past_the_first_hundred_still_counts(self):
        pr = pull_request(threads=[True] * 100, more_threads=True)
        everything = [thread("bob", resolved=True)] * 120 + [thread("alice")]
        self.assertEqual(state_of(pr, everything)[0], model.UNRESOLVED_THREADS)

    def test_a_hundred_resolved_threads_and_more_resolved_ones_leave_it_with_the_reviewers(self):
        pr = pull_request(threads=[True] * 100, more_threads=True)
        self.assertEqual(state_of(pr, [thread("bob", resolved=True)] * 130)[0], model.AWAITING_REVIEW)

    def test_without_unresolved_threads_nobody_is_asked_who_wrote_them(self):
        self.assertEqual(review_state(pull_request(threads=[True]), never_asked, ME)[0], model.AWAITING_REVIEW)


class ThreadAuthors(unittest.TestCase):
    def test_a_thread_you_opened_and_nobody_answered_waits_on_no_reply(self):
        self.assertFalse(unresolved_threads_await_user([thread(ME)], ME))

    def test_a_reviewer_replying_in_your_thread_turns_it_into_feedback(self):
        self.assertTrue(unresolved_threads_await_user([thread(ME, "alice")], ME))

    def test_a_resolved_reviewer_thread_is_behind_you(self):
        self.assertFalse(unresolved_threads_await_user([thread("alice", resolved=True)], ME))

    def test_a_thread_whose_comments_run_past_a_page_is_more_than_your_own_hint(self):
        self.assertTrue(unresolved_threads_await_user([thread(ME, more_comments=True)], ME))

    def test_a_comment_from_a_deleted_account_is_someone_elses(self):
        deleted = {"isResolved": False, "comments": {"nodes": [{"author": None}]}}
        self.assertTrue(unresolved_threads_await_user([deleted], ME))


class OpenPullRequests(unittest.TestCase):
    def test_a_pull_request_is_keyed_and_linked_like_a_merge_request(self):
        entry = FakeGitHub(authored=[pull_request()]).open_merge_requests(NoTasks())[KEY]
        self.assertEqual(entry["web_url"], f"https://github.com/{REPO}/pull/7")
        self.assertEqual((entry["repo"], entry["number"]), (REPO, 7))

    def test_the_pull_requests_you_opened_and_the_ones_assigned_to_you_are_listed(self):
        forge = FakeGitHub(authored=[pull_request(7)], assigned=[pull_request(8)])
        self.assertEqual(set(forge.open_merge_requests(NoTasks())), {KEY, f"{REPO}!8"})
        self.assertEqual({q["search"] for q in forge.asked("search")}, {
            "is:pr is:open archived:false author:@me",
            "is:pr is:open archived:false assignee:@me",
        })

    def test_a_pull_request_you_opened_and_are_assigned_to_is_read_once(self):
        forge = FakeGitHub(authored=[pull_request(7)], assigned=[pull_request(7)])
        self.assertEqual(list(forge.open_merge_requests(NoTasks())), [KEY])
        self.assertEqual(forge.asked("ids"), [{"ids": ["PR_7"]}])

    def test_a_long_list_is_read_in_batches_github_answers_in_time(self):
        forge = FakeGitHub(authored=[pull_request(n) for n in range(45)])
        self.assertEqual(len(forge.open_merge_requests(NoTasks())), 45)
        self.assertEqual(sorted(len(q["ids"]) for q in forge.asked("ids")), [5, 20, 20])

    def test_more_than_a_hundred_open_pull_requests_are_all_listed(self):
        forge = FakeGitHub(authored=[pull_request(n) for n in range(150)], assigned=[pull_request(150)])
        self.assertEqual(len(forge.open_merge_requests(NoTasks())), 151)
        self.assertEqual(sorted(q.get("after", "") for q in forge.asked("search")), ["", "", "100"])

    def test_an_owner_limits_both_searches_to_its_repositories(self):
        forge = FakeGitHub(github_owner="acme")
        forge.open_merge_requests(NoTasks())
        self.assertTrue(all(" user:acme " in q["search"] for q in forge.asked("search")))

    def test_with_nothing_open_nothing_more_is_asked(self):
        forge = FakeGitHub()
        self.assertEqual(forge.open_merge_requests(NoTasks()), {})
        self.assertEqual(len(forge.queries), 2)

    def test_a_pull_request_an_error_left_unreadable_is_skipped(self):
        forge = FakeGitHub(authored=[None, pull_request(7), pull_request(8)], hidden={"PR_8"})
        self.assertEqual(list(forge.open_merge_requests(NoTasks())), [KEY])

    def test_two_repositories_of_the_same_name_keep_both_their_pull_requests(self):
        labs = pull_request(7, repository={"nameWithOwner": "labs/web", "viewerPermission": "WRITE"})
        labs["id"] = "PR_labs_7"
        forge = FakeGitHub(authored=[pull_request(7), labs])
        self.assertEqual(set(forge.open_merge_requests(NoTasks())), {KEY, "labs/web!7"})

    def test_threads_are_asked_for_by_repository_and_number(self):
        forge = FakeGitHub(authored=[pull_request(threads=[False])], threads={7: [thread(ME)]})
        forge.open_merge_requests(NoTasks())
        self.assertEqual(forge.asked("owner"), [{"owner": "acme", "name": "web", "number": 7}])

    def test_an_issue_the_pull_request_closes_is_its_task(self):
        issue = {"number": 12, "title": "Login fails", "url": "https://github.com/acme/web/issues/12"}
        pr = pull_request(closingIssuesReferences={"nodes": [issue]})
        entry = FakeGitHub(authored=[pr]).open_merge_requests(IssueTasks())[KEY]
        self.assertEqual((entry["task"], entry["task_url"]), ("#12 Login fails", issue["url"]))

    def test_every_issue_the_pull_request_closes_is_counted_past_the_first_ten(self):
        issues = [{"number": n, "title": f"Issue {n}", "url": f"https://github.com/acme/web/issues/{n}"} for n in range(11)]
        pr = pull_request(closingIssuesReferences={"pageInfo": {"hasNextPage": True}, "nodes": issues[:10]})
        entry = FakeGitHub(authored=[pr], issues={7: issues}).open_merge_requests(IssueTasks())[KEY]
        self.assertEqual(entry["task"], "#0 Issue 0 (+10)")

    def test_a_branch_convention_reads_the_head_branch(self):
        tasks = task_source(Config(state_dir=Path("u"), task_source="jira"))
        entry = FakeGitHub(authored=[pull_request(headRefName="PROJ-9-login")]).open_merge_requests(tasks)[KEY]
        self.assertEqual((entry["task"], entry["task_url"]), ("PROJ-9 login", None))


MERGED = {"repo": REPO, "number": 7, "title": "feat: x", "task": "t", "task_url": "tu", "web_url": "u"}


class Merges(unittest.TestCase):
    def merged(self, **pull):
        merge = {"merged": True, "merge_commit_sha": "abc", "base": {"ref": "main"}, "merged_at": "2026-10-23T18:00:00Z"}
        return {PULL: {**merge, **pull}}

    def test_a_merged_pull_request_is_tracked_from_its_merge_commit(self):
        deployment = FakeGitHub(self.merged()).merged_deployment(MERGED)
        self.assertEqual(deployment, {**MERGED, "ref": "main", "sha": "abc", "merged_at": "2026-10-23T18:00:00Z"})

    def test_a_pull_request_closed_without_merging_is_not_tracked(self):
        self.assertIsNone(FakeGitHub({PULL: {"merged": False}}).merged_deployment(MERGED))

    def test_an_entry_that_names_no_pull_request_is_not_tracked(self):
        self.assertIsNone(FakeGitHub().merged_deployment({"title": "x"}))

    def test_a_merge_without_a_merge_commit_is_not_tracked(self):
        self.assertIsNone(FakeGitHub(self.merged(merge_commit_sha=None)).merged_deployment(MERGED))


class Deployments(unittest.TestCase):
    def dep(self):
        return {"repo": REPO, "number": 7, "ref": "main", "sha": "abc", "title": "feat: x"}

    def state(self, rest, forge=None):
        forge = forge or FakeGitHub(rest)
        return forge.deploy_states({KEY: self.dep()})[KEY]

    def frontier(self, sha="def"):
        return {RECENT_RUNS: listing(run(9, sha)), runs_for(sha): listing(run(9, sha)), jobs_of(9): jobs(job())}

    def own(self, *job_list, **run_fields):
        return {runs_for("abc"): listing(run(5, **run_fields)), jobs_of(5): jobs(*job_list)}

    def test_a_merge_carried_to_production_by_a_later_deploy_has_shipped(self):
        rest = {**self.frontier(), COMPARE: {"status": "ahead"}}
        self.assertEqual(self.state(rest).kind, model.SHIPPED)

    def test_a_merge_production_runs_exactly_has_shipped_without_a_comparison(self):
        forge = FakeGitHub(self.frontier("abc"))
        self.assertEqual(self.state({}, forge).kind, model.SHIPPED)
        self.assertNotIn(COMPARE, forge.calls)

    def test_a_merge_newer_than_production_is_judged_by_its_own_runs(self):
        rest = {**self.frontier(), COMPARE: {"status": "behind"},
                **self.own(job(status="waiting"), status="waiting", conclusion=None)}
        dep = self.dep()
        state = FakeGitHub(rest).deploy_states({KEY: dep})[KEY]
        self.assertEqual(state.kind, model.AWAITING_TRIGGER)
        self.assertEqual(state.url, f"https://github.com/{REPO}/actions/runs/5")
        self.assertEqual(dep["prod_run_id"], 5)

    def test_its_own_successful_deploy_ships_it_when_no_frontier_is_found(self):
        rest = {RECENT_RUNS: listing(), **self.own(job())}
        self.assertEqual(self.state(rest).kind, model.SHIPPED)

    def test_a_run_still_going_is_a_pipeline_in_flight(self):
        rest = {RECENT_RUNS: listing(), **self.own(job(status="in_progress"), status="in_progress")}
        self.assertEqual(self.state(rest), model.DeployState(model.PIPELINE_RUNNING, "running"))

    def test_a_failed_run_is_a_failed_pipeline(self):
        rest = {RECENT_RUNS: listing(), **self.own(job("test", conclusion="failure"), conclusion="failure")}
        self.assertEqual(self.state(rest).kind, model.PIPELINE_FAILED)

    def test_runs_without_a_production_job_end_the_tracking(self):
        rest = {RECENT_RUNS: listing(), **self.own(job("test"))}
        self.assertIn(self.state(rest).kind, model.FINISHED)

    def test_a_check_that_gets_no_answer_says_so_on_its_own_row(self):
        self.assertEqual(self.state({RECENT_RUNS: listing()}).kind, model.CHECK_FAILED)

    def test_a_finished_runs_jobs_and_a_comparison_are_asked_for_once(self):
        rest = {**self.frontier(), COMPARE: {"status": "diverged"}, **self.own(job("test"))}
        forge = FakeGitHub(rest)
        for _ in range(2):
            self.state({}, forge)
        self.assertEqual(forge.calls.count(jobs_of(9)), 1)
        self.assertEqual(forge.calls.count(COMPARE), 1)

    def test_a_running_runs_jobs_are_asked_for_again(self):
        rest = {RECENT_RUNS: listing(), **self.own(job(status="queued"), status="queued")}
        forge = FakeGitHub(rest)
        for _ in range(2):
            self.assertEqual(self.state({}, forge), model.DeployState(model.PIPELINE_RUNNING, "pending"))
        self.assertEqual(forge.calls.count(jobs_of(5)), 2)

    def test_the_frontier_is_the_newest_commit_that_shipped(self):
        rest = {
            RECENT_RUNS: listing(run(8, "old"), run(10, "new", status="in_progress"), run(9, "def")),
            runs_for("new"): listing(run(10, "new", status="in_progress")),
            jobs_of(10): jobs(job(status="in_progress")),
            runs_for("def"): listing(run(9, "def")),
            jobs_of(9): jobs(job()),
            compare("old", "def"): {"status": "ahead"},
            COMPARE: {"status": "identical"},
        }
        forge = FakeGitHub(rest)
        self.assertEqual(self.state({}, forge).kind, model.SHIPPED)
        self.assertNotIn(runs_for("old"), forge.calls)

    def test_a_late_run_of_an_older_deploy_does_not_hide_a_newer_one(self):
        rest = {
            RECENT_RUNS: listing(run(12, "old"), run(11, "def"), run(10, "old")),
            runs_for("old"): listing(run(12, "old"), run(10, "old")),
            jobs_of(12): jobs(job("lint")),
            jobs_of(10): jobs(job()),
            compare("def", "old"): {"status": "behind"},
            runs_for("def"): listing(run(11, "def")),
            jobs_of(11): jobs(job()),
            COMPARE: {"status": "ahead"},
        }
        self.assertEqual(self.state(rest).kind, model.SHIPPED)

    def test_an_auxiliary_deploy_in_another_workflow_does_not_pass_for_the_app_shipping(self):
        workflows = listing(run(5, status="waiting", conclusion=None), run(6))
        rest = {
            RECENT_RUNS: workflows,
            runs_for("abc"): workflows,
            jobs_of(5): jobs(job(status="waiting")),
            jobs_of(6): jobs(job("deploy-production-heavy-duty")),
        }
        self.assertEqual(self.state(rest).kind, model.AWAITING_TRIGGER)

    def test_a_production_job_past_the_first_hundred_jobs_is_found(self):
        tests = [job(f"test {n}", id=n) for n in range(100)]
        rest = {
            RECENT_RUNS: listing(),
            runs_for("abc"): listing(run(5)),
            jobs_of(5): jobs(*tests),
            jobs_of(5, page=2): jobs(job()),
        }
        self.assertEqual(self.state(rest).kind, model.SHIPPED)

    def test_a_listing_is_read_ten_pages_deep_at_most(self):
        full = jobs(*(job(f"test {n}", id=n) for n in range(100)))
        rest = {RECENT_RUNS: listing(), runs_for("abc"): listing(run(5)),
                **{jobs_of(5, page=page): full for page in range(1, 12)}}
        forge = FakeGitHub(rest)
        self.state({}, forge)
        self.assertIn(jobs_of(5, page=10), forge.calls)
        self.assertNotIn(jobs_of(5, page=11), forge.calls)

    def test_a_deploy_buried_under_a_hundred_scheduled_runs_is_found(self):
        scheduled = [run(100 + n) for n in range(100)]
        rest = {
            RECENT_RUNS: listing(),
            runs_for("abc"): listing(*scheduled),
            runs_for("abc", page=2): listing(run(5, status="waiting", conclusion=None)),
            jobs_of(5): jobs(job(status="waiting")),
            **{jobs_of(r["id"]): jobs(job("nightly checks")) for r in scheduled},
        }
        self.assertEqual(self.state(rest).kind, model.AWAITING_TRIGGER)

    def test_the_approval_link_leads_to_the_run_whose_production_job_waits(self):
        rest = {
            RECENT_RUNS: listing(),
            runs_for("abc"): listing(run(5), run(6, status="waiting", conclusion=None)),
            jobs_of(5): jobs(job("deploy-prod-eu")),
            jobs_of(6): jobs(job("deploy-prod-us", status="waiting", id=2)),
        }
        dep = self.dep()
        state = FakeGitHub(rest).deploy_states({KEY: dep})[KEY]
        self.assertEqual(state.url, f"https://github.com/{REPO}/actions/runs/6")
        self.assertEqual(dep["prod_run_id"], 6)

    def test_a_run_held_on_a_staging_approval_keeps_the_merge_tracked(self):
        held = run(5, status="waiting", conclusion=None)
        held["updated_at"] = "2026-10-23T18:00:00Z"
        rest = {RECENT_RUNS: listing(), runs_for("abc"): listing(held),
                jobs_of(5): jobs(job("deploy-staging", status="waiting"))}
        forge = FakeGitHub(rest, now=lambda: datetime(2026, 10, 24, 18, 0, tzinfo=UTC))
        self.assertEqual(self.state({}, forge), model.DeployState(model.PIPELINE_RUNNING, "waiting"))

    def test_a_merge_commit_whose_runs_have_not_shown_up_is_waited_for_a_while(self):
        dep = {**self.dep(), "merged_at": "2026-10-23T18:00:00Z"}
        at = datetime(2026, 10, 23, 18, 0, tzinfo=UTC)
        for minutes, kind in ((4, model.PIPELINE_RUNNING), (5, model.NO_PROD_JOB)):
            with self.subTest(minutes):
                forge = FakeGitHub({RECENT_RUNS: listing(), runs_for("abc"): listing()},
                                   now=lambda m=minutes: at + timedelta(minutes=m))
                self.assertEqual(forge.deploy_states({KEY: dict(dep)})[KEY].kind, kind)

    def test_a_deploy_started_by_another_workflow_is_waited_for_a_while(self):
        finished = run(5)
        finished["updated_at"] = "2026-10-23T18:00:00Z"
        rest = {RECENT_RUNS: listing(), runs_for("abc"): listing(finished), jobs_of(5): jobs(job("test"))}
        at = datetime(2026, 10, 23, 18, 0, tzinfo=UTC)
        for minutes, kind in ((4, model.PIPELINE_RUNNING), (5, model.NO_PROD_JOB)):
            with self.subTest(minutes):
                forge = FakeGitHub(rest, now=lambda m=minutes: at + timedelta(minutes=m))
                self.assertEqual(self.state({}, forge).kind, kind)


class ActionsStates(unittest.TestCase):
    def test_a_job_waiting_on_an_environments_reviewers_is_a_manual_job(self):
        self.assertEqual(actions_status({"status": "waiting"}), "manual")

    def test_a_cancelled_job_reads_as_canceled(self):
        self.assertEqual(actions_status({"status": "completed", "conclusion": "cancelled"}), "canceled")

    def test_a_state_github_adds_later_passes_through_raw(self):
        self.assertEqual(actions_status({"status": "completed", "conclusion": "neutral"}), "neutral")
        self.assertEqual(actions_status({"status": "deferred"}), "deferred")
        self.assertEqual(actions_status({}), "")


def gh(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TheGhCommand(unittest.TestCase):
    def setUp(self):
        self.forge = GitHub(Config(state_dir=Path("unused")))

    def answer(self, result):
        self.commands = []

        def run_tool(cmd, install_url):
            self.commands.append(cmd)
            return result

        return mock.patch("prtop.forges.github.run_tool", run_tool)

    def test_every_rest_call_names_github_com(self):
        with mock.patch("prtop.forges.github.get_json", return_value={"ok": 1}) as get_json:
            self.assertEqual(self.forge.api("user"), {"ok": 1})
        self.assertEqual(get_json.call_args.args[0], ["gh", "api", "--hostname", "github.com", "user"])

    def test_a_query_sends_numbers_as_numbers_and_the_rest_as_text(self):
        with self.answer(gh(json.dumps({"data": {"x": 1}}))):
            self.assertEqual(self.forge.query("{ x }", name="web", number=7), {"x": 1})
        self.assertEqual(self.commands[0], [
            "gh", "api", "--hostname", "github.com", "graphql",
            "-f", "query={ x }", "-f", "name=web", "-F", "number=7",
        ])

    def test_a_list_is_sent_one_item_at_a_time(self):
        with self.answer(gh(json.dumps({"data": {"nodes": []}}))):
            self.forge.query("{ x }", ids=["PR_1", "PR_2"])
        self.assertEqual(self.commands[0][-4:], ["-f", "ids[]=PR_1", "-f", "ids[]=PR_2"])

    def test_errors_that_come_with_data_are_reported_and_the_data_kept(self):
        payload = {"data": {"x": 1}, "errors": [{"message": "Resource protected by SAML enforcement"}]}
        stderr = io.StringIO()
        with self.answer(gh(json.dumps(payload), returncode=1)), redirect_stderr(stderr):
            self.assertEqual(self.forge.query("{ x }"), {"x": 1})
        self.assertEqual(stderr.getvalue(), "prtop: github.com: Resource protected by SAML enforcement\n")

    def test_a_query_that_returns_no_data_fails_with_ghs_message(self):
        refused = gh(json.dumps({"errors": [{"message": "x"}]}), 1, "gh: Bad credentials")
        with self.answer(refused), self.assertRaisesRegex(ForgeError, "Bad credentials"):
            self.forge.query("{ x }")

    def test_a_query_that_prints_no_json_fails(self):
        offline = gh("", 1, "error connecting to api.github.com")
        with self.answer(offline), self.assertRaisesRegex(ForgeError, "error connecting"):
            self.forge.query("{ x }")


if __name__ == "__main__":
    unittest.main()

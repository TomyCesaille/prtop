#!/usr/bin/env python3
"""Status-signal and table-rendering tests: `python3 test_status.py`.

Pure functions only — nothing here touches GitLab, the state file, or the
network. The accessibility contract in AGENTS.md is what these pin down.
"""

import unittest

from monitor_prs import (
    ACT,
    ANSWER,
    HEADERS,
    MIN_WIDTHS,
    PEOPLE,
    PIPELINE_INFLIGHT,
    PROD_DEPLOYING,
    RUNNING,
    STATUS_WIDTH,
    Row,
    _MARK_ORDER,
    _MERGE_STATUS,
    _by_severity,
    _display_width,
    _hyperlink,
    _render_table,
    _split_by_mark,
    extract_task_name,
    fmt_status,
    open_mr_rows,
    pending_deploy_status,
    status_signal,
    unresolved_threads_await_user,
)


def row(mark, *, pr="`p!1`", status="s", title="title", task="task"):
    return Row(mark=mark, title=title, task=task, pr=pr, status=status)


WIDEST_STATUS = "🔍 merge check running, unapproved"

PIPELINE_STATES = PIPELINE_INFLIGHT | {"failed", "success", "canceled", "skipped", "manual"}
JOB_STATES = PROD_DEPLOYING | {"failed", "success", "canceled", "skipped", "manual", "created"}


def all_status_cells():
    """Every Status cell the open and awaiting-deploy tables can produce, bar the
    ones carrying free text (an unmapped merge status, a `check failed:` error)."""
    for merge_status in (*_MERGE_STATUS, "mergeable"):
        for approved in (False, True):
            yield fmt_status(merge_status, approved, False)[1]
    for pipeline_status in PIPELINE_STATES:
        yield pending_deploy_status(pipeline_status, [], {"project_path": "p"})[1]
        for job_status in JOB_STATES:
            jobs = [{"name": "deploy_production", "status": job_status, "id": 1}]
            yield pending_deploy_status(pipeline_status, jobs, {"project_path": "p"})[1]


def mr(merge_status="not_approved", *, approved=False, conflicts=False, title="t", task="task",
       threads_await_user=True):
    return {
        "merge_status": merge_status,
        "fully_approved": approved,
        "has_conflicts": conflicts,
        "threads_await_user": threads_await_user,
        "title": title,
        "task_name": task,
    }


def thread(*authors, resolved=False):
    """A resolvable discussion, written by `authors` in order."""
    return {"notes": [
        {"author": {"username": a}, "resolvable": True, "resolved": resolved}
        for a in authors
    ]}


class SeverityMarks(unittest.TestCase):
    def test_a_fully_approved_mergeable_mr_is_on_the_user(self):
        self.assertEqual(fmt_status("mergeable", True, False)[0], ACT)

    def test_a_mergeable_mr_still_awaiting_approvals_is_on_the_reviewers(self):
        self.assertEqual(fmt_status("mergeable", False, False)[0], PEOPLE)

    def test_a_rebase_is_on_the_user(self):
        self.assertEqual(fmt_status("need_rebase", False, False)[0], ACT)

    def test_review_feedback_asks_the_user_for_an_answer(self):
        self.assertEqual(fmt_status("requested_changes", False, False)[0], ANSWER)
        self.assertEqual(fmt_status("discussions_not_resolved", False, False)[0], ANSWER)

    def test_a_running_pipeline_resolves_without_anyone(self):
        self.assertEqual(fmt_status("ci_still_running", False, False)[0], RUNNING)
        self.assertEqual(fmt_status("checking", False, False)[0], RUNNING)

    def test_a_merge_status_gitlab_adds_later_surfaces_instead_of_sinking(self):
        mark, cell = fmt_status("some_status_from_the_future", False, False)
        self.assertEqual(mark, ANSWER)
        self.assertIn("some_status_from_the_future", cell)

    def test_conflicts_outrank_whatever_review_state_gitlab_reports(self):
        # GitLab sets has_conflicts independently of detailed_merge_status.
        mark, cell = fmt_status("not_approved", False, True)
        self.assertEqual(mark, ACT)
        self.assertIn("conflicts", cell)

    def test_a_state_that_already_names_the_approval_skips_the_suffix(self):
        self.assertEqual(fmt_status("draft_status", False, False)[1], "📝 draft")
        self.assertEqual(fmt_status("not_approved", False, False)[1], "👀 awaiting review")

    def test_every_other_state_spells_out_the_approval_beside_it(self):
        self.assertEqual(fmt_status("mergeable", True, False)[1], "✅ mergeable, approved")
        self.assertEqual(fmt_status("ci_still_running", False, False)[1], "⏳ CI running, unapproved")


class OwnThreads(unittest.TestCase):
    """A thread the user opened on his own MR is a hint for the reviewer. GitLab
    blocks the merge on it all the same, so it stays in the report — but not as
    feedback waiting on him."""

    def test_a_thread_only_the_user_wrote_in_leaves_the_mr_with_the_reviewers(self):
        self.assertEqual(
            fmt_status("discussions_not_resolved", False, False, False),
            (PEOPLE, "👀 awaiting review"),
        )

    def test_his_own_threads_are_his_to_clear_once_the_mr_is_approved(self):
        mark, cell = fmt_status("discussions_not_resolved", True, False, False)
        self.assertEqual(mark, ACT)
        self.assertIn("resolve own threads", cell)

    def test_a_note_from_anyone_else_makes_the_thread_feedback_to_answer(self):
        mark, cell = fmt_status("discussions_not_resolved", False, False, True)
        self.assertEqual(mark, ANSWER)
        self.assertIn("unresolved threads", cell)

    def test_conflicts_outrank_his_own_threads_too(self):
        self.assertEqual(fmt_status("discussions_not_resolved", False, True, False)[0], ACT)

    def test_a_thread_the_user_opened_and_nobody_answered_waits_on_no_reply(self):
        self.assertFalse(unresolved_threads_await_user([thread("jdoe")]))

    def test_a_reviewer_replying_in_his_thread_turns_it_into_feedback(self):
        self.assertTrue(unresolved_threads_await_user([thread("jdoe", "alice42")]))

    def test_a_thread_a_reviewer_opened_waits_on_his_reply(self):
        self.assertTrue(unresolved_threads_await_user([thread("bobsmith")]))

    def test_a_resolved_reviewer_thread_is_behind_him(self):
        self.assertFalse(unresolved_threads_await_user([thread("alice42", resolved=True)]))

    def test_one_reviewer_thread_among_his_own_is_enough(self):
        discussions = [thread("jdoe"), thread("jdoe"), thread("carol7")]
        self.assertTrue(unresolved_threads_await_user(discussions))

    def test_a_plain_comment_is_not_a_thread_to_resolve(self):
        comment = {"individual_note": True, "notes": [
            {"author": {"username": "alice42"}, "resolvable": False},
        ]}
        self.assertFalse(unresolved_threads_await_user([comment]))

    def test_gitlabs_own_system_notes_never_make_a_thread_await_him(self):
        own = thread("jdoe")
        own["notes"].append(
            {"author": {"username": "gitlab-bot"}, "system": True, "resolvable": False}
        )
        self.assertFalse(unresolved_threads_await_user([own]))


class TaskNames(unittest.TestCase):
    HASH = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"

    def test_a_task_branch_reads_as_the_notion_task_name(self):
        self.assertEqual(
            extract_task_name(f"dev/1234-Add-a-dark-mode-toggle-{self.HASH}"),
            "1234 Add a dark mode toggle",
        )

    def test_the_mrs_of_a_task_split_across_a_stack_share_one_task_name(self):
        stem = f"dev/1234-Add-a-dark-mode-toggle-{self.HASH}"
        self.assertEqual(
            extract_task_name(f"{stem}-mr1-theme-tokens"),
            extract_task_name(f"{stem}-mr2-toggle-ui"),
        )
        self.assertEqual(
            extract_task_name(f"{stem}-mr1-theme-tokens"),
            extract_task_name(stem),
        )

    def test_a_branch_outside_the_task_convention_has_no_task(self):
        self.assertEqual(extract_task_name("dev/quick-fix"), "")
        self.assertEqual(extract_task_name("main"), "")
        self.assertEqual(extract_task_name(None), "")


class DeploySignals(unittest.TestCase):
    def dep(self):
        return {"project_path": "acme/common/frontend"}

    def test_an_untriggered_production_job_is_on_the_user(self):
        jobs = [{"name": "deploy_production", "status": "manual", "id": 7}]
        mark, cell = pending_deploy_status("success", jobs, self.dep())
        self.assertEqual(mark, ACT)
        self.assertIn("awaiting prod trigger", cell)

    def test_a_failed_production_deploy_is_on_the_user(self):
        jobs = [{"name": "deploy_production", "status": "failed", "id": 7}]
        self.assertEqual(pending_deploy_status("success", jobs, self.dep())[0], ACT)

    def test_a_deploy_in_flight_resolves_without_anyone(self):
        jobs = [{"name": "deploy_production", "status": "running", "id": 7}]
        self.assertEqual(pending_deploy_status("success", jobs, self.dep())[0], RUNNING)
        self.assertEqual(pending_deploy_status("running", [], self.dep())[0], RUNNING)

    def test_the_awaiting_trigger_cell_links_to_the_projects_main_commits_page(self):
        jobs = [{"name": "deploy_production", "status": "manual", "id": 7}]
        cell = pending_deploy_status("success", jobs, self.dep())[1]
        self.assertIn("https://gitlab.com/acme/common/frontend/-/commits/main", cell)

    def test_the_production_job_id_is_recorded_for_the_skill_to_trigger(self):
        dep = self.dep()
        pending_deploy_status("success", [{"name": "deploy_production", "status": "manual", "id": 7}], dep)
        self.assertEqual(dep["prod_job_id"], 7)


class AccessibilityContract(unittest.TestCase):
    """Every distinction must survive losing color, glyph fidelity, and detail."""

    def test_every_mark_is_a_single_ascii_character(self):
        for mark in _MARK_ORDER:
            self.assertEqual(len(mark), 1)
            self.assertTrue(mark.isascii(), mark)

    def test_every_mark_a_state_can_produce_has_a_sort_rank(self):
        # An unranked mark falls to the sort fallback and lands below `.`.
        produced = {status_signal(s, a)[0] for s in _MERGE_STATUS for a in (True, False)}
        produced |= {status_signal("mergeable", a)[0] for a in (True, False)}
        produced.add(status_signal("a_status_gitlab_adds_later", False)[0])
        self.assertLessEqual(produced, set(_MARK_ORDER))

    def test_no_state_is_reported_by_glyph_alone(self):
        for status, (_, _, label) in _MERGE_STATUS.items():
            self.assertTrue(label, f"{status} has no text label beside its glyph")
            self.assertIn(label, fmt_status(status, False, False)[1])

    def test_an_unrecognised_status_is_labelled_with_its_raw_enum(self):
        self.assertIn("a_status_gitlab_adds_later",
                      fmt_status("a_status_gitlab_adds_later", False, False)[1])

    def test_a_glyph_never_carries_two_meanings_in_the_status_column(self):
        # ⏳ is the one deliberate share: a pipeline in flight is a single state
        # whether the MR is open or already merged.
        by_glyph: dict[str, set[str]] = {}
        for status, (_, glyph, _label) in _MERGE_STATUS.items():
            by_glyph.setdefault(glyph, set()).add(status)
        deploy_cases = [
            ("running", []),
            ("failed", []),
            ("success", [{"name": "deploy_production", "status": "running", "id": 1}]),
            ("success", [{"name": "deploy_production", "status": "manual", "id": 1}]),
        ]
        for pipeline_status, jobs in deploy_cases:
            cell = pending_deploy_status(pipeline_status, jobs, {"project_path": "g/p"})[1]
            glyph = [c for c in cell if not c.isascii() and c not in "…—➖"]
            if glyph:
                by_glyph.setdefault(glyph[0], set()).add(f"deploy:{pipeline_status}")

        shared = {g: s for g, s in by_glyph.items() if len(s) > 1}
        expected = {
            "⏳": {"ci_still_running", "ci_must_pass", "deploy:running"},
            "🔍": {"checking", "unchecked"},
            "👀": {"not_approved"},
        }
        for glyph, states in shared.items():
            self.assertEqual(states, expected.get(glyph, set()), f"{glyph} means too many things")

    def test_a_glyph_is_never_the_only_difference_between_two_marks(self):
        marks = {status_signal(s, False)[0] for s in _MERGE_STATUS}
        self.assertGreater(len(marks), 1)


class Ordering(unittest.TestCase):
    def rows(self, *marks):
        return [row(m, pr=f"`p!{i}`") for i, m in enumerate(marks)]

    def test_rows_needing_the_user_sort_above_rows_that_do_not(self):
        ordered = _by_severity(self.rows(RUNNING, PEOPLE, ANSWER, ACT))
        self.assertEqual([r.mark for r in ordered], [ACT, ANSWER, PEOPLE, RUNNING])

    def test_the_mrs_of_one_task_stay_together_within_a_severity(self):
        rows = [
            row(ACT, task="beta", pr="`frontend!1`"),
            row(ACT, task="alpha", pr="`worker!2`"),
            row(ACT, task="alpha", pr="`frontend!3`"),
        ]
        self.assertEqual(
            [(r.task, r.pr) for r in _by_severity(rows)],
            [("alpha", "`frontend!3`"), ("alpha", "`worker!2`"), ("beta", "`frontend!1`")],
        )

    def test_the_same_repo_orders_by_mr_number_not_by_digits(self):
        rows = [row(ACT, pr="`frontend!999`"), row(ACT, pr="`frontend!1817`")]
        self.assertEqual([r.pr for r in _by_severity(rows)], ["`frontend!999`", "`frontend!1817`"])

    def test_each_severity_block_is_ruled_off_from_the_next(self):
        rows = _split_by_mark(self.rows(ACT, ACT, ANSWER, RUNNING))
        self.assertEqual(
            [r.mark if r is not None else "-" for r in rows],
            [ACT, ACT, "-", ANSWER, "-", RUNNING],
        )

    def test_a_single_severity_block_gets_no_rule(self):
        rows = _split_by_mark(self.rows(PEOPLE, PEOPLE))
        self.assertNotIn(None, rows)

    def test_no_rule_leads_a_block_so_the_two_groups_stay_split_by_one(self):
        self.assertIsNotNone(_split_by_mark(self.rows(ACT, PEOPLE))[0])

    def test_the_open_table_leads_with_whatever_is_on_the_user(self):
        rows = open_mr_rows({
            "p!1": mr("draft_status"),
            "p!2": mr("mergeable", approved=True),
            "p!3": mr("requested_changes"),
        })
        self.assertEqual([r.mark for r in rows], [ACT, ANSWER, PEOPLE])
        self.assertEqual(rows[0].pr, "`p!2`")


class TableRendering(unittest.TestCase):
    def test_emoji_bearing_rows_keep_the_right_border_aligned(self):
        rows = [
            row(ACT, status="✅ mergeable, approved"),
            row(RUNNING, pr="`p!2`", status="⏳ CI running, unapproved"),
        ]
        widths = {_display_width(line) for line in _render_table(HEADERS, rows).splitlines()}
        self.assertEqual(len(widths), 1, "table lines differ in rendered width")

    def test_a_hyperlinked_cell_occupies_only_its_visible_width(self):
        plain = row(ACT, status="🕹️ awaiting prod trigger")
        linked = plain._replace(status=_hyperlink("🕹️ awaiting prod trigger", "https://x.example/y"))
        self.assertEqual(
            _render_table(HEADERS, [plain]).splitlines()[-1],
            _render_table(HEADERS, [linked]).splitlines()[-1].replace(
                _hyperlink("🕹️ awaiting prod trigger", "https://x.example/y"),
                "🕹️ awaiting prod trigger",
            ),
        )

    def test_the_status_column_keeps_one_width_as_mrs_change_state(self):
        def rendered_width(status):
            table = _render_table(HEADERS, [row(ACT, status=status)], MIN_WIDTHS)
            return _display_width(table.splitlines()[0])

        self.assertEqual(rendered_width("✅ mergeable"), rendered_width(WIDEST_STATUS))

    def test_the_status_column_is_sized_to_the_widest_status_it_can_show(self):
        widest = max(all_status_cells(), key=_display_width)
        self.assertEqual(_display_width(widest), STATUS_WIDTH, f"widest cell is now {widest!r}")

    def test_none_renders_as_the_rule_between_the_two_row_groups(self):
        rows = [row(ACT), None, row(RUNNING, pr="`p!2`")]
        lines = _render_table(HEADERS, rows).splitlines()
        self.assertEqual(lines[1], lines[3])

    def test_the_mark_column_header_is_blank(self):
        self.assertEqual(HEADERS[0], "")
        self.assertEqual(len(HEADERS), len(Row._fields))

    def test_the_pr_title_column_comes_before_the_task_column(self):
        self.assertEqual(HEADERS[1:3], ("PR title", "Task"))
        self.assertEqual(Row._fields[1:3], ("title", "task"))

    def test_each_cell_lands_under_its_own_header(self):
        line = _render_table(HEADERS, [row(ACT, title="THE-TITLE", task="THE-TASK")]).splitlines()[-1]
        self.assertLess(line.index("THE-TITLE"), line.index("THE-TASK"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

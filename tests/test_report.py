"""Severity marks, status cells and table rendering — the accessibility contract."""

import unicodedata
import unittest
from pathlib import Path

from prtop import model
from prtop.config import Config
from prtop.forges.pipelines import PIPELINE_INFLIGHT, PROD_DEPLOYING, ProdJobs, classify_pending
from prtop.model import DeployState
from prtop.report import (
    ACT,
    ANSWER,
    DEPLOY_SIGNAL,
    HEADERS,
    MARK_ORDER,
    PEOPLE,
    REVIEW_SIGNAL,
    RUNNING,
    STATUS_WIDTH,
    TITLE_MAX,
    TITLE_MAX_ALONE,
    Row,
    by_severity,
    clip,
    deploy_signal,
    display_width,
    fmt_status,
    hyperlink,
    linked_mr_id,
    open_row,
    plain,
    render_report,
    split_by_mark,
    status_signal,
    unreachable_row,
)

HOST = "gitlab.example.com"
WIDEST_STATUS = "🔍 merge check running, unapproved"

_defaults = Config(state_dir=Path("unused"))
PROD_JOBS = ProdJobs(_defaults.deploy_job_pattern, _defaults.primary_deploy_job_pattern)
PIPELINE_STATES = PIPELINE_INFLIGHT | {"failed", "success", "canceled", "skipped", "manual"}
JOB_STATES = PROD_DEPLOYING | {"failed", "success", "canceled", "skipped", "manual", "created"}


def row(mark, *, pr="`p!1`", status="s", title="title", task="task"):
    return Row(mark=mark, title=title, task=task, pr=pr, status=status)


def mr(status=model.AWAITING_REVIEW, *, approved=False, conflicts=False, title="t", task="task",
       threads_await_user=True):
    return {
        "status": status,
        "fully_approved": approved,
        "has_conflicts": conflicts,
        "threads_await_user": threads_await_user,
        "title": title,
        "task": task,
    }


def all_status_cells():
    """Every Status cell the open and awaiting-deploy rows can produce, bar the
    ones carrying free text (an unmapped status, a `check failed:` error)."""
    yield unreachable_row(HOST).status
    for status in (*REVIEW_SIGNAL, model.MERGEABLE):
        for approved in (False, True):
            yield fmt_status(status, approved, False)[1]
    for pipeline_status in PIPELINE_STATES:
        job_lists = [[]] + [[{"name": "deploy_production", "status": s, "id": 1}] for s in JOB_STATES]
        for jobs in job_lists:
            state = classify_pending(pipeline_status, jobs, PROD_JOBS, "u")
            if state.kind not in model.FINISHED:
                yield deploy_signal(state)[1]


class SeverityMarks(unittest.TestCase):
    def test_a_fully_approved_mergeable_mr_is_on_you(self):
        self.assertEqual(fmt_status(model.MERGEABLE, True, False)[0], ACT)

    def test_a_mergeable_mr_still_awaiting_approvals_is_on_the_reviewers(self):
        self.assertEqual(fmt_status(model.MERGEABLE, False, False)[0], PEOPLE)

    def test_a_rebase_is_on_you(self):
        self.assertEqual(fmt_status(model.REBASE_NEEDED, False, False)[0], ACT)

    def test_review_feedback_asks_you_for_an_answer(self):
        self.assertEqual(fmt_status(model.CHANGES_REQUESTED, False, False)[0], ANSWER)
        self.assertEqual(fmt_status(model.UNRESOLVED_THREADS, False, False)[0], ANSWER)

    def test_a_running_pipeline_resolves_without_anyone(self):
        self.assertEqual(fmt_status(model.CI_RUNNING, False, False)[0], RUNNING)
        self.assertEqual(fmt_status(model.MERGE_CHECK_RUNNING, False, False)[0], RUNNING)

    def test_a_status_the_forge_adds_later_surfaces_instead_of_sinking(self):
        mark, cell = fmt_status("some_status_from_the_future", False, False)
        self.assertEqual(mark, ANSWER)
        self.assertIn("some_status_from_the_future", cell)

    def test_conflicts_outrank_whatever_review_state_the_forge_reports(self):
        mark, cell = fmt_status(model.AWAITING_REVIEW, False, True)
        self.assertEqual(mark, ACT)
        self.assertIn("conflicts", cell)

    def test_a_state_that_already_names_the_approval_skips_the_suffix(self):
        self.assertEqual(fmt_status(model.DRAFT, False, False)[1], "📝 draft")
        self.assertEqual(fmt_status(model.AWAITING_REVIEW, False, False)[1], "👀 awaiting review")

    def test_every_other_state_spells_out_the_approval_beside_it(self):
        self.assertEqual(fmt_status(model.MERGEABLE, True, False)[1], "✅ mergeable, approved")
        self.assertEqual(fmt_status(model.CI_RUNNING, False, False)[1], "⏳ CI running, unapproved")


class OwnThreads(unittest.TestCase):
    """A thread you opened on your own MR is a hint for the reviewer. The forge
    blocks the merge on it all the same, so it stays in the report — but not as
    feedback waiting on you."""

    def test_a_thread_only_you_wrote_in_leaves_the_mr_with_the_reviewers(self):
        self.assertEqual(
            fmt_status(model.UNRESOLVED_THREADS, False, False, False),
            (PEOPLE, "👀 awaiting review"),
        )

    def test_your_own_threads_are_yours_to_clear_once_the_mr_is_approved(self):
        mark, cell = fmt_status(model.UNRESOLVED_THREADS, True, False, False)
        self.assertEqual(mark, ACT)
        self.assertIn("resolve own threads", cell)

    def test_a_note_from_anyone_else_makes_the_thread_feedback_to_answer(self):
        mark, cell = fmt_status(model.UNRESOLVED_THREADS, False, False, True)
        self.assertEqual(mark, ANSWER)
        self.assertIn("unresolved threads", cell)

    def test_conflicts_outrank_your_own_threads_too(self):
        self.assertEqual(fmt_status(model.UNRESOLVED_THREADS, False, True, False)[0], ACT)


class DeploySignals(unittest.TestCase):
    def test_an_untriggered_production_job_is_on_you(self):
        mark, cell = deploy_signal(DeployState(model.AWAITING_TRIGGER, url="https://x.example/c"))
        self.assertEqual(mark, ACT)
        self.assertIn("awaiting prod trigger", cell)

    def test_the_awaiting_trigger_cell_links_to_where_the_job_is_triggered(self):
        cell = deploy_signal(DeployState(model.AWAITING_TRIGGER, url="https://x.example/c"))[1]
        self.assertEqual(cell, hyperlink("🚀 awaiting prod trigger", "https://x.example/c"))

    def test_a_failed_production_deploy_is_on_you(self):
        self.assertEqual(deploy_signal(DeployState(model.PROD_FAILED))[0], ACT)
        self.assertEqual(deploy_signal(DeployState(model.PIPELINE_FAILED))[0], ACT)

    def test_a_deploy_in_flight_resolves_without_anyone(self):
        self.assertEqual(deploy_signal(DeployState(model.PROD_DEPLOYING))[0], RUNNING)
        self.assertEqual(deploy_signal(DeployState(model.PIPELINE_RUNNING, "running"))[0], RUNNING)

    def test_a_failed_check_names_its_error(self):
        mark, cell = deploy_signal(DeployState(model.CHECK_FAILED, "timeout"))
        self.assertEqual(mark, ANSWER)
        self.assertIn("timeout", cell)


class UnreachableInstance(unittest.TestCase):
    """The forge not answering never fails the run, and is never silent either."""

    def test_the_row_names_the_instance_that_did_not_answer(self):
        r = unreachable_row(HOST)
        self.assertEqual(r.title, HOST)
        self.assertIn("unreachable", r.status)

    def test_an_unread_instance_is_on_you_so_the_tab_cannot_claim_all_clear(self):
        self.assertEqual(unreachable_row(HOST).mark, ACT)

    def test_it_leads_the_deploy_rows_the_run_could_still_read(self):
        rows = by_severity([row(PEOPLE, pr="`frontend!7`"), unreachable_row(HOST)])
        self.assertEqual(rows[0].title, HOST)

    def test_a_row_with_no_mr_number_still_sorts(self):
        rows = by_severity([row(ACT, pr="`frontend!7`"), unreachable_row(HOST)])
        self.assertEqual(len(rows), 2)


class AccessibilityContract(unittest.TestCase):
    """Every distinction must survive losing color, glyph fidelity, and detail."""

    def test_every_mark_is_a_single_ascii_character(self):
        for mark in MARK_ORDER:
            self.assertEqual(len(mark), 1)
            self.assertTrue(mark.isascii(), mark)

    def test_every_mark_a_state_can_produce_has_a_sort_rank(self):
        # An unranked mark falls to the sort fallback and lands below `.`.
        produced = {status_signal(s, a)[0] for s in REVIEW_SIGNAL for a in (True, False)}
        produced |= {status_signal(model.MERGEABLE, a)[0] for a in (True, False)}
        produced.add(status_signal("a_status_the_forge_adds_later", False)[0])
        produced |= {mark for mark, _ in DEPLOY_SIGNAL.values()}
        self.assertLessEqual(produced, set(MARK_ORDER))

    def test_no_state_is_reported_by_glyph_alone(self):
        for status, (_, _, label) in REVIEW_SIGNAL.items():
            self.assertTrue(label, f"{status} has no text label beside its glyph")
            self.assertIn(label, fmt_status(status, False, False)[1])
        for kind, (_, template) in DEPLOY_SIGNAL.items():
            self.assertTrue(template.split(" ", 1)[1:], f"{kind} has no text label beside its glyph")

    def test_an_unrecognised_status_is_labelled_with_its_raw_name(self):
        self.assertIn("a_status_the_forge_adds_later",
                      fmt_status("a_status_the_forge_adds_later", False, False)[1])

    def test_a_glyph_never_carries_two_meanings_in_the_status_column(self):
        by_glyph: dict[str, set[str]] = {}
        for status, (_, glyph, _label) in REVIEW_SIGNAL.items():
            by_glyph.setdefault(glyph, set()).add(status)
        for kind, (_, template) in DEPLOY_SIGNAL.items():
            glyph = template.split(" ", 1)[0]
            by_glyph.setdefault(glyph, set()).add(f"deploy:{kind}")
        by_glyph.setdefault("🔌", set()).add("unreachable_instance")

        shared = {g: s for g, s in by_glyph.items() if len(s) > 1}
        expected = {
            # A pipeline in flight is a single state whether the MR is open or merged.
            "⏳": {model.CI_RUNNING, model.CI_MUST_PASS, "deploy:pipeline_running"},
            "🔍": {model.MERGE_CHECK_RUNNING, model.MERGE_CHECK_PENDING},
            # One meaning, two scopes: the monitor got no answer. `check failed`
            # is a single deployment's call, this is a whole instance.
            "🔌": {"unreachable_instance", "deploy:check_failed"},
            # One meaning, two scopes: a run failed, the whole pipeline or its prod job.
            "❌": {"deploy:pipeline_failed", "deploy:prod_failed"},
        }
        for glyph, states in shared.items():
            self.assertEqual(states, expected.get(glyph, set()), f"{glyph} means too many things")

    def test_every_glyph_keeps_the_right_border_straight_in_any_terminal(self):
        glyphs = [
            *(glyph for _, glyph, _ in REVIEW_SIGNAL.values()),
            *(template.split(" ", 1)[0] for _, template in DEPLOY_SIGNAL.values()),
            unreachable_row(HOST).status.split(" ", 1)[0],
            status_signal(model.MERGEABLE, True)[1],
            status_signal("a_status_the_forge_adds_later", False)[1],
        ]
        for glyph in glyphs:
            self.assertEqual([unicodedata.east_asian_width(c) for c in glyph], ["W"], glyph)

    def test_a_glyph_is_never_the_only_difference_between_two_marks(self):
        marks = {status_signal(s, False)[0] for s in REVIEW_SIGNAL}
        self.assertGreater(len(marks), 1)


class Ordering(unittest.TestCase):
    def rows(self, *marks):
        return [row(m, pr=f"`p!{i}`") for i, m in enumerate(marks)]

    def test_rows_needing_you_sort_above_rows_that_do_not(self):
        ordered = by_severity(self.rows(RUNNING, PEOPLE, ANSWER, ACT))
        self.assertEqual([r.mark for r in ordered], [ACT, ANSWER, PEOPLE, RUNNING])

    def test_the_mrs_of_one_task_stay_together_within_a_severity(self):
        rows = [
            row(ACT, task="beta", pr="`frontend!1`"),
            row(ACT, task="alpha", pr="`worker!2`"),
            row(ACT, task="alpha", pr="`frontend!3`"),
        ]
        self.assertEqual(
            [(r.task, r.pr) for r in by_severity(rows)],
            [("alpha", "`frontend!3`"), ("alpha", "`worker!2`"), ("beta", "`frontend!1`")],
        )

    def test_the_same_repo_orders_by_mr_number_not_by_digits(self):
        rows = [row(ACT, pr="`frontend!999`"), row(ACT, pr="`frontend!1817`")]
        self.assertEqual([r.pr for r in by_severity(rows)], ["`frontend!999`", "`frontend!1817`"])

    def test_each_severity_block_is_ruled_off_from_the_next(self):
        rows = split_by_mark(self.rows(ACT, ACT, ANSWER, RUNNING))
        self.assertEqual(
            [r.mark if r is not None else "-" for r in rows],
            [ACT, ACT, "-", ANSWER, "-", RUNNING],
        )

    def test_a_single_severity_block_gets_no_rule(self):
        self.assertNotIn(None, split_by_mark(self.rows(PEOPLE, PEOPLE)))

    def test_no_rule_leads_a_block_so_the_two_groups_stay_split_by_one(self):
        self.assertIsNotNone(split_by_mark(self.rows(ACT, PEOPLE))[0])

    def test_the_open_rows_lead_with_whatever_is_on_you(self):
        rows = by_severity([
            open_row("p!1", mr(model.DRAFT)),
            open_row("p!2", mr(model.MERGEABLE, approved=True)),
            open_row("p!3", mr(model.CHANGES_REQUESTED)),
        ])
        self.assertEqual([r.mark for r in rows], [ACT, ANSWER, PEOPLE])
        self.assertEqual(rows[0].pr, "`p!2`")


class LinkedMrIds(unittest.TestCase):
    def test_the_pr_cell_opens_the_merge_request(self):
        self.assertEqual(
            linked_mr_id({"web_url": "https://x.example/mr/7"}, "p!7"),
            hyperlink("`p!7`", "https://x.example/mr/7"),
        )

    def test_a_cell_with_nowhere_to_point_stays_plain_text(self):
        self.assertEqual(linked_mr_id({}, "p!7"), "`p!7`")

    def test_a_row_reads_as_its_state_key(self):
        self.assertEqual(plain(open_row("frontend!1904", mr()).pr), "`frontend!1904`")

    def test_a_linked_cell_still_sorts_by_repo_and_mr_number(self):
        linked = [
            row(ACT, pr=linked_mr_id({"web_url": "u"}, "frontend!999")),
            row(ACT, pr=linked_mr_id({"web_url": "u"}, "frontend!17")),
        ]
        self.assertEqual([plain(r.pr) for r in by_severity(linked)], ["`frontend!17`", "`frontend!999`"])


class TableRendering(unittest.TestCase):
    def test_emoji_bearing_rows_keep_the_right_border_aligned(self):
        rows = [
            row(ACT, status="✅ mergeable, approved"),
            row(RUNNING, pr="`p!2`", status="⏳ CI running, unapproved"),
        ]
        widths = {display_width(line) for line in render_report(rows).splitlines()}
        self.assertEqual(len(widths), 1, "table lines differ in rendered width")

    def test_a_hyperlinked_cell_occupies_only_its_visible_width(self):
        label = "🚀 awaiting prod trigger"
        plain_row = row(ACT, status=label)
        linked_row = plain_row._replace(status=hyperlink(label, "https://x.example/y"))
        self.assertEqual(
            render_report([plain_row]).splitlines()[-1],
            plain(render_report([linked_row]).splitlines()[-1]),
        )

    def test_the_status_column_keeps_one_width_as_mrs_change_state(self):
        def rendered_width(status):
            return display_width(render_report([row(ACT, status=status)]).splitlines()[0])

        self.assertEqual(rendered_width("✅ mergeable"), rendered_width(WIDEST_STATUS))

    def test_the_status_column_is_sized_to_the_widest_status_it_can_show(self):
        widest = max(all_status_cells(), key=display_width)
        self.assertEqual(display_width(widest), STATUS_WIDTH, f"widest cell is now {widest!r}")

    def test_none_renders_as_the_rule_between_the_two_row_groups(self):
        lines = render_report([row(ACT), None, row(RUNNING, pr="`p!2`")]).splitlines()
        self.assertEqual(lines[1], lines[3])

    def test_the_mark_column_header_is_blank(self):
        self.assertEqual(render_report([row(ACT)]).splitlines()[0].split("|")[1].strip(), "")
        self.assertEqual(render_report([row(ACT)], show_task=False).splitlines()[0].split("|")[1].strip(), "")

    def test_without_a_task_source_the_task_column_is_left_out(self):
        table = render_report([row(ACT, task="THE-TASK")], show_task=False)
        self.assertNotIn("Task", table.splitlines()[0])
        self.assertNotIn("THE-TASK", table)

    def test_without_a_task_column_the_title_takes_its_width(self):
        with_task = render_report([row(ACT, title="x" * 200, task="y" * 200)])
        alone = render_report([row(ACT, title="x" * 200)], show_task=False)
        self.assertEqual(display_width(with_task.splitlines()[0]), display_width(alone.splitlines()[0]))
        self.assertIn("x" * TITLE_MAX_ALONE, alone.replace("…", "x"))

    def test_a_task_with_a_link_opens_the_task(self):
        line = render_report([row(ACT)._replace(task_url="https://x.example/t")]).splitlines()[-1]
        self.assertIn(hyperlink("task", "https://x.example/t"), line)

    def test_the_pr_title_column_comes_before_the_task_column(self):
        self.assertEqual(HEADERS[1:3], ("PR title", "Task"))
        self.assertEqual(Row._fields[1:3], ("title", "task"))

    def test_each_cell_lands_under_its_own_header(self):
        line = render_report([row(ACT, title="THE-TITLE", task="THE-TASK")]).splitlines()[-1]
        self.assertLess(line.index("THE-TITLE"), line.index("THE-TASK"))

    def test_a_line_cut_at_the_window_edge_keeps_its_link_closed(self):
        cut = clip("ab " + hyperlink("`web!1931`", "https://x.example/1931") + " cd", 8)
        self.assertEqual(plain(cut), "ab `web…")
        self.assertTrue(cut.endswith("\033]8;;\a"))

    def test_a_line_that_fits_is_not_cut(self):
        self.assertEqual(clip("✅ mergeable", 12), "✅ mergeable")

    def test_a_long_title_is_cut_so_the_table_never_wraps(self):
        line = render_report([row(ACT, title="x" * 80)]).splitlines()[-1]
        self.assertIn("x" * (TITLE_MAX - 1) + "…", line)
        self.assertNotIn("x" * TITLE_MAX, line)


if __name__ == "__main__":
    unittest.main()

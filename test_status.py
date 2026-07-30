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
    PEOPLE,
    RUNNING,
    Row,
    _MARK_LABELS,
    _MARK_ORDER,
    _MERGE_STATUS,
    _by_severity,
    _display_width,
    _hyperlink,
    _render_table,
    _split_by_mark,
    fmt_status,
    open_mr_rows,
    pending_deploy_status,
    status_signal,
    summary_line,
)


def mr(merge_status="not_approved", *, approved=False, conflicts=False, title="t", task="task"):
    return {
        "merge_status": merge_status,
        "fully_approved": approved,
        "has_conflicts": conflicts,
        "title": title,
        "task_name": task,
    }


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
        self.assertIn("conflict", cell)

    def test_draft_and_not_approved_labels_skip_the_redundant_qualifier(self):
        self.assertTrue(fmt_status("draft_status", False, False)[1].endswith("draft_status"))
        self.assertTrue(fmt_status("not_approved", False, False)[1].endswith("not_approved"))


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

    def test_every_mark_has_a_sort_rank_and_a_tally_label(self):
        self.assertEqual(set(_MARK_ORDER), {mark for mark, _ in _MARK_LABELS})

    def test_the_tally_orders_buckets_by_severity(self):
        ranks = [_MARK_ORDER[mark] for mark, _ in _MARK_LABELS]
        self.assertEqual(ranks, sorted(ranks))

    def test_no_state_is_reported_by_glyph_alone(self):
        for status in _MERGE_STATUS:
            cell = fmt_status(status, False, False)[1]
            self.assertIn(status, cell, f"{status} has no text label beside its glyph")

    def test_a_glyph_never_carries_two_meanings_in_the_status_column(self):
        # ⏳ is the one deliberate share: a pipeline in flight is a single state
        # whether the MR is open or already merged.
        by_glyph: dict[str, set[str]] = {}
        for status, (_, glyph) in _MERGE_STATUS.items():
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
        return [Row(m, "task", "title", f"`p!{i}`", "s") for i, m in enumerate(marks)]

    def test_rows_needing_the_user_sort_above_rows_that_do_not(self):
        ordered = _by_severity(self.rows(RUNNING, PEOPLE, ANSWER, ACT))
        self.assertEqual([r.mark for r in ordered], [ACT, ANSWER, PEOPLE, RUNNING])

    def test_mr_id_breaks_ties_within_a_severity(self):
        rows = [Row(ACT, "t", "t", "`b!2`", "s"), Row(ACT, "t", "t", "`a!1`", "s")]
        self.assertEqual([r.pr for r in _by_severity(rows)], ["`a!1`", "`b!2`"])

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


class Tally(unittest.TestCase):
    def test_the_tally_names_every_non_empty_bucket(self):
        rows = [Row(ACT, "", "", "`a!1`", ""), Row(RUNNING, "", "", "`a!2`", "")]
        self.assertEqual(summary_line(rows), "! 1 on you  . 1 running")

    def test_empty_buckets_stay_out_of_the_tally(self):
        rows = [Row(PEOPLE, "", "", "`a!1`", "")]
        self.assertEqual(summary_line(rows), "~ 1 with people")

    def test_the_separator_rule_is_not_counted_as_a_row(self):
        rows = [Row(ACT, "", "", "`a!1`", ""), None, Row(ACT, "", "", "`a!2`", "")]
        self.assertEqual(summary_line(rows), "! 2 on you")


class TableRendering(unittest.TestCase):
    def test_emoji_bearing_rows_keep_the_right_border_aligned(self):
        rows = [
            Row(ACT, "task", "title", "`p!1`", "✅ mergeable + fully_approved"),
            Row(RUNNING, "task", "title", "`p!2`", "⏳ ci_still_running"),
        ]
        widths = {_display_width(line) for line in _render_table(HEADERS, rows).splitlines()}
        self.assertEqual(len(widths), 1, "table lines differ in rendered width")

    def test_a_hyperlinked_cell_occupies_only_its_visible_width(self):
        plain = Row(ACT, "task", "title", "`p!1`", "🕹️ awaiting prod trigger")
        linked = plain._replace(status=_hyperlink("🕹️ awaiting prod trigger", "https://x.example/y"))
        self.assertEqual(
            _render_table(HEADERS, [plain]).splitlines()[-1],
            _render_table(HEADERS, [linked]).splitlines()[-1].replace(
                _hyperlink("🕹️ awaiting prod trigger", "https://x.example/y"),
                "🕹️ awaiting prod trigger",
            ),
        )

    def test_none_renders_as_the_rule_between_the_two_row_groups(self):
        rows = [Row(ACT, "t", "t", "`p!1`", "s"), None, Row(RUNNING, "t", "t", "`p!2`", "s")]
        lines = _render_table(HEADERS, rows).splitlines()
        self.assertEqual(lines[1], lines[3])

    def test_the_mark_column_header_is_blank(self):
        self.assertEqual(HEADERS[0], "")
        self.assertEqual(len(HEADERS), len(Row._fields))


if __name__ == "__main__":
    unittest.main(verbosity=2)

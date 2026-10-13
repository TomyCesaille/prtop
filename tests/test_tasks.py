"""Where the Task column comes from."""

import unittest
from pathlib import Path

from prtop.config import Config, ConfigError
from prtop.tasks import BranchTasks, Issue, IssueTasks, NoTasks, Task, task_source

PAGE_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
NOTION_BRANCH = f"dev/1234-Add-a-dark-mode-toggle-{PAGE_ID}"


def no_issues():
    raise AssertionError("a branch-based source asked the forge for issues")


def source(**settings):
    return task_source(Config(state_file=Path("unused"), **settings))


class NotionBranches(unittest.TestCase):
    def resolve(self, branch):
        return source(task_source="notion").resolve(branch, no_issues)

    def test_a_task_branch_reads_as_the_notion_task_and_links_to_its_page(self):
        self.assertEqual(
            self.resolve(NOTION_BRANCH),
            Task("1234 Add a dark mode toggle", f"https://www.notion.so/{PAGE_ID}"),
        )

    def test_the_mrs_of_a_task_split_across_a_stack_share_one_task(self):
        self.assertEqual(self.resolve(f"{NOTION_BRANCH}-mr1-theme-tokens"), self.resolve(NOTION_BRANCH))
        self.assertEqual(self.resolve(f"{NOTION_BRANCH}-mr2-toggle-ui"), self.resolve(NOTION_BRANCH))

    def test_a_branch_outside_the_task_convention_has_no_task(self):
        self.assertIsNone(self.resolve("dev/quick-fix"))
        self.assertIsNone(self.resolve("main"))
        self.assertIsNone(self.resolve(None))


class JiraBranches(unittest.TestCase):
    def test_a_jira_key_and_the_words_after_it_make_the_label(self):
        task = source(task_source="jira").resolve("feature/PROJ-123-add_sso-login", no_issues)
        self.assertEqual(task, Task("PROJ-123 add sso login"))

    def test_a_bare_key_is_the_whole_label(self):
        self.assertEqual(source(task_source="jira").resolve("PROJ-7", no_issues).label, "PROJ-7")

    def test_a_browse_url_links_the_key_to_its_issue(self):
        tasks = source(task_source="jira", task_url="https://acme.atlassian.net/browse/{id}")
        self.assertEqual(
            tasks.resolve("PROJ-123-fix", no_issues).url, "https://acme.atlassian.net/browse/PROJ-123"
        )

    def test_a_lowercase_branch_is_not_a_jira_key(self):
        self.assertIsNone(source(task_source="jira").resolve("dev/quick-fix-2", no_issues))


class CustomBranches(unittest.TestCase):
    def test_a_pattern_of_your_own_reads_any_convention(self):
        tasks = BranchTasks(r"^(?P<id>\d+)-(?P<title>.+)$", "https://tracker.example/{id}")
        self.assertEqual(tasks.resolve("482-fix-login", no_issues), Task("482 fix login", "https://tracker.example/482"))

    def test_the_branch_source_needs_a_pattern(self):
        with self.assertRaisesRegex(ConfigError, "PRTOP_TASK_BRANCH_PATTERN"):
            source(task_source="branch")

    def test_a_url_naming_a_group_the_pattern_lacks_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "ticket"):
            BranchTasks(r"(?P<id>\d+)", "https://tracker.example/{ticket}")


class Issues(unittest.TestCase):
    def test_the_issue_the_mr_closes_is_its_task(self):
        task = IssueTasks().resolve("any-branch", lambda: [Issue("#12", "Login fails", "https://x.example/12")])
        self.assertEqual(task, Task("#12 Login fails", "https://x.example/12"))

    def test_further_closed_issues_are_counted_beside_the_first(self):
        issues = [Issue("#12", "Login fails"), Issue("#13", "Logout fails")]
        self.assertEqual(IssueTasks().resolve(None, lambda: issues).label, "#12 Login fails (+1)")

    def test_an_mr_closing_no_issue_has_no_task(self):
        self.assertIsNone(IssueTasks().resolve(None, list))


class NoSource(unittest.TestCase):
    def test_without_a_source_there_is_no_task_column(self):
        tasks = source()
        self.assertIsInstance(tasks, NoTasks)
        self.assertFalse(tasks.shows_column)
        self.assertIsNone(tasks.resolve(NOTION_BRANCH, no_issues))


if __name__ == "__main__":
    unittest.main()

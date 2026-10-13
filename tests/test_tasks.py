"""The task each merge request serves."""

import unittest

from prtop.tasks import extract_task_name

PAGE_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


class NotionBranches(unittest.TestCase):
    def test_a_task_branch_reads_as_the_notion_task_name(self):
        self.assertEqual(
            extract_task_name(f"dev/1234-Add-a-dark-mode-toggle-{PAGE_ID}"),
            "1234 Add a dark mode toggle",
        )

    def test_the_mrs_of_a_task_split_across_a_stack_share_one_task_name(self):
        stem = f"dev/1234-Add-a-dark-mode-toggle-{PAGE_ID}"
        self.assertEqual(extract_task_name(f"{stem}-mr1-theme-tokens"), extract_task_name(stem))
        self.assertEqual(extract_task_name(f"{stem}-mr2-toggle-ui"), extract_task_name(stem))

    def test_a_branch_outside_the_task_convention_has_no_task(self):
        self.assertEqual(extract_task_name("dev/quick-fix"), "")
        self.assertEqual(extract_task_name("main"), "")
        self.assertEqual(extract_task_name(None), "")


if __name__ == "__main__":
    unittest.main()

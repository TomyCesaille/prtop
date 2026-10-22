"""The boundary between prtop and a code-hosting service.

A forge owns every call to its service and every field name of its API, and
hands the rest of prtop plain dicts in the vocabulary of `prtop.model`.

Adding a forge means implementing `Forge` in a module of this package and
registering it in `make_forge` and in the `FORGE` setting.
"""

from __future__ import annotations

from typing import Protocol

from prtop.config import Config
from prtop.model import DeployState
from prtop.tasks import TaskSource


class Forge(Protocol):
    # Names the instance in the report when it does not answer.
    host: str

    def open_merge_requests(self, tasks: TaskSource) -> dict[str, dict]:
        """Every open MR assigned to you, keyed by the `repo!iid` the table shows.

        Each entry carries `title`, `task` and `task_url` (from `tasks`),
        `status` (a review state of `prtop.model`), `fully_approved`,
        `has_conflicts`, `threads_await_user` and `web_url`, plus whatever the
        forge needs to recognise the MR again in `merged_deployment`. Raises
        `ForgeError` when the forge does not answer.
        """
        ...

    def merged_deployment(self, entry: dict) -> dict | None:
        """The deployment to track for an MR that left the open list, if it merged.

        `entry` is the MR's last `open_merge_requests` entry. The result keeps its
        `title`, `task`, `task_url` and `web_url`, plus whatever `deploy_states`
        needs.
        """
        ...

    def deploy_states(self, deployments: dict[str, dict]) -> dict[str, DeployState]:
        """Where each tracked deployment stands. One in `model.FINISHED` is dropped from
        tracking. May update the entries in place; they are saved with the state."""
        ...


def make_forge(config: Config) -> Forge:
    from prtop.forges.gitlab import GitLab

    return GitLab(config)

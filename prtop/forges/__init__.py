"""The boundary between prtop and a code-hosting service.

A forge owns every call to its service and every field name of its API, and
hands the rest of prtop plain dicts in the vocabulary of `prtop.model`. One
GitLab instance and github.com can be read together; each keeps its own state.

Adding a forge means implementing `Forge` in a module of this package and
registering it in `make_forges` and in `prtop.config.FORGES`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from prtop.config import Config
from prtop.model import DeployState
from prtop.tasks import TaskSource


class Forge(Protocol):
    # Names the instance in the report when it does not answer, and its state file.
    host: str

    def open_merge_requests(self, tasks: TaskSource) -> dict[str, dict]:
        """Every open MR of yours, keyed as `model.keyed_by_short_id` keys them.

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


def make_forges(config: Config) -> list[Forge]:
    from prtop.forges.github import GitHub
    from prtop.forges.gitlab import GitLab

    makers: dict[str, Callable[[Config], Forge]] = {"gitlab": GitLab, "github": GitHub}
    return [makers[name](config) for name in config.forges]

"""Settings: `PRTOP_*` variables, from the environment first and an env file second.

The env file is `$XDG_CONFIG_HOME/prtop/.env` (`~/.config/prtop/.env`), or
whatever `PRTOP_CONFIG_FILE` names. It holds `KEY=VALUE` lines; blank lines and
`#` comments are skipped, a leading `export ` and quotes around the value are
dropped. A variable set to an empty value counts as unset.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

PREFIX = "PRTOP_"
CONFIG_FILE_VAR = "PRTOP_CONFIG_FILE"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    state_file: Path
    forge: str = "gitlab"
    gitlab_host: str = "gitlab.com"
    # Read from the instance when unset.
    gitlab_username: str | None = None
    # Limits the listing to one group and its subgroups; unset lists the whole instance.
    gitlab_group: str | None = None
    # See `prtop.tasks` for what each source reads.
    task_source: str = "none"
    task_branch_pattern: str | None = None
    task_url: str | None = None
    deploy_tracking: bool = True
    # Matches every production deploy job, while leaving prod test/migrate jobs out.
    deploy_job_pattern: str = r"deploy.*prod|prod.*deploy"
    # The one job among those whose success means the app itself shipped.
    primary_deploy_job_pattern: str = r"^deploy[_-]production$"
    # iTerm2 only: an orange tab while an open MR is on you, green otherwise.
    iterm_tab_color: bool = False


def _text(name: str, value: str) -> str:
    return value


def _path(name: str, value: str) -> Path:
    return Path(value).expanduser()


def _flag(name: str, value: str) -> bool:
    lowered = value.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}={value!r}: expected on or off")


def _regex(name: str, value: str) -> str:
    try:
        re.compile(value)
    except re.error as e:
        raise ConfigError(f"{name}={value!r}: not a regular expression ({e})") from None
    return value


def _one_of(*allowed: str) -> Callable[[str, str], str]:
    def parse(name: str, value: str) -> str:
        if value not in allowed:
            raise ConfigError(f"{name}={value!r}: expected one of {', '.join(allowed)}")
        return value

    return parse


_SETTINGS: dict[str, tuple[str, Callable[[str, str], object]]] = {
    "STATE_FILE": ("state_file", _path),
    "FORGE": ("forge", _one_of("gitlab")),
    "GITLAB_HOST": ("gitlab_host", _text),
    "GITLAB_USERNAME": ("gitlab_username", _text),
    "GITLAB_GROUP": ("gitlab_group", _text),
    "TASK_SOURCE": ("task_source", _one_of("none", "issues", "notion", "jira", "branch")),
    "TASK_BRANCH_PATTERN": ("task_branch_pattern", _regex),
    "TASK_URL": ("task_url", _text),
    "DEPLOY_TRACKING": ("deploy_tracking", _flag),
    "DEPLOY_JOB_PATTERN": ("deploy_job_pattern", _regex),
    "PRIMARY_DEPLOY_JOB_PATTERN": ("primary_deploy_job_pattern", _regex),
    "ITERM_TAB_COLOR": ("iterm_tab_color", _flag),
}


def read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def config_file(environ: Mapping[str, str]) -> Path:
    if explicit := environ.get(CONFIG_FILE_VAR):
        return Path(explicit).expanduser()
    base = environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "prtop" / ".env"


def default_state_file(environ: Mapping[str, str]) -> Path:
    base = environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "prtop" / "state.json"


def load_config(environ: Mapping[str, str] = os.environ) -> Config:
    path = config_file(environ)
    from_file = read_env_file(path) if path.is_file() else {}
    from_env = {k: v for k, v in environ.items() if k.startswith(PREFIX) and k != CONFIG_FILE_VAR}

    settings: dict[str, object] = {}
    for key, value in {**from_file, **from_env}.items():
        name = key.removeprefix(PREFIX)
        if not key.startswith(PREFIX) or name not in _SETTINGS:
            print(f"prtop: ignoring {key}, which is not a prtop setting", file=sys.stderr)
            continue
        if value:
            field, parse = _SETTINGS[name]
            settings[field] = parse(key, value)

    settings.setdefault("state_file", default_state_file(environ))
    return Config(**settings)  # type: ignore[arg-type]

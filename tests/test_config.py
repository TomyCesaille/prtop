"""Settings from the environment and the env file."""

import io
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from prtop.config import _SETTINGS, ConfigError, config_file, default_state_file, load_config, read_env_file

EXAMPLE = Path(__file__).parents[1] / ".env.example"


class Settings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }

    def write_env_file(self, text, path=None):
        path = path or self.root / "config" / "prtop" / ".env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def load(self, **env):
        return load_config({**self.env, **env})

    def test_with_nothing_set_prtop_reads_gitlab_com_and_keeps_its_state_under_xdg(self):
        config = self.load()
        self.assertEqual(config.gitlab_host, "gitlab.com")
        self.assertIsNone(config.gitlab_group)
        self.assertEqual(config.state_file, self.root / "state" / "prtop" / "gitlab.com.json")

    def test_each_instance_keeps_a_state_file_of_its_own(self):
        state_file = self.load(PRTOP_GITLAB_HOST="gitlab.example.com:8443").state_file
        self.assertEqual(state_file.name, "gitlab.example.com_8443.json")

    def test_the_env_file_is_read_from_the_config_directory(self):
        self.write_env_file("# my instance\n\nexport PRTOP_GITLAB_HOST='gitlab.example.com'\nPRTOP_GITLAB_GROUP=\"acme\"\n")
        config = self.load()
        self.assertEqual((config.gitlab_host, config.gitlab_group), ("gitlab.example.com", "acme"))

    def test_the_environment_wins_over_the_env_file(self):
        self.write_env_file("PRTOP_GITLAB_HOST=from-file.example.com\n")
        self.assertEqual(self.load(PRTOP_GITLAB_HOST="from-env.example.com").gitlab_host, "from-env.example.com")

    def test_the_env_file_can_live_anywhere(self):
        path = self.write_env_file("PRTOP_GITLAB_HOST=elsewhere.example.com\n", self.root / "prtop.env")
        self.assertEqual(self.load(PRTOP_CONFIG_FILE=str(path)).gitlab_host, "elsewhere.example.com")

    def test_an_empty_value_counts_as_unset(self):
        self.assertEqual(self.load(PRTOP_GITLAB_HOST="").gitlab_host, "gitlab.com")

    def test_a_state_file_path_expands_the_home_directory(self):
        self.assertEqual(self.load(PRTOP_STATE_FILE="~/s.json").state_file, Path.home() / "s.json")

    def test_deploy_tracking_reads_on_and_off(self):
        self.assertFalse(self.load(PRTOP_DEPLOY_TRACKING="off").deploy_tracking)
        self.assertTrue(self.load(PRTOP_DEPLOY_TRACKING="yes").deploy_tracking)

    def test_a_misspelled_setting_is_reported_and_ignored(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            config = self.load(PRTOP_GITLAB_HOTS="typo.example.com")
        self.assertEqual(config.gitlab_host, "gitlab.com")
        self.assertIn("PRTOP_GITLAB_HOTS", stderr.getvalue())

    def test_a_value_outside_the_allowed_ones_names_the_setting(self):
        with self.assertRaisesRegex(ConfigError, "PRTOP_TASK_SOURCE"):
            self.load(PRTOP_TASK_SOURCE="trello")

    def test_a_broken_job_pattern_names_the_setting(self):
        with self.assertRaisesRegex(ConfigError, "PRTOP_DEPLOY_JOB_PATTERN"):
            self.load(PRTOP_DEPLOY_JOB_PATTERN="deploy(")

    def test_an_env_file_is_read_as_utf8_whatever_the_locale(self):
        self.write_env_file("").write_bytes("PRTOP_GITLAB_GROUP=équipe\n".encode())
        self.assertEqual(self.load().gitlab_group, "équipe")

    def test_the_example_env_file_lists_every_setting(self):
        listed = set(re.findall(r"^# PRTOP_(\w+)=", EXAMPLE.read_text(), re.MULTILINE))
        self.assertEqual(listed, set(_SETTINGS) - {"FORGE"})

    def test_the_example_env_file_loads_once_uncommented(self):
        lines = EXAMPLE.read_text().replace("# PRTOP_", "PRTOP_").splitlines()
        path = self.write_env_file("\n".join(line for line in lines if not line.startswith("#")))
        self.assertEqual(self.load(PRTOP_CONFIG_FILE=str(path)).task_source, "none")

    def test_a_line_without_a_value_points_at_its_line_number(self):
        path = self.write_env_file("PRTOP_GITLAB_HOST=gitlab.example.com\nPRTOP_GITLAB_GROUP\n")
        with self.assertRaisesRegex(ConfigError, ":2:"):
            read_env_file(path)


WINDOWS = {"APPDATA": "C:/Users/me/AppData/Roaming", "LOCALAPPDATA": "C:/Users/me/AppData/Local"}


class Locations(unittest.TestCase):
    def test_on_macos_and_linux_files_follow_the_xdg_defaults(self):
        for platform in ("darwin", "linux"):
            self.assertEqual(config_file({}, platform), Path.home() / ".config" / "prtop" / ".env")
            self.assertEqual(
                default_state_file({}, "gitlab.com", platform),
                Path.home() / ".local" / "state" / "prtop" / "gitlab.com.json",
            )

    def test_on_windows_settings_roam_and_state_stays_on_the_machine(self):
        self.assertEqual(config_file(WINDOWS, "win32"), Path("C:/Users/me/AppData/Roaming/prtop/.env"))
        self.assertEqual(
            default_state_file(WINDOWS, "gitlab.com", "win32"),
            Path("C:/Users/me/AppData/Local/prtop/gitlab.com.json"),
        )

    def test_xdg_directories_win_on_windows_too(self):
        environ = {**WINDOWS, "XDG_CONFIG_HOME": "D:/cfg", "XDG_STATE_HOME": "D:/state"}
        self.assertEqual(config_file(environ, "win32"), Path("D:/cfg/prtop/.env"))
        self.assertEqual(default_state_file(environ, "gitlab.com", "win32"), Path("D:/state/prtop/gitlab.com.json"))


if __name__ == "__main__":
    unittest.main()

"""The campus PC must keep its key even when its environment lost it."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import agent_auth


class AgentSecretTests(unittest.TestCase):
    def setUp(self):
        self._real_config = agent_auth.CONFIG_FILE

    def tearDown(self):
        agent_auth.CONFIG_FILE = self._real_config

    def _config_with(self, tmp: Path, secret) -> None:
        path = tmp / "config.json"
        path.write_text(json.dumps({"agent_secret": secret}), encoding="utf-8")
        agent_auth.CONFIG_FILE = path

    def test_environment_key_is_used_when_present(self):
        with mock.patch.dict(os.environ, {"AGENT_SECRET": "from-env"}):
            self.assertEqual(agent_auth.agent_secret(), "from-env")
            self.assertEqual(
                agent_auth.secret_headers(), {"X-Agent-Secret": "from-env"}
            )

    def test_cached_config_key_is_used_when_environment_is_silent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._config_with(Path(tmp), "from-config")
            with mock.patch.dict(os.environ, {"AGENT_SECRET": ""}):
                self.assertEqual(agent_auth.agent_secret(), "from-config")

    def test_no_key_anywhere_sends_no_header(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self._config_with(Path(tmp), "")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(agent_auth.agent_secret(), "")
                self.assertEqual(agent_auth.secret_headers(), {})

    def test_unreadable_config_is_not_an_error(self):
        agent_auth.CONFIG_FILE = Path("/nonexistent/ppis/config.json")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(agent_auth.agent_secret(), "")


if __name__ == "__main__":
    unittest.main()

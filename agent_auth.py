"""The shared key the cloud checks on every call the campus PC makes.

The key normally arrives as the AGENT_SECRET environment variable, but a
process started before the variable was set inherits an environment without
it, and a campus PC in that state loses its config, its face sync and its
attendance posts to 401s while everything else looks healthy. The cached
config.json holds the same key, so read that whenever the environment is
silent.
"""

import json
import os
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.json"


def _secret_from_config() -> str:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    secret = data.get("agent_secret", "")
    return secret if isinstance(secret, str) else ""


def agent_secret() -> str:
    """The key to send to the cloud, from the environment or the cached config."""
    return os.environ.get("AGENT_SECRET", "").strip() or _secret_from_config()


def secret_headers() -> dict:
    """Headers carrying the shared key, empty when the campus PC has none."""
    secret = agent_secret()
    return {"X-Agent-Secret": secret} if secret else {}

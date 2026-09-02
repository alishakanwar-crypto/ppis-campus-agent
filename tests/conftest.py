"""Keep the shared recorder-lockout state out of the tests.

The store is deliberately persistent on the campus PC: a refusal has to
outlive a restart so nothing knocks on a locked recorder again. In tests
that same persistence would carry one test's refusal into the next, so each
test gets its own throwaway file.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder_auth


@pytest.fixture(autouse=True)
def isolated_recorder_auth_state(monkeypatch, tmp_path_factory):
    state = tmp_path_factory.mktemp("recorder-auth") / "recorder_auth.json"
    monkeypatch.setenv("RECORDER_AUTH_STATE", str(state))
    monkeypatch.setattr(recorder_auth, "STATE_PATH", state)
    yield



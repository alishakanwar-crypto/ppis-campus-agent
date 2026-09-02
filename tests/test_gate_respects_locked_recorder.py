"""The gate counter must not keep a locked recorder locked."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gate_counter
import recorder_auth


class GateRecorderSafetyTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        env = patch.dict(os.environ, {
            "RECORDER_AUTH_STATE": str(Path(self._dir.name) / "state.json"),
        })
        env.start()
        self.addCleanup(env.stop)
        importlib.reload(recorder_auth)
        self.addCleanup(importlib.reload, recorder_auth)
        patcher = patch.object(gate_counter, "recorder_auth", recorder_auth)
        patcher.start()
        self.addCleanup(patcher.stop)
        gate_counter.DVR_CREDS["192.168.0.12"] = {
            "user": "admin", "pass": "secret",
        }
        self.addCleanup(gate_counter.DVR_CREDS.pop, "192.168.0.12", None)

    def _key(self):
        return recorder_auth.credential_key("admin", "secret")

    def test_no_login_is_sent_to_a_recorder_that_refused_us(self):
        recorder_auth.note_refusal("192.168.0.12", self._key())
        with patch.object(gate_counter.httpx, "Client") as client:
            frame = gate_counter.capture_gate_frame(54, "192.168.0.12")
        self.assertIsNone(frame)
        client.assert_not_called()

    def test_a_rejected_login_is_never_retried_with_basic_auth(self):
        response = MagicMock(status_code=401, headers={})
        client = MagicMock()
        client.__enter__.return_value.get.return_value = response
        with patch.object(gate_counter.httpx, "Client", return_value=client):
            frame = gate_counter.capture_gate_frame(54, "192.168.0.12")
        self.assertIsNone(frame)
        self.assertEqual(client.__enter__.return_value.get.call_count, 1)
        self.assertTrue(recorder_auth.is_refused("192.168.0.12", self._key()))

    def test_one_dead_channel_does_not_condemn_a_serving_recorder(self):
        recorder_auth.note_success("192.168.0.12")
        response = MagicMock(status_code=401, headers={})
        client = MagicMock()
        client.__enter__.return_value.get.return_value = response
        with patch.object(gate_counter.httpx, "Client", return_value=client):
            gate_counter.capture_gate_frame(54, "192.168.0.12")
        self.assertFalse(recorder_auth.is_refused("192.168.0.12", self._key()))

    def test_a_refused_snapshot_login_still_leaves_the_video_stream(self):
        # DVR 4's snapshot login is refused on its dead channels; its gate
        # cameras only ever worked through the video stream.
        gate_counter.DVR_CREDS["192.168.0.13"] = {
            "user": "admin", "pass": "secret",
        }
        self.addCleanup(gate_counter.DVR_CREDS.pop, "192.168.0.13", None)
        recorder_auth.note_refusal(
            "192.168.0.13", recorder_auth.credential_key("admin", "secret"),
        )
        frame = object()
        with patch.object(gate_counter.httpx, "Client") as client, \
                patch.object(
                    gate_counter, "_capture_gate_frame_rtsp", return_value=frame
                ) as rtsp:
            got = gate_counter.capture_gate_frame(20, "192.168.0.13")
        self.assertIs(got, frame)
        client.assert_not_called()
        rtsp.assert_called_once_with(20, "192.168.0.13")

    def test_every_gate_login_restarts_the_recorder_s_quiet_window(self):
        recorder_auth.note_refusal("192.168.0.14", "other-key")
        gate_counter.DVR_CREDS["192.168.0.14"] = {
            "user": "admin", "pass": "secret",
        }
        self.addCleanup(gate_counter.DVR_CREDS.pop, "192.168.0.14", None)
        response = MagicMock(status_code=500, headers={})
        client = MagicMock()
        client.__enter__.return_value.get.return_value = response
        with patch.object(gate_counter.httpx, "Client", return_value=client):
            gate_counter.capture_gate_frame(20, "192.168.0.14")
        since = recorder_auth.seconds_since_attempt("192.168.0.14")
        self.assertIsNotNone(since)
        self.assertLess(since, 5)


if __name__ == "__main__":
    unittest.main()

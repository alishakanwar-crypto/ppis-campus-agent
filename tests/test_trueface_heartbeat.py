import unittest
from unittest.mock import Mock, patch

import trueface_poller


class TrueFaceHeartbeatTests(unittest.TestCase):
    def setUp(self):
        trueface_poller._last_heartbeat_at = 0.0
        trueface_poller._last_liveness_log_at = 0.0

    def tearDown(self):
        trueface_poller._last_heartbeat_at = 0.0
        trueface_poller._last_liveness_log_at = 0.0

    def test_heartbeat_url_derives_from_event_url(self):
        self.assertTrue(trueface_poller.HEARTBEAT_API.endswith("/api/trueface/heartbeat"))

    def test_heartbeat_is_throttled_to_the_configured_interval(self):
        post = Mock(return_value=Mock(status_code=200))
        clock = [1000.0]

        with (
            patch.object(trueface_poller.httpx, "post", post),
            patch.object(trueface_poller.time, "monotonic", lambda: clock[0]),
        ):
            trueface_poller._send_heartbeat()
            clock[0] += trueface_poller.HEARTBEAT_INTERVAL_SECONDS - 1
            trueface_poller._send_heartbeat()
            self.assertEqual(post.call_count, 1)

            clock[0] += 2
            trueface_poller._send_heartbeat()
            self.assertEqual(post.call_count, 2)

        self.assertEqual(post.call_args.args[0], trueface_poller.HEARTBEAT_API)

    def test_heartbeat_failure_never_raises(self):
        post = Mock(side_effect=TimeoutError("cloud unavailable"))

        with patch.object(trueface_poller.httpx, "post", post):
            trueface_poller._send_heartbeat()

        post.assert_called_once()

    def test_heartbeat_disabled_when_interval_is_zero(self):
        post = Mock()

        with (
            patch.object(trueface_poller, "HEARTBEAT_INTERVAL_SECONDS", 0.0),
            patch.object(trueface_poller.httpx, "post", post),
        ):
            trueface_poller._send_heartbeat()

        post.assert_not_called()

    def test_liveness_line_is_logged_on_its_own_interval(self):
        clock = [1000.0]
        info = Mock()

        with (
            patch.object(trueface_poller.httpx, "post", Mock()),
            patch.object(trueface_poller.time, "monotonic", lambda: clock[0]),
            patch.object(trueface_poller.logger, "info", info),
        ):
            trueface_poller._send_heartbeat()
            clock[0] += trueface_poller.LIVENESS_LOG_INTERVAL_SECONDS - 1
            trueface_poller._send_heartbeat()
            self.assertEqual(info.call_count, 1)

            clock[0] += 2
            trueface_poller._send_heartbeat()
            self.assertEqual(info.call_count, 2)

    def test_liveness_line_is_logged_even_when_heartbeat_is_disabled(self):
        info = Mock()

        with (
            patch.object(trueface_poller, "HEARTBEAT_INTERVAL_SECONDS", 0.0),
            patch.object(trueface_poller.logger, "info", info),
        ):
            trueface_poller._send_heartbeat()

        info.assert_called_once()


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import Mock, patch

import trueface_poller


class TrueFaceCloudLatencyTests(unittest.TestCase):
    def setUp(self):
        trueface_poller._pending_events.clear()

    def tearDown(self):
        trueface_poller._pending_events.clear()

    def test_retry_budget_is_bounded_and_final_failure_is_requeued(self):
        events = [{"pin": "7", "timestamp": "2026-06-01 07:00:00"}]
        failed_post = Mock(side_effect=TimeoutError("cloud unavailable"))

        with (
            patch.object(trueface_poller.httpx, "post", failed_post),
            patch.object(trueface_poller.time, "sleep"),
        ):
            result = trueface_poller._send_to_cloud(events)

        self.assertIsNone(result)
        self.assertEqual(failed_post.call_count, 2)
        self.assertEqual(trueface_poller._pending_events, events)
        worst_case = (
            trueface_poller.CLOUD_POST_ATTEMPTS
            * trueface_poller.CLOUD_POST_TIMEOUT_SECONDS
            + (trueface_poller.CLOUD_POST_ATTEMPTS - 1)
            * trueface_poller.CLOUD_RETRY_BACKOFF_SECONDS
        )
        self.assertEqual(
            worst_case,
            2 * 8 + 0.5,
        )
        self.assertLessEqual(worst_case, 18)

    def test_requeued_events_are_sent_once_with_new_events(self):
        pending = [{"pin": "7", "timestamp": "2026-06-01 07:00:00"}]
        new_event = {"pin": "8", "timestamp": "2026-06-01 07:01:00"}
        trueface_poller._pending_events.extend(pending)
        response = Mock(status_code=200)
        response.json.return_value = {"results": []}

        with patch.object(trueface_poller.httpx, "post", return_value=response) as post:
            result = trueface_poller._send_to_cloud([new_event])

        self.assertEqual(result, {"results": []})
        self.assertEqual(trueface_poller._pending_events, [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            post.call_args.kwargs["json"],
            pending + [new_event],
        )

    def test_http_success_is_not_retried_when_response_json_is_invalid(self):
        event = {"pin": "7", "timestamp": "2026-06-01 07:00:00"}
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("invalid response")

        with patch.object(trueface_poller.httpx, "post", return_value=response) as post:
            result = trueface_poller._send_to_cloud([event])

        self.assertEqual(result, {})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(trueface_poller._pending_events, [])

    def test_correlated_event_log_contains_latency_fields(self):
        event = {"pin": "7", "timestamp": "2026-06-01 07:00:00"}
        post_metrics = {
            "elapsed": 1.25,
            "attempts": 2,
            "status": 200,
            "outcome": "success",
        }

        with patch.object(trueface_poller.logger, "info") as log_info:
            trueface_poller._log_event_timing(
                event,
                trueface_poller.time.monotonic() - 0.5,
                0.75,
                post_metrics,
            )

        message = log_info.call_args.args[0]
        fields = " ".join(str(arg) for arg in log_info.call_args.args[1:])
        self.assertIn("key=%s", message)
        self.assertIn("device_event=%s", message)
        self.assertIn("photo_elapsed=%.3fs", message)
        self.assertIn("cloud_post_elapsed=%.3fs", message)
        self.assertIn("attempts=%s", message)
        self.assertIn("http_status=%s", message)
        self.assertIn("outcome=%s", message)
        self.assertIn("7-2026-06-01 07:00:00", fields)
        self.assertIn("success", fields)


if __name__ == "__main__":
    unittest.main()

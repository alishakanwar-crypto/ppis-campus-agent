import asyncio
import time
import unittest
from unittest.mock import patch

from fake_camera import JPEG

import main


DVR = {
    "ip": "192.0.2.91",
    "port": 80,
    "username": "admin",
    "password": "secret",
}
CAMERA = (DVR, 5, "G1A  C1")


class CameraMinimumAttemptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._camera_outcomes.clear()

    def tearDown(self):
        main._camera_outcomes.clear()

    async def test_a_held_up_request_still_asks_the_camera(self):
        """A camera answering in half a second must not be called silent."""
        async def slow_camera(dvr, channel, **kwargs):
            await asyncio.sleep(0.5)
            return JPEG

        # The budget ran out while the request waited for the agent's own work.
        token = main._live_request_deadline.set(time.monotonic() - 5)
        try:
            with patch.object(main, "capture_snapshot", slow_camera):
                result = await main._capture_classroom_camera(
                    "GRADE 1A", CAMERA,
                )
        finally:
            main._live_request_deadline.reset(token)

        self.assertIsNotNone(result)

    async def test_a_camera_that_stays_silent_is_still_given_up_on(self):
        async def dead_camera(dvr, channel, **kwargs):
            await asyncio.sleep(60)

        token = main._live_request_deadline.set(time.monotonic() - 5)
        try:
            with patch.object(
                main, "_SNAPSHOT_CAMERA_MIN_ATTEMPT_SECONDS", 0.05
            ):
                with patch.object(main, "capture_snapshot", dead_camera):
                    result = await main._capture_classroom_camera(
                        "GRADE 1A", CAMERA,
                    )
        finally:
            main._live_request_deadline.reset(token)

        self.assertIsNone(result)


class AgentLagReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._loop_lag_samples.clear()

    def tearDown(self):
        main._loop_lag_samples.clear()

    async def test_the_delay_the_agent_itself_causes_is_measured(self):
        """Or a slow morning gets blamed on the cameras with no evidence."""
        with patch.object(main, "_LOOP_LAG_SAMPLE_SECONDS", 0.01):
            watcher = asyncio.create_task(main.watch_event_loop_lag())
            await asyncio.sleep(0.05)
            watcher.cancel()

        health = main.event_loop_lag_health()
        self.assertGreater(health["samples"], 0)
        self.assertGreaterEqual(health["worst_seconds"], 0.0)

    def test_no_samples_yet_reads_as_no_delay(self):
        self.assertEqual(
            main.event_loop_lag_health(),
            {"worst_seconds": 0.0, "usual_seconds": 0.0, "samples": 0},
        )

"""A parent's camera time must start when the camera is actually asked.

Several parents asking for rooms on one recorder queue behind each other, and
the wait used to be charged to the camera: a request could spend its whole
8-second budget in the queue and be reported as a camera failure without ever
having asked a camera. Those are the bursts of "unable to capture" while the
same room answers in under a second on its own.
"""
import time
import unittest
from unittest.mock import patch

from fake_camera import JPEG

import main


class Response:
    def __init__(self, content: bytes):
        self.status_code = 200 if content else 404
        self.headers = {"content-type": "image/jpeg"} if content else {}
        self.content = content
        self.history = ()


DVR = {
    "ip": "192.0.2.71",
    "port": 80,
    "username": "admin",
    "password": "secret",
}


class QueuedCaptureBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_slow_doors.clear()
        main._live_capture_silent_channels.clear()
        main._live_capture_busy_silences.clear()
        main._live_capture_in_flight.clear()
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        main._isapi_last_success.clear()
        main._rtsp_cooldowns.clear()
        main._channel_auth_cooldowns.clear()

    async def test_a_queued_request_still_gets_to_ask_the_camera(self):
        asked: list[str] = []

        class Client:
            async def get(_self, url, auth):
                asked.append(url)
                return Response(JPEG)

        # The whole camera budget is already gone by the time the recorder's
        # limiter lets this capture through.
        spent = main._SNAPSHOT_CAMERA_TIMEOUT_SECONDS + 1.0

        async def queue(ip: str, background: bool):
            await main._dvr_limiter(ip)
            time_slept.append(spent)
            return main._dvr_capture_limiters[ip]

        time_slept: list[float] = []
        real_monotonic = time.monotonic

        def monotonic() -> float:
            return real_monotonic() + sum(time_slept)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()), \
                patch.object(main, "_acquire_dvr_capture", queue), \
                patch.object(main.time, "monotonic", monotonic):
            picture = await main.capture_snapshot(DVR, 4)

        self.assertEqual(picture, JPEG)
        self.assertTrue(asked)

    async def test_doors_denied_their_full_time_are_not_called_silent(self):
        """A squeezed request must not send the room down the slow road."""

        class Client:
            async def get(_self, url, auth):
                raise main.httpx.ReadTimeout("no answer")

        # Only a sliver of the request is left, so the doors get far less than
        # a normal attempt.
        deadline = time.monotonic() + main._RTSP_RESERVE_SECONDS + 1.0

        with patch.object(main, "_get_live_dvr_client", return_value=Client()), \
                patch.object(main, "_capture_snapshot_rtsp", return_value=None):
            self.assertIsNone(
                await main.capture_snapshot(DVR, 5, deadline=deadline)
            )

        self.assertFalse(main._channel_doors_silent(DVR["ip"], 5))
        self.assertEqual(main._live_capture_busy_silences[(DVR["ip"], 5)], 1)

    async def test_a_genuinely_dead_channel_is_still_shortcut(self):
        """Forgiveness is bounded: a dead door cannot cost every parent a wait."""

        class Client:
            async def get(_self, url, auth):
                raise main.httpx.ReadTimeout("no answer")

        with patch.object(main, "_get_live_dvr_client", return_value=Client()), \
                patch.object(main, "_capture_snapshot_rtsp", return_value=None):
            for _ in range(main._LIVE_CAPTURE_BUSY_FORGIVENESS):
                await main.capture_snapshot(
                    DVR,
                    6,
                    deadline=time.monotonic() + main._RTSP_RESERVE_SECONDS + 1.0,
                )

        self.assertTrue(main._channel_doors_silent(DVR["ip"], 6))


if __name__ == "__main__":
    unittest.main()

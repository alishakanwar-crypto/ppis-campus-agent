import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fake_camera import JPEG

import main
from attendance_engine import AttendanceEngine

DVR = {"ip": "192.0.2.77", "port": 80, "username": "admin", "password": "x"}


class ScannerCooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._reset()
        self.engine = AttendanceEngine()

    def tearDown(self):
        self._reset()

    def _reset(self):
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        main._refused_credentials.clear()
        main._auth_refused_since_ist.clear()
        main._isapi_last_success.clear()
        main._channel_auth_cooldowns.clear()
        main._last_auth_attempt.clear()
        main._auth_unlock_next_probe.clear()
        main._auth_unlock_quiet.clear()

    async def test_scanner_leaves_a_locked_out_recorder_alone(self):
        main._isapi_cooldowns[DVR["ip"]] = (
            time.monotonic() + 600, "credentials refused"
        )
        client = AsyncMock()
        with patch.object(
            self.engine, "_get_dvr_client", return_value=client
        ), patch.object(
            main, "_capture_snapshot_rtsp", AsyncMock(return_value=b"rtsp")
        ):
            frame = await self.engine.capture_frame_from_dvr(DVR, 14)

        self.assertEqual(frame, b"rtsp")
        client.get.assert_not_called()

    async def test_a_refused_login_puts_the_recorder_on_cooldown(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(401)
        with patch.object(
            self.engine, "_get_dvr_client", return_value=client
        ), patch(
            "attendance_engine._probe_channel_resolution",
            AsyncMock(return_value=None),
        ):
            frame = await self.engine.capture_frame_from_dvr(DVR, 14)

        self.assertIsNone(frame)
        self.assertEqual(client.get.await_count, 1)
        self.assertEqual(
            main._isapi_cooldown(DVR["ip"]), "credentials refused"
        )

    async def test_a_working_recorder_is_scanned_normally(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )
        with patch.object(
            self.engine, "_get_dvr_client", return_value=client
        ), patch(
            "attendance_engine._probe_channel_resolution",
            AsyncMock(return_value=None),
        ):
            frame = await self.engine.capture_frame_from_dvr(DVR, 14)

        self.assertEqual(frame, JPEG)
        self.assertEqual(main._isapi_cooldown(DVR["ip"]), "")

    async def test_one_refusing_camera_does_not_pause_a_serving_recorder(self):
        main._note_isapi_success(DVR["ip"])
        client = AsyncMock()
        client.get.return_value = httpx.Response(401)
        with patch.object(
            self.engine, "_get_dvr_client", return_value=client
        ), patch(
            "attendance_engine._probe_channel_resolution",
            AsyncMock(return_value=None),
        ), patch.object(
            main, "_capture_snapshot_rtsp", AsyncMock(return_value=None)
        ):
            await self.engine.capture_frame_from_dvr(DVR, 14)

        self.assertEqual(main._isapi_cooldown(DVR["ip"]), "")
        self.assertTrue(main._channel_auth_refused(DVR["ip"], 14))

    async def test_the_sweep_stops_knocking_on_a_resting_camera(self):
        main._channel_auth_cooldowns[(DVR["ip"], 14)] = (
            time.monotonic() + 600
        )
        client = AsyncMock()
        with patch.object(
            self.engine, "_get_dvr_client", return_value=client
        ), patch.object(
            main, "_capture_snapshot_rtsp", AsyncMock(return_value=b"rtsp")
        ):
            frame = await self.engine.capture_frame_from_dvr(DVR, 14)

        self.assertEqual(frame, b"rtsp")
        client.get.assert_not_called()

    async def test_the_sweep_records_that_it_touched_the_recorder(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )
        with patch.object(
            self.engine, "_get_dvr_client", return_value=client
        ), patch(
            "attendance_engine._probe_channel_resolution",
            AsyncMock(return_value=None),
        ):
            await self.engine.capture_frame_from_dvr(DVR, 14)

        self.assertIn(DVR["ip"], main._last_auth_attempt)
        self.assertIn(DVR["ip"], main._isapi_last_success)


if __name__ == "__main__":
    unittest.main()

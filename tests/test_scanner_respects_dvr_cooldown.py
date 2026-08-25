import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import main
from attendance_engine import AttendanceEngine

DVR = {"ip": "192.0.2.77", "port": 80, "username": "admin", "password": "x"}


class ScannerCooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        self.engine = AttendanceEngine()

    def tearDown(self):
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()

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
            200, content=b"jpeg", headers={"content-type": "image/jpeg"}
        )
        with patch.object(
            self.engine, "_get_dvr_client", return_value=client
        ), patch(
            "attendance_engine._probe_channel_resolution",
            AsyncMock(return_value=None),
        ):
            frame = await self.engine.capture_frame_from_dvr(DVR, 14)

        self.assertEqual(frame, b"jpeg")
        self.assertEqual(main._isapi_cooldown(DVR["ip"]), "")


if __name__ == "__main__":
    unittest.main()

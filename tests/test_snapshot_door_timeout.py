import asyncio
import io
import unittest
from unittest.mock import patch

from PIL import Image

import main


def jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 90, 60)).save(buf, format="JPEG")
    return buf.getvalue()


class Response:
    def __init__(self, content: bytes):
        self.status_code = 200 if content else 404
        self.headers = {"content-type": "image/jpeg"} if content else {}
        self.content = content or b""


class SnapshotDoorTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_size_logged.clear()
        main._live_capture_slow_doors.clear()
        main._isapi_cooldowns.clear()
        main._isapi_consecutive_timeouts.clear()
        main._isapi_last_success.clear()
        main._rtsp_cooldowns.clear()
        main._live_capture_silent_channels.clear()
        self.dvr = {
            "ip": "192.0.2.70",
            "port": 80,
            "username": "admin",
            "password": "password",
        }

    def _client(self, hanging: str, picture: bytes, requested: list[str]):
        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                if hanging in url:
                    await asyncio.sleep(30)
                return Response(picture)

        return Client()

    async def test_a_silent_door_is_abandoned_for_the_next_one(self):
        """The full-size request hanging must not cost the whole attempt."""
        requested: list[str] = []
        picture = jpeg(1280, 720)
        with patch.object(main, "_LIVE_CAPTURE_DOOR_TIMEOUT_SECONDS", 0.2), \
                patch.object(
                    main.httpx,
                    "AsyncClient",
                    return_value=self._client(
                        "videoResolutionWidth", picture, requested
                    ),
                ):
            started = asyncio.get_running_loop().time()
            self.assertEqual(await main.capture_snapshot(self.dvr, 4), picture)
            elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, main._SNAPSHOT_HTTP_TIMEOUT_SECONDS)
        self.assertGreaterEqual(len(requested), 2)
        self.assertIn("videoResolutionWidth", requested[0])

    async def test_the_silent_door_is_tried_last_next_time(self):
        """A door that never answers must not be knocked on first again."""
        requested: list[str] = []
        picture = jpeg(1280, 720)
        with patch.object(main, "_LIVE_CAPTURE_DOOR_TIMEOUT_SECONDS", 0.2), \
                patch.object(
                    main.httpx,
                    "AsyncClient",
                    return_value=self._client(
                        "videoResolutionWidth", picture, requested
                    ),
                ):
            await main.capture_snapshot(self.dvr, 4)
            requested.clear()
            self.assertEqual(await main.capture_snapshot(self.dvr, 4), picture)

        self.assertEqual(len(requested), 1)
        self.assertNotIn("videoResolutionWidth", requested[0])

    async def test_a_recorder_answering_nothing_still_counts_as_a_timeout(self):
        """Doors timing out one by one must still put the recorder on cooldown."""
        requested: list[str] = []

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                await asyncio.sleep(30)

        with patch.object(main, "_LIVE_CAPTURE_DOOR_TIMEOUT_SECONDS", 0.05), \
                patch.object(main, "_SNAPSHOT_RETRY_BACKOFF_SECONDS", 0.0), \
                patch.object(main, "_ISAPI_TIMEOUTS_BEFORE_BACKOFF", 1), \
                patch.object(main, "_capture_snapshot_rtsp", return_value=None), \
                patch.object(main.httpx, "AsyncClient", return_value=Client()):
            self.assertIsNone(await main.capture_snapshot(self.dvr, 4))

        self.assertEqual(main._isapi_cooldown(self.dvr["ip"]), "not answering")

    async def test_the_scanner_sweep_also_reaches_the_next_door(self):
        """A background attempt is shorter than the door budget, so the budget
        must shrink to fit or a silent first door costs every later one."""
        requested: list[str] = []
        picture = jpeg(1280, 720)
        with patch.object(
            main.httpx,
            "AsyncClient",
            return_value=self._client("videoResolutionWidth", picture, requested),
        ):
            self.assertEqual(
                await main.capture_snapshot(self.dvr, 4, background=True), picture
            )

        self.assertGreaterEqual(len(requested), 2)

    async def test_a_door_is_forgiven_once_it_answers_again(self):
        """A recorder that was merely busy must not be avoided forever."""
        requested: list[str] = []
        picture = jpeg(1280, 720)
        main._live_capture_slow_doors[(self.dvr["ip"], 4)] = {
            0: main.time.monotonic()
        }

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                # Only the full-size door serves a picture now.
                return Response(picture if "videoResolution" in url else b"")

        with patch.object(main, "_SNAPSHOT_RETRY_BACKOFF_SECONDS", 0.0), \
                patch.object(main.httpx, "AsyncClient", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(self.dvr, 4), picture)

        self.assertNotIn(
            0, main._live_capture_slow_doors.get((self.dvr["ip"], 4), {})
        )


class LiveCaptureTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_slow_doors.clear()
        main._live_capture_silent_channels.clear()

    async def test_the_capture_timing_travels_with_the_photo(self):
        """The cloud cannot read the campus PC's log, so timing rides along."""
        picture = jpeg(1920, 1080)

        class Client:
            async def get(_self, url, auth):
                return Response(picture)

        report: dict = {}
        main._live_capture_report.set(report)
        dvr = {
            "ip": "192.0.2.71",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main.httpx, "AsyncClient", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(dvr, 7), picture)

        self.assertEqual(report["recorder"], "192.0.2.71")
        self.assertEqual(report["channel"], 7)
        self.assertEqual(report["door_timeouts"], 0)
        self.assertFalse(report["rtsp"])
        self.assertGreaterEqual(report["seconds"], 0.0)
        self.assertIn("slot_wait_seconds", report)
        self.assertIn("attempt_seconds", report)


if __name__ == "__main__":
    unittest.main()

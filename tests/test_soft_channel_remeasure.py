import asyncio
import io
import unittest
from unittest.mock import patch

from PIL import Image

import main


def jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (90, 140, 200)).save(
        buf, format="JPEG", quality=80
    )
    return buf.getvalue()


class SoftChannelRemeasureTests(unittest.IsolatedAsyncioTestCase):
    """A channel stuck on a sub-stream door is what parents call blurred."""

    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_size_logged.clear()
        main._live_capture_slow_doors.clear()
        main._live_capture_soft_remeasure_at.clear()

    def _client(self, pictures: dict[str, bytes], requested: list[str]):
        class Response:
            def __init__(self, content):
                self.status_code = 200 if content else 404
                self.headers = (
                    {"content-type": "image/jpeg"} if content else {}
                )
                self.content = content or b""

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                for marker, content in pictures.items():
                    if marker in url:
                        return Response(content)
                return Response(b"")

        return Client()

    async def test_a_soft_photo_makes_the_agent_measure_the_channel(self):
        """The parent keeps their photo; the sharp door is found behind it."""
        requested: list[str] = []
        tiny = jpeg(704, 480)
        sharp = jpeg(1920, 1080)
        pictures = {"/302/picture": tiny, "videoResolutionWidth": sharp}
        dvr = {
            "ip": "192.0.2.70",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        key = ("192.0.2.70", 3)
        main._live_capture_preferences[key] = ("digest", 2)
        main._live_capture_preference_age[key] = main.time.monotonic()
        # The full-size door is resting, so the request itself cannot try it.
        main._live_capture_slow_doors[key] = {0: main.time.monotonic()}
        with patch.object(main, "_save_capture_doors"), patch.object(
            main, "_LIVE_CAPTURE_SOFT_REMEASURE_DELAY_SECONDS", 0.0
        ), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=self._client(pictures, requested),
        ):
            self.assertEqual(await main.capture_snapshot(dvr, 3), tiny)
            await asyncio.sleep(0.05)

        self.assertEqual(main._live_capture_preferences[key], ("digest", 0))
        self.assertEqual(main._live_capture_best_pixels[key], 1920 * 1080)

    async def test_a_sharp_photo_is_not_measured_again(self):
        """Nothing to learn, so the recorder is left alone."""
        requested: list[str] = []
        sharp = jpeg(1920, 1080)
        dvr = {
            "ip": "192.0.2.71",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main, "_save_capture_doors"), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=self._client({"picture": sharp}, requested),
        ):
            self.assertEqual(await main.capture_snapshot(dvr, 3), sharp)

        self.assertEqual(main._live_capture_soft_remeasure_at, {})

    async def test_the_channel_is_measured_once_in_the_quiet_period(self):
        """Every parent's soft photo must not start its own measurement."""
        dvr = {
            "ip": "192.0.2.72",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        started: list[tuple[str, int]] = []

        async def remeasure(recorder, channel):
            started.append((recorder["ip"], channel))

        with patch.object(main, "_remeasure_soft_channel", remeasure):
            main._schedule_soft_channel_remeasure(dvr, 3)
            main._schedule_soft_channel_remeasure(dvr, 3)
            await asyncio.sleep(0.05)

        self.assertEqual(started, [("192.0.2.72", 3)])

    async def test_a_recorder_that_refused_our_login_is_not_measured(self):
        """A measurement must never re-arm a recorder's lockout."""
        requested: list[str] = []
        dvr = {
            "ip": "192.0.2.73",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main, "_credentials_refused", return_value=True), \
                patch.object(
                    main, "_LIVE_CAPTURE_SOFT_REMEASURE_DELAY_SECONDS", 0.0
                ), patch.object(
                    main.httpx,
                    "AsyncClient",
                    return_value=self._client(
                        {"picture": jpeg(704, 480)}, requested
                    ),
                ):
            await main._remeasure_soft_channel(dvr, 3)

        self.assertEqual(requested, [])


if __name__ == "__main__":
    unittest.main()

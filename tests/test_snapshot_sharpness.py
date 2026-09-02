import asyncio
import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

import main


def jpeg(width: int, height: int, quality: int = 80) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (90, 140, 200)).save(
        buf, format="JPEG", quality=quality
    )
    return buf.getvalue()


class SnapshotSharpnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_size_logged.clear()

    def _client(self, pictures: dict[str, bytes], requested: list[str]):
        class Response:
            def __init__(self, content):
                self.status_code = 200 if content else 404
                self.headers = {"content-type": "image/jpeg"} if content else {}
                self.content = content or b""

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                for marker, content in pictures.items():
                    if marker in url:
                        return Response(content)
                return Response(b"")

        return Client()

    async def test_the_nightly_measurement_finds_the_sharpest_door(self):
        """A 720p answer on one door does not settle it if another serves 1080p.

        Comparing doors happens in the nightly measurement, and every parent's
        request afterwards goes straight to the door it settled on.
        """
        requested: list[str] = []
        soft = jpeg(1280, 720)
        sharp = jpeg(1920, 1080)
        pictures = {"videoResolutionWidth": soft, "/301/picture": sharp}
        dvr = {
            "ip": "192.0.2.50",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(
            main.httpx, "AsyncClient", return_value=self._client(pictures, requested)
        ):
            main._measuring_cameras.set(True)
            try:
                self.assertEqual(
                    await main.capture_snapshot(dvr, 3, background=True), sharp
                )
            finally:
                main._measuring_cameras.set(False)
            requested.clear()
            # The sharp door is remembered, so the soft one is not tried again.
            self.assertEqual(await main.capture_snapshot(dvr, 3), sharp)

        self.assertEqual(len(requested), 1)
        self.assertNotIn("videoResolutionWidth", requested[0])

    async def test_a_soft_picture_from_the_day_does_not_settle_the_measurement(self):
        """A parent's soft photo must not stop the night finding the sharp door."""
        requested: list[str] = []
        soft = jpeg(1280, 720)
        sharp = jpeg(1920, 1080)
        pictures = {"videoResolutionWidth": soft, "/301/picture": sharp}
        dvr = {
            "ip": "192.0.2.55",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(
            main.httpx, "AsyncClient", return_value=self._client(pictures, requested)
        ):
            # During the day the first whole picture wins, soft as it is.
            self.assertEqual(await main.capture_snapshot(dvr, 3), soft)
            main._live_capture_preferences.clear()
            main._live_capture_preference_age.clear()
            main._measuring_cameras.set(True)
            try:
                self.assertEqual(
                    await main.capture_snapshot(dvr, 3, background=True), sharp
                )
            finally:
                main._measuring_cameras.set(False)

    async def test_a_parents_request_takes_one_door_only(self):
        """No comparing pictures while a parent waits: the first whole one wins."""
        requested: list[str] = []
        soft = jpeg(1280, 720)
        sharp = jpeg(1920, 1080)
        pictures = {"videoResolutionWidth": soft, "/301/picture": sharp}
        dvr = {
            "ip": "192.0.2.54",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(
            main.httpx, "AsyncClient", return_value=self._client(pictures, requested)
        ):
            self.assertEqual(await main.capture_snapshot(dvr, 3), soft)

        self.assertEqual(len(requested), 1)

    async def test_a_720p_camera_is_measured_once_and_then_trusted(self):
        requested: list[str] = []
        soft = jpeg(1280, 720)
        dvr = {
            "ip": "192.0.2.51",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(
            main.httpx,
            "AsyncClient",
            return_value=self._client({"picture": soft}, requested),
        ), self.assertLogs(main.logger, level="WARNING") as logs:
            self.assertEqual(await main.capture_snapshot(dvr, 3), soft)
            probes = len(requested)
            requested.clear()
            self.assertEqual(await main.capture_snapshot(dvr, 3), soft)

        self.assertLessEqual(probes, main._LIVE_CAPTURE_PROBE_PICTURES)
        self.assertEqual(len(requested), 1)
        self.assertTrue(
            any("look soft" in line for line in logs.output), logs.output
        )

    async def test_a_probe_that_hangs_does_not_lose_the_picture_in_hand(self):
        """A slow second door must not cost the parent the soft picture we have."""
        requested: list[str] = []
        soft = jpeg(1280, 720)

        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = soft

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                if len(requested) > 1:
                    await asyncio.sleep(30)
                return Response()

        dvr = {
            "ip": "192.0.2.52",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main, "_LIVE_CAPTURE_PROBE_BUDGET_SECONDS", 0.2), \
                patch.object(main.httpx, "AsyncClient", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(dvr, 3), soft)

    async def test_probing_stops_once_the_budget_is_spent(self):
        """A recorder that answers slowly is not probed a second time."""
        requested: list[str] = []
        soft = jpeg(1280, 720)

        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = soft

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                await asyncio.sleep(0.3)
                return Response()

        dvr = {
            "ip": "192.0.2.53",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main, "_LIVE_CAPTURE_PROBE_BUDGET_SECONDS", 0.2), \
                patch.object(main.httpx, "AsyncClient", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(dvr, 3), soft)

        self.assertEqual(len(requested), 1)

    async def test_a_parents_picture_is_not_squeezed_to_a_thumbnail(self):
        picture = jpeg(1920, 1080, quality=95)
        kept = main.compress_jpeg(
            picture,
            max_bytes=main._LIVE_SNAPSHOT_MAX_BYTES,
            quality_start=main._LIVE_SNAPSHOT_JPEG_QUALITY,
        )
        self.assertEqual(kept, picture)
        self.assertGreaterEqual(main._LIVE_SNAPSHOT_JPEG_QUALITY, 90)
        self.assertGreater(main._LIVE_SNAPSHOT_MAX_BYTES, 1_000_000)

    async def test_a_huge_picture_is_still_kept_within_whatsapps_limit(self):
        noise = Image.frombytes("RGB", (4000, 3000), os.urandom(4000 * 3000 * 3))
        buf = io.BytesIO()
        noise.save(buf, format="JPEG", quality=95)
        picture = buf.getvalue()
        self.assertGreater(len(picture), main._LIVE_SNAPSHOT_MAX_BYTES)
        smaller = main.compress_jpeg(
            picture,
            max_bytes=main._LIVE_SNAPSHOT_MAX_BYTES,
            quality_start=main._LIVE_SNAPSHOT_JPEG_QUALITY,
        )
        self.assertLessEqual(len(smaller), main._LIVE_SNAPSHOT_MAX_BYTES)
        with Image.open(io.BytesIO(smaller)) as img:
            self.assertLessEqual(max(img.size), 1920)


if __name__ == "__main__":
    unittest.main()

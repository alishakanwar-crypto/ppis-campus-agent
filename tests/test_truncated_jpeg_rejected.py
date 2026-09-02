"""A half-arrived picture must never reach a parent.

A recorder can close the connection mid-picture. The bytes still open like a
JPEG and their header still reports a size, so the agent used to hand a
parent a photo whose lower half was smeared streaks.
"""

import io
import unittest
from unittest.mock import patch

from PIL import Image

import main


def _jpeg(width: int = 320, height: int = 240) -> bytes:
    img = Image.new("RGB", (width, height))
    for x in range(width):
        for y in range(0, height, 3):
            img.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


class TruncatedJpegTests(unittest.TestCase):
    def test_accepts_a_whole_picture(self):
        self.assertTrue(main._jpeg_is_complete(_jpeg()))

    def test_rejects_a_picture_that_stopped_halfway(self):
        whole = _jpeg()
        half = whole[: len(whole) // 2]
        # It still looks like a JPEG and still reports its size, which is why
        # the smeared photo went out unnoticed.
        self.assertEqual(main._jpeg_size(half), (320, 240))
        self.assertFalse(main._jpeg_is_complete(half))

    def test_rejects_a_picture_missing_only_its_ending(self):
        whole = _jpeg()
        self.assertFalse(main._jpeg_is_complete(whole[:-2]))

    def test_rejects_an_empty_answer(self):
        self.assertFalse(main._jpeg_is_complete(b""))

    def test_rejects_bytes_that_are_not_a_picture(self):
        """An error page, or a JPEG cut before its header finished."""
        self.assertFalse(main._jpeg_is_complete(b"<html>not a picture</html>"))
        self.assertFalse(main._jpeg_is_complete(_jpeg()[:8]))

    def test_rejects_a_jpeg_whose_middle_was_lost(self):
        whole = _jpeg()
        broken = whole[:400] + whole[-2:]
        self.assertFalse(main._jpeg_is_complete(broken))


class Response:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.headers = {"content-type": "image/jpeg"}
        self.content = content


class CaptureRejectsTruncatedPictureTests(unittest.IsolatedAsyncioTestCase):
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
            "ip": "192.0.2.72",
            "port": 80,
            "username": "admin",
            "password": "password",
        }

    async def test_a_half_served_picture_sends_us_to_the_next_door(self):
        whole = _jpeg(1280, 720)
        half = whole[: len(whole) // 2]
        requested: list[str] = []

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                # The first door keeps cutting its picture short.
                return Response(half if len(requested) == 1 else whole)

        with patch.object(main, "_SNAPSHOT_RETRY_BACKOFF_SECONDS", 0.0), \
                patch.object(main.httpx, "AsyncClient", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(self.dvr, 4), whole)

        self.assertGreaterEqual(len(requested), 2)

    async def test_nothing_is_returned_when_every_door_cuts_the_picture(self):
        whole = _jpeg(1280, 720)
        half = whole[: len(whole) // 2]

        class Client:
            async def get(_self, url, auth):
                return Response(half)

        with patch.object(main, "_SNAPSHOT_RETRY_BACKOFF_SECONDS", 0.0), \
                patch.object(main, "_capture_snapshot_rtsp", return_value=None), \
                patch.object(main.httpx, "AsyncClient", return_value=Client()):
            self.assertIsNone(await main.capture_snapshot(self.dvr, 4))


if __name__ == "__main__":
    unittest.main()

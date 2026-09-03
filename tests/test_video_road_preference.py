import io
import unittest
from unittest.mock import patch

from PIL import Image

import main


def jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (80, 130, 190)).save(
        buf, format="JPEG", quality=80
    )
    return buf.getvalue()


class VideoRoadPreferenceTests(unittest.IsolatedAsyncioTestCase):
    """A channel whose only door is its sub-stream must not stay at 704x480."""

    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_size_logged.clear()
        main._live_capture_slow_doors.clear()
        main._live_capture_video_pixels.clear()
        main._live_capture_soft_remeasure_at.clear()
        main._rtsp_cooldowns.clear()
        main._rtsp_channel_cooldowns.clear()
        main._rtsp_credentials_worked.clear()

    def _dvr(self, ip: str) -> dict:
        return {
            "ip": ip,
            "port": 80,
            "username": "admin",
            "password": "password",
        }

    def _client(self, picture: bytes, requested: list[str]):
        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = picture

        class Client:
            async def get(_self, url, auth):
                requested.append(url)
                return Response()

        return Client()

    async def test_the_stream_is_taken_when_a_door_serves_a_quarter(self):
        """704x480 from a door against 720p on the stream: take the stream."""
        requested: list[str] = []
        tiny = jpeg(704, 480)
        frame = jpeg(1280, 720)
        dvr = self._dvr("192.0.2.80")
        key = ("192.0.2.80", 3)
        main._live_capture_preferences[key] = ("digest", 3)
        main._live_capture_preference_age[key] = main.time.monotonic()

        async def rtsp(recorder, channel, background=False):
            return frame

        with patch.object(main, "_save_capture_doors"), patch.object(
            main, "_capture_snapshot_rtsp", rtsp
        ), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=self._client(tiny, requested),
        ):
            # The first request learns both sizes: door 704x480, stream 720p.
            self.assertEqual(await main.capture_snapshot(dvr, 3), tiny)
            self.assertIsNone(main._live_capture_video_pixels.get(key))
            main._live_capture_video_pixels[key] = 1280 * 720
            requested.clear()
            self.assertEqual(await main.capture_snapshot(dvr, 3), frame)

        self.assertEqual(requested, [])

    async def test_the_stream_size_is_learned_without_a_parent_waiting(self):
        """A working small door must not hide the stream's bigger picture."""
        requested: list[str] = []
        tiny = jpeg(704, 480)
        frame = jpeg(1280, 720)
        dvr = self._dvr("192.0.2.85")
        key = ("192.0.2.85", 3)
        main._live_capture_preferences[key] = ("digest", 3)
        main._live_capture_preference_age[key] = main.time.monotonic()

        async def rtsp(recorder, channel, background=False):
            return frame

        with patch.object(main, "_save_capture_doors"), patch.object(
            main, "_LIVE_CAPTURE_SOFT_REMEASURE_DELAY_SECONDS", 0.0
        ), patch.object(
            main, "_capture_snapshot_rtsp", rtsp
        ), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=self._client(tiny, requested),
        ):
            # The parent gets the quarter-size picture the door hands over,
            # and the background work then samples the stream.
            self.assertEqual(await main.capture_snapshot(dvr, 3), tiny)
            for _ in range(20):
                await main.asyncio.sleep(0.01)
                if key in main._live_capture_video_pixels:
                    break
            self.assertEqual(
                main._live_capture_video_pixels.get(key), 1280 * 720
            )
            requested.clear()
            self.assertEqual(await main.capture_snapshot(dvr, 3), frame)

        self.assertEqual(requested, [])

    async def test_a_door_serving_the_wanted_size_keeps_the_stream_out(self):
        """The stream costs seconds, so it is not taken for nothing."""
        key = ("192.0.2.81", 3)
        main._live_capture_best_pixels[key] = 1280 * 720
        main._live_capture_video_pixels[key] = 1280 * 720
        self.assertFalse(main._video_road_is_sharper(*key))

    async def test_a_stream_no_sharper_than_the_door_is_not_taken(self):
        key = ("192.0.2.82", 3)
        main._live_capture_best_pixels[key] = 704 * 480
        main._live_capture_video_pixels[key] = 704 * 480
        self.assertFalse(main._video_road_is_sharper(*key))
        main._live_capture_video_pixels[key] = 1280 * 720
        self.assertTrue(main._video_road_is_sharper(*key))

    async def test_a_streams_frame_size_is_remembered(self):
        main._note_video_frame_size("192.0.2.83", 4, jpeg(1280, 720))
        main._note_video_frame_size("192.0.2.83", 4, jpeg(704, 480))
        self.assertEqual(
            main._live_capture_video_pixels[("192.0.2.83", 4)], 1280 * 720
        )

    async def test_a_resting_stream_does_not_stop_the_doors_being_used(self):
        """A picture in hand beats a road that just failed."""
        requested: list[str] = []
        tiny = jpeg(704, 480)
        dvr = self._dvr("192.0.2.84")
        key = ("192.0.2.84", 3)
        main._live_capture_preferences[key] = ("digest", 3)
        main._live_capture_preference_age[key] = main.time.monotonic()
        main._live_capture_best_pixels[key] = 704 * 480
        main._live_capture_video_pixels[key] = 1280 * 720
        main._mark_rtsp_failure("192.0.2.84", 3)

        with patch.object(main, "_save_capture_doors"), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=self._client(tiny, requested),
        ):
            self.assertEqual(await main.capture_snapshot(dvr, 3), tiny)

        self.assertTrue(requested)


if __name__ == "__main__":
    unittest.main()

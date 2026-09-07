import io
import unittest
from unittest.mock import patch

from PIL import Image

import main


def jpeg(width: int, height: int, shade: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (shade, 130, 190)).save(
        buf, format="JPEG", quality=80
    )
    return buf.getvalue()


DVR = {
    "ip": "192.0.2.90",
    "port": 80,
    "username": "admin",
    "password": "password",
}


class FreshStreamFrameTests(unittest.IsolatedAsyncioTestCase):
    """A recorder with dead doors opens few streams; none may be wasted."""

    def setUp(self):
        main._dvr_capture_limiters.clear()
        main._live_dvr_clients.clear()
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()
        main._live_capture_best_pixels.clear()
        main._live_capture_size_logged.clear()
        main._live_capture_slow_doors.clear()
        main._live_capture_silent_channels.clear()
        main._live_capture_busy_silences.clear()
        main._live_capture_video_pixels.clear()
        main._live_capture_soft_remeasure_at.clear()
        main._live_capture_in_flight.clear()
        main._live_capture_fresh_frames.clear()
        main._isapi_cooldowns.clear()
        main._rtsp_cooldowns.clear()
        main._rtsp_channel_cooldowns.clear()
        main._rtsp_credentials_worked.clear()

    def _dead_doors(self):
        class Client:
            async def get(_self, url, auth):
                raise main.httpx.ConnectTimeout("no answer")

            async def aclose(_self):
                return None

        return Client()

    async def test_a_second_parent_gets_the_stream_picture_just_taken(self):
        """The recorder is asked once; the next request costs it no slot."""
        frame = jpeg(1280, 720, 80)
        streams = 0

        async def rtsp(recorder, channel, background=False):
            nonlocal streams
            streams += 1
            return frame

        with patch.object(main, "_save_capture_doors"), patch.object(
            main, "_capture_snapshot_rtsp", rtsp
        ), patch.object(
            main.httpx, "AsyncClient", return_value=self._dead_doors()
        ), patch.object(
            main, "_get_live_dvr_client", return_value=self._dead_doors()
        ):
            self.assertEqual(await main.capture_snapshot(DVR, 5), frame)
            self.assertEqual(await main.capture_snapshot(DVR, 5), frame)

        self.assertEqual(streams, 1)

    async def test_a_picture_older_than_the_window_is_not_served(self):
        """Reuse is measured in seconds, so nobody is shown a stale room."""
        stale = jpeg(1280, 720, 10)
        current = jpeg(1280, 720, 200)
        key = (DVR["ip"], 6)
        main._live_capture_fresh_frames[key] = (
            main.time.monotonic() - main._LIVE_CAPTURE_FRESH_SECONDS - 1,
            stale,
        )

        async def rtsp(recorder, channel, background=False):
            return current

        with patch.object(main, "_save_capture_doors"), patch.object(
            main, "_capture_snapshot_rtsp", rtsp
        ), patch.object(
            main.httpx, "AsyncClient", return_value=self._dead_doors()
        ), patch.object(
            main, "_get_live_dvr_client", return_value=self._dead_doors()
        ):
            self.assertEqual(await main.capture_snapshot(DVR, 6), current)

    async def test_the_classroom_scanner_never_gets_a_reused_picture(self):
        """Attendance must recognise faces on the frame of its own sweep.

        The scanner takes no video stream of its own, so it comes back with
        nothing here rather than with the picture a parent was just served.
        """
        frame = jpeg(1280, 720, 80)

        async def rtsp(recorder, channel, background=False):
            return None if background else frame

        with patch.object(main, "_save_capture_doors"), patch.object(
            main, "_capture_snapshot_rtsp", rtsp
        ), patch.object(
            main.httpx, "AsyncClient", return_value=self._dead_doors()
        ), patch.object(
            main, "_get_live_dvr_client", return_value=self._dead_doors()
        ):
            self.assertEqual(await main.capture_snapshot(DVR, 7), frame)
            self.assertIsNone(
                await main.capture_snapshot(DVR, 7, background=True)
            )

    async def test_a_working_doors_picture_is_never_reused(self):
        """A door has no stream limit, so every parent gets it live."""
        picture = jpeg(1920, 1080, 80)
        asked = 0

        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = picture

        class Client:
            async def get(_self, url, auth):
                nonlocal asked
                asked += 1
                return Response()

            async def aclose(_self):
                return None

        with patch.object(main, "_save_capture_doors"), patch.object(
            main.httpx, "AsyncClient", return_value=Client()
        ), patch.object(main, "_get_live_dvr_client", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(DVR, 8), picture)
            self.assertEqual(await main.capture_snapshot(DVR, 8), picture)

        self.assertEqual(asked, 2)


if __name__ == "__main__":
    unittest.main()

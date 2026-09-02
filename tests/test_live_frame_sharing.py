import asyncio
import unittest
from unittest.mock import patch

import main

PICTURE = b"\xff\xd8jpeg\xff\xd9"


class Response:
    def __init__(self, content: bytes):
        self.status_code = 200
        self.headers = {"content-type": "image/jpeg"}
        self.content = content
        self.history = ()


DVR = {
    "ip": "192.0.2.71",
    "port": 80,
    "username": "admin",
    "password": "secret",
}


def _clear() -> None:
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


class SharedLiveCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear()

    async def test_parents_asking_together_share_one_capture(self):
        """Five parents of one class must not queue five captures."""
        started = 0

        class Client:
            async def get(_self, url, auth):
                nonlocal started
                started += 1
                await asyncio.sleep(0.05)
                return Response(PICTURE)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()):
            pictures = await asyncio.gather(*[
                main.capture_snapshot(DVR, 5) for _ in range(5)
            ])

        self.assertEqual(pictures, [PICTURE] * 5)
        self.assertEqual(started, 1)

    async def test_a_later_request_gets_its_own_fresh_picture(self):
        """Sharing lasts only while a capture runs; nothing stale is served."""
        calls = 0

        class Client:
            async def get(_self, url, auth):
                nonlocal calls
                calls += 1
                return Response(PICTURE)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()):
            self.assertEqual(await main.capture_snapshot(DVR, 6), PICTURE)
            self.assertEqual(await main.capture_snapshot(DVR, 6), PICTURE)

        self.assertEqual(calls, 2)
        self.assertNotIn((DVR["ip"], 6), main._live_capture_in_flight)

    async def test_the_classroom_scanner_captures_for_itself(self):
        """Attendance must recognise faces on its own sweep's frame."""
        sweeping = asyncio.Event()
        calls = 0

        class Client:
            async def get(_self, url, auth):
                nonlocal calls
                calls += 1
                sweeping.set()
                await asyncio.sleep(0.05)
                return Response(PICTURE)

            async def aclose(_self):
                return None

        with patch.object(main.httpx, "AsyncClient", return_value=Client()), \
                patch.object(main, "_get_live_dvr_client", return_value=Client()):
            sweep = asyncio.create_task(
                main.capture_snapshot(DVR, 8, background=True)
            )
            await sweeping.wait()
            self.assertEqual(await main.capture_snapshot(DVR, 8), PICTURE)
            self.assertEqual(await sweep, PICTURE)

        self.assertEqual(calls, 2)

    async def test_a_parent_arriving_after_the_first_gave_up_still_joins(self):
        """The first request timing out must not orphan its running capture."""
        calls = 0

        class Client:
            async def get(_self, url, auth):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.1)
                return Response(PICTURE)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()):
            gave_up = asyncio.create_task(main.capture_snapshot(DVR, 10))
            await asyncio.sleep(0.01)
            gave_up.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await gave_up
            self.assertEqual(await main.capture_snapshot(DVR, 10), PICTURE)

        self.assertEqual(calls, 1)

    async def test_a_timed_out_parent_does_not_cancel_the_shared_capture(self):
        class Client:
            async def get(_self, url, auth):
                await asyncio.sleep(0.2)
                return Response(PICTURE)

        with patch.object(main, "_get_live_dvr_client", return_value=Client()):
            waiter = asyncio.create_task(main.capture_snapshot(DVR, 9))
            await asyncio.sleep(0.01)
            giving_up = asyncio.create_task(main.capture_snapshot(DVR, 9))
            await asyncio.sleep(0.01)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(await giving_up, PICTURE)


class BusyRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear()

    def test_a_busy_recorder_does_not_condemn_a_channels_doors(self):
        ip = DVR["ip"]
        main._isapi_last_success[ip] = main.time.monotonic()
        main._mark_channel_doors_silent(ip, 21)
        self.assertFalse(main._channel_doors_silent(ip, 21))

    def test_a_channel_that_keeps_going_silent_is_still_shortcut(self):
        ip = DVR["ip"]
        main._isapi_last_success[ip] = main.time.monotonic()
        for _ in range(main._LIVE_CAPTURE_BUSY_FORGIVENESS):
            main._mark_channel_doors_silent(ip, 22)
        self.assertTrue(main._channel_doors_silent(ip, 22))

    def test_a_recorder_that_answers_nothing_is_shortcut_at_once(self):
        ip = DVR["ip"]
        main._mark_channel_doors_silent(ip, 23)
        self.assertTrue(main._channel_doors_silent(ip, 23))


if __name__ == "__main__":
    unittest.main()

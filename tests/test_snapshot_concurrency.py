import asyncio
import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import attendance_engine
import main


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.message_event = asyncio.Event()

    async def send(self, message):
        self.messages.append(json.loads(message))
        self.message_event.set()


class SnapshotConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._dvr_capture_limiters.clear()

    async def test_snapshot_uses_direct_main_stream_without_resolution_probe(self):
        requested_urls = []

        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"jpeg"

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, auth):
                requested_urls.append(url)
                return Response()

        dvr = {
            "ip": "192.0.2.1",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main.httpx, "AsyncClient", return_value=Client()):
            snapshot = await main.capture_snapshot(dvr, 3)

        self.assertEqual(snapshot, b"jpeg")
        self.assertEqual(
            requested_urls,
            ["http://192.0.2.1:80/ISAPI/Streaming/channels/301/picture"],
        )

    async def test_digest_auth_is_reused_for_repeat_snapshots(self):
        auth_objects = []

        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"jpeg"

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, auth):
                auth_objects.append(auth)
                return Response()

        dvr = {
            "ip": "192.0.2.2",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        main._digest_auth_cache.clear()
        with patch.object(main.httpx, "AsyncClient", return_value=Client()):
            await main.capture_snapshot(dvr, 3)
            await main.capture_snapshot(dvr, 3)

        self.assertIs(auth_objects[0], auth_objects[1])

    async def test_snapshot_retries_transient_timeout_then_succeeds(self):
        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"jpeg"

        class Client:
            def __init__(self):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, auth):
                self.calls += 1
                if self.calls == 1:
                    raise main.httpx.ConnectTimeout("busy")
                return Response()

        dvr = {
            "ip": "192.0.2.3",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        client = Client()
        with patch.object(main.httpx, "AsyncClient", return_value=client), patch.object(
            main, "_SNAPSHOT_RETRIES", 2
        ), patch.object(main, "_SNAPSHOT_RETRY_BACKOFF_SECONDS", 0):
            snapshot = await main.capture_snapshot(dvr, 3)

        self.assertEqual(snapshot, b"jpeg")
        self.assertEqual(client.calls, 2)

    async def test_snapshot_exhaustion_uses_rtsp_fallback(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, auth):
                raise main.httpx.ReadTimeout("busy")

        dvr = {
            "ip": "192.168.0.13",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main.httpx, "AsyncClient", return_value=Client()), patch.object(
            main, "_SNAPSHOT_RETRIES", 2
        ), patch.object(main, "_SNAPSHOT_RETRY_BACKOFF_SECONDS", 0), patch.object(
            main, "_capture_snapshot_rtsp", new=AsyncMock(return_value=b"rtsp")
        ) as fallback:
            snapshot = await main.capture_snapshot(dvr, 3)

        self.assertEqual(snapshot, b"rtsp")
        fallback.assert_awaited_once_with(dvr, 3)

    async def test_live_capture_uses_rtsp_for_other_dvrs(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, auth):
                raise main.httpx.ReadTimeout("busy")

        dvr = {
            "ip": "192.168.0.12",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main.httpx, "AsyncClient", return_value=Client()), patch.object(
            main, "_SNAPSHOT_RETRIES", 1
        ), patch.object(main, "_SNAPSHOT_CAMERA_TIMEOUT_SECONDS", 1), patch.object(
            main, "_capture_snapshot_rtsp", new=AsyncMock(return_value=b"rtsp")
        ) as fallback:
            snapshot = await main.capture_snapshot(dvr, 3)

        self.assertEqual(snapshot, b"rtsp")
        fallback.assert_awaited_once_with(dvr, 3)

    async def test_rtsp_failure_cooldown_skips_second_live_attempt(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, auth):
                raise main.httpx.ReadTimeout("busy")

        dvr = {
            "ip": "192.0.2.12",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        fallback = AsyncMock(return_value=None)
        with patch.object(main.httpx, "AsyncClient", return_value=Client()), patch.object(
            main, "_SNAPSHOT_RETRIES", 1
        ), patch.object(main, "_SNAPSHOT_CAMERA_TIMEOUT_SECONDS", 1), patch.object(
            main, "_capture_snapshot_rtsp", new=fallback
        ):
            self.assertIsNone(await main.capture_snapshot(dvr, 3))
            self.assertIsNone(await main.capture_snapshot(dvr, 3))

        fallback.assert_awaited_once_with(dvr, 3)

    async def test_background_capture_uses_short_budget_and_no_general_rtsp(self):
        class Client:
            def __init__(self):
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, auth):
                self.calls += 1
                raise main.httpx.ConnectTimeout("busy")

        dvr = {
            "ip": "192.0.2.5",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        client = Client()
        with patch.object(main.httpx, "AsyncClient", return_value=client), patch.object(
            main, "_SNAPSHOT_RETRIES", 3
        ), patch.object(main, "_SNAPSHOT_BACKGROUND_RETRIES", 1), patch.object(
            main, "_SNAPSHOT_BACKGROUND_CAMERA_TIMEOUT_SECONDS", 1
        ), patch.object(main, "_SNAPSHOT_BACKGROUND_RETRY_BACKOFF_SECONDS", 0), patch.object(
            main, "_capture_snapshot_rtsp", new=AsyncMock()
        ) as fallback:
            snapshot = await main.capture_snapshot(dvr, 3, background=True)

        self.assertIsNone(snapshot)
        self.assertEqual(client.calls, 1)
        fallback.assert_not_awaited()

    async def test_attendance_capture_keeps_native_resolution_request(self):
        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"high-resolution-jpeg"

        class Client:
            async def get(self, url):
                requested_urls.append(url)
                return Response()

        class Limiter:
            async def release(self, _background):
                return None

        requested_urls = []
        dvr = {
            "ip": "192.0.2.6",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        engine = attendance_engine.AttendanceEngine()
        engine._get_dvr_client = lambda _dvr: Client()
        attendance_engine._channel_resolution_cache.clear()
        with patch.object(
            main, "_acquire_dvr_capture", new=AsyncMock(return_value=Limiter())
        ), patch.object(
            attendance_engine, "_probe_channel_resolution",
            new=AsyncMock(return_value=(3840, 2160)),
        ), patch.object(
            main, "_SNAPSHOT_BACKGROUND_RETRIES", 1
        ):
            frame = await engine.capture_frame_from_dvr(dvr, 7)

        self.assertEqual(frame, b"high-resolution-jpeg")
        self.assertEqual(
            requested_urls,
            [
                "http://192.0.2.6:80/ISAPI/Streaming/channels/701/picture"
                "?snapShotImageType=JPEG"
                "&videoResolutionWidth=3840&videoResolutionHeight=2160"
            ],
        )

    async def test_attendance_probe_timeout_degrades_to_default_resolution(self):
        class Response:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"default-resolution-jpeg"

        class Client:
            async def get(self, url):
                requested_urls.append(url)
                return Response()

        class Limiter:
            async def release(self, _background):
                return None

        requested_urls = []
        dvr = {
            "ip": "192.0.2.7",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        engine = attendance_engine.AttendanceEngine()
        engine._get_dvr_client = lambda _dvr: Client()
        with patch.object(
            main, "_acquire_dvr_capture", new=AsyncMock(return_value=Limiter())
        ), patch.object(
            attendance_engine,
            "_probe_channel_resolution",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ), patch.object(main, "_SNAPSHOT_BACKGROUND_RETRIES", 1):
            frame = await engine.capture_frame_from_dvr(dvr, 7)

        self.assertEqual(frame, b"default-resolution-jpeg")
        self.assertIn("videoResolutionWidth=1920", requested_urls[0])
        self.assertIn("videoResolutionHeight=1080", requested_urls[0])

    async def test_attendance_counts_non_network_failure_and_logs_type(self):
        class Client:
            async def get(self, _url):
                raise RuntimeError()

        class Limiter:
            async def release(self, _background):
                return None

        dvr = {
            "ip": "192.0.2.8",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        engine = attendance_engine.AttendanceEngine()
        engine._get_dvr_client = lambda _dvr: Client()
        with patch.object(
            main, "_acquire_dvr_capture", new=AsyncMock(return_value=Limiter())
        ), patch.object(
            attendance_engine,
            "_probe_channel_resolution",
            new=AsyncMock(return_value=None),
        ), patch.object(main, "_SNAPSHOT_BACKGROUND_RETRIES", 1):
            frame = await engine.capture_frame_from_dvr(dvr, 7)

        self.assertIsNone(frame)
        self.assertEqual(engine._camera_errors["192.0.2.8:7"], 1)
        self.assertTrue(
            any("RuntimeError" in entry.get("details", "") for entry in engine.debug_logs)
        )

    async def test_empty_snapshot_exception_log_includes_type(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, _url, auth):
                raise RuntimeError()

        dvr = {
            "ip": "192.0.2.9",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(main.httpx, "AsyncClient", return_value=Client()), patch.object(
            main, "_SNAPSHOT_RETRIES", 1
        ), patch.object(main, "_SNAPSHOT_CAMERA_TIMEOUT_SECONDS", 1), patch.object(
            main, "_capture_snapshot_rtsp", new=AsyncMock(return_value=None)
        ), self.assertLogs(
            main.logger, level="ERROR"
        ) as logs:
            self.assertIsNone(await main.capture_snapshot(dvr, 3))

        self.assertTrue(any("RuntimeError" in line for line in logs.output))

    async def test_live_capture_has_priority_and_limiter_releases_on_error(self):
        limiter = main._DvrCaptureLimiter(limit=1, background_wait=0.05)
        live_started = asyncio.Event()
        release_live = asyncio.Event()
        background_started = asyncio.Event()

        async def live_capture():
            await limiter.acquire(True)
            live_started.set()
            try:
                await release_live.wait()
            finally:
                await limiter.release(True)

        async def background_capture():
            await limiter.acquire(False)
            background_started.set()
            await limiter.release(False)

        live_task = asyncio.create_task(live_capture())
        await live_started.wait()
        background_task = asyncio.create_task(background_capture())
        await asyncio.sleep(0)
        self.assertFalse(background_started.is_set())
        release_live.set()
        await asyncio.wait_for(asyncio.gather(live_task, background_task), timeout=1)
        self.assertTrue(background_started.is_set())

        with self.assertRaises(RuntimeError):
            async with limiter:
                raise RuntimeError("capture failed")
        await asyncio.wait_for(limiter.acquire(False), timeout=1)
        await limiter.release(False)

    async def test_abandoned_rtsp_capture_releases_slot_when_worker_finishes(self):
        class RunningFuture(concurrent.futures.Future):
            def cancel(self):
                return False

        class Executor:
            def __init__(self):
                self.future = RunningFuture()

            def submit(self, *_args):
                return self.future

        semaphore = asyncio.Semaphore(1)
        executor = Executor()
        dvr = {
            "ip": "192.168.0.13",
            "port": 80,
            "username": "admin",
            "password": "password",
        }
        with patch.object(
            main, "_rtsp_capture_semaphore", semaphore
        ), patch.object(main, "_rtsp_capture_executor", executor):
            task = asyncio.create_task(main._capture_snapshot_rtsp(dvr, 3))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(semaphore.acquire(), timeout=0.01)

            executor.future.set_result(b"late-frame")
            await asyncio.sleep(0)
            await asyncio.wait_for(semaphore.acquire(), timeout=1)
            semaphore.release()

    async def test_two_classroom_cameras_capture_in_parallel(self):
        cameras = [
            ({"ip": "192.0.2.1"}, 1, "TEST C1"),
            ({"ip": "192.0.2.1"}, 2, "TEST C2"),
        ]

        active_captures = 0
        max_active_captures = 0

        async def capture(_dvr, _channel):
            nonlocal active_captures, max_active_captures
            active_captures += 1
            max_active_captures = max(max_active_captures, active_captures)
            await asyncio.sleep(0)
            active_captures -= 1
            return b"jpeg"

        websocket = FakeWebSocket()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            main, "find_all_cameras_for_classroom", return_value=cameras
        ), patch.object(main, "capture_snapshot", side_effect=capture), patch.object(
            main, "SNAPSHOT_DIR", Path(directory)
        ), patch.object(main, "compress_jpeg", side_effect=lambda data: data):
            await main._handle_snapshot_request(websocket, "TEST", "request-1")

        self.assertEqual(max_active_captures, 2)
        self.assertEqual(
            [message["type"] for message in websocket.messages],
            ["snapshot_image", "snapshot_image", "snapshot_complete"],
        )
        self.assertEqual(
            [message.get("description") for message in websocket.messages[:2]],
            ["TEST C1", "TEST C2"],
        )

    async def test_first_completed_camera_is_sent_without_waiting_for_second(self):
        cameras = [
            ({"ip": "192.0.2.1"}, 1, "SLOW C1"),
            ({"ip": "192.0.2.1"}, 2, "FAST C2"),
        ]
        release_slow_camera = asyncio.Event()

        async def capture(_dvr, channel):
            if channel == 1:
                await release_slow_camera.wait()
            return b"jpeg"

        websocket = FakeWebSocket()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            main, "find_all_cameras_for_classroom", return_value=cameras
        ), patch.object(main, "capture_snapshot", side_effect=capture), patch.object(
            main, "SNAPSHOT_DIR", Path(directory)
        ), patch.object(main, "compress_jpeg", side_effect=lambda data: data):
            request_task = asyncio.create_task(
                main._handle_snapshot_request(websocket, "TEST", "request-1")
            )
            await asyncio.wait_for(websocket.message_event.wait(), timeout=1)
            self.assertEqual(websocket.messages[0]["type"], "snapshot_image")
            self.assertEqual(websocket.messages[0]["description"], "FAST C2")
            self.assertFalse(request_task.done())
            release_slow_camera.set()
            await request_task

        self.assertEqual(websocket.messages[-1]["type"], "snapshot_complete")

    async def test_two_snapshot_requests_can_run_concurrently(self):
        cameras = [
            ({"ip": "192.0.2.1"}, 1, "TEST C1"),
            ({"ip": "192.0.2.1"}, 2, "TEST C2"),
        ]

        active_captures = 0
        max_active_captures = 0

        async def capture(_dvr, _channel):
            nonlocal active_captures, max_active_captures
            active_captures += 1
            max_active_captures = max(max_active_captures, active_captures)
            await asyncio.sleep(0)
            active_captures -= 1
            return b"jpeg"

        first_websocket = FakeWebSocket()
        second_websocket = FakeWebSocket()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            main, "find_all_cameras_for_classroom", return_value=cameras
        ), patch.object(main, "capture_snapshot", side_effect=capture), patch.object(
            main, "SNAPSHOT_DIR", Path(directory)
        ), patch.object(main, "compress_jpeg", side_effect=lambda data: data):
            await asyncio.gather(
                main.handle_snapshot_request(first_websocket, "TEST A", "request-1"),
                main.handle_snapshot_request(second_websocket, "TEST B", "request-2"),
            )

        self.assertEqual(max_active_captures, 4)
        self.assertEqual(first_websocket.messages[-1]["type"], "snapshot_complete")
        self.assertEqual(second_websocket.messages[-1]["type"], "snapshot_complete")

    async def test_slow_camera_does_not_block_available_snapshot(self):
        cameras = [
            ({"ip": "192.0.2.1"}, 1, "TEST C1"),
            ({"ip": "192.0.2.1"}, 2, "TEST C2"),
        ]

        async def capture(_dvr, channel):
            if channel == 1:
                await asyncio.sleep(1)
            return b"jpeg"

        websocket = FakeWebSocket()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            main, "find_all_cameras_for_classroom", return_value=cameras
        ), patch.object(main, "capture_snapshot", side_effect=capture), patch.object(
            main, "SNAPSHOT_DIR", Path(directory)
        ), patch.object(main, "compress_jpeg", side_effect=lambda data: data), patch.object(
            main, "_SNAPSHOT_CAMERA_TIMEOUT_SECONDS", 0.01
        ):
            await main._handle_snapshot_request(websocket, "TEST", "request-1")

        self.assertEqual(
            [message["type"] for message in websocket.messages],
            ["snapshot_image", "snapshot_complete"],
        )
        self.assertEqual(websocket.messages[0]["description"], "TEST C2")
        self.assertEqual(websocket.messages[-1]["image_count"], 1)

    async def test_websocket_watchdog_restarts_missing_task(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def websocket_client():
            started.set()
            await release.wait()

        previous_task = main.ws_task
        previous_connection = main.ws_connection
        main.ws_task = None
        main.ws_connection = object()
        try:
            with patch.object(main, "websocket_client", side_effect=websocket_client):
                self.assertTrue(main._restart_websocket_task_if_needed())
                await asyncio.wait_for(started.wait(), timeout=1)
                self.assertIsNone(main.ws_connection)
                self.assertFalse(main._restart_websocket_task_if_needed())
        finally:
            release.set()
            if main.ws_task is not None:
                await main.ws_task
            main.ws_task = previous_task
            main.ws_connection = previous_connection


if __name__ == "__main__":
    unittest.main()

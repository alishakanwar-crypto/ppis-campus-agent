import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class SnapshotConcurrencyTests(unittest.IsolatedAsyncioTestCase):
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

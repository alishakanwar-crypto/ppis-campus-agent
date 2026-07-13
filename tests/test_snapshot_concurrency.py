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


if __name__ == "__main__":
    unittest.main()

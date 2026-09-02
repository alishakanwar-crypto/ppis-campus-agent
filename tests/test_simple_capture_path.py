"""The parent's request must be one quick camera call, not a search.

Covers the pieces that keep "show my child" fast: the door a channel serves
through survives a restart, the nightly measurement learns it while nobody is
waiting, and a parent's own request is capped at a few seconds.
"""
import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import main


def jpeg(width: int = 1920, height: int = 1080) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (80, 130, 190)).save(buf, format="JPEG")
    return buf.getvalue()


class RememberedDoorTests(unittest.TestCase):
    def setUp(self):
        main._live_capture_preferences.clear()
        main._live_capture_preference_age.clear()

    tearDown = setUp

    def test_a_learned_door_is_read_back_after_a_restart(self):
        main._live_capture_preferences[("192.0.2.60", 7)] = ("digest", 1)
        with patch.object(main, "_LIVE_CAPTURE_DOORS_FILE", self._file()):
            main._save_capture_doors()
            main._live_capture_preferences.clear()
            main._load_capture_doors()
        self.assertEqual(
            main._live_capture_preferences.get(("192.0.2.60", 7)), ("digest", 1)
        )

    def test_a_damaged_file_does_not_stop_the_agent(self):
        path = self._file()
        path.write_text("{ this is not json")
        with patch.object(main, "_LIVE_CAPTURE_DOORS_FILE", path):
            main._load_capture_doors()
        self.assertEqual(main._live_capture_preferences, {})

    def test_a_missing_file_is_normal_on_a_new_pc(self):
        with patch.object(main, "_LIVE_CAPTURE_DOORS_FILE", self._file("gone.json")):
            main._load_capture_doors()
        self.assertEqual(main._live_capture_preferences, {})

    def _file(self, name="doors.json"):
        return Path(tempfile.mkdtemp()) / name


class ParentWaitTests(unittest.TestCase):
    def test_a_parent_is_never_asked_to_wait_out_a_dead_camera(self):
        self.assertLessEqual(main._SNAPSHOT_CAMERA_TIMEOUT_SECONDS, 8.0)
        self.assertLessEqual(main._SNAPSHOT_LIVE_REQUEST_BUDGET_SECONDS, 12.0)
        self.assertLessEqual(main._SNAPSHOT_RETRIES, 2)
        self.assertLessEqual(main._LIVE_CAPTURE_DOOR_TIMEOUT_SECONDS, 3.0)
        # The second angle must still fit inside the shortened request.
        self.assertLess(
            main._SNAPSHOT_SECOND_ANGLE_RETRY_SECONDS,
            main._SNAPSHOT_LIVE_REQUEST_BUDGET_SECONDS / 2,
        )

    def test_clarity_is_not_traded_for_speed(self):
        self.assertEqual(
            (main._LIVE_SNAPSHOT_WIDTH, main._LIVE_SNAPSHOT_HEIGHT), (1920, 1080)
        )
        self.assertGreaterEqual(main._LIVE_SNAPSHOT_JPEG_QUALITY, 90)


class WarmUpTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_mapped_camera_is_measured_and_timed(self):
        cameras = {
            "GRADE 1A": [({"ip": "192.0.2.61"}, 3, "G1A C1")],
            "GRADE 1B": [({"ip": "192.0.2.61"}, 4, "G1B C1")],
        }
        with patch.object(main, "config", {"camera_mapping": cameras}), \
                patch.object(main, "find_all_cameras_for_classroom",
                             lambda room: cameras[room]), \
                patch.object(main, "capture_snapshot",
                             AsyncMock(return_value=jpeg())) as capture, \
                patch.object(main.asyncio, "sleep", AsyncMock()):
            measured = await main._warm_up_every_camera()

        self.assertEqual(sorted(measured), ["G1A C1", "G1B C1"])
        self.assertTrue(all(seconds >= 0 for seconds in measured.values()))
        # Measuring must never look like a parent's request to the recorder.
        self.assertTrue(
            all(call.kwargs.get("background") for call in capture.await_args_list)
        )

    async def test_a_camera_that_gives_nothing_is_reported_not_hidden(self):
        cameras = {"NUR-3": [({"ip": "192.0.2.62"}, 9, "NUR-3 C1")]}
        with patch.object(main, "config", {"camera_mapping": cameras}), \
                patch.object(main, "find_all_cameras_for_classroom",
                             lambda room: cameras[room]), \
                patch.object(main, "capture_snapshot",
                             AsyncMock(return_value=None)), \
                patch.object(main.asyncio, "sleep", AsyncMock()), \
                self.assertLogs(main.logger, level="WARNING") as logs:
            measured = await main._warm_up_every_camera()

        self.assertEqual(measured, {"NUR-3 C1": -1.0})
        self.assertTrue(
            any("gave no picture" in line for line in logs.output), logs.output
        )

    async def test_one_broken_camera_does_not_end_the_measurement(self):
        cameras = {
            "GRADE 2A": [({"ip": "192.0.2.63"}, 1, "G2A C1")],
            "GRADE 2B": [({"ip": "192.0.2.63"}, 2, "G2B C1")],
        }
        pictures = [RuntimeError("recorder hung up"), jpeg()]

        async def capture(*_args, **_kwargs):
            outcome = pictures.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(main, "config", {"camera_mapping": cameras}), \
                patch.object(main, "find_all_cameras_for_classroom",
                             lambda room: cameras[room]), \
                patch.object(main, "capture_snapshot", capture), \
                patch.object(main.asyncio, "sleep", AsyncMock()):
            measured = await main._warm_up_every_camera()

        self.assertEqual(list(measured), ["G2B C1"])


class MeasurementIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_locked_recorder_is_not_logged_into_by_the_measurement(self):
        locked = {"ip": "192.0.2.65", "username": "admin", "password": "x"}
        cameras = {"PREP-2": [(locked, 38, "PREP-2 C1")]}
        attempts = []

        async def capture(dvr, channel, **_kwargs):
            attempts.append((dvr["ip"], channel))
            return jpeg()

        with patch.object(main, "config", {"camera_mapping": cameras}), \
                patch.object(main, "find_all_cameras_for_classroom",
                             lambda room: cameras[room]), \
                patch.object(main, "_credentials_refused", lambda dvr: True), \
                patch.object(main, "capture_snapshot", capture), \
                patch.object(main.asyncio, "sleep", AsyncMock()):
            measured = await main._warm_up_every_camera()

        self.assertEqual(attempts, [])
        self.assertEqual(measured, {})

    async def test_measuring_does_not_leak_into_the_next_request(self):
        cameras = {"GRADE 1A": [({"ip": "192.0.2.66"}, 3, "G1A C1")]}

        async def capture(*_args, **_kwargs):
            assert main._measuring_cameras.get() is True
            return jpeg()

        with patch.object(main, "config", {"camera_mapping": cameras}), \
                patch.object(main, "find_all_cameras_for_classroom",
                             lambda room: cameras[room]), \
                patch.object(main, "capture_snapshot", capture), \
                patch.object(main.asyncio, "sleep", AsyncMock()):
            await main._warm_up_every_camera()

        self.assertFalse(main._measuring_cameras.get())


class RefusalReasonTests(unittest.TestCase):
    def _reply(self, body: bytes, status: int = 401, headers=None):
        return httpx.Response(status, content=body, headers=headers or {})

    def test_a_locked_account_is_named_so_no_one_retypes_the_password(self):
        said = main._refusal_reason(self._reply(
            b"<ResponseStatus><statusCode>4</statusCode>"
            b"<subStatusCode>userLocked</subStatusCode></ResponseStatus>"
        ))
        self.assertEqual(said, "recorder said userLocked")

    def test_a_wrong_password_is_named_as_such(self):
        said = main._refusal_reason(self._reply(
            b"<ResponseStatus><subStatusCode>badPassword</subStatusCode>"
            b"</ResponseStatus>"
        ))
        self.assertEqual(said, "recorder said badPassword")

    def test_a_silent_refusal_still_says_something_useful(self):
        said = main._refusal_reason(self._reply(b""))
        self.assertEqual(said, "recorder refused without offering a login")

    def test_a_normal_challenge_needs_no_explanation(self):
        said = main._refusal_reason(self._reply(
            b"", headers={"WWW-Authenticate": 'Digest realm="x"'}
        ))
        self.assertEqual(said, "")

    def test_health_publishes_what_the_recorder_said(self):
        main._isapi_cooldowns["192.0.2.67"] = (None, "credentials refused")
        main._auth_refusal_detail["192.0.2.67"] = "recorder said userLocked"
        try:
            entry = next(
                row for row in main.dvr_snapshot_health()
                if row["ip"] == "192.0.2.67"
            )
        finally:
            main._isapi_cooldowns.pop("192.0.2.67", None)
            main._auth_refusal_detail.pop("192.0.2.67", None)
        self.assertEqual(entry["refusal_detail"], "recorder said userLocked")


class ColourRepairTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_grey_photo_is_delivered_while_the_camera_is_fixed(self):
        grey = io.BytesIO()
        Image.new("RGB", (64, 48), (120, 120, 120)).save(grey, format="JPEG")
        picture = grey.getvalue()
        started = asyncio.get_running_loop().time()

        async def slow_repair(*_args):
            await asyncio.sleep(0.3)

        main._COLOUR_REPAIR_ATTEMPTED.clear()
        with patch.object(main, "_repair_colour_in_background", slow_repair):
            delivered = await main._repair_colour_if_night_mode(
                picture, ({"ip": "192.0.2.64"}, 5, "G4A C1")
            )
            waited = asyncio.get_running_loop().time() - started
            await asyncio.gather(*(
                task for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ))
        main._COLOUR_REPAIR_ATTEMPTED.clear()

        self.assertEqual(delivered, picture)
        self.assertLess(waited, 0.2)


if __name__ == "__main__":
    unittest.main()

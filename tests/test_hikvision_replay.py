import asyncio
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

import attendance_engine as replay_module
from attendance_engine import AttendanceEngine, IST


class HikvisionReplayPolicyTests(unittest.TestCase):
    def setUp(self):
        self.engine = AttendanceEngine()
        self.engine._is_holiday_today = lambda: False

    def test_historical_student_window_uses_recorded_ist_time(self):
        inside = datetime(2026, 7, 13, 8, 0, tzinfo=IST)
        outside = datetime(2026, 7, 13, 12, 1, tzinfo=IST)

        self.assertTrue(
            self.engine._is_within_attendance_window("STUDENT_1", inside)
        )
        self.assertFalse(
            self.engine._is_within_attendance_window("STUDENT_1", outside)
        )

    def test_hikvision_utc_times_are_converted_to_ist(self):
        parsed = self.engine._parse_hikvision_time("2026-07-13T02:00:00Z")

        self.assertEqual(
            parsed,
            datetime(2026, 7, 13, 7, 30, tzinfo=IST),
        )

    def test_hikvision_search_response_parses_namespaced_recording(self):
        response = b"""<?xml version="1.0" encoding="UTF-8"?>
        <CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
          <responseStatusStrg>OK</responseStatusStrg>
          <matchList><searchMatchItem>
            <timeSpan><startTime>2026-07-13T02:00:00Z</startTime>
              <endTime>2026-07-13T02:05:00Z</endTime></timeSpan>
            <mediaSegmentDescriptor>
              <playbackURI>rtsp://192.0.2.1/recording</playbackURI>
            </mediaSegmentDescriptor>
          </searchMatchItem></matchList>
        </CMSearchResult>"""

        state, segments = self.engine._parse_hikvision_search_response(response)

        self.assertEqual(state, "OK")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start"].hour, 7)
        self.assertEqual(segments[0]["start"].minute, 30)
        self.assertEqual(
            segments[0]["playback_uri"], "rtsp://192.0.2.1/recording",
        )

    def test_recording_coverage_rejects_gap_beyond_tolerance(self):
        start = datetime(2026, 7, 13, 7, 30, tzinfo=IST)
        end = datetime(2026, 7, 13, 11, 0, tzinfo=IST)
        complete = [
            {"start": start, "end": start + timedelta(hours=2)},
            {
                "start": start + timedelta(hours=2, seconds=30),
                "end": end,
            },
        ]
        gapped = [
            complete[0],
            {
                "start": start + timedelta(hours=2, minutes=2),
                "end": end,
            },
        ]

        self.assertTrue(
            self.engine._recording_coverage_complete(complete, start, end)
        )
        self.assertFalse(
            self.engine._recording_coverage_complete(gapped, start, end)
        )

    def test_live_and_replay_sightings_combine_without_duplicates(self):
        replay_start = datetime(2026, 7, 13, 7, 30, tzinfo=IST).timestamp()
        live_time = datetime(2026, 7, 13, 10, 59, tzinfo=IST).timestamp()

        self.assertEqual(
            self.engine._record_sighting(
                "STUDENT_1", 0.8, "GRADE 1", observed_at=live_time,
            ),
            1,
        )
        self.engine._record_sighting(
            "STUDENT_1", 0.8, "GRADE 1 [recording]",
            observed_at=replay_start,
        )
        self.assertEqual(len(self.engine._sightings["STUDENT_1"]), 2)
        self.assertEqual(
            self.engine._record_sighting(
                "STUDENT_1", 0.8, "GRADE 1 [recording]",
                observed_at=live_time - 30,
            ),
            2,
        )
        self.assertEqual(
            self.engine._record_sighting(
                "STUDENT_1", 0.8, "GRADE 1",
                observed_at=live_time - 29,
            ),
            2,
        )

    def test_live_and_replay_sightings_mark_present(self):
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            replay_module.db, "log_attendance", return_value=1,
        ), patch.object(
            replay_module, "ATTENDANCE_SNAPSHOTS_DIR", Path(temporary_directory),
        ), patch.object(replay_module.logger, "error"):
            engine = AttendanceEngine()
            engine._is_within_attendance_window = lambda *args: True
            engine._is_already_marked_today = (
                lambda person_id: person_id in engine.daily_marked
            )
            engine._check_anti_spoof = lambda *args: True
            engine._sync_attendance_to_cloud = lambda *args: None
            current_time = time.time()

            live_result = engine._process_attendance(
                "STUDENT_1", "Student", "", 0.6, b"image", (),
                "GRADE 1",
            )
            replay_result = engine._process_attendance(
                "STUDENT_1", "Student", "", 0.6, b"image", (),
                "GRADE 1 [recording]", observed_at=current_time - 30,
            )

        self.assertIsNone(live_result)
        self.assertIsNotNone(replay_result)
        self.assertEqual(replay_result["status"], "Present")

    def test_two_replay_sightings_mark_present(self):
        with tempfile.TemporaryDirectory() as temporary_directory, patch.object(
            replay_module.db, "log_attendance", return_value=1,
        ), patch.object(
            replay_module, "ATTENDANCE_SNAPSHOTS_DIR", Path(temporary_directory),
        ), patch.object(replay_module.logger, "error"):
            engine = AttendanceEngine()
            engine._is_within_attendance_window = lambda *args: True
            engine._is_already_marked_today = (
                lambda person_id: person_id in engine.daily_marked
            )
            engine._check_anti_spoof = lambda *args: True
            engine._sync_attendance_to_cloud = lambda *args: None
            current_time = time.time()

            first_result = engine._process_attendance(
                "STUDENT_1", "Student", "", 0.6, b"image", (),
                "GRADE 1 [recording]", observed_at=current_time - 60,
            )
            second_result = engine._process_attendance(
                "STUDENT_1", "Student", "", 0.6, b"image", (),
                "GRADE 1 [recording]", observed_at=current_time - 30,
            )

        self.assertIsNone(first_result)
        self.assertIsNotNone(second_result)
        self.assertEqual(second_result["status"], "Present")
        self.assertEqual(
            second_result["logged_at"],
            datetime.fromtimestamp(current_time - 30, IST).isoformat(),
        )

    @unittest.skipIf(replay_module.cv2 is None, "OpenCV unavailable")
    def test_recording_frames_keep_historical_timestamps(self):
        cv2 = replay_module.cv2
        old_sample_seconds = replay_module.HIKVISION_REPLAY_SAMPLE_SECONDS
        self.addCleanup(
            setattr,
            replay_module,
            "HIKVISION_REPLAY_SAMPLE_SECONDS",
            old_sample_seconds,
        )
        replay_module.HIKVISION_REPLAY_SAMPLE_SECONDS = 1

        observed = []
        self.engine.classwise_running = True
        self.engine.recognize_faces_in_image = (
            lambda image_bytes, **kwargs: observed.append(kwargs["observed_at"])
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            recording = Path(temporary_directory) / "recording.mp4"
            writer = cv2.VideoWriter(
                str(recording), cv2.VideoWriter_fourcc(*"mp4v"),
                10, (320, 240),
            )
            self.assertTrue(writer.isOpened())
            for frame_index in range(40):
                writer.write(np.full(
                    (240, 320, 3), frame_index, dtype=np.uint8,
                ))
            writer.release()

            segment_start = datetime(2026, 7, 13, 7, 29, 59, tzinfo=IST)
            replay_start = datetime(2026, 7, 13, 7, 30, tzinfo=IST)
            replay_end = replay_start + timedelta(seconds=2)
            processed = self.engine._process_hikvision_recording(
                recording, {"label": "GRADE 1"}, segment_start,
                replay_start, replay_end, None, None,
            )

        self.assertEqual(processed, 3)
        self.assertEqual(len(observed), 3)
        self.assertEqual(observed[0], replay_start.timestamp())
        self.assertEqual(observed[-1], replay_end.timestamp())


class HikvisionReplayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.old_state_path = replay_module.HIKVISION_REPLAY_STATE_PATH
        replay_module.HIKVISION_REPLAY_STATE_PATH = (
            Path(self.temporary_directory.name) / "replay_state.json"
        )

    async def _cleanup(self):
        replay_module.HIKVISION_REPLAY_STATE_PATH = self.old_state_path
        self.temporary_directory.cleanup()

    async def test_replay_starts_once_after_window_end(self):
        engine = AttendanceEngine()
        engine.test_mode = False
        calls = []

        async def replay(cameras, replay_date):
            calls.append((cameras, replay_date))

        engine._run_hikvision_recording_replay = replay
        before = datetime(2026, 7, 13, 10, 59, tzinfo=IST)
        due = datetime(2026, 7, 13, 11, 1, tzinfo=IST)
        engine._start_hikvision_recording_replay_if_due(before, [])
        engine._start_hikvision_recording_replay_if_due(due, [])
        await asyncio.sleep(0)
        engine._start_hikvision_recording_replay_if_due(due, [])
        await asyncio.sleep(0)

        self.assertEqual(len(calls), 1)
        restored = AttendanceEngine()
        self.assertEqual(restored._recording_replay_attempt_date, "2026-07-13")
        self.assertEqual(restored._recording_replay_status["state"], "failed")
        self.assertFalse(restored._recording_replay_status["coverage_complete"])

    async def test_missing_recordings_do_not_finalize_absence(self):
        engine = AttendanceEngine()
        engine.classwise_running = True
        engine._is_already_marked_today = lambda person_id: False
        engine._grade_face_cache = {
            "GRADE1": {
                "REPLAY_TEST_PERSON": {
                    "encodings": [], "name": "Test", "phone": "",
                },
            },
        }
        camera = {
            "grade": "GRADE1",
            "label": "GRADE 1 (DVR 1 Ch 1)",
            "channel": 1,
            "dvr": {
                "ip": "127.0.0.1", "port": 80,
                "username": "test", "password": "test",
            },
        }

        async def no_recordings(*args):
            return []

        engine._search_hikvision_recordings = no_recordings
        await engine._run_hikvision_recording_replay([camera], date.today())

        status = engine._recording_replay_status
        self.assertEqual(status["state"], "incomplete")
        self.assertFalse(status["coverage_complete"])
        self.assertEqual(status["attendance_backfilled"], 0)
        self.assertFalse(engine.daily_marked)


if __name__ == "__main__":
    unittest.main()

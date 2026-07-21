import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

import gate_face_audit


IST = timezone(timedelta(hours=5, minutes=30))


class FakeFaceRecognition:
    encoding = np.array([0.0, 0.0])
    locations = [(1, 3, 3, 1)]
    location_calls = 0
    encoded_locations = []

    @classmethod
    def face_locations(cls, image, number_of_times_to_upsample=1, model="hog"):
        cls.location_calls += 1
        return cls.locations

    @classmethod
    def face_encodings(cls, image, locations):
        cls.encoded_locations = locations
        return [cls.encoding.copy() for _ in locations]

    @staticmethod
    def face_distance(known_encodings, encoding):
        return np.array([
            np.linalg.norm(known_encoding - encoding)
            for known_encoding in known_encodings
        ])


class FakeHaarDetector:
    @staticmethod
    def detectMultiScale(image, scaleFactor, minNeighbors, minSize):
        return [(10, 10, 20, 20)]


class FakeCapture:
    def __init__(self):
        self.released = False
        self.frame = np.full((40, 40, 3), 127, dtype=np.uint8)

    def read(self):
        time.sleep(0.002)
        if self.released:
            return False, None
        return True, self.frame.copy()

    def release(self):
        self.released = True


class ErrorCapture:
    def __init__(self):
        self.released = False

    def read(self):
        raise gate_face_audit.cv2.error("decoder failed")

    def release(self):
        self.released = True


class FakeUnknownTracker:
    count = 0


class FakeAnalyzer:
    enrolled_people = 1
    enrollment_images = 2
    unknowns = FakeUnknownTracker()

    @staticmethod
    def analyze(frame, captured_at):
        return []


class GateFaceAuditTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 20, 8, 0, tzinfo=IST)
        self.frame = np.full((100, 100, 3), 127, dtype=np.uint8)
        self.known_faces = {
            "T001": {
                "name": "Teacher One",
                "role": "teacher",
                "phone": "",
                "encodings": [
                    np.array([0.05, 0.0]),
                    np.array([0.08, 0.0]),
                ],
            },
            "S001": {
                "name": "Student One",
                "role": "student",
                "phone": "",
                "encodings": [np.array([0.8, 0.8])],
            },
        }

    def _analyzer(self):
        return gate_face_audit.FaceAuditAnalyzer(
            known_faces=self.known_faces,
            minimum_face_width=1,
            minimum_sharpness=0,
            log_identities=True,
        )

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_uses_all_enrollment_images_for_candidate_match(self):
        FakeFaceRecognition.encoding = np.array([0.0, 0.0])

        observations = self._analyzer().analyze(self.frame, self.now)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["status"], "candidate_known")
        self.assertEqual(observations[0]["candidate_person_id"], "T001")
        self.assertEqual(observations[0]["candidate_category"], "teacher")
        self.assertEqual(observations[0]["review_state"], "pending")

    def test_latest_reader_records_opencv_error_without_thread_traceback(self):
        capture = ErrorCapture()
        reader = gate_face_audit.LatestFrameReader(capture)
        reader.start()

        latest = reader.next_frame(after_sequence=0, timeout=1)
        reader.close()

        self.assertIsNone(latest)
        self.assertTrue(reader.failed)
        self.assertIn("decoder failed", reader.failure_reason)
        self.assertTrue(capture.released)

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_fast_haar_detector_passes_locations_to_face_encoder(self):
        FakeFaceRecognition.encoding = np.array([0.0, 0.0])
        FakeFaceRecognition.location_calls = 0
        analyzer = gate_face_audit.FaceAuditAnalyzer(
            known_faces=self.known_faces,
            minimum_face_width=1,
            minimum_sharpness=0,
            detector="haar",
        )
        analyzer._haar = FakeHaarDetector()

        observations = analyzer.analyze(self.frame, self.now)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["status"], "candidate_known")
        self.assertEqual(FakeFaceRecognition.location_calls, 0)
        self.assertEqual(FakeFaceRecognition.encoded_locations, [(6, 34, 34, 6)])

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_default_pilot_excludes_students_and_pseudonymizes_candidates(self):
        FakeFaceRecognition.encoding = np.array([0.0, 0.0])
        analyzer = gate_face_audit.FaceAuditAnalyzer(
            known_faces=self.known_faces,
            minimum_face_width=1,
            minimum_sharpness=0,
        )

        observation = analyzer.analyze(self.frame, self.now)[0]

        self.assertEqual(observation["status"], "candidate_known")
        self.assertIn("candidate_token", observation)
        self.assertNotIn("candidate_person_id", observation)
        self.assertNotIn("candidate_name", observation)
        self.assertEqual(analyzer.enrolled_people, 1)

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_unknown_face_keeps_temporary_id_during_continuous_observation(self):
        FakeFaceRecognition.encoding = np.array([1.8, 1.8])
        analyzer = self._analyzer()

        first = analyzer.analyze(self.frame, self.now)[0]
        second = analyzer.analyze(self.frame, self.now + timedelta(seconds=2))[0]

        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["temporary_id"], "Unknown-001")
        self.assertEqual(second["temporary_id"], "Unknown-001")
        self.assertTrue(second["continuous_duplicate"])
        self.assertEqual(analyzer.unknowns.count, 1)

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_no_face_produces_no_identity_observation(self):
        FakeFaceRecognition.locations = []
        try:
            observations = self._analyzer().analyze(self.frame, self.now)
        finally:
            FakeFaceRecognition.locations = [(1, 3, 3, 1)]

        self.assertEqual(observations, [])

    def test_runtime_uses_continuous_rtsp_without_http_snapshot_polling(self):
        capture = FakeCapture()
        analyzer = FakeAnalyzer()
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(gate_face_audit, "load_dvr_passwords"),
                patch.object(
                    gate_face_audit,
                    "open_cpplus_stream",
                    return_value=capture,
                ),
                patch.object(
                    gate_face_audit,
                    "capture_cpplus_frame",
                    side_effect=AssertionError("HTTP capture should not run"),
                ),
                patch.object(
                    gate_face_audit,
                    "FaceAuditAnalyzer",
                    return_value=analyzer,
                ),
            ):
                output_path, summary = gate_face_audit.run_audit(
                    duration_minutes=0.001,
                    interval_seconds=0.005,
                    output_dir=Path(temp_dir),
                )

            self.assertTrue(output_path.exists())

        self.assertTrue(capture.released)
        self.assertEqual(summary["capture_source"], "rtsp_continuous")
        self.assertFalse(summary["stream_failed"])
        self.assertIsNone(summary["stream_failure_reason"])
        self.assertGreater(summary["stream_frames_read"], 0)
        self.assertGreater(summary["frames_captured"], 0)
        self.assertGreater(summary["rtsp_frames_analyzed"], 0)
        self.assertEqual(summary["http_frames_analyzed"], 0)
        self.assertEqual(summary["analysis_max_width"], 720)
        self.assertEqual(summary["face_detector"], "haar")
        self.assertFalse(summary["official_headcount_changed"])

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_summary_is_explicitly_non_additive(self):
        FakeFaceRecognition.encoding = np.array([0.0, 0.0])
        analyzer = self._analyzer()
        observations = analyzer.analyze(self.frame, self.now)
        records = [{
            "frame_ok": True,
            "processing_seconds": 0.1,
            "observations": observations,
        }]

        summary = gate_face_audit.summarize(records, analyzer)

        self.assertEqual(summary["mode"], "audit_only_non_additive")
        self.assertFalse(summary["official_headcount_changed"])
        self.assertFalse(summary["attendance_changed"])
        self.assertFalse(summary["cloud_data_sent"])
        self.assertEqual(summary["candidate_known"], 1)


if __name__ == "__main__":
    unittest.main()

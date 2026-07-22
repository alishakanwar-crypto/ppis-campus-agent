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
    encoded_image_shape = None

    @classmethod
    def face_locations(cls, image, number_of_times_to_upsample=1, model="hog"):
        cls.location_calls += 1
        return cls.locations

    @classmethod
    def face_encodings(cls, image, locations):
        cls.encoded_locations = locations
        cls.encoded_image_shape = image.shape
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
    multi_frame_review_candidates = 0
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
    def test_encodes_detected_faces_against_full_resolution_frame(self):
        FakeFaceRecognition.encoding = np.array([0.0, 0.0])
        full_resolution_frame = np.full((100, 200, 3), 127, dtype=np.uint8)
        analyzer = gate_face_audit.FaceAuditAnalyzer(
            known_faces=self.known_faces,
            minimum_face_width=1,
            minimum_sharpness=0,
            max_width=100,
            detector="haar",
        )
        analyzer._haar = FakeHaarDetector()

        observation = analyzer.analyze(full_resolution_frame, self.now)[0]

        self.assertEqual(FakeFaceRecognition.encoded_image_shape, (100, 200, 3))
        self.assertEqual(FakeFaceRecognition.encoded_locations, [(12, 68, 68, 12)])
        self.assertEqual(observation["face_width_px"], 56)

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
    def test_repeated_review_match_builds_manual_review_consensus(self):
        FakeFaceRecognition.encoding = np.array([0.6, 0.0])
        analyzer = self._analyzer()

        observations = [
            analyzer.analyze(self.frame, self.now + timedelta(seconds=index))[0]
            for index in range(3)
        ]

        self.assertEqual(observations[0]["status"], "unknown_needs_verification")
        self.assertTrue(observations[0]["best_evidence_so_far"])
        self.assertFalse(observations[1]["best_evidence_so_far"])
        self.assertEqual(observations[2]["candidate_support_frames"], 3)
        self.assertTrue(observations[2]["multi_frame_review_candidate"])
        self.assertEqual(analyzer.multi_frame_review_candidates, 1)

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_review_consensus_requires_observations_in_short_window(self):
        FakeFaceRecognition.encoding = np.array([0.6, 0.0])
        analyzer = gate_face_audit.FaceAuditAnalyzer(
            known_faces=self.known_faces,
            minimum_face_width=1,
            minimum_sharpness=0,
            log_identities=True,
            multi_frame_window_seconds=5,
        )

        observations = [
            analyzer.analyze(self.frame, self.now + timedelta(seconds=index * 6))[0]
            for index in range(3)
        ]

        self.assertEqual(observations[-1]["candidate_support_frames"], 1)
        self.assertFalse(observations[-1]["multi_frame_review_candidate"])
        self.assertEqual(analyzer.multi_frame_review_candidates, 0)

    @patch.object(gate_face_audit, "face_recognition", FakeFaceRecognition)
    def test_ambiguous_matches_do_not_build_manual_review_consensus(self):
        FakeFaceRecognition.encoding = np.array([0.51, 0.0])
        known_faces = {
            "T001": {
                "name": "Teacher One",
                "role": "teacher",
                "encodings": [np.array([0.0, 0.0])],
            },
            "T002": {
                "name": "Teacher Two",
                "role": "teacher",
                "encodings": [np.array([1.03, 0.0])],
            },
        }
        analyzer = gate_face_audit.FaceAuditAnalyzer(
            known_faces=known_faces,
            minimum_face_width=1,
            minimum_sharpness=0,
        )

        observations = [
            analyzer.analyze(self.frame, self.now + timedelta(seconds=index))[0]
            for index in range(3)
        ]

        self.assertEqual(observations[-1]["candidate_support_frames"], 0)
        self.assertFalse(observations[-1]["multi_frame_review_candidate"])
        self.assertEqual(analyzer.multi_frame_review_candidates, 0)

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
                patch.object(gate_face_audit.gate_counter, "running", True),
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
        self.assertEqual(summary["analysis_max_width"], 960)
        self.assertTrue(summary["full_resolution_encoding"])
        self.assertEqual(summary["face_detector"], "haar")
        self.assertFalse(summary["official_headcount_changed"])
        self.assertFalse(summary["stopped_early"])

    def test_runtime_honors_signal_shutdown_flag(self):
        capture = FakeCapture()
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(gate_face_audit, "load_dvr_passwords"),
                patch.object(gate_face_audit.gate_counter, "running", False),
                patch.object(
                    gate_face_audit,
                    "open_cpplus_stream",
                    return_value=capture,
                ),
                patch.object(
                    gate_face_audit,
                    "FaceAuditAnalyzer",
                    return_value=FakeAnalyzer(),
                ),
            ):
                _, summary = gate_face_audit.run_audit(
                    duration_minutes=10,
                    interval_seconds=0.005,
                    output_dir=Path(temp_dir),
                )

        self.assertTrue(capture.released)
        self.assertEqual(summary["frames_attempted"], 0)
        self.assertTrue(summary["stopped_early"])

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

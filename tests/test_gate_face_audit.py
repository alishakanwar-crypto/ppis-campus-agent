import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np

import gate_face_audit


IST = timezone(timedelta(hours=5, minutes=30))


class FakeFaceRecognition:
    encoding = np.array([0.0, 0.0])
    locations = [(1, 3, 3, 1)]

    @classmethod
    def face_locations(cls, image, number_of_times_to_upsample=1, model="hog"):
        return cls.locations

    @classmethod
    def face_encodings(cls, image, locations):
        return [cls.encoding.copy() for _ in locations]

    @staticmethod
    def face_distance(known_encodings, encoding):
        return np.array([
            np.linalg.norm(known_encoding - encoding)
            for known_encoding in known_encodings
        ])


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

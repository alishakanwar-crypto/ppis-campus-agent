"""A name that barely beats its classmates is a guess, not attendance.

Every attendance record in the school's database sits between 40% and 52%
similarity, which is the noise floor of the matcher. At that level the closest
child is often only a shade ahead of the next one in the same class, and
marking that child present tells a parent their absent child is in school.
"""
import unittest
from unittest.mock import patch

import numpy as np

from attendance_engine import engine


def face(vector):
    arr = np.array(vector, dtype=np.float64)
    return arr / np.linalg.norm(arr)


class MatchMarginTests(unittest.TestCase):
    def setUp(self):
        engine.identity_margin = 0.05

    def test_the_next_closest_child_is_reported_with_the_match(self):
        probe = face([1.0, 0.0, 0.0])
        roster = {
            "A_GRADE2B": {
                "name": "A",
                "phone": "1",
                "encodings": [face([1.0, 0.9, 0.0])],
            },
            "B_GRADE2B": {
                "name": "B",
                "phone": "2",
                "encodings": [face([1.0, 1.0, 0.0])],
            },
        }

        match = engine._match_insightface(probe, roster)

        self.assertEqual(match["person_id"], "A_GRADE2B")
        self.assertGreater(match["runner_up_confidence"], 0.0)
        self.assertAlmostEqual(
            match["margin"],
            match["confidence"] - match["runner_up_confidence"],
        )

    def test_a_child_barely_ahead_of_a_classmate_is_not_marked(self):
        engine.identity_margin = 0.05

        marked = engine._process_attendance(
            person_id="TANISHQ_GRADE2B",
            name="TANISHQ",
            phone="9",
            confidence=0.418,
            image_bytes=b"",
            face_location=(0, 0, 0, 0),
            camera_source="GRADE 2B (DVR 2 Ch 41)",
            margin=0.006,
        )

        self.assertIsNone(marked)

    def test_a_clear_match_still_goes_through_the_remaining_checks(self):
        """A comfortable margin must not be what stops attendance."""
        logs_before = len(engine.debug_logs)
        with patch.object(
            engine, "_is_within_attendance_window", return_value=False
        ):
            engine._process_attendance(
                person_id="TANISHQ_GRADE2B",
                name="TANISHQ",
                phone="9",
                confidence=0.60,
                image_bytes=b"",
                face_location=(0, 0, 0, 0),
                camera_source="GRADE 2B (DVR 2 Ch 41)",
                margin=0.20,
            )

        events = [
            log["event"]
            for log in engine.debug_logs[logs_before:]
        ]
        self.assertNotIn("ambiguous_match", events)


if __name__ == "__main__":
    unittest.main()

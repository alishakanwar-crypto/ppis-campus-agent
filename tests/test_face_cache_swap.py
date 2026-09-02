import unittest

from attendance_engine import engine


class FaceCacheSwapTests(unittest.TestCase):
    def test_a_reload_never_leaves_a_scan_with_an_empty_cache(self):
        """A classroom scan reading the cache mid-reload must see the old set."""
        engine.known_faces = {
            "ARJUN_MEHTA_GRADE5A": {"encoding": [0.1]},
            "TEACHER_MEENA": {"encoding": [0.2]},
        }
        engine.known_faces_insightface = {
            "ARJUN_MEHTA_GRADE5A": {"embedding": [0.3]},
        }
        in_use = {"GRADE5A": {"OLD_STUDENT_GRADE5A": {"encoding": [0.9]}}}
        engine._grade_face_cache = in_use

        engine._rebuild_grade_cache()

        self.assertEqual(
            in_use, {"GRADE5A": {"OLD_STUDENT_GRADE5A": {"encoding": [0.9]}}}
        )
        self.assertIn("ARJUN_MEHTA_GRADE5A", engine._grade_face_cache["GRADE5A"])
        self.assertIn("TEACHER_MEENA", engine._teacher_faces_cache)
        self.assertIn(
            "ARJUN_MEHTA_GRADE5A",
            engine._grade_face_cache_insightface["GRADE5A"],
        )


if __name__ == "__main__":
    unittest.main()

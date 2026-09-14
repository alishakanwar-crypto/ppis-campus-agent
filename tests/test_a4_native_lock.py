"""A4 registration must enter dlib behind the one native lock.

A face scan on a worker thread meeting an A4 capture inside dlib ends the
whole process, so this door has to use the same lock as every other.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import a4_capture
import face_native


class A4NativeLockTests(unittest.TestCase):
    def test_a4_capture_calls_the_locked_wrappers(self):
        source = Path(a4_capture.__file__).read_text(encoding="utf-8")
        self.assertNotIn("face_recognition.face_locations(", source)
        self.assertNotIn("face_recognition.face_encodings(", source)
        self.assertIn("face_native.face_locations(", source)
        self.assertIn("face_native.face_encodings(", source)

    def test_the_wrappers_hold_the_lock_while_they_run(self):
        held: list[bool] = []

        class FakeFaceRecognition:
            @staticmethod
            def face_locations(image, **kwargs):
                held.append(face_native.NATIVE_LOCK._is_owned())
                return []

        original = face_native.face_recognition
        face_native.face_recognition = FakeFaceRecognition
        self.addCleanup(setattr, face_native, "face_recognition", original)
        face_native.face_locations("frame")
        self.assertEqual(held, [True])


if __name__ == "__main__":
    unittest.main()

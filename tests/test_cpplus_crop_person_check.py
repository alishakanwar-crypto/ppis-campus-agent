import base64
import unittest
from unittest.mock import patch

import cv2
import numpy as np

import gate_counter


class FakeDetector:
    def __init__(self, found):
        self.found = found
        self.calls = 0

    def detect(self, frame, confidence_threshold=None):
        self.calls += 1
        if self.found:
            return [((0, 0, 10, 20), 0.9)]
        return []


def _decoded(b64: str) -> np.ndarray:
    raw = base64.b64decode(b64)
    return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)


class CpplusCropPersonCheckTests(unittest.TestCase):
    def setUp(self):
        self.lo_frame = np.zeros((120, 160, 3), dtype=np.uint8)
        self.lo_frame[20:100, 40:80] = 200  # the person, in the detection frame
        self.hi_frame = np.full((480, 640, 3), 50, dtype=np.uint8)
        self.bbox = (40, 20, 80, 100)

    def test_empty_hires_crop_falls_back_to_detection_frame(self):
        detector = FakeDetector(found=False)
        with patch.object(
            gate_counter, "capture_cpplus_frame", return_value=self.hi_frame
        ):
            crop = gate_counter.crop_person_hires_cpplus(
                {}, self.lo_frame, self.bbox, detector=detector
            )

        self.assertEqual(detector.calls, 1)
        image = _decoded(crop)
        # The low-res crop is the bbox itself, not the padded hi-res region.
        self.assertEqual(image.shape[:2], (80, 40))

    def test_hires_crop_kept_when_person_still_in_it(self):
        detector = FakeDetector(found=True)
        with patch.object(
            gate_counter, "capture_cpplus_frame", return_value=self.hi_frame
        ):
            crop = gate_counter.crop_person_hires_cpplus(
                {}, self.lo_frame, self.bbox, detector=detector
            )

        self.assertEqual(detector.calls, 1)
        image = _decoded(crop)
        self.assertGreater(image.shape[0], 80)

    def test_no_detector_keeps_previous_behaviour(self):
        with patch.object(
            gate_counter, "capture_cpplus_frame", return_value=self.hi_frame
        ):
            crop = gate_counter.crop_person_hires_cpplus(
                {}, self.lo_frame, self.bbox
            )

        image = _decoded(crop)
        self.assertGreater(image.shape[0], 80)


if __name__ == "__main__":
    unittest.main()

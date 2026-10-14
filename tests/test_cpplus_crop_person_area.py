import numpy as np

import gate_counter


class SliverDetector:
    """Finds only a fragment of somebody at the edge of the crop."""

    def detect(self, frame, confidence_threshold=None):
        return [((500, 820, 560, 870), 0.4)]


class PersonDetector:
    def detect(self, frame, confidence_threshold=None):
        return [((60, 100, 420, 700), 0.8)]


class BrokenDetector:
    def detect(self, frame, confidence_threshold=None):
        raise RuntimeError("model unavailable")


def _crop():
    return np.zeros((874, 563, 3), dtype=np.uint8)


def test_sliver_at_the_edge_does_not_count_as_a_person():
    assert gate_counter._crop_has_person(SliverDetector(), _crop()) is False


def test_person_filling_the_crop_counts():
    assert gate_counter._crop_has_person(PersonDetector(), _crop()) is True


def test_detector_failure_keeps_the_crop():
    assert gate_counter._crop_has_person(BrokenDetector(), _crop()) is True

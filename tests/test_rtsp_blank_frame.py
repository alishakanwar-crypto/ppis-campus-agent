import unittest

import numpy

import main


class FakeCapture:
    """A recorder's video stream that emits grey filler before a real picture."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.reads = 0

    def read(self):
        self.reads += 1
        if not self.frames:
            return False, None
        frame = self.frames.pop(0)
        return frame is not None, frame


def grey(height=8, width=8):
    return numpy.full((height, width, 3), 128, dtype=numpy.uint8)


def detailed(height=8, width=8):
    frame = numpy.zeros((height, width, 3), dtype=numpy.uint8)
    frame[:, ::2] = 255
    return frame


class RtspBlankFrameTests(unittest.TestCase):
    def test_grey_frames_are_skipped_for_a_real_picture(self):
        cap = FakeCapture([grey(), grey(), detailed()])

        frame = main._read_detailed_frame(cap, "192.168.0.14", 26)

        self.assertIsNotNone(frame)
        self.assertGreaterEqual(float(frame.std()), main._RTSP_MIN_FRAME_STDDEV)
        self.assertEqual(cap.reads, 3)

    def test_an_all_grey_stream_yields_no_photo(self):
        cap = FakeCapture([grey() for _ in range(5)])

        self.assertIsNone(main._read_detailed_frame(cap, "192.168.0.14", 26))

    def test_reading_stops_at_the_frame_budget(self):
        cap = FakeCapture([grey() for _ in range(main._RTSP_MAX_FRAMES_READ + 10)])

        main._read_detailed_frame(cap, "192.168.0.14", 26)

        self.assertLessEqual(cap.reads, main._RTSP_MAX_FRAMES_READ)

    def test_a_stream_that_ends_immediately_yields_no_photo(self):
        cap = FakeCapture([])

        self.assertIsNone(main._read_detailed_frame(cap, "192.168.0.14", 26))

    def test_a_first_frame_with_detail_is_used_at_once(self):
        cap = FakeCapture([detailed(), detailed()])

        self.assertIsNotNone(main._read_detailed_frame(cap, "192.168.0.14", 26))
        self.assertEqual(cap.reads, 1)


if __name__ == "__main__":
    unittest.main()

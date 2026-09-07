import unittest

import numpy

import main

from test_rtsp_blank_frame import FakeCapture, grey

HEIGHT = 200
WIDTH = 320


def room(height=HEIGHT, width=WIDTH):
    """A picture that varies both across and down, as a classroom does."""
    generator = numpy.random.default_rng(7)
    return generator.integers(
        0, 255, size=(height, width, 3), dtype=numpy.uint8
    )


def torn(smear_fraction=0.25, height=HEIGHT, width=WIDTH):
    """A classroom on top, the decoder's dragged columns underneath.

    The dragged columns are the last whole row repeated, which is what the
    decoder actually produces when a keyframe arrives with slices missing.
    """
    frame = room(height, width)
    starts_at = height - int(height * smear_fraction)
    frame[starts_at:] = frame[starts_at - 1]
    return frame


def letterboxed(height=HEIGHT, width=WIDTH):
    """A 4:3 camera's picture padded with a black bar along the bottom."""
    frame = room(height, width)
    frame[int(height * 0.85):] = 0
    return frame


class TornStreamFrameTests(unittest.TestCase):
    """A half-decoded keyframe is a whole JPEG and must still be refused."""

    def test_a_smeared_frame_is_recognised(self):
        self.assertTrue(main._frame_is_torn(torn()))

    def test_a_thin_smear_along_the_bottom_is_recognised(self):
        """The band a parent complained of was under a tenth of the photo."""
        self.assertTrue(main._frame_is_torn(torn(0.08)))

    def test_a_whole_picture_is_not_called_torn(self):
        self.assertFalse(main._frame_is_torn(room()))

    def test_a_black_bar_is_not_called_torn(self):
        """Padding holds no detail across, so it is the camera, not a tear."""
        self.assertFalse(main._frame_is_torn(letterboxed()))

    def test_a_grey_frame_is_left_to_the_blank_check(self):
        self.assertFalse(main._frame_is_torn(grey(HEIGHT, WIDTH)))

    def test_the_next_whole_frame_is_taken_instead_of_the_smear(self):
        whole = room()
        cap = FakeCapture([torn(), torn(), whole])

        frame = main._read_detailed_frame(cap, "192.168.0.12", 17)

        self.assertTrue(numpy.array_equal(frame, whole))
        self.assertEqual(cap.reads, 3)

    def test_a_stream_that_only_ever_tears_sends_nothing(self):
        """A retry beats a photo with a smeared band across the children."""
        cap = FakeCapture([torn(0.08) for _ in range(4)])

        self.assertIsNone(
            main._read_detailed_frame(cap, "192.168.0.12", 17)
        )


if __name__ == "__main__":
    unittest.main()

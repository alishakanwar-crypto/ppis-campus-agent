import unittest

import numpy

import main

from test_rtsp_blank_frame import FakeCapture, detailed, grey


def room(height=40, width=40):
    """A picture that varies both across and down, as a classroom does."""
    generator = numpy.random.default_rng(7)
    return generator.integers(
        0, 255, size=(height, width, 3), dtype=numpy.uint8
    )


def torn(height=40, width=40):
    """A classroom on top, the decoder's dragged columns underneath."""
    frame = room(height, width)
    smear = height - int(height * 0.25)
    frame[smear:] = frame[smear - 1]
    return frame


class TornStreamFrameTests(unittest.TestCase):
    """A half-decoded keyframe is a whole JPEG and must still be refused."""

    def test_a_smeared_frame_is_recognised(self):
        self.assertTrue(main._frame_is_torn(torn()))

    def test_a_whole_picture_is_not_called_torn(self):
        self.assertFalse(main._frame_is_torn(room()))

    def test_a_flat_striped_picture_is_not_called_torn(self):
        """A room with no vertical detail is not the decoder's fault."""
        self.assertFalse(main._frame_is_torn(detailed(40, 40)))

    def test_a_grey_frame_is_left_to_the_blank_check(self):
        self.assertFalse(main._frame_is_torn(grey(40, 40)))

    def test_the_next_whole_frame_is_taken_instead_of_the_smear(self):
        whole = room()
        cap = FakeCapture([torn(), torn(), whole])

        frame = main._read_detailed_frame(cap, "192.168.0.12", 17)

        self.assertTrue(numpy.array_equal(frame, whole))
        self.assertEqual(cap.reads, 3)

    def test_a_stream_that_only_ever_tears_still_gives_a_photo(self):
        """A torn classroom beats telling a parent the camera is down."""
        cap = FakeCapture([torn() for _ in range(4)])

        self.assertIsNotNone(
            main._read_detailed_frame(cap, "192.168.0.12", 17)
        )


if __name__ == "__main__":
    unittest.main()

import io
import unittest

import numpy
from PIL import Image

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
    frame[int(height * 0.7):] = 0
    return frame


def torn_and_letterboxed(height=HEIGHT, width=WIDTH):
    """A smeared band above a black bar taller than the smear itself."""
    frame = letterboxed(height, width)
    smear = int(height * 0.55)
    frame[smear:int(height * 0.7)] = frame[smear - 1]
    return frame


def green_filled(fill_fraction=0.6, height=HEIGHT, width=WIDTH):
    """A reception on top, and the green of data that never arrived below.

    Zeroed luma and colour decode to (0, 135, 0), which is what a parent was
    sent when she asked for the reception on the night of 21-09-2026.
    """
    frame = room(height, width)
    frame[height - int(height * fill_fraction):] = (0, 135, 0)
    return frame


def as_jpeg(frame, quality=85):
    buffer = io.BytesIO()
    Image.fromarray(frame).save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


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

    def test_a_smear_above_a_taller_black_bar_is_recognised(self):
        """The padding must not stand in for the smear above it."""
        self.assertTrue(main._frame_is_torn(torn_and_letterboxed()))

    def test_a_grey_frame_is_left_to_the_blank_check(self):
        self.assertFalse(main._frame_is_torn(grey(HEIGHT, WIDTH)))

    def test_the_next_whole_frame_is_taken_instead_of_the_smear(self):
        whole = room()
        cap = FakeCapture([torn(), torn(), whole])

        frame = main._read_detailed_frame(cap, "192.168.0.12", 17)

        self.assertTrue(numpy.array_equal(frame, whole))
        self.assertEqual(cap.reads, 3)

    def test_a_long_run_of_smears_is_read_past(self):
        """A loaded recorder smears more frames than a second of video has."""
        whole = room()
        cap = FakeCapture([torn() for _ in range(60)] + [whole])

        frame = main._read_detailed_frame(cap, "192.168.0.12", 17)

        self.assertTrue(numpy.array_equal(frame, whole))

    def test_a_stream_that_only_ever_tears_sends_nothing(self):
        """A retry beats a photo with a smeared band across the children."""
        cap = FakeCapture([torn(0.08) for _ in range(4)])

        self.assertIsNone(
            main._read_detailed_frame(cap, "192.168.0.12", 17)
        )


class GreenFilledPhotoTests(unittest.TestCase):
    """A photo can also arrive whole with nothing in its lower part."""

    def test_a_green_filled_frame_is_recognised(self):
        self.assertTrue(main._frame_is_torn(green_filled()))

    def test_a_green_filled_jpeg_is_refused(self):
        """These bytes open, decode and end properly, and are still no photo."""
        data = as_jpeg(green_filled())

        self.assertTrue(main._jpeg_is_complete(data))
        self.assertTrue(main._jpeg_is_torn(data))

    def test_a_whole_photo_is_still_sent(self):
        self.assertFalse(main._jpeg_is_torn(as_jpeg(room())))

    def test_a_black_barred_photo_is_still_sent(self):
        """Padding is flat too, and rejecting it would cost real photos."""
        self.assertFalse(main._jpeg_is_torn(as_jpeg(letterboxed())))

    def test_a_dark_night_photo_is_still_sent(self):
        generator = numpy.random.default_rng(3)
        night = generator.integers(
            18, 52, size=(HEIGHT, WIDTH, 3), dtype=numpy.uint8
        )

        self.assertFalse(main._jpeg_is_torn(as_jpeg(night)))

    def test_unreadable_bytes_are_left_alone(self):
        """A check that cannot run must never be the reason a photo is lost."""
        self.assertFalse(main._jpeg_is_torn(b"not a picture"))
        self.assertFalse(main._jpeg_is_torn(b""))


if __name__ == "__main__":
    unittest.main()

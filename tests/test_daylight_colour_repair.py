import io
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from PIL import Image

import main

DVR = {"ip": "192.0.2.10", "port": 80, "username": "admin", "password": "x"}
CAMERA = (DVR, 12, "G5A C1")
NOON_IST = datetime(2026, 8, 24, 12, 22, tzinfo=main._IST)
NIGHT_IST = datetime(2026, 8, 24, 21, 5, tzinfo=main._IST)


def jpeg(colour: tuple[int, int, int], mode: str = "RGB") -> bytes:
    img = Image.new("RGB", (64, 48), colour)
    if mode != "RGB":
        img = img.convert(mode)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


GREY = jpeg((120, 120, 120))
GREYSCALE_MODE = jpeg((30, 140, 200), mode="L")
COLOUR = jpeg((200, 40, 40))


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class ColourDetectionTests(unittest.TestCase):
    def test_a_grey_picture_is_reported_as_colourless(self):
        self.assertTrue(main._image_has_no_colour(GREY))
        self.assertTrue(main._image_has_no_colour(GREYSCALE_MODE))

    def test_a_colour_picture_is_left_alone(self):
        self.assertFalse(main._image_has_no_colour(COLOUR))

    def test_undecodable_data_is_inconclusive(self):
        self.assertIsNone(main._image_has_no_colour(b"not an image"))


class DaylightColourRepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._COLOUR_REPAIR_ATTEMPTED.clear()

    def tearDown(self):
        main._COLOUR_REPAIR_ATTEMPTED.clear()

    def _client(self, put_status=200):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=FakeResponse(
                text="<IrcutFilter><IrcutFilterType>night</IrcutFilterType>"
                     "</IrcutFilter>"
            )
        )
        client.put = AsyncMock(return_value=FakeResponse(status_code=put_status))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    async def _repair(self, snapshot, *, now=NOON_IST, put_status=200,
                      recaptured=COLOUR):
        client = self._client(put_status)
        with patch.object(main.httpx, "AsyncClient", return_value=client), \
             patch.object(main, "capture_snapshot",
                          AsyncMock(return_value=recaptured)) as capture, \
             patch.object(main, "datetime") as clock, \
             patch.object(main.asyncio, "sleep", AsyncMock()):
            clock.now.return_value = now
            result = await main._repair_colour_if_night_mode(snapshot, CAMERA)
        return result, client, capture

    async def test_colourless_daytime_picture_is_recaptured_in_colour(self):
        result, client, capture = await self._repair(GREY)
        self.assertEqual(result, COLOUR)
        self.assertEqual(client.put.await_count, 1)
        self.assertIn("auto", client.put.await_args.kwargs["content"])
        self.assertEqual(capture.await_count, 1)

    async def test_colour_picture_never_touches_the_recorder(self):
        result, client, capture = await self._repair(COLOUR)
        self.assertEqual(result, COLOUR)
        self.assertEqual(client.put.await_count, 0)
        self.assertEqual(capture.await_count, 0)

    async def test_night_time_greyscale_is_expected_and_left_alone(self):
        result, client, _ = await self._repair(GREY, now=NIGHT_IST)
        self.assertEqual(result, GREY)
        self.assertEqual(client.put.await_count, 0)

    async def test_repair_is_attempted_once_per_camera_per_day(self):
        await self._repair(GREY)
        _, client, capture = await self._repair(GREY)
        self.assertEqual(client.put.await_count, 0)
        self.assertEqual(capture.await_count, 0)

    async def test_a_refused_setting_still_delivers_the_original_photo(self):
        result, _, capture = await self._repair(GREY, put_status=403)
        self.assertEqual(result, GREY)
        self.assertEqual(capture.await_count, 0)

    async def test_a_still_grey_camera_delivers_the_recapture(self):
        result, _, _ = await self._repair(GREY, recaptured=GREYSCALE_MODE)
        self.assertEqual(result, GREYSCALE_MODE)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import AsyncMock, patch

import main

CHANNEL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<VideoInputChannelList>
  <VideoInputChannel><id>10</id><name>G8B C1</name></VideoInputChannel>
  <VideoInputChannel><id>14</id><name>G8B C2</name></VideoInputChannel>
  <VideoInputChannel><id>21</id><name>G9C C1</name></VideoInputChannel>
  <VideoInputChannel><id></id><name>unnamed</name></VideoInputChannel>
</VideoInputChannelList>
"""

DVRS = [{"ip": "192.0.2.10", "port": 80, "username": "admin", "password": "x"}]


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class SecondCameraDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._discovered_dvr_channels.clear()
        self._config = dict(main.config)

    def tearDown(self):
        main.config = self._config
        main._discovered_dvr_channels.clear()

    async def _discover(self, response):
        main.config = {"dvrs": DVRS, "camera_mapping": {}}
        client = AsyncMock()
        client.get = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch.object(main.httpx, "AsyncClient", return_value=client):
            return await main.discover_dvr_channel_names()

    async def test_channel_names_are_read_from_the_dvr(self):
        count = await self._discover(FakeResponse(text=CHANNEL_XML))
        self.assertEqual(count, 3)
        self.assertEqual(
            main._discovered_dvr_channels[0],
            {"G8B C1": 10, "G8B C2": 14, "G9C C1": 21},
        )

    async def test_unreachable_dvr_is_ignored(self):
        count = await self._discover(FakeResponse(status_code=401))
        self.assertEqual(count, 0)
        self.assertEqual(main._discovered_dvr_channels, {})

    async def test_unmapped_second_camera_is_captured(self):
        await self._discover(FakeResponse(text=CHANNEL_XML))
        main.config = {
            "dvrs": DVRS,
            "camera_mapping": {
                "GRADE 8B": {
                    "dvr_index": 0,
                    "channel": 10,
                    "description": "G8B C1",
                },
            },
        }
        cameras = main.find_all_cameras_for_classroom("GRADE 8B")
        self.assertEqual(
            [(channel, desc) for _dvr, channel, desc in cameras],
            [(10, "G8B C1"), (14, "G8B C2")],
        )

    async def test_other_rooms_are_not_pulled_in(self):
        await self._discover(FakeResponse(text=CHANNEL_XML))
        main.config = {
            "dvrs": DVRS,
            "camera_mapping": {
                "GRADE 9C": {
                    "dvr_index": 0,
                    "channel": 21,
                    "description": "G9C C1",
                },
            },
        }
        cameras = main.find_all_cameras_for_classroom("GRADE 9C")
        self.assertEqual([channel for _d, channel, _n in cameras], [21])

    async def test_a_camera_mapped_twice_is_captured_once(self):
        main.config = {
            "dvrs": DVRS,
            "camera_mapping": {
                "GRADE 8B": {
                    "dvr_index": 0,
                    "channel": 10,
                    "description": "G8B C1",
                },
                "GRADE 8B (DVR 3 Ch 10)": {
                    "dvr_index": 0,
                    "channel": 10,
                    "description": "G8B C1",
                },
            },
        }
        cameras = main.find_all_cameras_for_classroom("GRADE 8B")
        self.assertEqual([channel for _d, channel, _n in cameras], [10])


if __name__ == "__main__":
    unittest.main()

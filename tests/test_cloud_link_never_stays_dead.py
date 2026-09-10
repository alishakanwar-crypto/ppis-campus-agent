"""An outage that survives every rebuild must end the process, not the day.

The cloud link went dead at 10:01 IST and stayed dead for an hour of parents'
requests: the watchdog rebuilt the socket over and over, and each rebuild reset
the outage clock, so the agent never knew it had been offline for more than a
few seconds and never gave up on itself.
"""

import unittest
from unittest.mock import AsyncMock, patch

import main


class FakeSocket:
    def __init__(self, open_=True):
        self.open = open_


class OutageClockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.ws_connection = None
        main.ws_task = None
        main._ws_disconnected_since = 0.0
        main._ws_offline_since = 0.0
        main._ws_recycles = 0
        main._ws_last_recycle = 0.0

    def tearDown(self):
        main.ws_connection = None
        main.ws_task = None
        main._ws_offline_since = 0.0

    async def test_rebuilding_the_link_does_not_forget_the_outage(self):
        with patch.object(main, "websocket_client", AsyncMock()):
            await main._repair_cloud_link_if_needed()
            offline_since = main._ws_offline_since
            main._ws_disconnected_since -= main._WS_STALE_SECONDS + 1
            await main._repair_cloud_link_if_needed()

        self.assertEqual(main._ws_offline_since, offline_since)
        main.ws_task.cancel()

    async def test_traffic_on_the_link_clears_the_outage(self):
        await main._repair_cloud_link_if_needed()
        self.assertGreater(main._ws_offline_since, 0.0)

        main._note_ws_activity()

        self.assertEqual(main._ws_offline_since, 0.0)

    async def test_an_hour_offline_exits_so_the_wrapper_starts_a_fresh_agent(self):
        with patch.object(main, "_recycle_websocket", AsyncMock()), patch.object(
            main, "_STARTED_BY_WRAPPER", True
        ), patch.object(main.os, "_exit") as exit_:
            await main._repair_cloud_link_if_needed()
            main._ws_offline_since -= main._WS_HARD_RESTART_SECONDS + 1
            await main._repair_cloud_link_if_needed()

        exit_.assert_called_once_with(0)

    async def test_a_short_outage_never_exits(self):
        with patch.object(main, "_recycle_websocket", AsyncMock()), patch.object(
            main, "_STARTED_BY_WRAPPER", True
        ), patch.object(main.os, "_exit") as exit_:
            await main._repair_cloud_link_if_needed()
            await main._repair_cloud_link_if_needed()

        exit_.assert_not_called()

    async def test_an_agent_started_by_hand_is_not_exited(self):
        with patch.object(main, "_recycle_websocket", AsyncMock()), patch.object(
            main, "_STARTED_BY_WRAPPER", False
        ), patch.object(main.os, "_exit") as exit_:
            await main._repair_cloud_link_if_needed()
            main._ws_offline_since -= main._WS_HARD_RESTART_SECONDS + 1
            await main._repair_cloud_link_if_needed()

        exit_.assert_not_called()

    async def test_a_half_open_link_the_cloud_cannot_see_starts_the_clock(self):
        main.ws_connection = FakeSocket()
        with patch.object(
            main, "_cloud_says_we_are_connected", AsyncMock(return_value=False)
        ), patch.object(main, "_recycle_websocket", AsyncMock()):
            await main._repair_cloud_link_if_needed()

        self.assertGreater(main._ws_offline_since, 0.0)

    async def test_health_reports_how_long_the_cloud_has_not_seen_us(self):
        await main._repair_cloud_link_if_needed()
        main._ws_offline_since -= 120.0

        self.assertGreaterEqual(main.ws_link_health()["offline_seconds"], 120.0)

    async def test_the_restart_is_not_slower_than_a_school_lesson(self):
        self.assertLessEqual(main._WS_HARD_RESTART_SECONDS, 600.0)


if __name__ == "__main__":
    unittest.main()

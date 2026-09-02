import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import main


class FakeSocket:
    def __init__(self, open_=True):
        self.open = open_


class CloudLinkWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.ws_connection = None
        main.ws_task = None
        main._ws_disconnected_since = 0.0
        main._ws_recycles = 0
        main._ws_last_recycle = 0.0

    def tearDown(self):
        main.ws_connection = None
        main.ws_task = None

    async def test_a_half_open_link_the_cloud_cannot_see_is_rebuilt(self):
        main.ws_connection = FakeSocket()
        with patch.object(
            main, "_cloud_says_we_are_connected", AsyncMock(return_value=False)
        ), patch.object(
            main, "_recycle_websocket", AsyncMock()
        ) as recycle:
            await main._repair_cloud_link_if_needed()

        recycle.assert_awaited_once()

    async def test_a_link_the_cloud_can_see_is_left_alone(self):
        main.ws_connection = FakeSocket()
        with patch.object(
            main, "_cloud_says_we_are_connected", AsyncMock(return_value=True)
        ), patch.object(
            main, "_recycle_websocket", AsyncMock()
        ) as recycle:
            await main._repair_cloud_link_if_needed()

        recycle.assert_not_awaited()

    async def test_an_unreachable_cloud_is_not_treated_as_a_broken_link(self):
        main.ws_connection = FakeSocket()
        with patch.object(
            main, "_cloud_says_we_are_connected", AsyncMock(return_value=None)
        ), patch.object(
            main, "_recycle_websocket", AsyncMock()
        ) as recycle:
            await main._repair_cloud_link_if_needed()

        recycle.assert_not_awaited()

    async def test_a_short_outage_is_given_time_to_reconnect_itself(self):
        with patch.object(main, "_recycle_websocket", AsyncMock()) as recycle:
            await main._repair_cloud_link_if_needed()
            await main._repair_cloud_link_if_needed()

        recycle.assert_not_awaited()
        self.assertGreater(main._ws_disconnected_since, 0.0)

    async def test_a_long_outage_forces_a_fresh_connection(self):
        with patch.object(main, "_recycle_websocket", AsyncMock()) as recycle:
            await main._repair_cloud_link_if_needed()
            main._ws_disconnected_since -= main._WS_STALE_SECONDS + 1
            await main._repair_cloud_link_if_needed()

        recycle.assert_awaited_once()

    async def test_recycling_cancels_the_old_task_and_starts_a_new_one(self):
        started = asyncio.Event()

        async def fake_client():
            started.set()
            await asyncio.sleep(3600)

        old = asyncio.create_task(asyncio.sleep(3600))
        main.ws_task = old
        main.ws_connection = FakeSocket()
        with patch.object(main, "websocket_client", fake_client):
            await main._recycle_websocket("test")
            await asyncio.wait_for(started.wait(), timeout=1)

        self.assertTrue(old.cancelled())
        self.assertIsNot(main.ws_task, old)
        self.assertIsNone(main.ws_connection)
        self.assertEqual(main._ws_recycles, 1)
        main.ws_task.cancel()

    async def test_an_outage_is_found_within_a_minute(self):
        self.assertLessEqual(main._WS_STALE_SECONDS, 60.0)
        self.assertLessEqual(main._WS_LINK_CHECK_SECONDS, 20.0)

    async def test_a_fresh_link_is_not_recycled_again_at_once(self):
        started = 0

        async def fake_client():
            nonlocal started
            started += 1
            await asyncio.sleep(3600)

        with patch.object(main, "websocket_client", fake_client):
            await main._recycle_websocket("first outage")
            await main._recycle_websocket("cloud still catching up")
            await asyncio.sleep(0)

        self.assertEqual(started, 1)
        self.assertEqual(main._ws_recycles, 1)
        main.ws_task.cancel()

    async def test_the_link_watchdog_checks_far_more_often_than_a_minute(self):
        calls = 0

        async def fake_repair():
            nonlocal calls
            calls += 1

        with patch.object(main, "_WS_LINK_CHECK_SECONDS", 0.01), patch.object(
            main, "_restart_websocket_task_if_needed", lambda: False
        ), patch.object(main, "_repair_cloud_link_if_needed", fake_repair):
            task = asyncio.create_task(main._cloud_link_watchdog())
            await asyncio.sleep(0.1)
            task.cancel()

        self.assertGreater(calls, 1)

    async def test_a_failing_check_does_not_kill_the_watchdog(self):
        calls = 0

        async def failing_repair():
            nonlocal calls
            calls += 1
            raise RuntimeError("cloud unreachable")

        with patch.object(main, "_WS_LINK_CHECK_SECONDS", 0.01), patch.object(
            main, "_restart_websocket_task_if_needed", lambda: False
        ), patch.object(main, "_repair_cloud_link_if_needed", failing_repair):
            task = asyncio.create_task(main._cloud_link_watchdog())
            await asyncio.sleep(0.1)
            task.cancel()

        self.assertGreater(calls, 1)

    async def test_link_health_reports_the_recycle_count(self):
        main.ws_connection = FakeSocket()
        main._ws_recycles = 2
        health = main.ws_link_health()

        self.assertTrue(health["connected"])
        self.assertEqual(health["recycles"], 2)


if __name__ == "__main__":
    unittest.main()

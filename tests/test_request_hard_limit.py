"""A parent's request must not be able to live for ever.

Sending an image over a cloud link that has already died can block with
nothing to time it out. The request then stays counted as work in flight,
which stops the agent taking merged fixes at all — the campus PC ran a whole
day's parent traffic on old code because of one such request.
"""
import asyncio
import importlib
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class RequestHardLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_request_that_never_returns_is_abandoned(self):
        async def hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        with patch.object(main, "_serve_snapshot_request", hang), \
                patch.object(main, "_SNAPSHOT_REQUEST_HARD_LIMIT_SECONDS", 0.05):
            await main.handle_snapshot_request(None, "GRADE 3C", "request-1")

        self.assertEqual(main._live_requests_in_flight, 0)

    async def test_the_limit_leaves_room_for_the_queue(self):
        """It must never cut a request that is still legitimately working."""
        self.assertGreaterEqual(
            main._SNAPSHOT_REQUEST_HARD_LIMIT_SECONDS,
            main._SNAPSHOT_LIVE_REQUEST_BUDGET_SECONDS * 2,
        )

    async def test_a_longer_configured_budget_still_fits(self):
        """A raised request budget must raise the backstop with it."""
        with patch.dict(
            os.environ,
            {"SNAPSHOT_LIVE_REQUEST_BUDGET_SECONDS": "120"},
        ):
            importlib.reload(main)
            self.assertGreater(
                main._SNAPSHOT_REQUEST_HARD_LIMIT_SECONDS,
                main._SNAPSHOT_LIVE_REQUEST_BUDGET_SECONDS * 2,
            )
        importlib.reload(main)


if __name__ == "__main__":
    unittest.main()

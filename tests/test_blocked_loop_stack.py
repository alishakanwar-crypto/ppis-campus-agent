import threading
import time
import unittest
from datetime import datetime

import main


class BlockedLoopStackTests(unittest.TestCase):
    def setUp(self):
        main._loop_stalls.clear()
        main._loop_block_stack = ""
        main._loop_block_seen_at = 0.0

    def tearDown(self):
        main._loop_stalls.clear()
        main._loop_block_stack = ""
        main._loop_block_seen_at = 0.0

    def test_the_stack_is_caught_while_the_loop_is_still_held(self):
        """The blocking code has returned by the time a stall is noticed."""
        main._loop_pulse = time.monotonic() - 60
        watch = threading.Thread(
            target=main._watch_for_a_blocked_loop,
            args=(threading.get_ident(),),
            daemon=True,
        )
        watch.start()
        deadline = time.monotonic() + 5
        while not main._loop_block_stack and time.monotonic() < deadline:
            time.sleep(0.05)

        self.assertIn(
            "test_the_stack_is_caught_while_the_loop_is_still_held",
            main._loop_block_stack,
        )

    def test_a_stall_names_the_code_that_held_the_loop(self):
        main._loop_block_stack = "main.py:10 sweep_cameras"
        main._loop_block_seen_at = time.monotonic()

        main._note_loop_stall(9.0, "", datetime.now(main._IST))

        stall = main.event_loop_lag_health()["stalls"][0]
        self.assertEqual(stall["held_in"], "main.py:10 sweep_cameras")

    def test_an_older_catch_is_not_pinned_on_a_later_stall(self):
        """Yesterday's blocker must not explain this morning's delay."""
        main._loop_block_stack = "main.py:10 sweep_cameras"
        main._loop_block_seen_at = time.monotonic() - 300

        main._note_loop_stall(9.0, "", datetime.now(main._IST))

        self.assertEqual(main._loop_stalls[0]["held_in"], "")


if __name__ == "__main__":
    unittest.main()

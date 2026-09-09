import unittest

import main


class LoopStallAttributionTests(unittest.TestCase):
    def setUp(self):
        main._loop_stalls.clear()
        main._loop_lag_samples.clear()

    def tearDown(self):
        main._loop_stalls.clear()
        main._loop_lag_samples.clear()

    def test_a_stall_keeps_its_time_and_what_was_running(self):
        """Health is all the cloud can see of the campus PC's morning."""
        main._note_loop_stall(18.3)

        stall = main.event_loop_lag_health()["stalls"][0]
        self.assertEqual(stall["seconds"], 18.3)
        self.assertTrue(stall["at_ist"].endswith("IST"))
        self.assertTrue(stall["work"])

    def test_only_the_worst_stalls_are_kept(self):
        for seconds in (3.0, 21.0, 4.0, 9.0, 30.0, 2.5, 15.0):
            main._note_loop_stall(seconds)

        kept = [stall["seconds"] for stall in main._loop_stalls]
        self.assertEqual(kept, [30.0, 21.0, 15.0, 9.0, 4.0])

    def test_the_stalls_outlive_the_rolling_readings(self):
        """The five-minute window rolls past; the incident still counts."""
        main._note_loop_stall(7.0)

        self.assertEqual(main.event_loop_lag_health()["samples"], 0)
        self.assertEqual(len(main.event_loop_lag_health()["stalls"]), 1)

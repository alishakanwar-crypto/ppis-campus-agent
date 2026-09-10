from datetime import datetime

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
        main._note_loop_stall(
            18.3, "2 parent request(s)", datetime.now(main._IST)
        )

        stall = main.event_loop_lag_health()["stalls"][0]
        self.assertEqual(stall["seconds"], 18.3)
        self.assertTrue(stall["began_ist"].endswith("IST"))
        self.assertTrue(stall["noticed_ist"].endswith("IST"))
        self.assertEqual(stall["work_last_seen"], "2 parent request(s)")
        self.assertTrue(stall["work_seen_at_ist"].endswith("IST"))

    def test_the_stall_is_timed_from_when_it_began(self):
        """Noticing is the end of it; a room's delay started 18s earlier."""
        main._note_loop_stall(18.3, "", datetime.now(main._IST))

        stall = main._loop_stalls[0]
        self.assertNotEqual(stall["began_ist"], stall["noticed_ist"])

    def test_only_the_worst_stalls_are_kept(self):
        for seconds in (3.0, 21.0, 4.0, 9.0, 30.0, 2.5, 15.0):
            main._note_loop_stall(seconds, "", datetime.now(main._IST))

        kept = [stall["seconds"] for stall in main._loop_stalls]
        self.assertEqual(kept, [30.0, 21.0, 15.0, 9.0, 4.0])

    def test_a_longer_stall_is_not_dropped_by_rounding(self):
        """4.04s and 4.01s both read as 4.0s; the longer one must stay."""
        for seconds in (30.0, 21.0, 15.0, 9.0, 4.01):
            main._note_loop_stall(seconds, "", datetime.now(main._IST))
        main._note_loop_stall(4.04, "", datetime.now(main._IST))

        self.assertEqual(main._loop_stalls[-1]["_lag"], 4.04)

    def test_the_ranking_figure_stays_out_of_health(self):
        main._note_loop_stall(7.0, "", datetime.now(main._IST))

        self.assertNotIn("_lag", main.event_loop_lag_health()["stalls"][0])

    def test_the_stalls_outlive_the_rolling_readings(self):
        """The five-minute window rolls past; the incident still counts."""
        main._note_loop_stall(7.0, "", datetime.now(main._IST))

        self.assertEqual(main.event_loop_lag_health()["samples"], 0)
        self.assertEqual(len(main.event_loop_lag_health()["stalls"]), 1)

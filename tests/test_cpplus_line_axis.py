import unittest

from gate_counter import CentroidTracker


class CPPlusLineAxisTests(unittest.TestCase):
    def test_vertical_entry_boundary_counts_diagonal_approach(self):
        tracker = CentroidTracker(max_distance=100, line_axis="vertical")
        tracker.set_line(50, hysteresis=2)

        self.assertEqual(tracker.update([((10, 10, 20, 30), 0.9)]), [])
        crossings = tracker.update([((60, 50, 70, 70), 0.9)])

        self.assertEqual([crossing["direction"] for crossing in crossings], ["IN"])

    def test_vertical_line_classifies_right_to_left_as_out(self):
        tracker = CentroidTracker(max_distance=100, line_axis="vertical")
        tracker.set_line(50, hysteresis=2)

        self.assertEqual(tracker.update([((60, 10, 70, 30), 0.9)]), [])
        crossings = tracker.update([((10, 10, 20, 30), 0.9)])

        self.assertEqual([crossing["direction"] for crossing in crossings], ["OUT"])

    def test_horizontal_line_behavior_remains_available(self):
        tracker = CentroidTracker(
            max_distance=100,
            anchor_y="bottom",
            line_axis="horizontal",
        )
        tracker.set_line(50, hysteresis=2)

        self.assertEqual(tracker.update([((10, 10, 20, 20), 0.9)]), [])
        crossings = tracker.update([((10, 60, 20, 70), 0.9)])

        self.assertEqual([crossing["direction"] for crossing in crossings], ["IN"])


if __name__ == "__main__":
    unittest.main()

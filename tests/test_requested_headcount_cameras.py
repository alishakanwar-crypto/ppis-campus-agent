import unittest

import gate_counter


class RequestedHeadcountCameraTests(unittest.TestCase):
    def test_requested_hikvision_sources_are_enabled(self):
        cameras = {camera["name"]: camera for camera in gate_counter.GATE_CAMERAS}

        self.assertEqual(cameras["ENTRY GATE-1"]["channel"], 20)
        self.assertEqual(cameras["ENTRY GATE-2"]["channel"], 16)
        self.assertEqual(cameras["GALLERY MID"]["channel"], 17)
        self.assertEqual(cameras["DISPERSAL EXIT"]["channel"], 8)
        self.assertEqual(cameras["Basement Main Gate"]["channel"], 12)
        self.assertTrue(any(name.startswith("Reception C") for name in cameras))

    def test_cpplus_source_remains_enabled(self):
        self.assertTrue(
            any("CP Plus" in camera["name"] for camera in gate_counter.HEADCOUNT_CAMERAS)
        )

    def test_dispersal_is_the_only_requested_exit_source(self):
        self.assertEqual(gate_counter._camera_direction("DISPERSAL EXIT"), "OUT")
        for camera in (
            "ENTRY GATE-1",
            "ENTRY GATE-2",
            "GALLERY MID",
            "Reception C1",
            "Basement Main Gate",
        ):
            self.assertEqual(gate_counter._camera_direction(camera), "IN")

    def test_phase_two_candidates_preserve_overlapping_c2_views(self):
        self.assertEqual(
            gate_counter.CANDIDATE_BOUNDARY_CAMERAS,
            {
                "ENTRY GATE-1": "C2",
                "ENTRY GATE-2": "C2",
                "Basement Main Gate": "C4",
            },
        )

    def test_candidate_crossings_keep_image_direction_unverified(self):
        timestamp = gate_counter.datetime(
            2026, 7, 17, 14, 0, tzinfo=gate_counter.IST
        )
        down = gate_counter._build_candidate_boundary_event(
            "ENTRY GATE-1", "IN", 0.5, timestamp
        )
        up = gate_counter._build_candidate_boundary_event(
            "Basement Main Gate", "OUT", 0.5, timestamp
        )

        self.assertEqual(down["boundary"], "C2")
        self.assertEqual(down["image_direction"], "TOP_TO_BOTTOM")
        self.assertEqual(up["boundary"], "C4")
        self.assertEqual(up["image_direction"], "BOTTOM_TO_TOP")
        self.assertEqual(down["timestamp"], "2026-07-17 14:00:00")
        self.assertNotEqual(down["event_id"], up["event_id"])


if __name__ == "__main__":
    unittest.main()

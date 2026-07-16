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


if __name__ == "__main__":
    unittest.main()

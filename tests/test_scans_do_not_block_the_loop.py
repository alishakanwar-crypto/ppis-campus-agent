"""A background face scan must not hold a parent's request.

Face detection and emotion inference take seconds of solid CPU work. Run
directly inside the scanning coroutine they freeze the event loop, and the
agent's own stall record named exactly that: twelve to fourteen second
stalls whose innermost frame was a sighting or mood scan.
"""

import asyncio
import time
import unittest

import mood_detector
import teacher_sighting

DVRS = [{"ip": "192.168.0.12", "user": "admin", "password": "x"}]
MAPPING = {
    "RECEPTION": {"dvr_index": 0, "channel": 1, "all_cameras": []},
}


async def _ticks_while(work, seconds: float = 0.6) -> int:
    """How often a plain timer gets its turn while the scan runs."""
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await asyncio.wait_for(work, timeout=seconds + 5)
    finally:
        ticker.cancel()
    return ticks


class ScansStayOffTheLoopTests(unittest.TestCase):
    def test_a_sighting_scan_leaves_the_loop_free(self):
        tracker = teacher_sighting.TeacherSightingTracker()
        tracker._teacher_encodings = {"TEACHER_1": {}}

        async def frame(dvr, channel):
            return b"jpeg"

        def slow_detect(_frame):
            time.sleep(0.5)
            return []

        tracker._capture_frame = frame
        tracker._detect_teachers = slow_detect
        tracker._detect_faces_with_visitors = lambda f: (slow_detect(f), [])

        ticks = asyncio.run(
            _ticks_while(tracker.scan_cameras_for_sightings(DVRS, MAPPING))
        )

        self.assertGreater(ticks, 5)

    def test_a_mood_scan_leaves_the_loop_free(self):
        watcher = mood_detector.MoodDetector()
        watcher._tracked_encodings = {"CHAIRMAN": {}}

        async def frame(dvr, channel):
            return b"jpeg"

        def slow_detect(_frame):
            time.sleep(0.5)
            return []

        watcher._capture_frame = frame
        watcher._detect_tracked_person = slow_detect

        ticks = asyncio.run(
            _ticks_while(watcher.scan_cameras_for_mood(DVRS, MAPPING))
        )

        self.assertGreater(ticks, 5)


if __name__ == "__main__":
    unittest.main()

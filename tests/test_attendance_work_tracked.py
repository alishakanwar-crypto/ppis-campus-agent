"""Attendance work must be visible to the auto-update from the moment it starts.

The updater exits only when nothing is in flight. A notification that has been
scheduled but has not begun running is still a parent's message owed, so it has
to be counted before it is handed to the loop.
"""
import asyncio
import unittest

from attendance_engine import engine


class ScheduledWorkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine._work_in_flight = 0
        engine._background_tasks.clear()

    async def test_a_scheduled_job_counts_before_it_runs_and_until_it_ends(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def job():
            started.set()
            await release.wait()

        loop = asyncio.get_running_loop()
        engine.schedule_background(job(), loop)
        self.assertGreater(engine.work_in_flight(), 0, "not counted at scheduling")

        await started.wait()
        self.assertGreater(engine.work_in_flight(), 0)

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(engine.work_in_flight(), 0)

    async def test_a_failing_job_is_not_counted_forever(self):
        async def job():
            raise RuntimeError("cloud unreachable")

        engine.schedule_background(job(), asyncio.get_running_loop())
        for _ in range(5):
            await asyncio.sleep(0)

        self.assertEqual(engine.work_in_flight(), 0)

    async def test_nothing_is_counted_when_there_is_no_loop_to_run_it(self):
        async def job():
            return None

        engine.schedule_background(job(), None)

        self.assertEqual(engine.work_in_flight(), 0)


if __name__ == "__main__":
    unittest.main()

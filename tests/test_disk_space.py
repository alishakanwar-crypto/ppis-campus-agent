import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import disk_space  # noqa: E402


class DiskSpaceTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self._here = disk_space._HERE
        disk_space._HERE = Path(self._folder.name)

    def tearDown(self):
        disk_space._HERE = self._here
        self._folder.cleanup()

    def _write(self, name: str, size: int) -> Path:
        path = disk_space._HERE / name
        path.write_bytes(b"x" * size)
        return path

    def test_an_unrotated_log_is_cut_back_to_its_tail(self):
        big = self._write("gate_counter.log", disk_space.KEEP_LOG_BYTES * 3)
        trimmed = disk_space.trim_oversized_logs()
        self.assertEqual(big.stat().st_size, disk_space.KEEP_LOG_BYTES)
        self.assertTrue(any("gate_counter.log" in line for line in trimmed))

    def test_the_end_of_the_log_is_what_survives(self):
        path = disk_space._HERE / "trueface_poller.log"
        path.write_bytes(
            b"o" * (disk_space.KEEP_LOG_BYTES * 2) + b"the last crash"
        )
        disk_space.trim_oversized_logs()
        self.assertTrue(path.read_bytes().endswith(b"the last crash"))

    def test_a_small_log_is_left_alone(self):
        small = self._write("chairman_mood.log", 1024)
        self.assertEqual(disk_space.trim_oversized_logs(), [])
        self.assertEqual(small.stat().st_size, 1024)

    def test_a_filling_drive_is_called_low(self):
        usage = shutil.disk_usage

        def fake_usage(path):
            return type(usage(path))(
                total=500 * 1024 * 1024 * 1024,
                used=0,
                free=500 * 1024 * 1024,
            )

        shutil.disk_usage = fake_usage
        try:
            health = disk_space.disk_health()
        finally:
            shutil.disk_usage = usage
        self.assertTrue(health["low"])
        self.assertEqual(health["free_mb"], 500.0)

    def test_a_roomy_drive_is_not_called_low(self):
        health = disk_space.disk_health()
        self.assertFalse(health["low"])
        self.assertIsNotNone(health["free_mb"])

    def test_the_agents_own_logs_are_measured(self):
        self._write("campus_agent.log", 2 * 1024 * 1024)
        self._write("campus_agent.log.1", 1024 * 1024)
        health = disk_space.disk_health()
        self.assertGreaterEqual(health["logs_mb"], 3.0)

    def test_lines_written_during_a_trim_survive(self):
        path = disk_space._HERE / "wrapper_campus.log"
        path.write_bytes(b"o" * (disk_space.KEEP_LOG_BYTES * 2 + 16))
        real_open = Path.open

        def appending_open(self, mode="r", *args, **kwargs):
            handle = real_open(self, mode, *args, **kwargs)
            if mode == "r+b" and self == path:
                with real_open(self, "ab") as other:
                    other.write(b"exit code 112")
            return handle

        Path.open = appending_open
        try:
            disk_space.trim_oversized_logs()
        finally:
            Path.open = real_open
        self.assertTrue(path.read_bytes().endswith(b"exit code 112"))

    def test_gate_recordings_count_towards_the_drive(self):
        folder = disk_space._HERE / "cpplus_recordings"
        folder.mkdir()
        (folder / "cpplus_a__b.mp4").write_bytes(b"v" * (3 * 1024 * 1024))
        health = disk_space.disk_health()
        self.assertGreaterEqual(health["recordings_mb"], 3.0)
        self.assertEqual(health["snapshots_mb"], 0.0)

    def test_a_relocated_recording_folder_is_still_measured(self):
        elsewhere = Path(self._folder.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "cpplus_a__b.mp4").write_bytes(b"v" * (2 * 1024 * 1024))
        previous = disk_space.GATE_RECORDING_DIR
        disk_space.GATE_RECORDING_DIR = str(elsewhere)
        try:
            health = disk_space.disk_health()
        finally:
            disk_space.GATE_RECORDING_DIR = previous
        self.assertGreaterEqual(health["recordings_mb"], 2.0)

    def test_the_module_imports_without_a_campus_pc(self):
        importlib.reload(disk_space)


if __name__ == "__main__":
    unittest.main()

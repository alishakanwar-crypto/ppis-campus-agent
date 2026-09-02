"""Two processes writing the shared store must not erase each other.

The campus agent, the gate counter and the mood watcher all write this file.
If one saves a snapshot it read before another's write landed, that write is
gone — and a recorder whose refusal is forgotten gets knocked on again, the
exact loop that kept DVR 2 locked all day.
"""
import os
import sys
import unittest
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recorder_auth

REFUSED = "192.168.0.12"
KEY = "abc123"


def _note_attempts(state_path: str, ips: list[str]) -> None:
    """A separate process, exactly as the gate counter is."""
    import importlib

    os.environ["RECORDER_AUTH_STATE"] = state_path
    module = importlib.reload(importlib.import_module("recorder_auth"))
    for ip in ips:
        module.note_attempt(ip)


class ConcurrentStoreTests(unittest.TestCase):
    def test_no_process_loses_its_write_to_another(self):
        recorder_auth.note_refusal(REFUSED, KEY)
        state = str(recorder_auth.STATE_PATH)
        batches = [
            [f"10.0.{worker}.{n}" for n in range(30)] for worker in range(4)
        ]

        with ProcessPoolExecutor(4, mp_context=get_context("spawn")) as pool:
            list(pool.map(_note_attempts, [state] * 4, batches))

        missing = [
            ip for batch in batches for ip in batch
            if recorder_auth.seconds_since_attempt(ip) is None
        ]
        self.assertEqual(missing, [])
        self.assertTrue(recorder_auth.is_refused(REFUSED, KEY))

    def test_a_stale_lock_never_blocks_bookkeeping(self):
        lock = recorder_auth._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("")
        stale = recorder_auth.time.time() - recorder_auth._LOCK_STALE_SECONDS - 1
        os.utime(lock, (stale, stale))

        recorder_auth.note_refusal(REFUSED, KEY)

        self.assertTrue(recorder_auth.is_refused(REFUSED, KEY))
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()

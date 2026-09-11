import unittest

import main


class FakeStateName:
    def __init__(self, name):
        self.name = name


class LinkLivenessTests(unittest.TestCase):
    def test_no_socket_is_not_connected(self):
        self.assertEqual(main._link_liveness(None), (False, "no socket"))

    def test_a_new_library_is_read_from_state(self):
        class NewClient:
            state = FakeStateName("OPEN")

        live, basis = main._link_liveness(NewClient())
        self.assertTrue(live)
        self.assertEqual(basis, "state=OPEN")

        class ClosedClient:
            state = FakeStateName("CLOSED")

        live, basis = main._link_liveness(ClosedClient())
        self.assertFalse(live)
        self.assertEqual(basis, "state=CLOSED")

    def test_the_pinned_library_is_read_from_open(self):
        class OldClient:
            open = True

        self.assertEqual(main._link_liveness(OldClient()), (True, "open"))

    def test_a_socket_we_cannot_read_is_not_torn_down(self):
        class Opaque:
            pass

        live, basis = main._link_liveness(Opaque())
        self.assertIsNone(live)
        self.assertEqual(basis, "unreadable")

    def test_only_a_definite_close_counts_as_disconnected(self):
        class Opaque:
            pass

        original = main.ws_connection
        try:
            main.ws_connection = Opaque()
            self.assertTrue(main._ws_looks_connected())
            main.ws_connection = None
            self.assertFalse(main._ws_looks_connected())
        finally:
            main.ws_connection = original

    def test_health_says_how_the_link_was_judged(self):
        class NewClient:
            state = FakeStateName("OPEN")

        original = main.ws_connection
        try:
            main.ws_connection = NewClient()
            health = main.ws_link_health()
        finally:
            main.ws_connection = original
        self.assertTrue(health["connected"])
        self.assertEqual(health["liveness_basis"], "state=OPEN")
        self.assertIn("library_version", health)


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime
from unittest.mock import Mock

import gate_counter


class CPPlusSDPlaybackTests(unittest.TestCase):
    def test_parses_dahua_file_find_response(self):
        response = "\r\n".join((
            "found=2",
            "items[0].Channel=1",
            "items[0].FilePath=/mnt/sd/2026-07-13/001/a.dav",
            "items[1].Channel=1",
            "items[1].FilePath=/mnt/sd/2026-07-13/001/b.mp4",
            "",
        ))

        self.assertEqual(
            gate_counter._parse_cpplus_recording_paths(response),
            [
                "/mnt/sd/2026-07-13/001/a.dav",
                "/mnt/sd/2026-07-13/001/b.mp4",
            ],
        )

    def test_finds_all_sd_recording_paths_and_closes_finder(self):
        responses = [
            Mock(status_code=200, text="result=42\r\n"),
            Mock(status_code=200, text="OK\r\n"),
            Mock(
                status_code=200,
                text=(
                    "found=2\r\n"
                    "items[0].FilePath=/mnt/sd/a.dav\r\n"
                    "items[1].FilePath=/mnt/sd/b.dav\r\n"
                ),
            ),
            Mock(status_code=200, text="OK\r\n"),
            Mock(status_code=200, text="OK\r\n"),
        ]
        client = Mock()
        client.get.side_effect = responses

        paths = gate_counter._find_cpplus_recording_paths(
            client,
            "http://camera",
            Mock(),
            1,
            datetime(2026, 7, 13, 7),
            datetime(2026, 7, 13, 8),
        )

        self.assertEqual(paths, ["/mnt/sd/a.dav", "/mnt/sd/b.dav"])
        find_call = client.get.call_args_list[1]
        self.assertEqual(find_call.kwargs["params"]["condition.Channel"], "1")
        self.assertEqual(find_call.kwargs["params"]["condition.Types[0]"], "dav")
        self.assertEqual(client.get.call_args_list[-2].kwargs["params"]["action"], "close")
        self.assertEqual(client.get.call_args_list[-1].kwargs["params"]["action"], "destroy")

    def test_prioritizes_latest_completed_hour(self):
        hours = gate_counter._completed_replay_hours(datetime(2026, 7, 13, 13, 41))

        self.assertEqual(hours[0], (datetime(2026, 7, 13, 12), datetime(2026, 7, 13, 13)))
        self.assertEqual(hours[-1], (datetime(2026, 7, 13, 6), datetime(2026, 7, 13, 7)))


if __name__ == "__main__":
    unittest.main()

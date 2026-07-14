import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import gate_counter


class CPPlusSDPlaybackTests(unittest.TestCase):
    def test_logs_into_camera_rpc_playback_session(self):
        challenge = Mock(status_code=200)
        challenge.json.return_value = {
            "session": 42,
            "params": {"realm": "Login to CP Plus", "random": "nonce"},
        }
        success = Mock(status_code=200)
        success.json.return_value = {"result": True, "session": 43}
        client = Mock()
        client.post.side_effect = [challenge, success]

        session = gate_counter._cpplus_rpc_login(
            client, "http://camera", "admin", "secret",
        )

        first_hash = hashlib.md5(
            b"admin:Login to CP Plus:secret"
        ).hexdigest().upper()
        expected = hashlib.md5(
            f"admin:nonce:{first_hash}".encode()
        ).hexdigest().upper()
        self.assertEqual(session, "43")
        login_request = client.post.call_args_list[1]
        self.assertEqual(login_request.kwargs["json"]["params"]["password"], expected)
        self.assertEqual(
            login_request.kwargs["headers"]["Cookie"],
            "DhWebClientSessionID=42",
        )

    @patch("gate_counter._cpplus_rpc_call")
    def test_finds_recordings_through_rpc_playback_session(self, rpc_call):
        rpc_call.side_effect = [
            {"instanceID": 42},
            True,
            {"infos": [
                {"FilePath": "/mnt/sd/a.dav"},
                {"FilePath": "/mnt/sd/b.mp4"},
            ]},
            True,
            True,
        ]
        client = Mock()

        paths = gate_counter._find_cpplus_rpc_recording_paths(
            client,
            "http://camera",
            "session",
            0,
            datetime(2026, 7, 14, 7),
            datetime(2026, 7, 14, 8),
        )

        self.assertEqual(paths, ["/mnt/sd/a.dav", "/mnt/sd/b.mp4"])
        find_params = rpc_call.call_args_list[1].args[4]
        self.assertEqual(find_params["condition"]["Channel"], 0)
        self.assertEqual(find_params["condition"]["Types"], ["dav", "mp4"])
        self.assertEqual(
            rpc_call.call_args_list[-2].args[3], "mediaFileFind.close",
        )
        self.assertEqual(
            rpc_call.call_args_list[-1].args[3], "mediaFileFind.destroy",
        )

    def test_resumes_interrupted_rpc_download_with_range(self):
        first = Mock(
            status_code=206,
            headers={"content-range": "bytes 0-1023/2048"},
        )

        def interrupted_bytes():
            yield b"a" * 1024
            raise gate_counter.httpx.RemoteProtocolError("camera closed stream")

        first.iter_bytes.return_value = interrupted_bytes()
        second = Mock(
            status_code=206,
            headers={"content-range": "bytes 1024-2047/2048"},
        )
        second.iter_bytes.return_value = iter([b"b" * 1024])
        first_context = Mock()
        first_context.__enter__ = Mock(return_value=first)
        first_context.__exit__ = Mock(return_value=False)
        second_context = Mock()
        second_context.__enter__ = Mock(return_value=second)
        second_context.__exit__ = Mock(return_value=False)
        client = Mock()
        client.stream.side_effect = [first_context, second_context]
        keepalive = Mock()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "recording.dav"
            downloaded = gate_counter._download_cpplus_rpc_file(
                client,
                "http://camera/RPC_Loadfile/file",
                {},
                target,
                keepalive=keepalive,
            )

            self.assertTrue(downloaded)
            self.assertEqual(target.read_bytes(), b"a" * 1024 + b"b" * 1024)
        self.assertEqual(
            client.stream.call_args_list[1].kwargs["headers"]["Range"],
            "bytes=1024-8389631",
        )
        self.assertEqual(keepalive.call_count, 2)

    def test_requires_downloaded_segments_to_cover_complete_hour(self):
        hour_start = datetime(2026, 7, 14, 8)
        hour_end = datetime(2026, 7, 14, 9)
        complete_paths = [
            f"/mnt/sd/08/{start}-{end}[M][0@0][0].dav"
            for start, end in (
                ("07.59.59", "08.07.00"),
                ("08.07.00", "08.15.00"),
                ("08.15.00", "08.23.00"),
                ("08.23.00", "08.31.00"),
                ("08.31.00", "08.39.00"),
                ("08.39.00", "08.47.00"),
                ("08.47.00", "08.55.00"),
                ("08.55.00", "09.03.00"),
            )
        ]

        self.assertTrue(gate_counter._cpplus_recordings_cover_hour(
            complete_paths, hour_start, hour_end,
        ))
        self.assertFalse(gate_counter._cpplus_recordings_cover_hour(
            complete_paths[:2], hour_start, hour_end,
        ))

    @patch("gate_counter._download_cpplus_rpc_file", return_value=True)
    @patch("gate_counter._cpplus_rpc_call")
    @patch("gate_counter._find_cpplus_rpc_recording_paths")
    @patch("gate_counter._cpplus_rpc_login", return_value="session")
    def test_rejects_incomplete_camera_hour(
        self, login, find_paths, rpc_call, download_file,
    ):
        find_paths.return_value = [
            "/mnt/sd/08/07.59.59-08.07.00[M][0@0][0].dav",
            "/mnt/sd/08/08.07.00-08.15.00[M][0@0][0].dav",
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = gate_counter._download_cpplus_rpc_recordings(
                Mock(),
                "http://camera",
                "admin",
                "secret",
                [0],
                datetime(2026, 7, 14, 8),
                datetime(2026, 7, 14, 9),
                Path(directory) / "recording.dav",
            )

        self.assertIsNone(result)
        self.assertEqual(download_file.call_count, 2)

    @patch("gate_counter._cpplus_rpc_call")
    @patch("gate_counter._find_cpplus_rpc_recording_paths", return_value=[])
    @patch("gate_counter._cpplus_rpc_login", return_value="session")
    def test_logs_out_camera_rpc_session(self, login, find_paths, rpc_call):
        result = gate_counter._download_cpplus_rpc_recordings(
            Mock(),
            "http://camera",
            "admin",
            "secret",
            [0],
            datetime(2026, 7, 14, 7),
            datetime(2026, 7, 14, 8),
            Path("recording.dav"),
        )

        self.assertIsNone(result)
        self.assertEqual(rpc_call.call_args.args[3], "global.logout")

    @patch("gate_counter.httpx.Client")
    @patch("gate_counter._download_cpplus_rpc_recordings")
    def test_prefers_rpc_playback_session_before_legacy_cgi(
        self, rpc_download, client_class,
    ):
        expected = [Path("recording.dav")]
        rpc_download.return_value = expected
        client_class.return_value.__enter__.return_value = Mock()
        cam = {"ip": "camera", "user": "admin", "pass": "secret"}

        result = gate_counter._download_cpplus_recording(
            cam,
            datetime(2026, 7, 14, 7),
            datetime(2026, 7, 14, 8),
            Path("recording.dav"),
        )

        self.assertEqual(result, expected)
        rpc_download.assert_called_once()

    @patch("gate_counter.httpx.post")
    def test_uploads_recording_source_with_recount(self, post):
        post.return_value.raise_for_status.return_value = None

        uploaded = gate_counter._post_cpplus_recount(
            datetime(2026, 7, 14, 7),
            datetime(2026, 7, 14, 8),
            12,
            7200,
            "camera_sd_recording",
        )

        self.assertTrue(uploaded)
        self.assertEqual(
            post.call_args.kwargs["json"]["source"], "camera_sd_recording",
        )

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

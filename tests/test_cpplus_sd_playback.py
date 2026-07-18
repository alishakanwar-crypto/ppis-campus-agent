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

    @patch("gate_counter._cpplus_rpc_call")
    def test_reads_hourly_count_from_camera_people_counter(self, rpc_call):
        rpc_call.side_effect = [
            {"instanceID": 17},
            {"token": 23, "totalCount": 1},
            {
                "info": [
                    {
                        "StartTime": "2026-07-14 07:00:00",
                        "EndTime": "2026-07-14 07:59:59",
                        "RuleName": "NumberStat",
                        "EnteredSubtotal": 31,
                        "ExitedSubtotal": 4,
                    },
                ],
            },
            True,
        ]

        count = gate_counter._cpplus_native_hourly_count(
            Mock(),
            "http://camera",
            "session",
            datetime(2026, 7, 14, 7),
            datetime(2026, 7, 14, 8),
        )

        self.assertEqual(count, 31)
        self.assertEqual(
            rpc_call.call_args_list[1].args[3],
            "videoStatServer.startFind",
        )
        self.assertEqual(
            rpc_call.call_args_list[-1].args[3],
            "videoStatServer.stopFind",
        )

    def test_parses_live_camera_people_count_summary(self):
        response = "\r\n".join((
            "summary.Channel=0",
            "summary.EnteredSubtotal.Hour=7",
            "summary.EnteredSubtotal.Today=31",
            "summary.ExitedSubtotal.Today=4",
            "summary.RuleName=NumberStat",
        ))

        self.assertEqual(
            gate_counter._parse_cpplus_native_summary(response),
            (31, 4, 7),
        )

    def test_live_camera_summary_produces_completed_hour_delta(self):
        state, completed = gate_counter._cpplus_native_summary_transition(
            {},
            datetime(2026, 7, 15, 13, 58, tzinfo=gate_counter.IST),
            20,
        )
        self.assertIsNone(completed)
        self.assertFalse(state["complete"])

        state, completed = gate_counter._cpplus_native_summary_transition(
            state,
            datetime(2026, 7, 15, 14, 0, 2, tzinfo=gate_counter.IST),
            20,
        )
        self.assertIsNone(completed)
        self.assertTrue(state["complete"])

        state, completed = gate_counter._cpplus_native_summary_transition(
            state,
            datetime(2026, 7, 15, 15, 0, 2, tzinfo=gate_counter.IST),
            27,
        )
        self.assertEqual(
            completed,
            (
                datetime(2026, 7, 15, 14, tzinfo=gate_counter.IST),
                datetime(2026, 7, 15, 15, tzinfo=gate_counter.IST),
                7,
            ),
        )

    def test_live_camera_summary_rejects_late_boundary_sample(self):
        state = {
            "date": "2026-07-15",
            "hour_start": "2026-07-15 14:00:00",
            "entered_today": 20,
            "complete": True,
        }

        state, completed = gate_counter._cpplus_native_summary_transition(
            state,
            datetime(2026, 7, 15, 15, 1, tzinfo=gate_counter.IST),
            27,
        )

        self.assertIsNone(completed)
        self.assertFalse(state["complete"])

    def test_missed_boundary_poll_still_completes_hour(self):
        # First sample lands cleanly at the top of the hour: baseline is valid.
        state, completed = gate_counter._cpplus_native_summary_transition(
            {},
            datetime(2026, 7, 15, 14, 0, 5, tzinfo=gate_counter.IST),
            20,
            0,
        )
        self.assertIsNone(completed)
        self.assertTrue(state["complete"])

        # Intra-hour poll records the running end value for the hour.
        state, completed = gate_counter._cpplus_native_summary_transition(
            state,
            datetime(2026, 7, 15, 14, 42, tzinfo=gate_counter.IST),
            25,
            5,
        )
        self.assertIsNone(completed)
        self.assertEqual(state["entered_end"], 25)

        # The 15:00:00-10 boundary poll is missed; the first sample of the new
        # hour arrives late, yet the camera's current-hour subtotal reconstructs
        # the exact 14:00-15:00 boundary and keeps the new baseline valid.
        state, completed = gate_counter._cpplus_native_summary_transition(
            state,
            datetime(2026, 7, 15, 15, 0, 40, tzinfo=gate_counter.IST),
            27,
            2,
        )
        self.assertEqual(
            completed,
            (
                datetime(2026, 7, 15, 14, tzinfo=gate_counter.IST),
                datetime(2026, 7, 15, 15, tzinfo=gate_counter.IST),
                5,
            ),
        )
        self.assertTrue(state["complete"])
        self.assertEqual(state["entered_today"], 25)

        state, completed = gate_counter._cpplus_native_summary_transition(
            state,
            datetime(2026, 7, 15, 16, 1, tzinfo=gate_counter.IST),
            33,
            0,
        )
        self.assertEqual(
            completed,
            (
                datetime(2026, 7, 15, 15, tzinfo=gate_counter.IST),
                datetime(2026, 7, 15, 16, tzinfo=gate_counter.IST),
                8,
            ),
        )

    def test_pending_queue_dedupes_and_retries_failed_upload(self):
        hour_start = datetime(2026, 7, 15, 14, tzinfo=gate_counter.IST)
        hour_end = datetime(2026, 7, 15, 15, tzinfo=gate_counter.IST)

        pending = gate_counter._queue_cpplus_native_pending(
            [], hour_start, hour_end, 5,
        )
        # Re-queuing the same hour must not create a duplicate entry.
        pending = gate_counter._queue_cpplus_native_pending(
            pending, hour_start, hour_end, 6,
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["in_count"], 6)

        with patch("gate_counter._post_cpplus_recount", return_value=False):
            remaining = gate_counter._flush_cpplus_native_pending(pending)
        self.assertEqual(remaining, pending)

        with patch(
            "gate_counter._post_cpplus_recount", return_value=True,
        ) as post:
            remaining = gate_counter._flush_cpplus_native_pending(pending)
        self.assertEqual(remaining, [])
        self.assertEqual(post.call_args.args[4], "camera_native_counter")

    def test_pending_queue_round_trips_through_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.json"
            with patch.object(
                gate_counter, "CPPLUS_NATIVE_SUMMARY_PENDING_FILE", path,
            ):
                entry = [{
                    "hour_start": "2026-07-15 14:00:00",
                    "hour_end": "2026-07-15 15:00:00",
                    "in_count": 5,
                }]
                gate_counter._save_cpplus_native_summary_pending(entry)
                self.assertEqual(
                    gate_counter._load_cpplus_native_summary_pending(), entry,
                )

    @patch("gate_counter._cpplus_rpc_call")
    def test_rejects_missing_camera_people_count_statistics(self, rpc_call):
        rpc_call.side_effect = [
            {"instanceID": 17},
            {"token": 23, "totalCount": 0},
            True,
        ]

        count = gate_counter._cpplus_native_hourly_count(
            Mock(),
            "http://camera",
            "session",
            datetime(2026, 7, 14, 7),
            datetime(2026, 7, 14, 8),
        )

        self.assertIsNone(count)
        self.assertEqual(
            rpc_call.call_args_list[-1].args[3],
            "videoStatServer.stopFind",
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

    def test_replays_recording_when_native_recount_upload_is_rejected(self):
        hour_start = datetime(2026, 7, 14, 7)
        hour_end = datetime(2026, 7, 14, 8)
        original_running = gate_counter.running
        gate_counter.running = True

        def stop_worker(_seconds):
            gate_counter.running = False

        try:
            with (
                patch("gate_counter._load_cpplus_replay_state", return_value={}),
                patch(
                    "gate_counter._completed_replay_hours",
                    return_value=[(hour_start, hour_end)],
                ),
                patch("gate_counter._fetch_cpplus_native_hourly_count", return_value=4),
                patch("gate_counter._post_cpplus_recount", side_effect=[False, True]) as post,
                patch(
                    "gate_counter._local_recordings_for_hour",
                    return_value=[Path("recording.mp4")],
                ),
                patch("gate_counter.count_cpplus_recordings", return_value=(31, 7200)) as count,
                patch("gate_counter.PersonDetector"),
                patch("gate_counter._save_cpplus_replay_state"),
                patch("gate_counter.time.sleep", side_effect=stop_worker),
            ):
                gate_counter.run_cpplus_replay_worker({"name": "CP Plus"})
        finally:
            gate_counter.running = original_running

        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[-1], "camera_native_counter")
        self.assertEqual(post.call_args_list[1].args[-1], "school_pc_recording")
        count.assert_called_once()

    def test_retries_pending_native_history_before_expensive_replay(self):
        hour_start = datetime(2026, 7, 14, 7, tzinfo=gate_counter.IST)
        hour_end = datetime(2026, 7, 14, 8, tzinfo=gate_counter.IST)
        original_running = gate_counter.running
        gate_counter.running = True

        def stop_worker(_seconds):
            gate_counter.running = False

        try:
            with (
                patch("gate_counter._load_cpplus_replay_state", return_value={}),
                patch(
                    "gate_counter._completed_replay_hours",
                    return_value=[(hour_start, hour_end)],
                ),
                patch(
                    "gate_counter._fetch_cpplus_native_hourly_count",
                    return_value=None,
                ),
                patch(
                    "gate_counter._cpplus_native_history_grace_open",
                    return_value=True,
                ),
                patch("gate_counter._local_recordings_for_hour") as local_recordings,
                patch("gate_counter._post_cpplus_recount") as post,
                patch("gate_counter.time.sleep", side_effect=stop_worker),
            ):
                gate_counter.run_cpplus_replay_worker({"name": "CP Plus"})
        finally:
            gate_counter.running = original_running

        local_recordings.assert_not_called()
        post.assert_not_called()

    def test_segment_worker_uploads_completed_hour_as_non_additive_check(self):
        hour_start = datetime(2026, 7, 14, 7, tzinfo=gate_counter.IST)
        hour_end = datetime(2026, 7, 14, 8, tzinfo=gate_counter.IST)
        original_running = gate_counter.running
        gate_counter.running = True

        def stop_worker(_seconds):
            gate_counter.running = False

        midpoint = datetime(2026, 7, 14, 7, 30, tzinfo=gate_counter.IST)
        tracker = Mock()
        try:
            with (
                patch("gate_counter._load_cpplus_segment_replay_state", return_value={}),
                patch(
                    "gate_counter._segment_replay_hours",
                    return_value=[(hour_start, hour_end)],
                ),
                patch(
                    "gate_counter._local_recording_segments_for_hour",
                    return_value=[
                        (hour_start, midpoint, Path("first.mp4")),
                        (midpoint, hour_end, Path("second.mp4")),
                    ],
                ),
                patch(
                    "gate_counter._build_cpplus_replay_tracker",
                    return_value=tracker,
                ),
                patch(
                    "gate_counter._count_cpplus_recording_paths_with_tracker",
                    side_effect=[(14, 3600), (15, 3600)],
                ) as count,
                patch("gate_counter._post_cpplus_recount", return_value=True) as post,
                patch("gate_counter.PersonDetector"),
                patch("gate_counter._save_cpplus_segment_replay_state") as save,
                patch("gate_counter.time.sleep", side_effect=stop_worker),
            ):
                gate_counter.run_cpplus_segment_replay_worker({"name": "CP Plus"})
        finally:
            gate_counter.running = original_running

        self.assertEqual(count.call_count, 2)
        self.assertIs(count.call_args_list[0].args[3], tracker)
        self.assertIs(count.call_args_list[1].args[3], tracker)
        self.assertEqual(post.call_args.args[2], 29)
        self.assertEqual(post.call_args.args[3], 7200)
        self.assertEqual(post.call_args.args[4], "school_pc_segment_recording")
        save.assert_called_once()

    def test_segment_worker_does_not_upload_hour_with_recording_gap(self):
        hour_start = datetime(2026, 7, 14, 7, tzinfo=gate_counter.IST)
        first_end = datetime(2026, 7, 14, 7, 20, tzinfo=gate_counter.IST)
        second_start = datetime(2026, 7, 14, 7, 30, tzinfo=gate_counter.IST)
        hour_end = datetime(2026, 7, 14, 8, tzinfo=gate_counter.IST)
        original_running = gate_counter.running
        gate_counter.running = True

        def stop_worker(_seconds):
            gate_counter.running = False

        try:
            with (
                patch("gate_counter._load_cpplus_segment_replay_state", return_value={}),
                patch(
                    "gate_counter._segment_replay_hours",
                    return_value=[(hour_start, hour_end)],
                ),
                patch(
                    "gate_counter._local_recording_segments_for_hour",
                    return_value=[
                        (hour_start, first_end, Path("first.mp4")),
                        (second_start, hour_end, Path("second.mp4")),
                    ],
                ),
                patch("gate_counter._build_cpplus_replay_tracker"),
                patch(
                    "gate_counter._count_cpplus_recording_paths_with_tracker",
                    return_value=(14, 2400),
                ) as count,
                patch("gate_counter._post_cpplus_recount") as post,
                patch("gate_counter.PersonDetector"),
                patch("gate_counter._save_cpplus_segment_replay_state") as save,
                patch("gate_counter.time.sleep", side_effect=stop_worker),
            ):
                gate_counter.run_cpplus_segment_replay_worker({"name": "CP Plus"})
        finally:
            gate_counter.running = original_running

        count.assert_called_once()
        post.assert_not_called()
        save.assert_not_called()

    def test_prioritizes_latest_completed_hour(self):
        hours = gate_counter._completed_replay_hours(datetime(2026, 7, 13, 13, 41))

        self.assertEqual(hours[0], (datetime(2026, 7, 13, 12), datetime(2026, 7, 13, 13)))
        self.assertEqual(hours[-1], (datetime(2026, 7, 13, 6), datetime(2026, 7, 13, 7)))


if __name__ == "__main__":
    unittest.main()

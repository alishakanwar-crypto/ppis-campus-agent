"""Classroom cameras no longer mark or announce a child's attendance.

Teacher and staff attendance comes from the TrueFace device and must carry on,
and the same classroom cameras must keep serving a parent's snapshot.
"""
import asyncio
import unittest
from unittest.mock import patch

import attendance_engine
from attendance_engine import engine


class StudentCameraAttendanceOffTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(attendance_engine.STUDENT_CAMERA_ATTENDANCE)

    def test_a_clear_student_match_is_not_marked(self):
        with patch.object(attendance_engine.db, "log_attendance") as logged:
            marked = engine._process_attendance(
                person_id="TANISHQ_GRADE2B",
                name="TANISHQ",
                phone="9",
                confidence=0.60,
                image_bytes=b"",
                face_location=(0, 0, 0, 0),
                camera_source="GRADE 2B (DVR 2 Ch 41)",
                margin=0.20,
            )

        self.assertIsNone(marked)
        logged.assert_not_called()

    def test_a_student_notification_is_withheld(self):
        with patch.object(attendance_engine.httpx, "AsyncClient") as client:
            asyncio.run(engine._send_whatsapp_notification(
                attendance_id=1,
                person_id="TANISHQ_GRADE2B",
                name="TANISHQ",
                time_str="08:10 AM",
                phone="9",
            ))

        client.assert_not_called()

    def test_a_teacher_notification_still_goes_out(self):
        with patch.object(attendance_engine.httpx, "AsyncClient") as client:
            client.side_effect = RuntimeError("stop after the guard")
            asyncio.run(engine._send_whatsapp_notification(
                attendance_id=1,
                person_id="TEACHER_SUNITA",
                name="SUNITA",
                time_str="07:10 AM",
                phone="9",
            ))

        self.assertTrue(client.called)

    def test_classroom_cameras_are_still_known_for_snapshots(self):
        dvrs = [{"ip": "192.168.0.12", "username": "u", "password": "p"}]
        cameras = engine.build_classroom_camera_list({
            "GRADE 2B": {"all_cameras": [{"dvr_index": 0, "channel": 41}]},
        }, dvrs)

        self.assertEqual(
            [c["cam_type"] for c in cameras], ["classroom"])


if __name__ == "__main__":
    unittest.main()

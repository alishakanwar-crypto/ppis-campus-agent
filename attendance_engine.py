"""
Face recognition attendance engine.

Monitors DVR camera feeds via RTSP or periodic ISAPI snapshots,
detects faces, matches against registered encodings, and logs attendance.
Supports a test mode that only tracks a single person_id.
"""

import asyncio
import io
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

try:
    import dlib
except ImportError:
    dlib = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import face_recognition
except ImportError:
    face_recognition = None

try:
    from PIL import Image
except ImportError:
    Image = None

import database as db
import face_db

logger = logging.getLogger("ppis-agent.attendance")

ATTENDANCE_SNAPSHOTS_DIR = Path(__file__).parent / "attendance_snapshots"
ATTENDANCE_SNAPSHOTS_DIR.mkdir(exist_ok=True)

# Minimum seconds between attendance entries for the same person
COOLDOWN_SECONDS = 300  # 5 minutes


class AttendanceEngine:
    """Runs face recognition attendance monitoring."""

    def __init__(self):
        self.running = False
        self.test_mode = True  # Only track test_person_id when True
        self.test_person_id = "TEST001"
        self.confidence_threshold = 0.85  # Match confidence > 85%
        self.known_faces: dict = {}
        self.last_attendance: dict[str, float] = {}  # person_id -> timestamp
        self.debug_logs: list[dict] = []
        self.max_debug_logs = 500
        self.scan_interval = 3.0  # seconds between scans
        self.whatsapp_api_url = ""
        self.whatsapp_phone = ""
        self._task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

    def add_debug_log(self, event: str, details: str = "",
                      person_id: str = "", confidence: float = 0.0):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": event,
            "details": details,
            "person_id": person_id,
            "confidence": confidence,
        }
        self.debug_logs.append(entry)
        if len(self.debug_logs) > self.max_debug_logs:
            self.debug_logs = self.debug_logs[-self.max_debug_logs:]
        logger.info(f"[ATTENDANCE] {event}: {details} "
                     f"(person={person_id}, conf={confidence:.2f})")

    def reload_faces(self):
        """Reload registered faces from database."""
        self.known_faces = face_db.load_known_faces()
        self.add_debug_log("faces_reloaded",
                           f"{len(self.known_faces)} person(s) loaded")

    def recognize_faces_in_image(self, image_bytes: bytes,
                                 camera_source: str = "") -> list[dict]:
        """Detect and recognize faces in a single image.

        Returns list of recognition results.
        """
        if face_recognition is None:
            self.add_debug_log("error", "face_recognition library not available")
            return []

        # Use dlib native file loader to avoid numpy ABI issues on Windows
        tmp_path = None
        try:
            if Image is not None and dlib is not None:
                pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                max_dim = 800
                if max(pil_img.size) > max_dim:
                    pil_img.thumbnail((max_dim, max_dim), Image.LANCZOS)
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                    pil_img.save(f, format="JPEG", quality=95)
                    tmp_path = f.name
                img_array = dlib.load_rgb_image(tmp_path)
            else:
                img_array = face_recognition.load_image_file(io.BytesIO(image_bytes))
        except Exception as e:
            self.add_debug_log("error", f"Failed to load image: {e}")
            return []
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

        face_locations = face_recognition.face_locations(img_array, model="hog")

        if not face_locations:
            self.add_debug_log("no_face_detected",
                               f"No faces in frame from {camera_source}")
            return []

        self.add_debug_log("face_detected",
                           f"{len(face_locations)} face(s) detected from {camera_source}")

        face_encodings = face_recognition.face_encodings(img_array, face_locations)
        results = []

        for i, (encoding, location) in enumerate(zip(face_encodings, face_locations)):
            match_result = self._match_face(encoding)

            if match_result:
                person_id = match_result["person_id"]
                confidence = match_result["confidence"]

                # In test mode, only process test_person_id
                if self.test_mode and person_id != self.test_person_id:
                    self.add_debug_log("test_mode_skip",
                                       f"Ignoring non-test person {person_id}",
                                       person_id=person_id,
                                       confidence=confidence)
                    continue

                self.add_debug_log("face_matched",
                                   f"Matched {match_result['name']} "
                                   f"(confidence: {confidence:.1%})",
                                   person_id=person_id,
                                   confidence=confidence)

                if confidence >= self.confidence_threshold:
                    result = self._process_attendance(
                        person_id=person_id,
                        name=match_result["name"],
                        phone=match_result["phone"],
                        confidence=confidence,
                        image_bytes=image_bytes,
                        face_location=location,
                        camera_source=camera_source,
                    )
                    if result:
                        results.append(result)
                else:
                    self.add_debug_log("low_confidence",
                                       f"Confidence {confidence:.1%} < "
                                       f"{self.confidence_threshold:.0%} threshold",
                                       person_id=person_id,
                                       confidence=confidence)
            else:
                if not self.test_mode:
                    self.add_debug_log("face_unknown",
                                       "Unregistered face detected")

        return results

    def _match_face(self, encoding: np.ndarray) -> dict | None:
        """Match a face encoding against all known faces.

        Returns best match or None.
        """
        best_match = None
        best_confidence = 0.0

        for person_id, person_data in self.known_faces.items():
            known_encodings = person_data["encodings"]
            if not known_encodings:
                continue

            distances = face_recognition.face_distance(known_encodings, encoding)
            min_distance = float(np.min(distances))
            # Convert distance to confidence (0-1 scale)
            confidence = max(0.0, 1.0 - min_distance)

            if confidence > best_confidence:
                best_confidence = confidence
                best_match = {
                    "person_id": person_id,
                    "name": person_data["name"],
                    "phone": person_data["phone"],
                    "confidence": confidence,
                    "distance": min_distance,
                }

        return best_match

    def _process_attendance(self, person_id: str, name: str, phone: str,
                            confidence: float, image_bytes: bytes,
                            face_location: tuple,
                            camera_source: str) -> dict | None:
        """Process an attendance detection: check cooldown, log, and notify."""
        now = time.time()
        last = self.last_attendance.get(person_id, 0)

        if now - last < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - (now - last))
            self.add_debug_log("cooldown_active",
                               f"{name} on cooldown, {remaining}s remaining",
                               person_id=person_id,
                               confidence=confidence)
            return None

        # Save snapshot of the detected face
        ts = int(now)
        snapshot_filename = f"attendance_{person_id}_{ts}.jpg"
        snapshot_path = ATTENDANCE_SNAPSHOTS_DIR / snapshot_filename
        with open(snapshot_path, "wb") as f:
            f.write(image_bytes)

        # Log attendance
        attendance_id = db.log_attendance(
            person_id=person_id,
            name=name,
            status="Present",
            confidence=confidence,
            snapshot_path=str(snapshot_path),
            camera_source=camera_source,
        )

        self.last_attendance[person_id] = now
        time_str = datetime.now().strftime("%I:%M %p")

        self.add_debug_log("attendance_marked",
                           f"{name} marked Present at {time_str} "
                           f"(confidence: {confidence:.1%})",
                           person_id=person_id,
                           confidence=confidence)

        result = {
            "attendance_id": attendance_id,
            "person_id": person_id,
            "name": name,
            "status": "Present",
            "confidence": confidence,
            "time": time_str,
            "snapshot": snapshot_filename,
            "camera_source": camera_source,
        }

        # Trigger WhatsApp notification asynchronously
        if phone:
            task = asyncio.create_task(
                self._send_whatsapp_notification(
                    attendance_id=attendance_id,
                    name=name,
                    time_str=time_str,
                    phone=phone,
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return result

    async def _send_whatsapp_notification(self, attendance_id: int,
                                           name: str, time_str: str,
                                           phone: str):
        """Send WhatsApp attendance notification via cloud bot API."""
        message = f"[Attendance] {name} has arrived at {time_str}."

        # Try cloud bot WhatsApp API
        api_url = self.whatsapp_api_url or "https://app-itszlsnn.fly.dev"
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {}
        if agent_secret:
            headers["X-Agent-Secret"] = agent_secret
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{api_url}/api/send-whatsapp",
                    json={
                        "phone": phone,
                        "message": message,
                        "type": "attendance_notification",
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    db.update_whatsapp_sent(attendance_id)
                    self.add_debug_log("whatsapp_sent",
                                       f"Notification sent to {phone}: {message}")
                else:
                    self.add_debug_log("whatsapp_failed",
                                       f"API returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            self.add_debug_log("whatsapp_error", f"Failed to send: {e}")

    async def capture_frame_from_dvr(self, dvr: dict, channel: int) -> bytes | None:
        """Capture a single frame from a Hikvision DVR via ISAPI snapshot."""
        ip = dvr["ip"]
        port = dvr.get("port", 80)
        user = dvr["username"]
        pwd = dvr["password"]

        stream_channel = channel * 100 + 1
        url = f"http://{ip}:{port}/ISAPI/Streaming/channels/{stream_channel}/picture"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, auth=httpx.DigestAuth(user, pwd))
                if resp.status_code == 401:
                    resp = await client.get(url, auth=httpx.BasicAuth(user, pwd))
                if resp.status_code == 200 and resp.headers.get(
                        "content-type", "").startswith("image"):
                    return resp.content
        except Exception as e:
            self.add_debug_log("dvr_error", f"Capture failed from {ip} ch{channel}: {e}")
        return None

    async def scan_camera(self, dvr: dict, channel: int,
                          camera_label: str = "") -> list[dict]:
        """Capture a frame from a camera and run face recognition on it."""
        frame = await self.capture_frame_from_dvr(dvr, channel)
        if frame is None:
            return []

        source = camera_label or f"{dvr['ip']}:ch{channel}"
        return self.recognize_faces_in_image(frame, camera_source=source)

    async def monitoring_loop(self, dvrs: list[dict],
                               entrance_camera: dict | None = None):
        """Continuous monitoring loop scanning entrance camera(s).

        Args:
            dvrs: List of DVR configs
            entrance_camera: Specific camera to monitor, e.g.
                {"dvr_index": 0, "channel": 1, "label": "Entrance"}
                If None, uses the first available DVR channel 1.
        """
        self.running = True
        self.reload_faces()

        if entrance_camera:
            dvr_idx = entrance_camera.get("dvr_index", 0)
            channel = entrance_camera.get("channel", 1)
            label = entrance_camera.get("label", "Entrance")
        else:
            dvr_idx = 0
            channel = 1
            label = "Default Entrance"

        self.add_debug_log("monitoring_started",
                           f"Monitoring {label} (DVR {dvr_idx + 1}, Ch {channel}), "
                           f"interval={self.scan_interval}s, "
                           f"test_mode={'ON' if self.test_mode else 'OFF'}")

        try:
            while self.running:
                try:
                    if dvr_idx < len(dvrs):
                        await self.scan_camera(dvrs[dvr_idx], channel, label)
                    else:
                        self.add_debug_log("error",
                                           f"DVR index {dvr_idx} out of range "
                                           f"(have {len(dvrs)} DVRs)")
                        await asyncio.sleep(30)
                        continue
                except Exception as e:
                    self.add_debug_log("scan_error", f"Error in scan loop: {e}")

                await asyncio.sleep(self.scan_interval)
        finally:
            self.running = False
            self.add_debug_log("monitoring_stopped", "Attendance monitoring stopped")

    def stop(self):
        """Stop the monitoring loop."""
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self.add_debug_log("monitoring_stopped", "Stop requested")

    def get_status(self) -> dict:
        """Return current engine status."""
        return {
            "running": self.running,
            "test_mode": self.test_mode,
            "test_person_id": self.test_person_id,
            "confidence_threshold": self.confidence_threshold,
            "scan_interval": self.scan_interval,
            "registered_persons": len(self.known_faces),
            "total_encodings": sum(
                len(p["encodings"]) for p in self.known_faces.values()
            ),
            "cooldown_seconds": COOLDOWN_SECONDS,
        }

    def get_debug_logs(self, limit: int = 100) -> list[dict]:
        """Return recent debug logs."""
        return self.debug_logs[-limit:]


# Module-level singleton
engine = AttendanceEngine()

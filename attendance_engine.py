"""
Face recognition attendance engine.

Monitors DVR camera feeds via RTSP or periodic ISAPI snapshots,
detects faces, matches against registered encodings, and logs attendance.

Supports:
- Single-camera test mode (tracks one person_id)
- Multi-camera classroom-wise attendance (all classrooms simultaneously)
  Each camera only checks faces of students in that class.
"""

import asyncio
import io
import logging
import os
import re
import tempfile
import time
from datetime import datetime, date, timedelta
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

# Attendance time window (7:15 AM to 8:00 AM)
ATTENDANCE_START_HOUR = 7
ATTENDANCE_START_MINUTE = 15
ATTENDANCE_END_HOUR = 8
ATTENDANCE_END_MINUTE = 0

# Grade pattern to extract grade from camera location names
_GRADE_RE = re.compile(
    r"(?:GRADE\s*(\d+[A-Z]?))"
    r"|(?:(NUR|NURSERY)[\s\-]*(\d?))"
    r"|(?:(PREP)[\s\-]*(\d?))"
    r"|(?:(Popsicles?))",
    re.IGNORECASE,
)


def _normalize_grade(grade_str: str) -> str:
    """Normalize grade string to a canonical form for matching.

    Examples:
        'Grade 3C' -> 'GRADE3C'
        'GRADE 10A' -> 'GRADE10A'
        'NUR-1' -> 'NUR1'
        'PREP-2' -> 'PREP2'
        'Popsicles' -> 'POPSICLES'
    """
    s = grade_str.upper().strip()
    s = re.sub(r"[\s\-]+", "", s)
    return s


def _extract_grade_from_location(location: str) -> str | None:
    """Extract grade/class from a camera location string.

    Returns normalized grade or None if not a classroom camera.
    """
    m = _GRADE_RE.search(location)
    if not m:
        return None
    if m.group(1):  # GRADE Nx
        return f"GRADE{m.group(1).upper()}"
    if m.group(2):  # NUR/NURSERY
        n = m.group(3) or "1"
        return f"NUR{n}"
    if m.group(4):  # PREP
        n = m.group(5) or "1"
        return f"PREP{n}"
    if m.group(6):  # Popsicles
        return "POPSICLES"
    return None


def _grade_from_person_id(person_id: str) -> str | None:
    """Extract grade from person_id like 'SUHAAN_AHUJA_GRADE3C'.

    The person_id format from parent photo registration:
        STUDENT_NAME_GRADEXX  (e.g., SUHAAN_AHUJA_GRADE3C)
    """
    m = re.search(
        r"(GRADE\d+[A-Z]?|NUR\d*|PREP\d*|POPSICLES?)$",
        person_id.upper(),
    )
    return m.group(1) if m else None


class AttendanceEngine:
    """Runs face recognition attendance monitoring."""

    def __init__(self):
        self.running = False
        self.classwise_running = False
        self.test_mode = True  # Only track test_person_id when True
        self.test_person_id = "TEST001"
        self.confidence_threshold = 0.10  # Match confidence > 10%
        self.known_faces: dict = {}
        self.last_attendance: dict[str, float] = {}  # person_id -> timestamp
        self.daily_marked: dict[str, str] = {}  # person_id -> date string
        self._notification_sent: dict[str, str] = {}  # person_id -> date (dedup notifications)
        self.debug_logs: list[dict] = []
        self.max_debug_logs = 500
        self.scan_interval = 3.0  # seconds between scans
        self.whatsapp_api_url = ""
        self.whatsapp_phone = ""
        self._task: asyncio.Task | None = None
        self._classwise_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

        # Classwise monitoring state
        self._classwise_stats: dict = {
            "total_cameras": 0,
            "cameras_scanned": 0,
            "current_camera": "",
            "cycle_count": 0,
            "last_cycle_duration": 0.0,
            "faces_detected_total": 0,
            "attendance_marked_today": 0,
            "errors": 0,
        }

        # Cache: grade -> list of (person_id, person_data)
        self._grade_face_cache: dict[str, dict] = {}

        # Auto-recovery tracking
        self._camera_errors: dict[str, int] = {}  # cam_key -> consecutive errors
        self._health: dict = {
            "camera_feed": "ok",
            "recognition_engine": "ok",
            "notification_system": "ok",
            "last_health_check": "",
            "uptime_start": datetime.now().isoformat(),
            "total_recoveries": 0,
            "auto_start_enabled": True,
        }
        self._admin_alerted: set = set()  # Track which issues already alerted

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
        self._rebuild_grade_cache()
        self.add_debug_log("faces_reloaded",
                           f"{len(self.known_faces)} person(s) loaded")

    def _rebuild_grade_cache(self):
        """Build per-grade face lookup for classwise monitoring."""
        self._grade_face_cache.clear()
        for person_id, person_data in self.known_faces.items():
            grade = _grade_from_person_id(person_id)
            if grade:
                if grade not in self._grade_face_cache:
                    self._grade_face_cache[grade] = {}
                self._grade_face_cache[grade][person_id] = person_data
        grades_with_faces = {g: len(v) for g, v in self._grade_face_cache.items()}
        logger.info(f"Grade face cache: {grades_with_faces}")

    def get_faces_for_grade(self, grade: str | None) -> dict:
        """Return known_faces filtered to a specific grade.

        If grade is None, returns all known faces (for entry gates etc).
        """
        if grade is None:
            return self.known_faces
        return self._grade_face_cache.get(grade, {})

    def _is_already_marked_today(self, person_id: str) -> bool:
        """Check if attendance already marked for this person today."""
        today = date.today().isoformat()
        return self.daily_marked.get(person_id) == today

    def _mark_daily(self, person_id: str):
        """Record that this person was marked present today."""
        self.daily_marked[person_id] = date.today().isoformat()

    def recognize_faces_in_image(self, image_bytes: bytes,
                                 camera_source: str = "",
                                 faces_subset: dict | None = None) -> list[dict]:
        """Detect and recognize faces in a single image.

        Args:
            image_bytes: Raw JPEG image bytes
            camera_source: Label for the camera
            faces_subset: If provided, only match against these faces
                          (for classwise filtering). If None, uses all known faces.

        Returns list of recognition results.
        """
        if face_recognition is None:
            self.add_debug_log("error", "face_recognition library not available")
            return []

        faces_to_check = faces_subset if faces_subset is not None else self.known_faces

        # Use dlib native file loader to avoid numpy ABI issues on Windows
        tmp_path = None
        try:
            if Image is not None and dlib is not None:
                pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
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

        # Upsample 2x to detect smaller/distant faces from security cameras
        face_locations = face_recognition.face_locations(
            img_array, model="hog", number_of_times_to_upsample=2)

        if not face_locations:
            # Only log no-face for single-camera mode (classwise is too noisy)
            if not self.classwise_running:
                self.add_debug_log("no_face_detected",
                                   f"No faces in frame ({img_array.shape[1]}x{img_array.shape[0]}) from {camera_source}")
            return []

        self.add_debug_log("face_detected",
                           f"{len(face_locations)} face(s) detected from {camera_source}")

        face_encodings = face_recognition.face_encodings(img_array, face_locations)
        results = []

        for i, (encoding, location) in enumerate(zip(face_encodings, face_locations)):
            match_result = self._match_face(encoding, faces_to_check)

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
                                       f"Unregistered face in {camera_source}",
                                       confidence=0.0)
                    # Save snapshot for manual review
                    try:
                        ts = int(time.time())
                        snap_path = str(ATTENDANCE_SNAPSHOTS_DIR / f"unknown_{ts}_{i}.jpg")
                        with open(snap_path, "wb") as f:
                            f.write(image_bytes)
                        db.log_unrecognized_face(camera_source, 0.0, snap_path)
                    except Exception:
                        pass

        return results

    def _match_face(self, encoding: np.ndarray,
                    faces: dict | None = None) -> dict | None:
        """Match a face encoding against known faces.

        Args:
            encoding: 128-d face encoding
            faces: dict of faces to check (default: all known faces)

        Returns best match or None.
        """
        if faces is None:
            faces = self.known_faces

        best_match = None
        best_confidence = 0.0

        for person_id, person_data in faces.items():
            known_encodings = person_data["encodings"]
            if not known_encodings:
                continue

            distances = face_recognition.face_distance(known_encodings, encoding)
            min_distance = float(np.min(distances))
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

    def _is_within_attendance_window(self) -> bool:
        """Check if the current time is within the attendance window."""
        now = datetime.now()
        start = now.replace(hour=ATTENDANCE_START_HOUR, minute=ATTENDANCE_START_MINUTE,
                            second=0, microsecond=0)
        end = now.replace(hour=ATTENDANCE_END_HOUR, minute=ATTENDANCE_END_MINUTE,
                          second=0, microsecond=0)
        return start <= now <= end

    def _is_notification_sent_today(self, person_id: str) -> bool:
        """Check if notification was already sent for this person today."""
        today = date.today().isoformat()
        return self._notification_sent.get(person_id) == today

    def _mark_notification_sent(self, person_id: str):
        """Record that notification was sent for this person today."""
        self._notification_sent[person_id] = date.today().isoformat()

    def _process_attendance(self, person_id: str, name: str, phone: str,
                            confidence: float, image_bytes: bytes,
                            face_location: tuple,
                            camera_source: str) -> dict | None:
        """Process an attendance detection: check time window, cooldown/daily dedup, log, and notify."""
        now = time.time()

        # Time window check: only mark attendance between 7:15 AM and 8:00 AM
        if not self._is_within_attendance_window():
            return None

        # Daily dedup: one entry per student per day
        if self._is_already_marked_today(person_id):
            self.add_debug_log("daily_already_marked",
                               f"{name} already marked today",
                               person_id=person_id,
                               confidence=confidence)
            return None

        # Cooldown check (prevents rapid duplicate detections)
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
        self._mark_daily(person_id)
        time_str = datetime.now().strftime("%I:%M %p")

        self.add_debug_log("attendance_marked",
                           f"{name} marked Present at {time_str} "
                           f"(confidence: {confidence:.1%}) via {camera_source}",
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

        # Send WhatsApp notification ONCE per student per day
        if phone and not self._is_notification_sent_today(person_id):
            self._mark_notification_sent(person_id)
            phone_list = [p.strip() for p in phone.split(",") if p.strip()]
            for parent_phone in phone_list:
                task = asyncio.create_task(
                    self._send_whatsapp_notification(
                        attendance_id=attendance_id,
                        name=name,
                        time_str=time_str,
                        phone=parent_phone,
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        return result

    async def _send_whatsapp_notification(self, attendance_id: int,
                                           name: str, time_str: str,
                                           phone: str):
        """Send WhatsApp attendance notification via cloud bot API.

        Tries template message first (works without 24-hour window),
        falls back to plain text message.
        """
        api_url = self.whatsapp_api_url or "https://app-itszlsnn.fly.dev"
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {}
        if agent_secret:
            headers["X-Agent-Secret"] = agent_secret

        sent = False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Try template message first (no 24-hour window needed)
                resp = await client.post(
                    f"{api_url}/api/send-whatsapp",
                    json={
                        "phone": phone,
                        "template_name": "ppis_attendance_alert",
                        "template_params": [name, time_str],
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        sent = True

                # Fallback to plain text if template failed
                if not sent:
                    message = f"Your child {name} has been marked present today at {time_str}."
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
                        sent = True

                if sent:
                    db.update_whatsapp_sent(attendance_id)
                    self.add_debug_log("whatsapp_sent",
                                       f"Notification sent to {phone}: "
                                       f"[Attendance] {name} has arrived at {time_str}.")
                else:
                    self.add_debug_log("whatsapp_failed",
                                       f"API returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            self.add_debug_log("whatsapp_error", f"Failed to send: {e}")

    async def capture_frame_from_dvr(self, dvr: dict, channel: int,
                                     max_retries: int = 3) -> bytes | None:
        """Capture a single frame from a Hikvision DVR via ISAPI snapshot.

        Auto-retries on connection failures with exponential backoff.
        """
        ip = dvr["ip"]
        port = dvr.get("port", 80)
        user = dvr["username"]
        pwd = dvr["password"]

        stream_channel = channel * 100 + 1
        url = f"http://{ip}:{port}/ISAPI/Streaming/channels/{stream_channel}/picture"

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, auth=httpx.DigestAuth(user, pwd))
                    if resp.status_code == 401:
                        resp = await client.get(url, auth=httpx.BasicAuth(user, pwd))
                    if resp.status_code == 200 and resp.headers.get(
                            "content-type", "").startswith("image"):
                        # Reset consecutive error count on success
                        cam_key = f"{ip}:{channel}"
                        self._camera_errors.pop(cam_key, None)
                        return resp.content
            except Exception as e:
                cam_key = f"{ip}:{channel}"
                self._camera_errors[cam_key] = self._camera_errors.get(cam_key, 0) + 1
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    await asyncio.sleep(backoff)
                else:
                    self.add_debug_log("dvr_error",
                                       f"Capture failed from {ip} ch{channel} "
                                       f"after {max_retries} attempts: {e}")
        return None

    async def scan_camera(self, dvr: dict, channel: int,
                          camera_label: str = "",
                          faces_subset: dict | None = None) -> list[dict]:
        """Capture a frame from a camera and run face recognition on it."""
        frame = await self.capture_frame_from_dvr(dvr, channel)
        if frame is None:
            return []

        source = camera_label or f"{dvr['ip']}:ch{channel}"
        return self.recognize_faces_in_image(
            frame, camera_source=source, faces_subset=faces_subset)

    # ------------------------------------------------------------------
    # Single-camera monitoring (existing test mode)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Classwise multi-camera monitoring
    # ------------------------------------------------------------------

    def build_classroom_camera_list(self, camera_mapping: dict,
                                     dvrs: list[dict]) -> list[dict]:
        """Build list of classroom cameras with their DVR configs and grade.

        Each classroom may have multiple cameras (C1, C2). This includes
        ALL camera feeds per classroom from the all_cameras field.

        Returns list of dicts:
            {
                "location": "GRADE 3C",
                "grade": "GRADE3C",
                "dvr_index": 1,
                "channel": 13,
                "dvr": {...},
                "label": "GRADE 3C (DVR 2 Ch 13)",
                "is_gate": False,
            }
        """
        cameras = []
        seen = set()  # (dvr_index, channel) to avoid duplicates

        for location, cam_data in camera_mapping.items():
            grade = _extract_grade_from_location(location)
            if grade is None:
                continue  # Not a classroom camera

            # Add ALL cameras for this classroom (C1, C2, etc.)
            all_cams = cam_data.get("all_cameras", [])
            if all_cams:
                for ac in all_cams:
                    dvr_idx = ac.get("dvr_index", 0)
                    channel = ac.get("channel", 1)
                    key = (dvr_idx, channel)
                    if key in seen or dvr_idx >= len(dvrs):
                        continue
                    seen.add(key)
                    desc = ac.get("description", location)
                    cameras.append({
                        "location": location,
                        "grade": grade,
                        "dvr_index": dvr_idx,
                        "channel": channel,
                        "dvr": dvrs[dvr_idx],
                        "label": f"{location} (DVR {dvr_idx + 1} Ch {channel})",
                        "is_gate": False,
                    })
            else:
                dvr_idx = cam_data.get("dvr_index", 0)
                channel = cam_data.get("channel", 1)
                key = (dvr_idx, channel)
                if key in seen or dvr_idx >= len(dvrs):
                    continue
                seen.add(key)
                cameras.append({
                    "location": location,
                    "grade": grade,
                    "dvr_index": dvr_idx,
                    "channel": channel,
                    "dvr": dvrs[dvr_idx],
                    "label": f"{location} (DVR {dvr_idx + 1} Ch {channel})",
                    "is_gate": False,
                })

        # Also include entry gate cameras (check ALL faces)
        _GATE_KEYWORDS = {"ENTRY", "ENTRANCE", "DISPERSAL"}
        for location, cam_data in camera_mapping.items():
            loc_upper = location.upper()
            # Only match actual entry gates, not PARK GATE or other park cameras
            if any(kw in loc_upper for kw in _GATE_KEYWORDS):
                all_cams = cam_data.get("all_cameras", [])
                cams_to_add = all_cams if all_cams else [cam_data]
                for ac in cams_to_add:
                    dvr_idx = ac.get("dvr_index", 0)
                    channel = ac.get("channel", 1)
                    key = (dvr_idx, channel)
                    if key in seen or dvr_idx >= len(dvrs):
                        continue
                    seen.add(key)
                    cameras.append({
                        "location": location,
                        "grade": None,  # Check ALL faces at gates
                        "dvr_index": dvr_idx,
                        "channel": channel,
                        "dvr": dvrs[dvr_idx],
                        "label": f"{location} (DVR {dvr_idx + 1} Ch {channel})",
                        "is_gate": True,
                    })

        return cameras

    async def classwise_monitoring_loop(self, dvrs: list[dict],
                                         camera_mapping: dict):
        """Multi-camera classroom-wise attendance monitoring.

        Round-robin scans ALL classroom cameras. For each camera:
        1. Extract grade from camera name
        2. Load only faces of students in that grade
        3. Run face recognition
        4. Mark attendance (daily dedup - one entry per student per day)

        Entry gate cameras check ALL registered faces.
        """
        self.classwise_running = True
        self.reload_faces()

        cameras = self.build_classroom_camera_list(camera_mapping, dvrs)
        classroom_cams = [c for c in cameras if c["grade"] is not None]
        gate_cams = [c for c in cameras if c["grade"] is None]

        self._classwise_stats["total_cameras"] = len(cameras)
        self.add_debug_log(
            "classwise_started",
            f"Monitoring {len(classroom_cams)} classroom cameras + "
            f"{len(gate_cams)} entry gate cameras, "
            f"{len(self.known_faces)} total faces loaded, "
            f"{len(self._grade_face_cache)} grades with faces"
        )

        # Clear daily marks at start if it's a new day
        today = date.today().isoformat()
        self.daily_marked = {
            pid: d for pid, d in self.daily_marked.items() if d == today
        }

        try:
            cycle = 0
            consecutive_full_failures = 0
            while self.classwise_running:
                cycle += 1
                cycle_start = time.time()
                self._classwise_stats["cycle_count"] = cycle
                scanned = 0
                faces_in_cycle = 0
                cycle_errors = 0

                # Check if day changed - reset daily marks
                new_today = date.today().isoformat()
                if new_today != today:
                    today = new_today
                    self.daily_marked.clear()
                    self._notification_sent.clear()
                    self._camera_errors.clear()
                    self._admin_alerted.clear()
                    self.add_debug_log("daily_reset",
                                       f"New day {today}: cleared attendance marks and notifications")

                # Periodically reload faces (picks up new registrations)
                if cycle % 20 == 0:
                    self.reload_faces()

                # Scan entry gate cameras first (priority)
                for cam in gate_cams:
                    if not self.classwise_running:
                        break
                    try:
                        self._classwise_stats["current_camera"] = cam["label"]
                        results = await self.scan_camera(
                            cam["dvr"], cam["channel"], cam["label"],
                            faces_subset=None,
                        )
                        scanned += 1
                        faces_in_cycle += len(results)
                    except Exception as e:
                        self._classwise_stats["errors"] += 1
                        cycle_errors += 1
                        logger.error(f"Error scanning {cam['label']}: {e}")
                    await asyncio.sleep(0.5)

                # Scan classroom cameras with grade-filtered faces
                for cam in classroom_cams:
                    if not self.classwise_running:
                        break
                    try:
                        grade = cam["grade"]
                        grade_faces = self.get_faces_for_grade(grade)

                        if not grade_faces:
                            scanned += 1
                            continue

                        self._classwise_stats["current_camera"] = cam["label"]
                        results = await self.scan_camera(
                            cam["dvr"], cam["channel"], cam["label"],
                            faces_subset=grade_faces,
                        )
                        scanned += 1
                        faces_in_cycle += len(results)
                    except Exception as e:
                        self._classwise_stats["errors"] += 1
                        cycle_errors += 1
                        logger.error(f"Error scanning {cam['label']}: {e}")
                    await asyncio.sleep(0.5)

                cycle_duration = time.time() - cycle_start
                self._classwise_stats["cameras_scanned"] = scanned
                self._classwise_stats["last_cycle_duration"] = round(cycle_duration, 1)
                self._classwise_stats["faces_detected_total"] += faces_in_cycle
                self._classwise_stats["attendance_marked_today"] = sum(
                    1 for d in self.daily_marked.values()
                    if d == date.today().isoformat()
                )

                # Track full-cycle failures for auto-recovery
                if scanned == 0 and cycle_errors > 0:
                    consecutive_full_failures += 1
                    self._health["camera_feed"] = "degraded"
                    if consecutive_full_failures >= 5:
                        self._health["camera_feed"] = "error"
                        self.add_debug_log("auto_recovery",
                                           "All cameras failed 5 cycles in a row — "
                                           "waiting 30s before retry")
                        await asyncio.sleep(30)
                        consecutive_full_failures = 0
                        self._health["total_recoveries"] += 1
                else:
                    consecutive_full_failures = 0
                    self._health["camera_feed"] = "ok"

                if cycle % 5 == 0:
                    self.add_debug_log(
                        "classwise_cycle",
                        f"Cycle {cycle}: scanned {scanned} cameras in "
                        f"{cycle_duration:.1f}s, {faces_in_cycle} faces detected, "
                        f"{self._classwise_stats['attendance_marked_today']} marked today"
                    )

                self._health["last_health_check"] = datetime.now().isoformat()

                # Brief cooldown between full cycles
                await asyncio.sleep(2.0)

        except asyncio.CancelledError:
            self.add_debug_log("classwise_cancelled",
                               "Classwise monitoring was cancelled")
        except Exception as e:
            self.add_debug_log("classwise_crash",
                               f"Classwise monitoring crashed: {e}")
            self._health["recognition_engine"] = "error"
        finally:
            self.classwise_running = False
            self._classwise_stats["current_camera"] = ""
            self.add_debug_log("classwise_stopped",
                               "Classwise attendance monitoring stopped")

    def stop(self):
        """Stop the monitoring loop."""
        self.running = False
        self.classwise_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._classwise_task and not self._classwise_task.done():
            self._classwise_task.cancel()
        self.add_debug_log("monitoring_stopped", "Stop requested")

    def get_status(self) -> dict:
        """Return current engine status."""
        status = {
            "running": self.running,
            "classwise_running": self.classwise_running,
            "test_mode": self.test_mode,
            "test_person_id": self.test_person_id,
            "confidence_threshold": self.confidence_threshold,
            "scan_interval": self.scan_interval,
            "registered_persons": len(self.known_faces),
            "total_encodings": sum(
                len(p["encodings"]) for p in self.known_faces.values()
            ),
            "cooldown_seconds": COOLDOWN_SECONDS,
            "attendance_marked_today": sum(
                1 for d in self.daily_marked.values()
                if d == date.today().isoformat()
            ),
            "grades_with_faces": len(self._grade_face_cache),
            "health": self._health.copy(),
        }
        if self.classwise_running:
            status["classwise_stats"] = self._classwise_stats.copy()
        return status

    def get_debug_logs(self, limit: int = 100) -> list[dict]:
        """Return recent debug logs."""
        return self.debug_logs[-limit:]


# Module-level singleton
engine = AttendanceEngine()

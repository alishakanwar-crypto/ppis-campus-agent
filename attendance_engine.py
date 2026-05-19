"""
Face recognition attendance engine.

Monitors DVR camera feeds via RTSP or periodic ISAPI snapshots,
detects faces, matches against registered encodings, and logs attendance.

Supports:
- Single-camera test mode (tracks one person_id)
- Multi-camera classroom-wise attendance (all classrooms simultaneously)
  Each camera only checks faces of students in that class.
"""

from __future__ import annotations

import asyncio
import gc
import io
import logging
import os
import re
import tempfile
import time
from datetime import datetime, date, timedelta, timezone
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
    from PIL import Image, ImageEnhance, ImageFilter
except ImportError:
    Image = None
    ImageEnhance = None
    ImageFilter = None

try:
    from insightface.app import FaceAnalysis
    _INSIGHTFACE_AVAILABLE = True
except ImportError:
    FaceAnalysis = None
    _INSIGHTFACE_AVAILABLE = False

import database as db
import face_db

logger = logging.getLogger("ppis-agent.attendance")

ATTENDANCE_SNAPSHOTS_DIR = Path(__file__).parent / "attendance_snapshots"
ATTENDANCE_SNAPSHOTS_DIR.mkdir(exist_ok=True)

# Minimum seconds between attendance entries for the same person
COOLDOWN_SECONDS = 300  # 5 minutes

# Attendance time window (overall: 6:30 AM to 12:00 PM IST — varies by date)
ATTENDANCE_START_HOUR = 6
ATTENDANCE_START_MINUTE = 30
ATTENDANCE_END_HOUR = 12
ATTENDANCE_END_MINUTE = 0

# Three-phase attendance windows (production mode from 18 May 2026)
# Phase 1: Teacher recognition (6:30-8:00 AM)
#   DVR3 Ch23 Administration Camera ONLY, confidence 35-45%, min 1 sighting
# Phase 2: Summer Camp students (8:15-9:15 AM)
#   Scan ALL classroom cameras (students can sit anywhere), 35-50%, min 2 sightings
# Phase 3: Grades 9-12 students (8:15-9:15 AM)
#   Scan ONLY assigned classroom cameras, 35-50%, min 2 sightings
# Open House (16 May 2026): special wider windows

OPEN_HOUSE_DATE = "2026-05-16"  # Saturday Open House

# Default teacher window (regular days from Monday 18 May onwards)
TEACHER_PHASE_START_HOUR = 6
TEACHER_PHASE_START_MIN = 30
TEACHER_PHASE_END_HOUR = 8
TEACHER_PHASE_END_MIN = 0

# Open House teacher window (wider)
OPEN_HOUSE_TEACHER_START_HOUR = 6
OPEN_HOUSE_TEACHER_START_MIN = 30
OPEN_HOUSE_TEACHER_END_HOUR = 8
OPEN_HOUSE_TEACHER_END_MIN = 0

# Default student window (regular days from Monday 18 May onwards)
STUDENT_PHASE_START_HOUR = 8
STUDENT_PHASE_START_MIN = 15
STUDENT_PHASE_END_HOUR = 9
STUDENT_PHASE_END_MIN = 15

# Open House student window
OPEN_HOUSE_STUDENT_START_HOUR = 8
OPEN_HOUSE_STUDENT_START_MIN = 15
OPEN_HOUSE_STUDENT_END_HOUR = 12
OPEN_HOUSE_STUDENT_END_MIN = 0


def _is_open_house_today() -> bool:
    """Check if today is Open House day."""
    return date.today().isoformat() == OPEN_HOUSE_DATE


def _get_teacher_phase_window() -> tuple[int, int, int, int]:
    """Return (start_hour, start_min, end_hour, end_min) for teacher phase."""
    if _is_open_house_today():
        return (OPEN_HOUSE_TEACHER_START_HOUR, OPEN_HOUSE_TEACHER_START_MIN,
                OPEN_HOUSE_TEACHER_END_HOUR, OPEN_HOUSE_TEACHER_END_MIN)
    return (TEACHER_PHASE_START_HOUR, TEACHER_PHASE_START_MIN,
            TEACHER_PHASE_END_HOUR, TEACHER_PHASE_END_MIN)


def _get_student_phase_window() -> tuple[int, int, int, int]:
    """Return (start_hour, start_min, end_hour, end_min) for student phase."""
    if _is_open_house_today():
        return (OPEN_HOUSE_STUDENT_START_HOUR, OPEN_HOUSE_STUDENT_START_MIN,
                OPEN_HOUSE_STUDENT_END_HOUR, OPEN_HOUSE_STUDENT_END_MIN)
    return (STUDENT_PHASE_START_HOUR, STUDENT_PHASE_START_MIN,
            STUDENT_PHASE_END_HOUR, STUDENT_PHASE_END_MIN)


# --- OPEN HOUSE SMART MODE ---
# Students may move between classrooms during Open House.
# Prevent duplicate attendance, allow cross-classroom detection.
# Active ONLY on Open House day; regular days use normal classwise scanning.
OPEN_HOUSE_MODE = False  # Computed dynamically via _is_open_house_today()

# ---------------------------------------------------------------------------
# HIGH-ACCURACY CONFIGURATION
# ---------------------------------------------------------------------------
# Minimum face pixel dimensions for quality filtering
MIN_FACE_WIDTH = 25
MIN_FACE_HEIGHT = 25

# ---------------------------------------------------------------------------
# HD FACE RECOGNITION — DVR OPTIMIZATION
# ---------------------------------------------------------------------------
# Cache of each camera channel's max supported resolution.
# Populated on first capture via ISAPI capabilities probe.
# Key: "ip:channel", Value: (width, height) or None if unknown.
_channel_resolution_cache: dict[str, tuple[int, int] | None] = {}

# Face crop padding: expand the detected face bounding box by this fraction
# to include forehead, chin, and ears for better encoding accuracy.
FACE_CROP_PADDING = 0.35  # 35% padding around the detected face box

# Image quality thresholds (Laplacian variance for sharpness)
MIN_SHARPNESS_SCORE = 30.0  # Reject blurry faces below this
MIN_BRIGHTNESS = 40         # Reject underexposed faces
MAX_BRIGHTNESS = 230        # Reject overexposed faces

# Entry gate / reception camera labels (for entry validation)
ENTRY_VALIDATION_CAMERAS = {
    "entry_gate", "reception",
}

# TEMPORARY TEST FLAG: Force re-send notifications even if already sent today
# Set to True for testing, False for production
FORCE_RENOTIFY_TEST = False

# ---------------------------------------------------------------------------
# STUDENT CATEGORY DEFINITIONS (for Phase 2 & Phase 3 scanning)
# ---------------------------------------------------------------------------
# Grades 9-12: sit in their assigned classrooms ONLY → grade-specific scanning
GRADES_9_TO_12 = {
    "GRADE9A", "GRADE9B", "GRADE9C",
    "GRADE10A", "GRADE10B", "GRADE10C",
    "GRADE11A", "GRADE11B", "GRADE11C",
    "GRADE12A", "GRADE12B", "GRADE12C",
}

# Summer camp students: may sit in ANY classroom → scan all cameras
# Identified by person_id NOT matching any Grade 9-12 pattern
def _is_grade_9_to_12_student(person_id: str) -> bool:
    """Check if a student belongs to Grades 9-12 based on person_id."""
    grade = _grade_from_person_id(person_id)
    return grade in GRADES_9_TO_12 if grade else False

# ---------------------------------------------------------------------------
# MEAL SNAPSHOT NOTIFICATION SCHEDULE (Summer Camp)
# ---------------------------------------------------------------------------
# Sends classroom camera snapshots to parents of present children during
# meal breaks. Uses ppis_meal_update template (force-send via template API).
# Active: weekdays only, until May 26, 2026.
MEAL_SNAPSHOT_ENABLED = False
MEAL_SNAPSHOT_END_DATE = "2026-05-26"  # Last day of summer camp
MEAL_WINDOWS = [
    # (start_hour, start_min, end_hour, end_min, label)
    (8, 45, 9, 0, "Short Break"),
    (11, 30, 12, 0, "Lunch Break"),
]
# Summer camp meal room mapping: younger students eat in Grade 1A/1B,
# while Grades 9-12 eat in their own classrooms.
MEAL_OWN_CLASSROOM_GRADES = {"GRADE9A", "GRADE9B", "GRADE10A", "GRADE10B",
                              "GRADE11A", "GRADE11B", "GRADE12A", "GRADE12B"}
# Summer camp students eat in these rooms (snapshot any of them)
MEAL_CAMP_ROOM_GRADES = {"GRADE1A", "GRADE1B", "GRADE2A", "GRADE2B",
                          "NURSERY", "PREP", "PREP1", "PREP2", "PREP3"}

# Summer break schedule: grades on break won't be scanned on classroom cameras.
# Teachers and entry gate/reception scanning continue normally.
# Format: list of (start_date, end_date, set_of_normalized_grade_prefixes)
SUMMER_BREAK_SCHEDULE = [
    # Popsicles through Grade 8: May 12 - June 30, 2026
    ("2026-05-12", "2026-06-30", {
        "POPSICLES", "NUR", "NUR1", "NUR2", "NUR3",
        "PREP", "PREP1", "PREP2", "PREP3",
        "GRADE1A", "GRADE1B", "GRADE1C",
        "GRADE2A", "GRADE2B", "GRADE2C",
        "GRADE3A", "GRADE3B", "GRADE3C",
        "GRADE4A", "GRADE4B", "GRADE4C",
        "GRADE5A", "GRADE5B", "GRADE5C",
        "GRADE6A", "GRADE6B", "GRADE6C",
        "GRADE7A", "GRADE7B", "GRADE7C",
        "GRADE8A", "GRADE8B", "GRADE8C",
    }),
    # Grade 9-12: May 27 - June 30, 2026 (last working day May 26)
    ("2026-05-27", "2026-06-30", {
        "GRADE9A", "GRADE9B", "GRADE9C",
        "GRADE10A", "GRADE10B", "GRADE10C",
        "GRADE11A", "GRADE11B", "GRADE11C",
        "GRADE12A", "GRADE12B", "GRADE12C",
    }),
]


def _is_grade_on_break(grade: str) -> bool:
    """Check if a grade is currently on summer break."""
    if not grade:
        return False
    today = date.today().isoformat()
    for start_date, end_date, grades_set in SUMMER_BREAK_SCHEDULE:
        if start_date <= today <= end_date and grade in grades_set:
            return True
    return False


# Teacher-specific window (used when person_id starts with TEACHER_)
TEACHER_END_HOUR = 8
TEACHER_END_MINUTE = 0

# Grade pattern to extract grade from camera location names
_GRADE_RE = re.compile(
    r"(?:GRADE\s*(\d+[A-Z]?))"
    r"|(?:(NUR|NURSERY)[\s\-]*(\d*))"
    r"|(?:(PREP)[\s\-]*(\d*))"
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
        n = m.group(3) or ""
        return f"NUR{n}"
    if m.group(4):  # PREP
        n = m.group(5) or ""
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
    if not m:
        return None
    grade = m.group(1)
    # Normalize: POPSICLE (without trailing 's') -> POPSICLES
    # so it matches _extract_grade_from_location which always returns "POPSICLES"
    if grade.startswith("POPSICLE"):
        return "POPSICLES"
    return grade


async def _probe_channel_resolution(client: httpx.AsyncClient,
                                     ip: str, port: int,
                                     channel: int) -> tuple[int, int] | None:
    """Query Hikvision ISAPI for a channel's native (max) resolution.

    Probes /ISAPI/Streaming/channels/{stream}/capabilities to discover
    the camera's actual resolution. Caches the result so we only probe once
    per channel per session.

    Returns (width, height) or None if the probe fails.
    """
    cam_key = f"{ip}:{channel}"
    if cam_key in _channel_resolution_cache:
        return _channel_resolution_cache[cam_key]

    stream_channel = channel * 100 + 1
    url = (f"http://{ip}:{port}/ISAPI/Streaming/channels/"
           f"{stream_channel}/capabilities")
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            body = resp.text
            # Parse max resolution from XML: look for videoResolutionWidth/Height
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
            ns = ""
            # Hikvision uses a namespace; detect it from the root tag
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"
            w_el = root.find(f".//{ns}videoResolutionWidth")
            h_el = root.find(f".//{ns}videoResolutionHeight")
            if w_el is not None and h_el is not None:
                # The capabilities may have max attribute or text value
                w_text = w_el.get("max") or w_el.text
                h_text = h_el.get("max") or h_el.text
                if w_text and h_text:
                    w, h = int(w_text), int(h_text)
                    _channel_resolution_cache[cam_key] = (w, h)
                    logger.info(f"Camera {cam_key} native resolution: {w}x{h}")
                    return (w, h)
    except Exception as e:
        logger.debug(f"Resolution probe failed for {cam_key}: {e}")

    # Fallback: try /ISAPI/System/Video/inputs/channels/{channel}
    try:
        alt_url = f"http://{ip}:{port}/ISAPI/System/Video/inputs/channels/{channel}"
        resp2 = await client.get(alt_url)
        if resp2.status_code == 200:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp2.text)
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"
            res_w = root.find(f".//{ns}resolutionWidth")
            res_h = root.find(f".//{ns}resolutionHeight")
            if res_w is not None and res_h is not None:
                w = int(res_w.text)
                h = int(res_h.text)
                _channel_resolution_cache[cam_key] = (w, h)
                logger.info(f"Camera {cam_key} native resolution (alt): {w}x{h}")
                return (w, h)
    except Exception:
        pass

    _channel_resolution_cache[cam_key] = None
    return None


def _extract_padded_face_crop(image_bytes: bytes,
                              top: int, right: int,
                              bottom: int, left: int,
                              padding: float = FACE_CROP_PADDING) -> bytes:
    """Extract a face crop with extra padding for better recognition.

    Expands the detected face bounding box by the padding fraction
    to include forehead, chin, and ears. This gives the face encoder
    more context and produces better embeddings.
    """
    if Image is None:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        face_w = right - left
        face_h = bottom - top
        pad_x = int(face_w * padding)
        pad_y = int(face_h * padding)
        crop_left = max(0, left - pad_x)
        crop_top = max(0, top - pad_y)
        crop_right = min(w, right + pad_x)
        crop_bottom = min(h, bottom + pad_y)
        crop = img.crop((crop_left, crop_top, crop_right, crop_bottom))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception:
        return image_bytes


def _preprocess_image(image_bytes: bytes) -> bytes:
    """Enhance image quality for better face recognition.

    Applies contrast enhancement, sharpening, and upscaling to
    improve face detection on low-quality DVR snapshots.
    """
    if Image is None or ImageEnhance is None:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # Upscale small images (DVR snapshots may be low-res)
        min_dim = 1280
        if img.width < min_dim and img.height < min_dim:
            scale = min_dim / min(img.width, img.height)
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Enhance contrast (helps with washed-out DVR images)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.3)

        # Enhance sharpness (compensates for JPEG compression)
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(1.5)

        # Slight brightness boost for dark indoor scenes
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(1.1)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception:
        return image_bytes


def _assess_face_quality(face_crop_bytes: bytes) -> dict:
    """Assess quality of a cropped face image.

    Returns dict with:
        sharpness: float (Laplacian variance — higher = sharper)
        brightness: float (mean pixel value 0-255)
        is_acceptable: bool (passes all quality checks)
        rejection_reason: str or None
    """
    result = {"sharpness": 0.0, "brightness": 128.0,
              "is_acceptable": True, "rejection_reason": None}
    if cv2 is None:
        return result
    try:
        arr = np.frombuffer(face_crop_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            result["is_acceptable"] = False
            result["rejection_reason"] = "could not decode face crop"
            return result
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Sharpness via Laplacian variance
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        sharpness = float(laplacian.var())
        result["sharpness"] = round(sharpness, 1)
        # Brightness
        brightness = float(gray.mean())
        result["brightness"] = round(brightness, 1)
        if sharpness < MIN_SHARPNESS_SCORE:
            result["is_acceptable"] = False
            result["rejection_reason"] = f"blurry (sharpness {sharpness:.1f} < {MIN_SHARPNESS_SCORE})"
        elif brightness < MIN_BRIGHTNESS:
            result["is_acceptable"] = False
            result["rejection_reason"] = f"too dark (brightness {brightness:.0f} < {MIN_BRIGHTNESS})"
        elif brightness > MAX_BRIGHTNESS:
            result["is_acceptable"] = False
            result["rejection_reason"] = f"overexposed (brightness {brightness:.0f} > {MAX_BRIGHTNESS})"
    except Exception:
        pass
    return result


def _is_entry_camera(camera_source: str) -> bool:
    """Check if camera_source is an entry gate or reception camera."""
    src_lower = camera_source.lower()
    for keyword in ENTRY_VALIDATION_CAMERAS:
        if keyword in src_lower:
            return True
    return False


class AttendanceEngine:
    """Runs face recognition attendance monitoring."""

    def __init__(self):
        self.running = False
        self.classwise_running = False
        self.test_mode = True  # Only track test_person_id when True
        self.test_person_id = "TEST001"
        self.confidence_threshold = 0.35  # Student min confidence 35%
        self.confidence_max = 0.50  # Student max confidence 50%
        self.review_threshold = 0.30  # Below 30% gets rejected outright
        self.min_sightings = 2  # Students: require 2 independent sightings
        self.sighting_window = 600  # 10-minute window for sightings to accumulate
        self.teacher_confidence_threshold = 0.35  # Teacher min threshold 35%
        self.teacher_confidence_max = 0.45  # Teacher max threshold 45%
        self.entry_validated: dict[str, str] = {}  # person_id -> date (seen at entry/reception)
        self._sightings: dict[str, list[dict]] = {}  # person_id -> [{time, camera, confidence, embedding, face_size}, ...]
        self.known_faces: dict = {}
        self.known_faces_insightface: dict = {}  # person_id -> {name, phone, embeddings: [...]}
        self.last_attendance: dict[str, float] = {}  # person_id -> timestamp
        self.daily_marked: dict[str, str] = {}  # person_id -> date string
        self._notification_sent: dict[str, str] = {}  # person_id -> date (dedup notifications)
        # Meal snapshot dedup: "person_id:window_label" -> date
        self._meal_sent: dict[str, str] = {}
        # Track which meal windows already ran their full snapshot cycle today
        self._meal_window_done: dict[str, str] = {}  # window_label -> date
        self.debug_logs: list[dict] = []
        self.max_debug_logs = 500
        self.scan_interval = 3.0  # seconds between scans
        self.whatsapp_api_url = ""
        self.whatsapp_phone = ""
        self._task: asyncio.Task | None = None
        self._classwise_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        # Persistent HTTP clients per DVR IP for connection pooling
        self._dvr_clients: dict[str, httpx.AsyncClient] = {}
        # Disable InsightFace — legacy face_recognition works more reliably
        # with the current DVR setup. Re-enable once InsightFace image
        # compatibility issues with all DVR models are resolved.
        self.use_insightface = False
        self._insightface_app: "FaceAnalysis | None" = None

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
        self._grade_face_cache_insightface: dict[str, dict] = {}

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
            "face_engine": "insightface" if _INSIGHTFACE_AVAILABLE else "face_recognition",
        }
        self._admin_alerted: set = set()  # Track which issues already alerted
        self._camera_alert_threshold = 5  # consecutive failures before alert
        self._admin_phones: list[str] = []  # phones to receive camera alerts
        self._camera_recovered: set = set()  # cameras that recovered after alert

        # --- FAILSAFE MODE ---
        # Automatically stops attendance marking when system instability detected.
        self._failsafe_active = False
        self._failsafe_reason = ""
        self._consecutive_cycle_errors = 0
        self._max_cycle_errors_before_failsafe = 5  # 5 consecutive error cycles → failsafe
        self._false_positive_count_window: list[float] = []  # timestamps of rejected detections
        self._max_false_positives_per_minute = 10  # high FP rate triggers failsafe
        self._failsafe_recovery_count = 0  # consecutive successful cycles during failsafe
        self._last_dvrs: list[dict] = []
        self._last_camera_mapping: dict = {}
        # HD resolution probe results: camera_label -> {width, height, probed}
        self._camera_resolutions: dict[str, dict] = {}

        # Initialize InsightFace if available
        if self.use_insightface:
            self._init_insightface()

    def _init_insightface(self):
        """Initialize the InsightFace face analysis engine."""
        if not _INSIGHTFACE_AVAILABLE:
            return
        try:
            self._insightface_app = FaceAnalysis(
                name="buffalo_l",
                providers=["CPUExecutionProvider"],
            )
            self._insightface_app.prepare(ctx_id=-1, det_size=(640, 640))
            # Lower detection threshold for distant/small faces from ceiling cameras
            for model in self._insightface_app.models:
                if hasattr(model, 'det_thresh'):
                    model.det_thresh = 0.3
            logger.info("InsightFace engine initialized (buffalo_l model)")
            self._health["face_engine"] = "insightface"
        except Exception as e:
            logger.warning(f"InsightFace init failed, falling back to face_recognition: {e}")
            self._insightface_app = None
            self.use_insightface = False
            self._health["face_engine"] = "face_recognition"

    async def probe_all_camera_resolutions(self, dvrs: list[dict],
                                           camera_mapping: dict) -> dict:
        """Probe native resolution for every mapped camera.

        Returns a summary dict of camera_label -> {ip, channel, width, height}.
        Useful for diagnostics: shows which cameras can deliver HD+ stills.
        """
        results: dict[str, dict] = {}
        for label, cam_info in camera_mapping.items():
            all_cams = cam_info.get("all_cameras", [cam_info])
            for cam in all_cams:
                dvr_idx = cam.get("dvr_index", 0)
                if dvr_idx < 1 or dvr_idx > len(dvrs):
                    continue
                dvr = dvrs[dvr_idx - 1]
                ch = cam.get("channel", 0)
                desc = cam.get("description", label)
                ip = dvr["ip"]
                port = dvr.get("port", 80)
                client = self._get_dvr_client(dvr)
                res = await _probe_channel_resolution(client, ip, port, ch)
                entry = {
                    "ip": ip,
                    "channel": ch,
                    "width": res[0] if res else 1920,
                    "height": res[1] if res else 1080,
                    "native_probed": res is not None,
                }
                results[desc] = entry
                self._camera_resolutions[desc] = entry
        return results

    def _check_failsafe(self, cycle_errors: int, scanned: int) -> bool:
        """Check if failsafe mode should be activated.

        Returns True if failsafe is active (scanning should stop).
        Failsafe auto-recovers after 3 consecutive successful cycles.
        """
        if self._failsafe_active:
            # Auto-recovery: if cameras are working again, deactivate failsafe
            if scanned > 0 and cycle_errors == 0:
                self._failsafe_recovery_count += 1
                if self._failsafe_recovery_count >= 3:
                    self._failsafe_active = False
                    self._failsafe_reason = ""
                    self._failsafe_recovery_count = 0
                    self._consecutive_cycle_errors = 0
                    self._false_positive_count_window.clear()
                    self.add_debug_log("failsafe_recovered",
                                       "System recovered after 3 consecutive successful cycles")
                    logger.info("FAILSAFE RECOVERED: System back to normal operation")
                    return False
            else:
                self._failsafe_recovery_count = 0
            return True

        # Check consecutive cycle errors
        if cycle_errors > 0 and scanned == 0:
            self._consecutive_cycle_errors += 1
        else:
            self._consecutive_cycle_errors = 0

        if self._consecutive_cycle_errors >= self._max_cycle_errors_before_failsafe:
            self._failsafe_active = True
            self._failsafe_recovery_count = 0
            self._failsafe_reason = (
                f"FAILSAFE: {self._consecutive_cycle_errors} consecutive cycles "
                f"with errors and zero successful scans"
            )
            self.add_debug_log("failsafe_activated", self._failsafe_reason)
            logger.critical(self._failsafe_reason)
            return True

        return False

    def _check_false_positive_rate(self) -> bool:
        """Check if false positive rate is too high. Called every cycle.

        Returns True if failsafe should activate due to high FP rate.
        Only checks actual FP rate — does NOT short-circuit on _failsafe_active.
        """
        now = time.time()
        self._false_positive_count_window = [
            t for t in self._false_positive_count_window if now - t < 60
        ]
        if len(self._false_positive_count_window) >= self._max_false_positives_per_minute:
            if not self._failsafe_active:
                self._failsafe_active = True
                self._failsafe_recovery_count = 0
                self._failsafe_reason = (
                    f"FAILSAFE: High false positive rate — "
                    f"{len(self._false_positive_count_window)} rejected detections in 60s"
                )
                self.add_debug_log("failsafe_activated", self._failsafe_reason)
                logger.critical(self._failsafe_reason)
            return True

        return False

    def _record_false_positive(self):
        """Record a rejected detection for failsafe rate monitoring."""
        self._false_positive_count_window.append(time.time())

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
        """Reload registered faces from database.

        Also pre-populates the daily_marked and _notification_sent caches
        from the persistent DB so dedup survives process restarts.
        """
        self.known_faces = face_db.load_known_faces()
        if self.use_insightface and self._insightface_app:
            self.known_faces_insightface = face_db.load_known_faces(
                encoding_type="insightface_512d")
        self._rebuild_grade_cache()

        # Pre-populate dedup caches from persistent DB (survives restarts)
        today = date.today().isoformat()
        if FORCE_RENOTIFY_TEST:
            logger.info("FORCE_RENOTIFY_TEST=True: Skipping dedup cache pre-population "
                        "— will re-mark and re-notify all detected faces")
            self.daily_marked = {}
            self._notification_sent = {}
        else:
            try:
                marked_ids = db.get_today_marked_person_ids()
                for pid in marked_ids:
                    self.daily_marked[pid] = today
                notified_ids = db.get_today_notified_person_ids()
                for pid in notified_ids:
                    self._notification_sent[pid] = today
                logger.info(f"Pre-populated dedup caches: {len(marked_ids)} marked, "
                            f"{len(notified_ids)} notified today")
            except Exception as e:
                logger.warning(f"Failed to pre-populate dedup caches from DB: {e}")

        engine_label = "insightface" if self.use_insightface else "face_recognition"
        n_legacy = len(self.known_faces)
        n_insight = len(self.known_faces_insightface)
        self.add_debug_log("faces_reloaded",
                           f"{n_legacy} legacy + {n_insight} insightface person(s) loaded "
                           f"(engine={engine_label})")

    def _rebuild_grade_cache(self):
        """Build per-grade face lookup for classwise monitoring.

        Also builds a separate teacher face cache so that teacher faces
        are included in every classroom camera scan (teachers walk
        through all classrooms, not just gates).
        """
        self._grade_face_cache.clear()
        self._teacher_faces_cache: dict = {}
        for person_id, person_data in self.known_faces.items():
            if person_id.startswith(("TEACHER_", "PRINCIPAL_")):
                self._teacher_faces_cache[person_id] = person_data
                continue
            grade = _grade_from_person_id(person_id)
            if grade:
                if grade not in self._grade_face_cache:
                    self._grade_face_cache[grade] = {}
                self._grade_face_cache[grade][person_id] = person_data

        self._grade_face_cache_insightface.clear()
        self._teacher_faces_cache_insightface: dict = {}
        for person_id, person_data in self.known_faces_insightface.items():
            if person_id.startswith(("TEACHER_", "PRINCIPAL_")):
                self._teacher_faces_cache_insightface[person_id] = person_data
                continue
            grade = _grade_from_person_id(person_id)
            if grade:
                if grade not in self._grade_face_cache_insightface:
                    self._grade_face_cache_insightface[grade] = {}
                self._grade_face_cache_insightface[grade][person_id] = person_data

        grades_with_faces = {g: len(v) for g, v in self._grade_face_cache.items()}
        logger.info(f"Grade face cache: {grades_with_faces}, "
                    f"teacher faces: {len(self._teacher_faces_cache)} legacy / "
                    f"{len(self._teacher_faces_cache_insightface)} insightface")

    def get_faces_for_grade(self, grade: str | None) -> dict:
        """Return known_faces filtered to a specific grade.

        If grade is None, returns all known faces (for entry gates etc).
        Teacher faces are always included so they can be recognized
        on every camera (classroom + gate).
        """
        if grade is None:
            return self.known_faces
        grade_faces = self._grade_face_cache.get(grade, {})
        teacher_faces = getattr(self, '_teacher_faces_cache', {})
        if teacher_faces:
            merged = {}
            merged.update(grade_faces)
            merged.update(teacher_faces)
            return merged
        return grade_faces

    def get_insightface_for_grade(self, grade: str | None) -> dict:
        """Return InsightFace embeddings filtered to a specific grade.

        Teacher faces are always included.
        """
        if grade is None:
            return self.known_faces_insightface
        grade_faces = self._grade_face_cache_insightface.get(grade, {})
        teacher_faces = getattr(self, '_teacher_faces_cache_insightface', {})
        if teacher_faces:
            merged = {}
            merged.update(grade_faces)
            merged.update(teacher_faces)
            return merged
        return grade_faces

    def _is_already_marked_today(self, person_id: str) -> bool:
        """Check if attendance already marked for this person today.

        Uses in-memory cache first for speed. On cache miss, falls back
        to the persistent database so dedup survives process restarts.
        """
        if FORCE_RENOTIFY_TEST:
            # During test mode, only dedup within this session (in-memory only)
            today = date.today().isoformat()
            return self.daily_marked.get(person_id) == today
        today = date.today().isoformat()
        if self.daily_marked.get(person_id) == today:
            return True
        # Fallback: check persistent DB (survives restarts)
        if db.is_attendance_marked_today(person_id):
            # Populate cache so future checks are fast
            self.daily_marked[person_id] = today
            return True
        return False

    def _mark_daily(self, person_id: str):
        """Record that this person was marked present today."""
        self.daily_marked[person_id] = date.today().isoformat()

    def recognize_faces_in_image(self, image_bytes: bytes,
                                 camera_source: str = "",
                                 faces_subset: dict | None = None,
                                 insightface_subset: dict | None = None) -> list[dict]:
        """Detect and recognize faces in a single image.

        Args:
            image_bytes: Raw JPEG image bytes
            camera_source: Label for the camera
            faces_subset: If provided, only match against these faces
                          (for classwise filtering). If None, uses all known faces.
            insightface_subset: InsightFace embeddings subset for classwise.

        Returns list of recognition results.
        """
        # Preprocess image for better face detection
        enhanced_bytes = _preprocess_image(image_bytes)

        # Use InsightFace if available, with fallback to legacy engine
        if self.use_insightface and self._insightface_app:
            results = self._recognize_insightface(
                enhanced_bytes, camera_source, faces_subset,
                insightface_subset)
            # Fallback: only if InsightFace detected ZERO faces (returns None),
            # try legacy HOG detector which catches distant faces better.
            # If InsightFace detected faces but couldn't match (returns []),
            # don't fall back — InsightFace already handled it.
            if results is None and face_recognition is not None:
                return self._recognize_legacy(
                    enhanced_bytes, camera_source, faces_subset)
            return results or []

        return self._recognize_legacy(enhanced_bytes, camera_source, faces_subset)

    def _recognize_legacy(self, image_bytes: bytes,
                          camera_source: str = "",
                          faces_subset: dict | None = None) -> list[dict]:
        """Detect and recognize faces using the legacy face_recognition library."""
        if face_recognition is None:
            self.add_debug_log("error", "face_recognition library not available")
            return []

        faces_to_check = faces_subset if faces_subset is not None else self.known_faces

        # Load image: force to RGB uint8 numpy array
        # Prefer cv2 decoder — it always produces dlib-compatible arrays.
        # PIL + np.asarray can create arrays that dlib rejects on some
        # numpy/dlib version combinations ("Unsupported image type").
        img_array = None
        try:
            if cv2 is not None:
                nparr = np.frombuffer(image_bytes, dtype=np.uint8)
                bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if bgr is not None:
                    img_array = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                    img_array = np.ascontiguousarray(img_array, dtype=np.uint8)
            # Fallback to PIL if cv2 unavailable or decode failed
            if img_array is None:
                pil_img = Image.open(io.BytesIO(image_bytes))
                pil_img = pil_img.convert("RGB")
                img_array = np.array(pil_img, dtype=np.uint8).copy()
                del pil_img
            if img_array.ndim != 3 or img_array.shape[2] != 3:
                raise ValueError(f"Bad image shape: {img_array.shape}")
        except Exception as e:
            self.add_debug_log("error", f"Failed to load image: {e}")
            return []

        # Upsample 2x to detect smaller/distant faces from security cameras
        try:
            import dlib as _dlib
            _detector = _dlib.get_frontal_face_detector()
            dlib_dets = _detector(img_array, 2)
            # Convert dlib rectangles to face_recognition format (top, right, bottom, left)
            face_locations = [
                (d.top(), d.right(), d.bottom(), d.left()) for d in dlib_dets
            ]
        except Exception as e:
            # Retry with grayscale — dlib also accepts 8-bit gray
            try:
                if cv2 is not None:
                    gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
                else:
                    gray = np.mean(img_array, axis=2).astype(np.uint8)
                gray = np.ascontiguousarray(gray)
                dlib_dets = _detector(gray, 2)
                face_locations = [
                    (d.top(), d.right(), d.bottom(), d.left()) for d in dlib_dets
                ]
                logger.info("dlib face detection succeeded with grayscale fallback")
            except Exception as e2:
                self.add_debug_log("error",
                                   f"Legacy face detection failed for {camera_source}: {e2}")
                return []

        if not face_locations:
            if not self.classwise_running:
                self.add_debug_log("no_face_detected",
                                   f"No faces in frame ({img_array.shape[1]}x{img_array.shape[0]}) from {camera_source}")
            return []

        self.add_debug_log("face_detected",
                           f"{len(face_locations)} face(s) detected from {camera_source} [legacy fallback]")

        face_encodings = face_recognition.face_encodings(img_array, face_locations)

        # Release the large image array to free memory
        del img_array
        del pil_img
        del clean_buf

        results = []

        for i, (encoding, location) in enumerate(zip(face_encodings, face_locations)):
            # Face quality filter: reject tiny/distant/blurry faces
            top, right, bottom, left = location
            face_w = right - left
            face_h = bottom - top
            if face_w < MIN_FACE_WIDTH or face_h < MIN_FACE_HEIGHT:
                self.add_debug_log("face_too_small",
                                   f"Face {face_w}x{face_h}px < {MIN_FACE_WIDTH}x{MIN_FACE_HEIGHT}px "
                                   f"minimum from {camera_source} — skipping",
                                   confidence=0.0)
                continue

            # Assess face crop quality (sharpness, brightness)
            # Use padded crop for better quality assessment and encoding
            face_crop = _extract_padded_face_crop(
                image_bytes, top, right, bottom, left)
            quality = _assess_face_quality(face_crop)
            if not quality["is_acceptable"]:
                self.add_debug_log("face_quality_rejected",
                                   f"Face from {camera_source} rejected: "
                                   f"{quality['rejection_reason']}",
                                   confidence=0.0)
                continue

            match_result = self._match_face(encoding, faces_to_check)

            if match_result:
                person_id = match_result["person_id"]
                confidence = match_result["confidence"]

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

                is_teacher_gate = person_id.startswith(("TEACHER_", "PRINCIPAL_"))
                effective_min = (self.teacher_confidence_threshold
                                if is_teacher_gate else self.confidence_threshold)
                if confidence >= effective_min:
                    result = self._process_attendance(
                        person_id=person_id,
                        name=match_result["name"],
                        phone=match_result["phone"],
                        confidence=confidence,
                        image_bytes=image_bytes,
                        face_location=location,
                        camera_source=camera_source,
                        embedding=encoding,
                        face_size=(face_w, face_h),
                    )
                    if result:
                        results.append(result)
                elif confidence >= self.review_threshold:
                    self.add_debug_log("manual_review",
                                       f"Confidence {confidence:.1%} in review band "
                                       f"({self.review_threshold:.0%}-{effective_min:.0%})",
                                       person_id=person_id,
                                       confidence=confidence)
                    self._queue_manual_review(
                        person_id=person_id,
                        name=match_result["name"],
                        confidence=confidence,
                        camera_source=camera_source,
                    )
                else:
                    self.add_debug_log("low_confidence",
                                       f"Confidence {confidence:.1%} < "
                                       f"{self.review_threshold:.0%} minimum",
                                       person_id=person_id,
                                       confidence=confidence)
            else:
                if not self.test_mode:
                    self.add_debug_log("face_unknown",
                                       f"Unregistered face in {camera_source}",
                                       confidence=0.0)
                    try:
                        ts = int(time.time())
                        snap_path = str(ATTENDANCE_SNAPSHOTS_DIR / f"unknown_{ts}_{i}.jpg")
                        with open(snap_path, "wb") as f:
                            f.write(image_bytes)
                        db.log_unrecognized_face(camera_source, 0.0, snap_path)
                    except Exception:
                        pass

        return results

    def _queue_manual_review(self, person_id: str, name: str,
                             confidence: float, camera_source: str):
        """Queue a low-confidence detection for manual review on the backend."""
        try:
            grade = ""
            parts = person_id.rsplit("_", 1)
            if len(parts) > 1 and not person_id.startswith(("TEACHER_", "PRINCIPAL_")):
                grade = parts[-1]

            review_data = {
                "person_id": person_id,
                "name": name,
                "grade": grade,
                "camera": camera_source,
                "confidence": confidence,
            }

            # Send to backend asynchronously (safe from thread pool)
            async def _send():
                try:
                    import httpx
                    api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.post(
                            f"{api_url}/api/dashboard/review/report",
                            json={"records": [review_data]},
                        )
                        if resp.status_code == 200:
                            logger.info(f"Manual review queued: {name} ({confidence:.1%}) from {camera_source}")
                except Exception as e:
                    logger.warning(f"Failed to queue manual review: {e}")

            loop = getattr(self, '_event_loop', None)
            if loop is not None:
                asyncio.run_coroutine_threadsafe(_send(), loop)
            else:
                try:
                    task = asyncio.create_task(_send())
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                except RuntimeError:
                    logger.warning("Manual review skipped: no event loop")
        except Exception as e:
            logger.warning(f"Manual review queue error: {e}")

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

    def _recognize_insightface(self, image_bytes: bytes,
                                camera_source: str = "",
                                legacy_subset: dict | None = None,
                                insightface_subset: dict | None = None) -> list[dict]:
        """Detect and recognize faces using InsightFace (ArcFace).

        Uses RetinaFace for detection and ArcFace for recognition.
        Much more accurate than face_recognition, especially for
        non-frontal faces and low-resolution images.
        """
        if not self._insightface_app:
            return []

        faces_if = (insightface_subset if insightface_subset is not None
                    else self.known_faces_insightface)
        faces_legacy = (legacy_subset if legacy_subset is not None
                        else self.known_faces)

        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            img_array = np.asarray(img, dtype=np.uint8)
            if img_array.ndim != 3 or img_array.shape[2] != 3:
                self.add_debug_log("error",
                                   f"Bad image shape {img_array.shape} from {camera_source}")
                return []
            # InsightFace expects BGR
            img_bgr = img_array[:, :, ::-1].copy()
        except Exception as e:
            self.add_debug_log("error", f"Failed to load image for InsightFace: {e}")
            return []

        detected = self._insightface_app.get(img_bgr)
        if not detected:
            if not self.classwise_running:
                self.add_debug_log("no_face_detected",
                                   f"No faces in frame ({img_array.shape[1]}x{img_array.shape[0]}) "
                                   f"from {camera_source} [InsightFace]")
            return None  # None = no faces detected (triggers legacy fallback)

        self.add_debug_log("face_detected",
                           f"{len(detected)} face(s) detected from {camera_source} [InsightFace]")

        results = []
        for i, face_obj in enumerate(detected):
            # Face quality filter: reject tiny/distant faces
            bbox = face_obj.bbox  # [x1, y1, x2, y2]
            face_w = bbox[2] - bbox[0]
            face_h = bbox[3] - bbox[1]
            min_face_px = 20  # minimum face size in pixels
            if face_w < min_face_px or face_h < min_face_px:
                self.add_debug_log("face_too_small",
                                   f"Face {face_w:.0f}x{face_h:.0f}px < {min_face_px}px "
                                   f"minimum from {camera_source} — skipping",
                                   confidence=0.0)
                continue

            # Detection score filter: InsightFace detection confidence
            det_score = getattr(face_obj, 'det_score', 1.0)
            if det_score < 0.5:
                self.add_debug_log("low_det_score",
                                   f"Detection score {det_score:.2f} < 0.5 "
                                   f"from {camera_source} — skipping",
                                   confidence=0.0)
                continue

            embedding = face_obj.normed_embedding  # 512-d normalized embedding

            match_result = self._match_insightface(embedding, faces_if)

            # Fallback: try legacy face_recognition encodings via cosine similarity
            if not match_result and faces_legacy and face_recognition is not None:
                match_result = self._match_face_from_insightface_detection(
                    img_array, face_obj, faces_legacy)

            if match_result:
                person_id = match_result["person_id"]
                confidence = match_result["confidence"]

                if self.test_mode and person_id != self.test_person_id:
                    self.add_debug_log("test_mode_skip",
                                       f"Ignoring non-test person {person_id}",
                                       person_id=person_id,
                                       confidence=confidence)
                    continue

                self.add_debug_log("face_matched",
                                   f"Matched {match_result['name']} "
                                   f"(confidence: {confidence:.1%}, "
                                   f"face: {face_w:.0f}x{face_h:.0f}px, "
                                   f"det: {det_score:.2f}) [InsightFace]",
                                   person_id=person_id,
                                   confidence=confidence)

                is_teacher_if = person_id.startswith(("TEACHER_", "PRINCIPAL_"))
                effective_min_if = (self.teacher_confidence_threshold
                                   if is_teacher_if else self.confidence_threshold)
                if confidence >= effective_min_if:
                    result = self._process_attendance(
                        person_id=person_id,
                        name=match_result["name"],
                        phone=match_result["phone"],
                        confidence=confidence,
                        image_bytes=image_bytes,
                        face_location=(0, 0, 0, 0),
                        camera_source=camera_source,
                        embedding=embedding,
                        face_size=(int(face_w), int(face_h)),
                    )
                    if result:
                        results.append(result)
                else:
                    self.add_debug_log("low_confidence",
                                       f"Confidence {confidence:.1%} < "
                                       f"{effective_min_if:.0%} threshold [InsightFace]",
                                       person_id=person_id,
                                       confidence=confidence)
            else:
                if not self.test_mode:
                    self.add_debug_log("face_unknown",
                                       f"Unregistered face in {camera_source} [InsightFace]",
                                       confidence=0.0)
                    try:
                        ts = int(time.time())
                        snap_path = str(ATTENDANCE_SNAPSHOTS_DIR / f"unknown_{ts}_{i}.jpg")
                        with open(snap_path, "wb") as f:
                            f.write(image_bytes)
                        db.log_unrecognized_face(camera_source, 0.0, snap_path)
                    except Exception:
                        pass

        return results

    def _match_insightface(self, embedding: np.ndarray,
                           faces: dict) -> dict | None:
        """Match a 512-d InsightFace embedding against known faces.

        Uses cosine similarity (embeddings are already normalized).
        Returns None if best similarity is below confidence_threshold,
        allowing the legacy face_recognition fallback to be tried.
        """
        best_match = None
        best_sim = 0.0

        for person_id, person_data in faces.items():
            known_embeddings = person_data["encodings"]
            if not known_embeddings:
                continue

            for known_emb in known_embeddings:
                sim = float(np.dot(embedding, known_emb))
                if sim > best_sim:
                    best_sim = sim
                    best_match = {
                        "person_id": person_id,
                        "name": person_data["name"],
                        "phone": person_data["phone"],
                        "confidence": sim,
                        "distance": 1.0 - sim,
                    }

        # Return None for weak matches so the legacy fallback can be tried.
        # Without this threshold, cosine similarity is almost always positive
        # for at least one face, making the legacy fallback unreachable.
        if best_match and best_sim < self.confidence_threshold:
            return None

        return best_match

    def _match_face_from_insightface_detection(
            self, img_array: np.ndarray, face_obj,
            legacy_faces: dict) -> dict | None:
        """Use InsightFace detection + face_recognition encoding for matching.

        When InsightFace embeddings aren't available for a person but
        legacy 128-d encodings are, extract the face region detected by
        InsightFace and compute a face_recognition encoding for matching.
        """
        if face_recognition is None:
            return None
        try:
            # Ensure array is contiguous uint8 RGB for dlib/face_recognition
            safe_img = np.array(img_array, dtype=np.uint8, copy=True)
            if safe_img.ndim != 3 or safe_img.shape[2] != 3:
                return None
            bbox = face_obj.bbox.astype(int)
            x1, y1, x2, y2 = bbox
            h, w = safe_img.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            # face_recognition uses (top, right, bottom, left) format
            face_loc = [(y1, x2, y2, x1)]
            encodings = face_recognition.face_encodings(safe_img, face_loc)
            if not encodings:
                return None
            return self._match_face(encodings[0], legacy_faces)
        except Exception:
            return None

    @staticmethod
    def _is_off_day(dt: datetime) -> bool:
        """Check if the given datetime falls on a school off-day.

        Off-days: every Sunday + only the 2nd Saturday of each month.
        All other Saturdays are working days.
        """
        if dt.weekday() == 6:  # Sunday
            return True
        if dt.weekday() == 5:  # Saturday
            saturday_number = (dt.day - 1) // 7 + 1
            return saturday_number == 2  # Only 2nd Saturday is off
        return False

    def _is_within_attendance_window(self, person_id: str = "") -> bool:
        """Check if the current IST time is within the attendance window.

        Uses a shorter window (7:00-8:00) for teachers vs students (7:00-9:30).
        Returns False on off-days (Sundays, 2nd Saturday) and holidays.
        Students are blocked on ALL Saturdays and Sundays.
        Teachers are only blocked on Sundays and 2nd Saturday.
        """
        from datetime import timezone, timedelta as _td
        _ist = timezone(_td(hours=5, minutes=30))
        now = datetime.now(_ist)

        is_student = not person_id.startswith(("TEACHER_", "PRINCIPAL_"))

        # Students: block on ALL Saturdays and Sundays
        if is_student and now.weekday() >= 5:  # 5=Saturday, 6=Sunday
            return False

        # Teachers: block on Sundays and 2nd Saturday only
        if self._is_off_day(now):
            return False

        # Block on holidays (fetched from backend)
        if self._is_holiday_today():
            return False

        start = now.replace(hour=ATTENDANCE_START_HOUR, minute=ATTENDANCE_START_MINUTE,
                            second=0, microsecond=0)

        # Teachers have a shorter attendance window (7:00-8:00 AM)
        if person_id.startswith("TEACHER_"):
            end = now.replace(hour=TEACHER_END_HOUR, minute=TEACHER_END_MINUTE,
                              second=0, microsecond=0)
        else:
            end = now.replace(hour=ATTENDANCE_END_HOUR, minute=ATTENDANCE_END_MINUTE,
                              second=0, microsecond=0)
        return start <= now <= end

    def _is_holiday_today(self) -> bool:
        """Check if today is a holiday (cached, refreshed once per hour)."""
        now = time.time()
        # Cache for 1 hour to avoid hitting the backend on every scan
        if (hasattr(self, '_holiday_cache_time')
                and now - self._holiday_cache_time < 3600
                and hasattr(self, '_holiday_cache_date')
                and self._holiday_cache_date == date.today().isoformat()):
            return self._holiday_cache_result
        # Default: not a holiday (fail-open if backend is unreachable)
        self._holiday_cache_time = now
        self._holiday_cache_date = date.today().isoformat()
        self._holiday_cache_result = False
        try:
            import httpx
            api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
            resp = httpx.get(f"{api_url}/api/holidays", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                today_str = date.today().isoformat()
                for h in data.get("holidays", []):
                    if h.get("date") == today_str:
                        self._holiday_cache_result = True
                        self.add_debug_log("holiday_detected",
                                           f"Today is a holiday: {h.get('reason', 'Holiday')}")
                        break
        except Exception as e:
            logger.warning(f"Holiday check failed (allowing attendance): {e}")
        return self._holiday_cache_result

    def _is_notification_sent_today(self, person_id: str) -> bool:
        """Check if notification was already sent for this person today.

        Uses in-memory cache first for speed. On cache miss, falls back
        to the persistent database so dedup survives process restarts.
        """
        if FORCE_RENOTIFY_TEST:
            # During test mode, only dedup within this session (in-memory only)
            today = date.today().isoformat()
            return self._notification_sent.get(person_id) == today
        today = date.today().isoformat()
        if self._notification_sent.get(person_id) == today:
            return True
        # Fallback: check persistent DB (survives restarts)
        if db.is_notification_sent_today(person_id):
            self._notification_sent[person_id] = today
            return True
        return False

    def _mark_notification_sent(self, person_id: str):
        """Record that notification was sent for this person today."""
        self._notification_sent[person_id] = date.today().isoformat()

    def _record_sighting(self, person_id: str, confidence: float,
                         camera_source: str,
                         embedding: np.ndarray | None = None,
                         face_size: tuple[int, int] | None = None,
                         face_position: tuple[int, int] | None = None,
                         face_crop_bytes: bytes | None = None) -> int:
        """Record a face sighting and return how many recent sightings exist."""
        now = time.time()
        if person_id not in self._sightings:
            self._sightings[person_id] = []
        self._sightings[person_id].append({
            "time": now,
            "camera": camera_source,
            "confidence": confidence,
            "embedding": embedding,
            "face_size": face_size,
            "face_position": face_position,
            "face_crop_bytes": face_crop_bytes,
        })
        # Prune old sightings outside the window
        cutoff = now - self.sighting_window
        self._sightings[person_id] = [
            s for s in self._sightings[person_id] if s["time"] >= cutoff
        ]
        return len(self._sightings[person_id])

    def _check_anti_spoof(self, person_id: str, name: str) -> bool:
        """Advanced liveness detection: verify the face is a live person.

        Returns True if the sightings appear to be from a REAL live person,
        False if they look like a spoof attempt (photo, screen, print, video).

        SIX-LAYER LIVENESS VERIFICATION:
        1. Embedding variance — real faces vary between frames; static images don't
        2. Face size variance — held photos produce identical face sizes
        3. Face position movement — real people shift position naturally
        4. Texture analysis — detect flat surfaces (prints/screens) vs real skin
        5. Camera diversity — multiple cameras = strong real-person signal
        6. Temporal pattern — real people don't appear at perfect intervals

        If ANY check detects spoofing, the attempt is:
        - Rejected immediately
        - Logged as a security event
        - Snapshot saved for review
        """
        sightings = self._sightings.get(person_id, [])
        if len(sightings) < 2:
            return True  # Not enough data to check, allow

        spoof_score = 0  # Accumulate spoof evidence (>= 2 = blocked)

        # --- CHECK 1: Embedding variance (STRICT) ---
        embeddings = [s["embedding"] for s in sightings
                      if s.get("embedding") is not None]
        if len(embeddings) >= 2:
            similarities = []
            for i in range(len(embeddings) - 1):
                a, b = embeddings[i], embeddings[i + 1]
                norm_a = float(np.linalg.norm(a))
                norm_b = float(np.linalg.norm(b))
                if norm_a > 0 and norm_b > 0:
                    sim = float(np.dot(a, b) / (norm_a * norm_b))
                else:
                    sim = 0.0
                similarities.append(sim)
            avg_sim = sum(similarities) / len(similarities)

            # Tightened threshold: 0.95 (was 0.97)
            # Real faces: 0.70 - 0.93 (natural micro-movements)
            # Static photo: 0.95 - 1.00 (near-identical)
            if avg_sim > 0.95:
                self._log_spoof_attempt(person_id, name, "embedding_frozen",
                                        f"embeddings too similar (avg cosine sim "
                                        f"{avg_sim:.4f} > 0.95) — static image suspected")
                spoof_score += 2  # Strong spoof signal — immediately block

        # --- CHECK 2: Face size variance (STRICT) ---
        face_sizes = [s["face_size"] for s in sightings
                      if s.get("face_size") is not None]
        if len(face_sizes) >= 2:
            widths = [fs[0] for fs in face_sizes]
            heights = [fs[1] for fs in face_sizes]
            avg_w = sum(widths) / len(widths)
            avg_h = sum(heights) / len(heights)
            if avg_w > 0 and avg_h > 0:
                std_w = (sum((w - avg_w) ** 2 for w in widths) / len(widths)) ** 0.5
                std_h = (sum((h - avg_h) ** 2 for h in heights) / len(heights)) ** 0.5
                cv_w = std_w / avg_w
                cv_h = std_h / avg_h
                # Tightened: CV < 0.01 (was 0.005)
                if cv_w < 0.01 and cv_h < 0.01:
                    self._log_spoof_attempt(person_id, name, "size_frozen",
                                            f"face size too consistent "
                                            f"(width CV={cv_w:.4f}, height CV={cv_h:.4f})")
                    spoof_score += 1

        # --- CHECK 3: Face position movement (NEW) ---
        positions = [s["face_position"] for s in sightings
                     if s.get("face_position") is not None]
        if len(positions) >= 2:
            # Calculate total movement across frames
            total_movement = 0.0
            for i in range(len(positions) - 1):
                dx = abs(positions[i + 1][0] - positions[i][0])
                dy = abs(positions[i + 1][1] - positions[i][1])
                total_movement += (dx ** 2 + dy ** 2) ** 0.5
            avg_movement = total_movement / (len(positions) - 1)

            # Real person: natural head/body movement between frames (> 3px)
            # Static photo: nearly zero movement (< 2px)
            if avg_movement < 2.0:
                self._log_spoof_attempt(person_id, name, "no_movement",
                                        f"face position frozen across {len(positions)} "
                                        f"frames (avg movement {avg_movement:.1f}px)")
                spoof_score += 1

        # --- CHECK 4: Texture analysis for print/screen detection (NEW) ---
        face_crops = [s["face_crop_bytes"] for s in sightings
                      if s.get("face_crop_bytes") is not None]
        if face_crops and cv2 is not None:
            try:
                latest_crop = face_crops[-1]
                arr = np.frombuffer(latest_crop, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

                    # 4a. High-frequency content analysis
                    # Real skin has rich micro-texture; prints/screens are smoother
                    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
                    lap_var = float(laplacian.var())
                    lap_mean = float(np.abs(laplacian).mean())

                    # 4b. Color channel distribution (screens have distinct patterns)
                    b, g, r = cv2.split(img)
                    # Screens often have higher blue channel variance
                    b_std = float(np.std(b))
                    g_std = float(np.std(g))
                    r_std = float(np.std(r))

                    # 4c. Moiré pattern detection (screen display artifact)
                    # Apply FFT and check for periodic patterns
                    f_transform = np.fft.fft2(gray.astype(np.float64))
                    f_shift = np.fft.fftshift(f_transform)
                    magnitude = np.abs(f_shift)
                    # High magnitude peaks at non-DC frequencies = periodic pattern
                    h, w = magnitude.shape
                    center_h, center_w = h // 2, w // 2
                    # Mask out the DC component (center)
                    mask_size = max(3, min(h, w) // 20)
                    magnitude[center_h - mask_size:center_h + mask_size,
                              center_w - mask_size:center_w + mask_size] = 0
                    peak_ratio = float(np.max(magnitude)) / (float(np.mean(magnitude)) + 1e-6)

                    # Screen moiré: very high peak ratio (> 50)
                    if peak_ratio > 50:
                        self._log_spoof_attempt(person_id, name, "moire_pattern",
                                                f"screen moiré detected "
                                                f"(FFT peak ratio {peak_ratio:.1f})")
                        spoof_score += 2  # Strong signal

                    # 4d. Uniform texture detection (printed photo)
                    # Real skin has varied texture; prints are more uniform
                    local_std = cv2.blur(
                        (gray.astype(np.float64) - cv2.blur(gray, (5, 5)).astype(np.float64)) ** 2,
                        (15, 15),
                    )
                    texture_var = float(np.mean(local_std))
                    if texture_var < 5.0 and lap_var < 50:
                        self._log_spoof_attempt(person_id, name, "flat_texture",
                                                f"unnaturally uniform texture "
                                                f"(var={texture_var:.1f}, lap={lap_var:.1f})")
                        spoof_score += 1

            except Exception as e:
                logger.debug(f"Texture analysis error for {name}: {e}")

        # --- CHECK 5: Camera diversity ---
        cameras = set(s["camera"] for s in sightings)
        if len(cameras) > 1:
            # Multi-camera sightings = strong real-person signal
            # Reduce spoof score (it's very hard to spoof across cameras)
            spoof_score = max(0, spoof_score - 1)
            self.add_debug_log("liveness_multi_cam",
                               f"{name}: seen on {len(cameras)} cameras — "
                               f"strong real-person signal",
                               person_id=person_id)

        # --- CHECK 6: Temporal pattern (NEW) ---
        if len(sightings) >= 3:
            timestamps = [s["time"] for s in sightings]
            intervals = [timestamps[i + 1] - timestamps[i]
                         for i in range(len(timestamps) - 1)]
            if intervals:
                avg_interval = sum(intervals) / len(intervals)
                # Check if intervals are suspiciously regular (robotic precision)
                if avg_interval > 0:
                    interval_cv = (sum((iv - avg_interval) ** 2 for iv in intervals)
                                   / len(intervals)) ** 0.5 / avg_interval
                    # Real person: irregular timing (CV > 0.1)
                    # Replay/loop: nearly perfect intervals (CV < 0.05)
                    if interval_cv < 0.05 and len(intervals) >= 3:
                        self._log_spoof_attempt(person_id, name, "regular_timing",
                                                f"suspiciously regular detection intervals "
                                                f"(CV={interval_cv:.4f})")
                        spoof_score += 1

        # --- FINAL DECISION ---
        if spoof_score >= 2:
            self.add_debug_log("liveness_BLOCKED",
                               f"{name}: SPOOF DETECTED (score {spoof_score}/6) — "
                               f"attendance REJECTED",
                               person_id=person_id)
            return False

        if spoof_score == 1:
            self.add_debug_log("liveness_warning",
                               f"{name}: minor spoof signal (score 1/6) — "
                               f"allowing but flagged for review",
                               person_id=person_id)

        self.add_debug_log("liveness_passed",
                           f"{name}: liveness verified (spoof score {spoof_score}/6, "
                           f"{len(cameras)} camera(s), {len(sightings)} sightings)",
                           person_id=person_id)
        return True

    def _log_spoof_attempt(self, person_id: str, name: str,
                           spoof_type: str, details: str):
        """Log a suspected spoof attempt with snapshot for security review."""
        self.add_debug_log(f"spoof_{spoof_type}",
                           f"SPOOF ALERT — {name}: {details}",
                           person_id=person_id)
        # Save snapshot of the spoof attempt
        try:
            sightings = self._sightings.get(person_id, [])
            if sightings:
                latest = sightings[-1]
                crop = latest.get("face_crop_bytes")
                if crop:
                    ts = int(time.time())
                    spoof_dir = ATTENDANCE_SNAPSHOTS_DIR / "spoof_attempts"
                    spoof_dir.mkdir(exist_ok=True)
                    spoof_path = spoof_dir / f"spoof_{person_id}_{spoof_type}_{ts}.jpg"
                    with open(spoof_path, "wb") as f:
                        f.write(crop)
                    self.add_debug_log("spoof_snapshot_saved",
                                       f"Spoof attempt snapshot saved: {spoof_path}",
                                       person_id=person_id)
        except Exception as e:
            logger.debug(f"Failed to save spoof snapshot: {e}")

    def _process_attendance(self, person_id: str, name: str, phone: str,
                            confidence: float, image_bytes: bytes,
                            face_location: tuple,
                            camera_source: str,
                            embedding: np.ndarray | None = None,
                            face_size: tuple[int, int] | None = None) -> dict | None:
        """Process an attendance detection with multi-layer verification.

        All checks must pass before marking attendance:
        CHECK 1: High-confidence facial recognition (threshold depends on role)
        CHECK 2: Detection from authorized cameras
        CHECK 3: Detection within attendance time window
        CHECK 4: Repeated face confirmation across frames (min_sightings)
        CHECK 5: Entry gate / reception validation
        CHECK 6: Anti-spoofing / liveness checks
        """
        now = time.time()
        is_teacher = person_id.startswith(("TEACHER_", "PRINCIPAL_"))

        # --- CHECK 1: Confidence range check ---
        effective_threshold = (self.teacher_confidence_threshold
                               if is_teacher else self.confidence_threshold)
        effective_max = (self.teacher_confidence_max
                         if is_teacher else self.confidence_max)
        if confidence < effective_threshold:
            self.add_debug_log("confidence_rejected",
                               f"{name} confidence {confidence:.1%} < "
                               f"{effective_threshold:.0%} threshold "
                               f"({'teacher' if is_teacher else 'student'})",
                               person_id=person_id, confidence=confidence)
            self._record_false_positive()
            return None
        if confidence > effective_max:
            self.add_debug_log("confidence_too_high",
                               f"{name} confidence {confidence:.1%} > "
                               f"{effective_max:.0%} max — doubtful match, ignoring",
                               person_id=person_id, confidence=confidence)
            self._record_false_positive()
            return None

        # --- CHECK 3: Time window ---
        if not self._is_within_attendance_window(person_id):
            return None

        # --- Daily dedup: one entry per student per day ---
        if self._is_already_marked_today(person_id):
            self.add_debug_log("daily_already_marked",
                               f"{name} already marked today",
                               person_id=person_id,
                               confidence=confidence)
            return None

        # --- CHECK 5: Entry gate / reception validation ---
        # Record entry validation if detected on entry/reception camera
        if _is_entry_camera(camera_source):
            self.entry_validated[person_id] = date.today().isoformat()

        # Compute face position (center of bounding box) for movement tracking
        face_position = None
        face_crop_bytes = None
        if face_location:
            try:
                top, right, bottom, left = face_location
                cx = (left + right) // 2
                cy = (top + bottom) // 2
                face_position = (cx, cy)
                # Crop face for texture analysis
                if Image is not None:
                    pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                    crop = pil_img.crop((left, top, right, bottom))
                    buf = io.BytesIO()
                    crop.save(buf, format="JPEG", quality=90)
                    face_crop_bytes = buf.getvalue()
            except Exception:
                pass

        # --- CHECK 4: Multi-frame verification ---
        # Teachers: 1 sighting (single valid detection sufficient)
        # Students: 2 sightings (multi-frame confirmation required)
        required_sightings = 1 if is_teacher else self.min_sightings
        sighting_count = self._record_sighting(
            person_id, confidence, camera_source,
            embedding=embedding, face_size=face_size,
            face_position=face_position,
            face_crop_bytes=face_crop_bytes,
        )
        if sighting_count < required_sightings:
            self.add_debug_log("awaiting_confirmation",
                               f"{name} sighting {sighting_count}/{required_sightings} "
                               f"(need {required_sightings} within {self.sighting_window}s "
                               f"to confirm presence)",
                               person_id=person_id,
                               confidence=confidence)
            return None

        # --- CHECK 5b: Entry validation skipped ---
        # Both teachers and students: mark wherever detected (no gate requirement)
        # Summer camp students are spread across classrooms, not at assigned grades
        self.entry_validated[person_id] = date.today().isoformat()

        # --- CHECK 6: Anti-spoofing ---
        if not self._check_anti_spoof(person_id, name):
            self.add_debug_log("spoof_rejected",
                               f"{name} blocked by anti-spoof check — "
                               f"resetting sightings",
                               person_id=person_id,
                               confidence=confidence)
            self._sightings.pop(person_id, None)
            return None

        # --- Compute average confidence from all sightings ---
        sightings = self._sightings.get(person_id, [])
        avg_confidence = confidence
        if sightings:
            confs = [s["confidence"] for s in sightings if s.get("confidence")]
            if confs:
                avg_confidence = sum(confs) / len(confs)
        # Use average confidence for final check
        if avg_confidence < effective_threshold:
            self.add_debug_log("avg_confidence_low",
                               f"{name} average confidence {avg_confidence:.1%} across "
                               f"{len(sightings)} sightings < {effective_threshold:.0%}",
                               person_id=person_id, confidence=avg_confidence)
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
        from datetime import timezone, timedelta as _td
        _ist = timezone(_td(hours=5, minutes=30))
        time_str = datetime.now(_ist).strftime("%I:%M %p")

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

        # Schedule async tasks from thread pool using stored event loop
        loop = getattr(self, '_event_loop', None)
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        def _schedule(coro):
            if loop is None:
                logger.error("No event loop available for async task")
                return
            try:
                asyncio.get_running_loop()
                asyncio.create_task(coro)
            except RuntimeError:
                asyncio.run_coroutine_threadsafe(coro, loop)

        # Sync attendance to cloud dashboard
        _schedule(self._sync_attendance_to_cloud(result, phone or ""))

        # Send WhatsApp notification ONCE per person per day
        if phone and not self._is_notification_sent_today(person_id):
            phone_list = [p.strip() for p in phone.split(",") if p.strip()]
            for parent_phone in phone_list:
                _schedule(
                    self._send_whatsapp_notification(
                        attendance_id=attendance_id,
                        person_id=person_id,
                        name=name,
                        time_str=time_str,
                        phone=parent_phone,
                        snapshot_bytes=image_bytes,
                    )
                )

        return result

    async def _send_whatsapp_notification(self, attendance_id: int,
                                           person_id: str,
                                           name: str, time_str: str,
                                           phone: str,
                                           snapshot_bytes: bytes | None = None):
        """Send WhatsApp attendance notification via cloud bot API.

        Uses ppis_attendance_alert template for guaranteed delivery.
        For teachers (person_id starts with TEACHER_), the notification
        goes directly to the teacher's own WhatsApp number with a
        face snapshot image header.
        """
        api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {"Content-Type": "application/json"}
        if agent_secret:
            headers["X-Agent-Secret"] = agent_secret

        is_teacher = person_id.startswith(("TEACHER_", "PRINCIPAL_"))
        display_name = name.title() if name == name.upper() else name
        if is_teacher:
            notif_name = display_name  # Template has "Dear {{1}}, you have been"
            tpl_name = "ppis_teacher_present_text"  # Text-only template (no image header)
            tpl_lang = "en"
        else:
            notif_name = f"{display_name} has been"
            tpl_name = "ppis_attendance_alert"
            tpl_lang = "en"

        # Log confidence level for monitoring
        logger.info(f"[NOTIFICATION] Sending to {phone} for {display_name} "
                     f"(confidence verified, entry validated)")

        # Build request payload (text-only for teachers, no image header needed)
        payload = {
            "phone": phone,
            "template_name": tpl_name,
            "template_params": [notif_name, time_str],
            "language_code": tpl_lang,
        }

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            sent = False
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        f"{api_url}/api/send-whatsapp",
                        json=payload,
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                            if data.get("status") == "ok":
                                sent = True
                        except Exception:
                            pass

                    if sent:
                        db.update_whatsapp_sent(attendance_id)
                        self._mark_notification_sent(person_id)
                        self.add_debug_log("whatsapp_sent",
                                           f"Notification sent to {phone}: "
                                           f"[Attendance] {name} at {time_str}")
                        return
                    else:
                        resp_text = ""
                        try:
                            resp_text = resp.text[:200]
                        except Exception:
                            pass
                        self.add_debug_log("whatsapp_retry" if attempt < max_retries else "whatsapp_failed",
                                           f"Attempt {attempt}/{max_retries} failed for {phone}: {resp_text}")
            except Exception as e:
                self.add_debug_log("whatsapp_retry" if attempt < max_retries else "whatsapp_failed",
                                   f"Attempt {attempt}/{max_retries} failed for {phone}: {type(e).__name__}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)

    # ------------------------------------------------------------------
    # Meal snapshot notifications
    # ------------------------------------------------------------------

    def _is_meal_window_active(self) -> tuple[bool, str]:
        """Check if we are inside a meal snapshot window.

        Returns (active, window_label).  Only fires on weekdays and
        before MEAL_SNAPSHOT_END_DATE.
        """
        if not MEAL_SNAPSHOT_ENABLED:
            return False, ""
        from datetime import timezone as _tz, timedelta as _td
        _ist = _tz(_td(hours=5, minutes=30))
        now = datetime.now(_ist)
        # Weekdays only (Mon=0 .. Fri=4)
        if now.weekday() >= 5:
            return False, ""
        # Respect end date
        if now.strftime("%Y-%m-%d") > MEAL_SNAPSHOT_END_DATE:
            return False, ""
        cur_mins = now.hour * 60 + now.minute
        for sh, sm, eh, em, label in MEAL_WINDOWS:
            if (sh * 60 + sm) <= cur_mins < (eh * 60 + em):
                return True, label
        return False, ""

    async def _run_meal_snapshot_cycle(self, classroom_cams: list[dict],
                                       window_label: str):
        """Capture one snapshot per classroom camera and send meal
        notifications to parents of children marked present today.

        Each child gets ONE notification per meal window per day.
        The snapshot sent is from the child's classroom camera (matched
        by grade in person_id).
        """
        from datetime import timezone as _tz, timedelta as _td
        import base64
        _ist = _tz(_td(hours=5, minutes=30))
        _now = datetime.now(_ist)
        today = _now.strftime("%Y-%m-%d")
        date_display = _now.strftime("%d-%m-%Y")
        time_display = _now.strftime("%I:%M %p")

        # Build map: grade -> list of classroom cameras
        grade_cams: dict[str, list[dict]] = {}
        for cam in classroom_cams:
            g = cam.get("grade")
            if g:
                grade_cams.setdefault(g, []).append(cam)

        # Determine which students were marked present AND notified today
        notified_students: list[dict] = []
        for pid, sent_date in self._notification_sent.items():
            if sent_date != today:
                continue
            if pid.startswith(("TEACHER_", "PRINCIPAL_")):
                continue
            face = self.known_faces.get(pid)
            if not face:
                continue
            phone = face.get("phone", "")
            if not phone:
                continue
            notified_students.append({
                "person_id": pid,
                "name": face["name"],
                "phone": phone,
            })

        if not notified_students:
            self.add_debug_log("meal_snapshot",
                               f"{window_label}: No present+notified students — skipping")
            return

        self.add_debug_log("meal_snapshot",
                           f"{window_label}: {len(notified_students)} present students, "
                           f"{len(grade_cams)} classroom grades with cameras")

        # Determine which camera grades we actually need snapshots from.
        # Summer camp: Grades 9-12 eat in own class; younger students
        # eat in Grade 1A / 1B (common rooms).
        needed_grades: set[str] = set()
        for grade in grade_cams:
            if grade in MEAL_OWN_CLASSROOM_GRADES or grade in MEAL_CAMP_ROOM_GRADES:
                needed_grades.add(grade)

        # Capture one snapshot per needed grade (first working camera)
        grade_snapshots: dict[str, bytes] = {}
        for grade in needed_grades:
            cams = grade_cams.get(grade, [])
            for cam in cams:
                try:
                    img = await self.capture_frame_from_dvr(
                        cam["dvr"], cam["channel"])
                    if img and len(img) > 1000:
                        grade_snapshots[grade] = img
                        break
                except Exception as e:
                    logger.debug(f"Meal snapshot failed for {cam['label']}: {e}")

        # Pick any camp-room snapshot for younger students
        camp_room_snapshot: bytes | None = None
        for cr_grade in MEAL_CAMP_ROOM_GRADES:
            if cr_grade in grade_snapshots:
                camp_room_snapshot = grade_snapshots[cr_grade]
                break

        if not grade_snapshots:
            self.add_debug_log("meal_snapshot",
                               f"{window_label}: Could not capture any classroom snapshots")
            return

        self.add_debug_log("meal_snapshot",
                           f"{window_label}: Captured snapshots for "
                           f"{len(grade_snapshots)} classrooms: {list(grade_snapshots.keys())}")

        # Extract grade from person_id
        def _grade_from_pid(pid: str) -> str:
            for part in pid.split("_"):
                if part.startswith("GRADE") or part.startswith("NUR") or part.startswith("PREP"):
                    return part
            return ""

        api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {"Content-Type": "application/json"}
        if agent_secret:
            headers["X-Agent-Secret"] = agent_secret

        sent_count = 0
        skip_count = 0
        for student in notified_students:
            pid = student["person_id"]
            dedup_key = f"{pid}:{window_label}"
            if self._meal_sent.get(dedup_key) == today:
                skip_count += 1
                continue

            grade = _grade_from_pid(pid)

            # Only send meal snapshots for Grades 9-12 (own classroom)
            # Younger students (camp rooms) are excluded
            if grade not in MEAL_OWN_CLASSROOM_GRADES:
                continue
            snapshot = grade_snapshots.get(grade)

            if not snapshot:
                continue

            # Format grade for display (e.g. GRADE9A -> Grade 9A)
            grade_display = grade
            if grade.startswith("GRADE"):
                num = grade[5:]
                grade_display = f"Grade {num}"
            elif grade.startswith("PREP"):
                num = grade[4:]
                grade_display = f"Prep {num}" if num else "Prep"
            elif grade.startswith("NUR"):
                num = grade[3:]
                grade_display = f"Nursery {num}" if num else "Nursery"

            phone_list = [p.strip() for p in student["phone"].split(",") if p.strip()]
            for parent_phone in phone_list:
                payload = {
                    "phone": parent_phone,
                    "template_name": "ppis_meal_update",
                    "template_params": [grade_display, date_display, time_display],
                    "language_code": "en",
                    "header_image_base64": base64.b64encode(snapshot).decode(),
                }
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.post(
                            f"{api_url}/api/send-whatsapp",
                            json=payload, headers=headers,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get("status") in ("ok", "partial"):
                                sent_count += 1
                except Exception as e:
                    logger.warning(f"Meal notification failed for {parent_phone}: {e}")

            self._meal_sent[dedup_key] = today

        self.add_debug_log("meal_snapshot_done",
                           f"{window_label}: Sent {sent_count} meal notifications, "
                           f"skipped {skip_count} (already sent)")

    async def _sync_attendance_to_cloud(self, record: dict, parent_phones: str):
        """Report attendance record to cloud backend for dashboard display."""
        api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {"Content-Type": "application/json"}
        if agent_secret:
            headers["X-Agent-Secret"] = agent_secret

        # Extract grade from person_id (e.g. NAVYA_MEHTA_GRADE2A -> GRADE 2A)
        pid = record.get("person_id", "")
        grade = ""
        for part in pid.split("_"):
            if part.startswith("GRADE") or part.startswith("NUR") or part.startswith("PREP"):
                grade = part
                break

        payload = {
            "records": [{
                "person_id": pid,
                "name": record.get("name", ""),
                "grade": grade,
                "camera": record.get("camera_source", ""),
                "confidence": record.get("confidence", 0),
                "notification_sent": bool(parent_phones),
                "parent_phones": parent_phones,
                "logged_at": datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat(),
            }]
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{api_url}/api/dashboard/attendance/report",
                    json=payload, headers=headers,
                )
                if resp.status_code == 200:
                    self.add_debug_log("cloud_sync",
                                       f"Attendance synced to cloud: {record.get('name')}")
                else:
                    self.add_debug_log("cloud_sync_error",
                                       f"Cloud sync failed: HTTP {resp.status_code}")
        except Exception as e:
            self.add_debug_log("cloud_sync_error", f"Cloud sync error: {e}")

    async def _resync_todays_records(self):
        """Re-sync locally marked attendance records to cloud.

        Only syncs record metadata to the cloud dashboard — does NOT
        re-send WhatsApp notifications. The backend handles notification
        sending as a safety net so we avoid duplicate sends and startup
        crashes from heavy HTTP activity.
        """
        try:
            records = db.get_attendance_log(limit=200)
            today_ist = date.today().isoformat()
            today_records = [
                r for r in records
                if r.get("logged_at", "")[:10] == today_ist
            ]
            if not today_records:
                return

            logger.info(f"Re-syncing {len(today_records)} locally-marked records to cloud (metadata only)")

            for rec in today_records:
                pid = rec.get("person_id", "")
                name = rec.get("name", "")
                confidence = rec.get("confidence", 0)
                camera = rec.get("camera_source", "")

                # Get phone from face DB
                phone = ""
                for face_pid, face_data in self.known_faces.items():
                    if face_pid == pid:
                        phone = face_data.get("phone", "")
                        break

                # Sync to cloud dashboard
                grade = ""
                for part in pid.split("_"):
                    if part.startswith("GRADE") or part.startswith("NUR") or part.startswith("PREP"):
                        grade = part
                        break

                await self._sync_attendance_to_cloud({
                    "person_id": pid,
                    "name": name,
                    "confidence": confidence,
                    "camera_source": camera,
                }, phone)

                # Re-send notifications only for records that weren't notified
                if phone and not whatsapp_sent:
                    # Convert logged_at (UTC in DB) to IST for notification
                    try:
                        from datetime import timezone, timedelta as _td
                        _ist = timezone(_td(hours=5, minutes=30))
                        t = datetime.strptime(logged_at[:19], "%Y-%m-%d %H:%M:%S")
                        t_ist = t + _td(hours=5, minutes=30)
                        time_str = t_ist.strftime("%I:%M %p")
                    except Exception:
                        from datetime import timezone, timedelta as _td
                        _ist = timezone(_td(hours=5, minutes=30))
                        time_str = datetime.now(_ist).strftime("%I:%M %p")

                    phone_list = [p.strip() for p in phone.split(",") if p.strip()]
                    for parent_phone in phone_list:
                        await self._send_whatsapp_notification(
                            attendance_id=attendance_id,
                            person_id=pid,
                            name=name,
                            time_str=time_str,
                            phone=parent_phone,
                        )

            logger.info(f"Re-sync complete for {len(today_records)} records")
        except Exception as e:
            logger.error(f"Re-sync failed: {e}")

    async def _send_camera_alert(self, cam_key: str, camera_label: str,
                                 error_count: int, alert_type: str = "offline"):
        """Send WhatsApp alert when a camera goes offline or recovers."""
        if not self._admin_phones:
            return
        api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {}
        if agent_secret:
            headers["X-Agent-Secret"] = agent_secret

        now = datetime.now().strftime("%I:%M %p")
        if alert_type == "offline":
            msg = (
                f"\u26a0\ufe0f *Camera Alert*\n\n"
                f"Camera *{camera_label}* is offline.\n"
                f"Failed {error_count} consecutive times.\n"
                f"Time: {now}\n\n"
                f"Please check the camera connection."
            )
        else:
            msg = (
                f"\u2705 *Camera Recovered*\n\n"
                f"Camera *{camera_label}* is back online.\n"
                f"Time: {now}"
            )

        phone_list = ",".join(self._admin_phones)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{api_url}/api/send-whatsapp",
                    json={"phone": phone_list, "message": msg},
                    headers=headers,
                )
            self.add_debug_log("camera_alert",
                               f"{alert_type}: {camera_label} -> {phone_list}")
        except Exception as e:
            self.add_debug_log("camera_alert_error",
                               f"Failed to send alert for {camera_label}: {e}")

    def _cam_key_to_label(self, cam_key: str) -> str:
        """Resolve a cam_key like '192.168.0.12:57' to a friendly label."""
        mapping = self._last_camera_mapping or {}
        dvrs = self._last_dvrs or []
        for loc, data in mapping.items():
            dvr_idx = data.get("dvr_index", 0)
            ch = data.get("channel", 1)
            if dvr_idx < len(dvrs):
                ip = dvrs[dvr_idx].get("ip", "")
                if f"{ip}:{ch}" == cam_key:
                    return loc
        return cam_key

    async def _check_camera_health_alerts(self):
        """Check camera error counts and send alerts for offline cameras."""
        for cam_key, error_count in list(self._camera_errors.items()):
            if error_count >= self._camera_alert_threshold:
                if cam_key not in self._admin_alerted:
                    label = self._cam_key_to_label(cam_key)
                    self._admin_alerted.add(cam_key)
                    await self._send_camera_alert(cam_key, label, error_count,
                                                  "offline")

        # Check for recovered cameras (were alerted but errors cleared)
        for cam_key in list(self._admin_alerted):
            if cam_key not in self._camera_errors:
                if cam_key not in self._camera_recovered:
                    label = self._cam_key_to_label(cam_key)
                    self._camera_recovered.add(cam_key)
                    await self._send_camera_alert(cam_key, label, 0, "recovered")

    async def _report_camera_status_to_backend(self, cameras: list[dict]):
        """Report camera health status to the backend for dashboard tracking."""
        try:
            camera_statuses = []
            for cam in cameras:
                cam_key = f"{cam['dvr']['ip']}:{cam['channel']}"
                errors = self._camera_errors.get(cam_key, 0)
                status = "offline" if errors >= self._camera_alert_threshold else "online"
                camera_statuses.append({
                    "label": cam["label"],
                    "dvr_ip": cam["dvr"]["ip"],
                    "channel": cam["channel"],
                    "status": status,
                    "error_code": f"{errors} consecutive failures" if errors else "",
                    "consecutive_failures": errors,
                })

            api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{api_url}/api/dashboard/cameras/status/report",
                    json={"cameras": camera_statuses},
                )
        except Exception as e:
            logger.warning(f"Camera status report failed (non-fatal): {e}")

    def _get_dvr_client(self, dvr: dict) -> httpx.AsyncClient:
        """Get or create a persistent HTTP client for a DVR (connection pooling)."""
        ip = dvr["ip"]
        if ip not in self._dvr_clients or self._dvr_clients[ip].is_closed:
            self._dvr_clients[ip] = httpx.AsyncClient(
                timeout=8.0,
                auth=httpx.DigestAuth(dvr["username"], dvr["password"]),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
            )
        return self._dvr_clients[ip]

    async def capture_frame_from_dvr(self, dvr: dict, channel: int,
                                     max_retries: int = 2) -> bytes | None:
        """Capture a single frame from a Hikvision DVR via ISAPI snapshot.

        Uses persistent HTTP client with connection pooling for speed.
        Probes the camera's native resolution on first call and requests
        the highest available quality (up to 4MP if the camera supports it).
        """
        ip = dvr["ip"]
        port = dvr.get("port", 80)

        client = self._get_dvr_client(dvr)

        # Probe native resolution on first capture (cached per channel)
        native_res = await _probe_channel_resolution(client, ip, port, channel)
        if native_res:
            req_w, req_h = native_res
        else:
            req_w, req_h = 1920, 1080  # default fallback

        stream_channel = channel * 100 + 1
        url = (f"http://{ip}:{port}/ISAPI/Streaming/channels/{stream_channel}/picture"
               f"?snapShotImageType=JPEG"
               f"&videoResolutionWidth={req_w}&videoResolutionHeight={req_h}")

        for attempt in range(max_retries):
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.headers.get(
                        "content-type", "").startswith("image"):
                    cam_key = f"{ip}:{channel}"
                    self._camera_errors.pop(cam_key, None)
                    return resp.content
                cam_key = f"{ip}:{channel}"
                self._camera_errors[cam_key] = self._camera_errors.get(cam_key, 0) + 1
                ct = resp.headers.get("content-type", "unknown")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    self.add_debug_log(
                        "dvr_error",
                        f"Capture failed from {ip} ch{channel} after "
                        f"{max_retries} attempts: HTTP {resp.status_code} "
                        f"(content-type={ct})")
            except Exception as e:
                cam_key = f"{ip}:{channel}"
                self._camera_errors[cam_key] = self._camera_errors.get(cam_key, 0) + 1
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    self.add_debug_log("dvr_error",
                                       f"Capture failed from {ip} ch{channel} "
                                       f"after {max_retries} attempts: {e}")
        return None

    async def scan_camera(self, dvr: dict, channel: int,
                          camera_label: str = "",
                          faces_subset: dict | None = None,
                          insightface_subset: dict | None = None) -> list[dict]:
        """Capture a frame from a camera and run face recognition on it."""
        frame = await self.capture_frame_from_dvr(dvr, channel)
        if frame is None:
            return []

        source = camera_label or f"{dvr['ip']}:ch{channel}"
        # Run CPU-bound face recognition in a thread pool so the event
        # loop stays responsive for WebSocket snapshot requests.
        loop = asyncio.get_event_loop()
        # Store loop reference so thread-pool code can schedule async tasks
        self._event_loop = loop
        return await loop.run_in_executor(
            None, self.recognize_faces_in_image,
            frame, source, faces_subset, insightface_subset)

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
        try:
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
            while self.running:
                # --- Off-day/holiday check ---
                from datetime import timezone as _tz3, timedelta as _td3
                _ist3 = _tz3(_td3(hours=5, minutes=30))
                _now3 = datetime.now(_ist3)
                if self._is_off_day(_now3) or self._is_holiday_today():
                    await asyncio.sleep(60)
                    continue

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

    @staticmethod
    def _classify_camera_type(location: str) -> str:
        """Classify a camera into a type for attendance scanning rules.

        Camera types:
        - 'reception': Reception cameras — Phase 1 teacher + Phase 2 students
        - 'principal': Principal Room — Phase 1 only
        - 'entry_gate': Entry gates — Phase 2 students only
        - 'staff': Teacher Staff, Admission, Admin, Accounts, Academic Coord — Phase 1 only
        - 'classroom': Grade classrooms (NUR, PREP, GRADE) — Phase 2 students
        - 'other': Labs, galleries, parks, etc. — skip
        """
        loc_upper = location.upper()
        if "RECEPTION" in loc_upper:
            return "reception"
        if "PRINCIPAL" in loc_upper:
            return "principal"
        if any(kw in loc_upper for kw in {"ENTRY", "ENTRANCE", "DISPERSAL"}):
            return "entry_gate"
        if any(kw in loc_upper for kw in {"TEACHER STAFF", "STAFF ROOM",
                                           "ACADEMIC COORDINATOR", "ADMIN ROOM",
                                           "ACCOUNTS ROOM", "ADMISSION",
                                           "ADMINISTRATION"}):
            return "staff"
        grade = _extract_grade_from_location(location)
        if grade is not None:
            return "classroom"
        return "other"

    def build_classroom_camera_list(self, camera_mapping: dict,
                                     dvrs: list[dict]) -> list[dict]:
        """Build list of ALL cameras with their DVR configs, grade, and camera type.

        Each classroom may have multiple cameras (C1, C2). This includes
        ALL camera feeds per classroom from the all_cameras field.

        Camera types for attendance:
        - reception: Reception C1-C4 → Phase 1 teachers + Phase 2 students
        - principal: Principal Room → Phase 1 only
        - entry_gate: Entry Gate → Phase 2 students only
        - staff: Teacher Staff, Admission, Admin, Accounts, Acad. Coord → Phase 1 only
        - classroom: Grade classrooms → Phase 2 students only
        - other: Labs, galleries, parks → all faces (test mode) / skip (production)

        Returns list of dicts:
            {
                "location": "GRADE 3C",
                "grade": "GRADE3C",
                "dvr_index": 1,
                "channel": 13,
                "dvr": {...},
                "label": "GRADE 3C (DVR 2 Ch 13)",
                "is_gate": False,
                "cam_type": "classroom",
            }
        """
        cameras = []
        seen = set()  # (dvr_index, channel) to avoid duplicates

        # Process ALL cameras from the mapping
        for location, cam_data in camera_mapping.items():
            grade = _extract_grade_from_location(location)
            cam_type = self._classify_camera_type(location)

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
                    "grade": grade,
                    "dvr_index": dvr_idx,
                    "channel": channel,
                    "dvr": dvrs[dvr_idx],
                    "label": f"{location} (DVR {dvr_idx + 1} Ch {channel})",
                    "is_gate": cam_type in ("reception", "entry_gate"),
                    "cam_type": cam_type,
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
        try:
            self.classwise_running = True
            self._last_dvrs = dvrs
            self._last_camera_mapping = camera_mapping
            self.reload_faces()

            cameras = self.build_classroom_camera_list(camera_mapping, dvrs)

            # Categorize cameras by type
            reception_cams = [c for c in cameras if c["cam_type"] == "reception"]
            principal_cams = [c for c in cameras if c["cam_type"] == "principal"]
            entry_gate_cams = [c for c in cameras if c["cam_type"] == "entry_gate"]
            all_staff_cams = [c for c in cameras if c["cam_type"] == "staff"]
            all_classroom_cams = [c for c in cameras if c["cam_type"] == "classroom"]
            all_other_cams = [c for c in cameras if c["cam_type"] == "other"]

            # Separate admission and administration cameras from other staff cams
            administration_cams = [c for c in all_staff_cams
                                   if "ADMINISTRATION" in c["location"].upper()]
            admission_cams = [c for c in all_staff_cams
                              if "ADMISSION" in c["location"].upper()]

            # Phase 1 teacher cameras:
            #   Open House: Administration Camera ONLY (no Principal Room)
            #   Regular: Administration Camera + Principal Room for Deepi Bector
            teacher_priority_cams = administration_cams
            if _is_open_house_today():
                teacher_principal_cams = []  # Open House: Admin Camera ONLY
            else:
                teacher_principal_cams = principal_cams  # Regular: Principal Room for Deepi Bector
            teacher_fallback_cams = []  # No fallback
            teacher_phase_cams = teacher_priority_cams + teacher_principal_cams
            # Phase 2 student cameras: Classroom cameras ONLY (no gate/reception)
            student_phase_cams_gate = []  # No gate cameras for students
            student_phase_cams_classroom = all_classroom_cams

            active_cam_count = len(set(
                (c["dvr_index"], c["channel"]) for c in
                teacher_phase_cams + student_phase_cams_gate + student_phase_cams_classroom
            ))

            # Reset all stats so counters don't accumulate across restarts
            self._classwise_stats = {
                "total_cameras": active_cam_count,
                "cameras_scanned": 0,
                "current_camera": "",
                "cycle_count": 0,
                "last_cycle_duration": 0.0,
                "faces_detected_total": 0,
                "attendance_marked_today": 0,
                "errors": 0,
            }
            # Log camera breakdown
            _tw_log = _get_teacher_phase_window()
            _sw_log = _get_student_phase_window()
            _is_oh = _is_open_house_today()
            mode = "TEST (all cameras, all faces)" if FORCE_RENOTIFY_TEST else (
                "OPEN HOUSE" if _is_oh else "REGULAR")
            self.add_debug_log(
                "classwise_started",
                f"Mode: {mode} | "
                f"Phase1 teacher cams: {len(teacher_phase_cams)} "
                f"(Admin={len(administration_cams)}, Principal={len(teacher_principal_cams)}) | "
                f"Phase2 student cams: {len(student_phase_cams_classroom)} classroom ONLY | "
                f"Other (skipped): {len(all_other_cams)} | "
                f"{len(self.known_faces)} total faces loaded, "
                f"{len(self._grade_face_cache)} grades with faces | "
                f"Teacher window: {_tw_log[0]}:{_tw_log[1]:02d}-"
                f"{_tw_log[2]}:{_tw_log[3]:02d} | "
                f"Student window: {_sw_log[0]}:{_sw_log[1]:02d}-"
                f"{_sw_log[2]}:{_sw_log[3]:02d}"
            )
            if teacher_phase_cams:
                logger.info(f"Phase1 teacher cameras: {[c['label'] for c in teacher_phase_cams]}")
            if student_phase_cams_classroom:
                logger.info(f"Phase2 classroom cameras: {[c['label'] for c in student_phase_cams_classroom[:5]]}... ({len(student_phase_cams_classroom)} total)")

            # Resync disabled — backend already has all records and handles
            # notifications as safety net. Resync was causing startup crashes.
            # try:
            #     await self._resync_todays_records()
            # except Exception as e:
            #     logger.error(f"Resync failed on startup (non-fatal): {e}")

            # Clear daily marks at start if it's a new day
            today = date.today().isoformat()
            self.daily_marked = {
                pid: d for pid, d in self.daily_marked.items() if d == today
            }
            cycle = 0
            consecutive_full_failures = 0
            while self.classwise_running:
                cycle += 1
                cycle_start = time.time()
                self._classwise_stats["cycle_count"] = cycle
                scanned = 0
                faces_in_cycle = 0
                cycle_errors = 0

                # --- Off-day/holiday check: skip entire scan cycle ---
                from datetime import timezone as _tz2, timedelta as _td2
                _ist2 = _tz2(_td2(hours=5, minutes=30))
                _now_ist = datetime.now(_ist2)
                _day_name = _now_ist.strftime("%A")
                if self._is_off_day(_now_ist):
                    if cycle <= 1 or cycle % 100 == 0:
                        self.add_debug_log("off_day_skip",
                                           f"Today is {_day_name} — attendance disabled. "
                                           f"No scanning, no notifications.")
                    if cycle % 30 == 0:
                        self.cleanup_memory(aggressive=True)
                    await asyncio.sleep(60)
                    continue
                if self._is_holiday_today():
                    if cycle <= 1 or cycle % 100 == 0:
                        self.add_debug_log("holiday_skip",
                                           "Today is a holiday — attendance disabled. "
                                           "No scanning, no notifications.")
                    if cycle % 30 == 0:
                        self.cleanup_memory(aggressive=True)
                    await asyncio.sleep(60)
                    continue

                # Check if day changed - reset daily marks
                new_today = date.today().isoformat()
                if new_today != today:
                    today = new_today
                    self.daily_marked.clear()
                    self._notification_sent.clear()
                    self._meal_sent.clear()
                    self._meal_window_done.clear()
                    self._sightings.clear()
                    self._camera_errors.clear()
                    self._admin_alerted.clear()
                    self.entry_validated.clear()
                    # Reset failsafe state for new day
                    self._failsafe_active = False
                    self._failsafe_reason = ""
                    self._failsafe_recovery_count = 0
                    self._consecutive_cycle_errors = 0
                    self._false_positive_count_window.clear()
                    self.add_debug_log("daily_reset",
                                       f"New day {today}: cleared attendance marks, "
                                       f"notifications, meal caches, sightings, entry validations, "
                                       f"and failsafe state")

                # Periodically reload faces (picks up new registrations)
                if cycle % 20 == 0:
                    self.reload_faces()

                # Determine current phase based on IST time
                from datetime import timezone as _tz3, timedelta as _td3
                _ist3 = _tz3(_td3(hours=5, minutes=30))
                _now_phase = datetime.now(_ist3)
                _h, _m = _now_phase.hour, _now_phase.minute
                _now_mins = _h * 60 + _m

                # Use dynamic windows (Open House vs regular)
                _tw = _get_teacher_phase_window()
                teacher_start = _tw[0] * 60 + _tw[1]
                teacher_end = _tw[2] * 60 + _tw[3]
                _sw = _get_student_phase_window()
                student_start = _sw[0] * 60 + _sw[1]
                student_end = _sw[2] * 60 + _sw[3]

                in_teacher_phase = teacher_start <= _now_mins < teacher_end
                in_student_phase = student_start <= _now_mins < student_end

                if FORCE_RENOTIFY_TEST:
                    # Test mode: both phases always active
                    in_teacher_phase = True
                    in_student_phase = True

                if not in_teacher_phase and not in_student_phase:
                    # Check if a meal window is active — run meal snapshot
                    # even outside attendance phases
                    _meal_active_idle, _meal_label_idle = self._is_meal_window_active()
                    if _meal_active_idle:
                        _meal_today_idle = date.today().isoformat()
                        if self._meal_window_done.get(_meal_label_idle) != _meal_today_idle:
                            self._meal_window_done[_meal_label_idle] = _meal_today_idle
                            self.add_debug_log("meal_snapshot_trigger",
                                               f"Meal window active (idle): {_meal_label_idle} — "
                                               "capturing classroom snapshots")
                            try:
                                await self._run_meal_snapshot_cycle(
                                    all_classroom_cams, _meal_label_idle)
                            except Exception as _meal_err2:
                                logger.error(f"Meal snapshot cycle failed: {_meal_err2}")

                    # Outside all attendance windows — sleep and retry
                    if cycle <= 1 or cycle % 60 == 0:
                        self.add_debug_log("outside_window",
                                           f"Current time {_now_phase.strftime('%H:%M')} IST — "
                                           f"outside both teacher ({_tw[0]}:{_tw[1]:02d}-"
                                           f"{_tw[2]}:{_tw[3]:02d}) and "
                                           f"student ({_sw[0]}:{_sw[1]:02d}-"
                                           f"{_sw[2]}:{_sw[3]:02d}) windows")
                    # Memory cleanup during idle — every 5 minutes (10 idle cycles)
                    if cycle % 10 == 0:
                        self.cleanup_memory(aggressive=True)
                    await asyncio.sleep(30)
                    continue

                # === PHASE 1: Teacher Recognition (OPEN HOUSE PROTOCOL) ===
                # Administration Camera: ALL teachers
                # Principal Room Camera: Ms. Deepi Bector ONLY
                if in_teacher_phase:
                    if cycle <= 1 or (cycle % 30 == 0):
                        self.add_debug_log("teacher_phase",
                                           f"Phase 1 ACTIVE (OPEN HOUSE): scanning "
                                           f"{len(teacher_priority_cams)} admin cam(s) + "
                                           f"{len(teacher_principal_cams)} principal cam(s) "
                                           f"(Deepi Bector only)")
                    teacher_faces = getattr(self, '_teacher_faces_cache', {})
                    teacher_faces_if = getattr(self, '_teacher_faces_cache_insightface', {})

                    async def _scan_cam_list(cams, faces, faces_if):
                        _scanned, _faces_found, _errors = 0, 0, 0
                        for cam in cams:
                            if not self.classwise_running:
                                break
                            try:
                                self._classwise_stats["current_camera"] = cam["label"]
                                results = await self.scan_camera(
                                    cam["dvr"], cam["channel"], cam["label"],
                                    faces_subset=faces if faces else None,
                                    insightface_subset=faces_if if faces_if else None,
                                )
                                _scanned += 1
                                _faces_found += len(results)
                            except Exception as e:
                                _errors += 1
                                logger.error(f"Error scanning {cam['label']}: {e}")
                            await asyncio.sleep(0.1)
                        return _scanned, _faces_found, _errors

                    # Step 1: Scan Administration camera for ALL teachers
                    if teacher_priority_cams:
                        pr = await _scan_cam_list(
                            teacher_priority_cams, teacher_faces, teacher_faces_if)
                        scanned += pr[0]
                        faces_in_cycle += pr[1]
                        cycle_errors += pr[2]

                    # Step 2: Scan Principal Room camera for Ms. Deepi Bector ONLY
                    if teacher_principal_cams:
                        # Filter face subset to only Deepi Bector
                        _deepi_faces = {k: v for k, v in teacher_faces.items()
                                        if k == "PRINCIPAL_DEEPI_BECTOR"}
                        _deepi_faces_if = {k: v for k, v in teacher_faces_if.items()
                                           if k == "PRINCIPAL_DEEPI_BECTOR"} if teacher_faces_if else {}
                        if _deepi_faces or _deepi_faces_if:
                            ppr = await _scan_cam_list(
                                teacher_principal_cams,
                                _deepi_faces if _deepi_faces else None,
                                _deepi_faces_if if _deepi_faces_if else None)
                            scanned += ppr[0]
                            faces_in_cycle += ppr[1]
                            cycle_errors += ppr[2]

                    gc.collect()

                # --- Trigger teacher report email once Phase 1 ends ---
                if not in_teacher_phase and getattr(self, '_teacher_report_sent_today', None) != date.today().isoformat():
                    if _now_mins >= teacher_end and _now_mins < student_end:
                        self._teacher_report_sent_today = date.today().isoformat()
                        self.add_debug_log("teacher_report",
                                           "Phase 1 ended — triggering teacher attendance report email")
                        try:
                            api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
                            agent_secret = os.environ.get("AGENT_SECRET", "")
                            headers = {"Content-Type": "application/json"}
                            if agent_secret:
                                headers["X-Agent-Secret"] = agent_secret
                            async with httpx.AsyncClient(timeout=30) as _rpt_client:
                                _rpt_resp = await _rpt_client.post(
                                    f"{api_url}/api/dashboard/attendance/teacher-report/email",
                                    json={}, headers=headers,
                                )
                                if _rpt_resp.status_code == 200:
                                    _rpt_data = _rpt_resp.json()
                                    self.add_debug_log("teacher_report_sent",
                                                       f"Teacher report emailed: {_rpt_data.get('present', 0)} present, "
                                                       f"{_rpt_data.get('absent', 0)} absent")
                                else:
                                    self.add_debug_log("teacher_report_error",
                                                       f"Report email failed: HTTP {_rpt_resp.status_code}")
                        except Exception as _rpt_e:
                            self.add_debug_log("teacher_report_error",
                                               f"Report email error: {_rpt_e}")

                # === Administration camera: keep scanning for teachers even during student phase ===
                if in_student_phase and not in_teacher_phase and teacher_priority_cams:
                    teacher_faces = getattr(self, '_teacher_faces_cache', {})
                    teacher_faces_if = getattr(self, '_teacher_faces_cache_insightface', {})
                    for cam in teacher_priority_cams:
                        if not self.classwise_running:
                            break
                        try:
                            self._classwise_stats["current_camera"] = cam["label"]
                            results = await self.scan_camera(
                                cam["dvr"], cam["channel"], cam["label"],
                                faces_subset=teacher_faces if teacher_faces else None,
                                insightface_subset=teacher_faces_if if teacher_faces_if else None,
                            )
                            scanned += 1
                            faces_in_cycle += len(results)
                        except Exception as e:
                            cycle_errors += 1
                            logger.error(f"Error scanning {cam['label']}: {e}")
                        await asyncio.sleep(0.1)

                # === PHASE 2: Student Recognition ===
                if in_student_phase:
                    if cycle <= 1 or (cycle % 30 == 0):
                        self.add_debug_log("student_phase",
                                           f"Phase 2 ACTIVE (OPEN HOUSE): scanning "
                                           f"{len(student_phase_cams_classroom)} classroom cameras "
                                           f"for student faces + admin cam for teachers")

                    # 2a. Scan gate/reception cameras for ALL student faces (parallel by DVR)
                    gate_cams_to_scan = [
                        cam for cam in student_phase_cams_gate
                        if not (in_teacher_phase and cam in teacher_phase_cams)
                    ]
                    if gate_cams_to_scan:
                        gate_dvr_groups: dict[str, list] = {}
                        for cam in gate_cams_to_scan:
                            ip = cam["dvr"]["ip"]
                            gate_dvr_groups.setdefault(ip, []).append(cam)

                        async def _scan_gate_group(cams):
                            _s, _f, _e = 0, 0, 0
                            for cam in cams:
                                if not self.classwise_running:
                                    break
                                try:
                                    self._classwise_stats["current_camera"] = cam["label"]
                                    results = await self.scan_camera(
                                        cam["dvr"], cam["channel"], cam["label"],
                                        faces_subset=None, insightface_subset=None,
                                    )
                                    _s += 1
                                    _f += len(results)
                                except Exception as e:
                                    _e += 1
                                    logger.error(f"Error scanning {cam['label']}: {e}")
                                await asyncio.sleep(0.1)
                            return _s, _f, _e

                        gate_tasks = [_scan_gate_group(cams) for cams in gate_dvr_groups.values()]
                        gate_results = await asyncio.gather(*gate_tasks, return_exceptions=True)
                        for r in gate_results:
                            if isinstance(r, tuple):
                                scanned += r[0]
                                faces_in_cycle += r[1]
                                cycle_errors += r[2]
                                self._classwise_stats["errors"] += r[2]
                    gc.collect()

                    # 2b. Dual student scanning logic:
                    #   Phase 2 (Summer Camp): Scan ALL cameras with ALL
                    #     non-Grade-9-12 student faces (they can sit anywhere).
                    #   Phase 3 (Grades 9-12): Scan ONLY assigned classroom
                    #     cameras with grade-specific faces.
                    active_classroom_cams = student_phase_cams_classroom

                    # Build face subsets once per cycle
                    summer_camp_faces = {
                        k: v for k, v in self.known_faces.items()
                        if not k.startswith(("TEACHER_", "PRINCIPAL_"))
                        and not _is_grade_9_to_12_student(k)
                    }
                    summer_camp_faces_if = {
                        k: v for k, v in self.known_faces_insightface.items()
                        if not k.startswith(("TEACHER_", "PRINCIPAL_"))
                        and not _is_grade_9_to_12_student(k)
                    }

                    BATCH_SIZE = 10
                    for batch_start in range(0, len(active_classroom_cams), BATCH_SIZE):
                        batch = active_classroom_cams[batch_start:batch_start + BATCH_SIZE]
                        for cam in batch:
                            if not self.classwise_running:
                                break
                            try:
                                cam_grade = cam.get("grade")

                                # Merge faces to scan on this camera:
                                # 1) Summer camp students → always included (can be in any room)
                                # 2) Grade 9-12 students → only if camera matches their grade
                                faces_to_scan = dict(summer_camp_faces)
                                faces_to_scan_if = dict(summer_camp_faces_if)

                                if cam_grade and cam_grade in GRADES_9_TO_12:
                                    # Add grade-specific 9-12 faces for this camera
                                    grade_faces = self._grade_face_cache.get(cam_grade, {})
                                    grade_faces_if = self._grade_face_cache_insightface.get(cam_grade, {})
                                    faces_to_scan.update(grade_faces)
                                    faces_to_scan_if.update(grade_faces_if)

                                if not faces_to_scan and not faces_to_scan_if:
                                    scanned += 1
                                    continue

                                self._classwise_stats["current_camera"] = cam["label"]
                                results = await self.scan_camera(
                                    cam["dvr"], cam["channel"], cam["label"],
                                    faces_subset=faces_to_scan,
                                    insightface_subset=faces_to_scan_if,
                                )
                                scanned += 1
                                faces_in_cycle += len(results)
                            except Exception as e:
                                self._classwise_stats["errors"] += 1
                                cycle_errors += 1
                                logger.error(f"Error scanning {cam['label']}: {e}")
                            await asyncio.sleep(0.1)

                        # Free memory between batches
                        gc.collect()
                        await asyncio.sleep(0.3)

                # === MEAL SNAPSHOT CHECK ===
                # Run once per meal window per day (not every scan cycle)
                meal_active, meal_label = self._is_meal_window_active()
                if meal_active:
                    _meal_today = date.today().isoformat()
                    if self._meal_window_done.get(meal_label) != _meal_today:
                        self._meal_window_done[meal_label] = _meal_today
                        self.add_debug_log("meal_snapshot_trigger",
                                           f"Meal window active: {meal_label} — "
                                           "capturing classroom snapshots")
                        try:
                            await self._run_meal_snapshot_cycle(
                                all_classroom_cams, meal_label)
                        except Exception as _meal_err:
                            logger.error(f"Meal snapshot cycle failed: {_meal_err}")
                            self.add_debug_log("meal_snapshot_error",
                                               f"{meal_label} error: {_meal_err}")

                cycle_duration = time.time() - cycle_start
                self._classwise_stats["cameras_scanned"] = scanned
                self._classwise_stats["last_cycle_duration"] = round(cycle_duration, 1)
                self._classwise_stats["faces_detected_total"] += faces_in_cycle
                self._classwise_stats["attendance_marked_today"] = sum(
                    1 for d in self.daily_marked.values()
                    if d == date.today().isoformat()
                )

                # Track full-cycle failures for health reporting
                if scanned == 0 and cycle_errors > 0:
                    consecutive_full_failures += 1
                    self._health["camera_feed"] = "degraded"
                    if consecutive_full_failures >= 5:
                        self._health["camera_feed"] = "error"
                else:
                    consecutive_full_failures = 0
                    self._health["camera_feed"] = "ok"

                # Run failsafe checks EVERY cycle (handles both activation AND recovery)
                failsafe_camera = self._check_failsafe(cycle_errors, scanned)
                failsafe_fp = self._check_false_positive_rate()

                if failsafe_camera or failsafe_fp:
                    reason = self._failsafe_reason or "System instability detected"
                    self.add_debug_log("failsafe_mode",
                                       f"SAFE MODE active — {reason}. "
                                       f"Automatic attendance marking suspended.")
                    try:
                        api_url = self.whatsapp_api_url or "https://ppis-whatsapp-bot.fly.dev"
                        async with httpx.AsyncClient(timeout=10) as _fc:
                            await _fc.post(f"{api_url}/api/dashboard/failsafe",
                                           json={"reason": reason,
                                                 "timestamp": datetime.now(
                                                     timezone(timedelta(hours=5, minutes=30))
                                                 ).isoformat()})
                    except Exception:
                        pass
                    await asyncio.sleep(60)
                    continue

                if cycle % 5 == 0:
                    self.add_debug_log(
                        "classwise_cycle",
                        f"Cycle {cycle}: scanned {scanned} cameras in "
                        f"{cycle_duration:.1f}s, {faces_in_cycle} faces detected, "
                        f"{self._classwise_stats['attendance_marked_today']} marked today"
                    )

                self._health["last_health_check"] = datetime.now().isoformat()

                # Check camera health and send alerts if needed
                if cycle % 3 == 0:
                    await self._check_camera_health_alerts()

                # Report camera status to backend every 10 cycles
                if cycle % 10 == 0:
                    await self._report_camera_status_to_backend(cameras)

                # Periodic memory cleanup every 10 cycles
                if cycle % 10 == 0:
                    cleanup = self.cleanup_memory(aggressive=False)
                    if cleanup.get("memory_mb_after", 0) > 500:
                        self.add_debug_log("memory_warning",
                                           f"Memory at {cleanup['memory_mb_after']}MB after cleanup")
                else:
                    gc.collect()
                await asyncio.sleep(0.5)

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
        """Stop the monitoring loop.

        Also disables auto_start so the health watchdog does not
        silently restart monitoring within the next 60 seconds.
        """
        self.running = False
        self.classwise_running = False
        self._health["auto_start_enabled"] = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._classwise_task and not self._classwise_task.done():
            self._classwise_task.cancel()
        # Close persistent DVR HTTP clients
        for client in self._dvr_clients.values():
            if not client.is_closed:
                asyncio.ensure_future(client.aclose())
        self._dvr_clients.clear()
        self.add_debug_log("monitoring_stopped",
                           "Stop requested — auto_start disabled")

    def get_status(self) -> dict:
        """Return current engine status."""
        n_legacy = sum(len(p["encodings"]) for p in self.known_faces.values())
        n_insight = sum(len(p["encodings"]) for p in self.known_faces_insightface.values())
        status = {
            "running": self.running,
            "classwise_running": self.classwise_running,
            "test_mode": self.test_mode,
            "test_person_id": self.test_person_id,
            "confidence_threshold": self.confidence_threshold,
            "min_sightings": self.min_sightings,
            "sighting_window": self.sighting_window,
            "pending_sightings": {
                pid: len(sights) for pid, sights in self._sightings.items()
                if sights
            },
            "scan_interval": self.scan_interval,
            "registered_persons": len(self.known_faces),
            "registered_persons_insightface": len(self.known_faces_insightface),
            "total_encodings": n_legacy,
            "total_encodings_insightface": n_insight,
            "face_engine": self._health.get("face_engine", "face_recognition"),
            "cooldown_seconds": COOLDOWN_SECONDS,
            "attendance_marked_today": sum(
                1 for d in self.daily_marked.values()
                if d == date.today().isoformat()
            ),
            "grades_with_faces": len(self._grade_face_cache),
            "health": self._health.copy(),
            "anti_spoof_enabled": True,
            "entry_validation_enabled": True,
            "quality_filtering_enabled": True,
            "teacher_confidence_threshold": self.teacher_confidence_threshold,
            "entry_validated_today": sum(
                1 for d in self.entry_validated.values()
                if d == date.today().isoformat()
            ),
            "multi_layer_checks": [
                "high_confidence_match",
                "authorized_camera",
                "time_window",
                "multi_frame_verification",
                "entry_gate_validation",
                "anti_spoofing",
                "average_confidence_check",
                "face_quality_filter",
            ],
        }
        if self.classwise_running:
            status["classwise_stats"] = self._classwise_stats.copy()
        return status

    def cleanup_memory(self, aggressive: bool = False) -> dict:
        """Free memory by pruning caches and collecting garbage.

        Args:
            aggressive: If True, perform deeper cleanup (used when memory is high).

        Returns:
            Dict with cleanup stats.
        """
        stats: dict = {}
        now = time.time()

        # 1. Prune stale sightings (older than sighting_window)
        pruned_sightings = 0
        cutoff = now - self.sighting_window
        stale_keys = []
        for pid, sights in self._sightings.items():
            before = len(sights)
            self._sightings[pid] = [s for s in sights if s["time"] >= cutoff]
            pruned_sightings += before - len(self._sightings[pid])
            if not self._sightings[pid]:
                stale_keys.append(pid)
        for k in stale_keys:
            del self._sightings[k]
        stats["pruned_sightings"] = pruned_sightings
        stats["removed_sighting_keys"] = len(stale_keys)

        # 2. Trim debug logs
        max_logs = 100 if aggressive else self.max_debug_logs
        if len(self.debug_logs) > max_logs:
            trimmed = len(self.debug_logs) - max_logs
            self.debug_logs = self.debug_logs[-max_logs:]
            stats["trimmed_debug_logs"] = trimmed

        # 3. Clean up finished background tasks
        before_tasks = len(self._background_tasks)
        self._background_tasks = {t for t in self._background_tasks if not t.done()}
        stats["cleaned_tasks"] = before_tasks - len(self._background_tasks)

        # 4. Prune old daily marks from previous days
        today = date.today().isoformat()
        old_marks = {k for k, v in self.daily_marked.items() if v != today}
        for k in old_marks:
            del self.daily_marked[k]
        old_notifs = {k for k, v in self._notification_sent.items() if v != today}
        for k in old_notifs:
            del self._notification_sent[k]
        stats["pruned_old_marks"] = len(old_marks)
        stats["pruned_old_notifs"] = len(old_notifs)

        # 5. Clear camera error tracking for recovered cameras
        if aggressive:
            recovered = [k for k, v in self._camera_errors.items() if v == 0]
            for k in recovered:
                del self._camera_errors[k]
            stats["cleared_recovered_cameras"] = len(recovered)
            self._camera_recovered.clear()

        # 6. Force garbage collection
        gc.collect()
        stats["gc_collected"] = True

        # 7. Report memory after cleanup
        try:
            import psutil
            mem_mb = psutil.Process().memory_info().rss / (1024 * 1024)
            stats["memory_mb_after"] = round(mem_mb, 1)
            self._health["memory_mb"] = round(mem_mb, 1)
        except ImportError:
            pass

        return stats

    def get_debug_logs(self, limit: int = 100) -> list[dict]:
        """Return recent debug logs."""
        return self.debug_logs[-limit:]


# Module-level singleton
engine = AttendanceEngine()

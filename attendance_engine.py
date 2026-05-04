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
import gc
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

# Attendance time window (7:15 AM to 8:30 AM)
ATTENDANCE_START_HOUR = 7
ATTENDANCE_START_MINUTE = 15
ATTENDANCE_END_HOUR = 8
ATTENDANCE_END_MINUTE = 30

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


class AttendanceEngine:
    """Runs face recognition attendance monitoring."""

    def __init__(self):
        self.running = False
        self.classwise_running = False
        self.test_mode = True  # Only track test_person_id when True
        self.test_person_id = "TEST001"
        self.confidence_threshold = 0.30  # Match confidence > 30%
        self.known_faces: dict = {}
        self.known_faces_insightface: dict = {}  # person_id -> {name, phone, embeddings: [...]}
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
        self._last_dvrs: list[dict] = []
        self._last_camera_mapping: dict = {}

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
        """Build per-grade face lookup for classwise monitoring."""
        self._grade_face_cache.clear()
        for person_id, person_data in self.known_faces.items():
            grade = _grade_from_person_id(person_id)
            if grade:
                if grade not in self._grade_face_cache:
                    self._grade_face_cache[grade] = {}
                self._grade_face_cache[grade][person_id] = person_data

        self._grade_face_cache_insightface.clear()
        for person_id, person_data in self.known_faces_insightface.items():
            grade = _grade_from_person_id(person_id)
            if grade:
                if grade not in self._grade_face_cache_insightface:
                    self._grade_face_cache_insightface[grade] = {}
                self._grade_face_cache_insightface[grade][person_id] = person_data

        grades_with_faces = {g: len(v) for g, v in self._grade_face_cache.items()}
        logger.info(f"Grade face cache: {grades_with_faces}")

    def get_faces_for_grade(self, grade: str | None) -> dict:
        """Return known_faces filtered to a specific grade.

        If grade is None, returns all known faces (for entry gates etc).
        """
        if grade is None:
            return self.known_faces
        return self._grade_face_cache.get(grade, {})

    def get_insightface_for_grade(self, grade: str | None) -> dict:
        """Return InsightFace embeddings filtered to a specific grade."""
        if grade is None:
            return self.known_faces_insightface
        return self._grade_face_cache_insightface.get(grade, {})

    def _is_already_marked_today(self, person_id: str) -> bool:
        """Check if attendance already marked for this person today.

        Uses in-memory cache first for speed. On cache miss, falls back
        to the persistent database so dedup survives process restarts.
        """
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
        try:
            pil_img = Image.open(io.BytesIO(image_bytes))
            pil_img = pil_img.convert("RGB")
            # Save to temp file — dlib loads via its own C++ loader
            import tempfile as _tf
            _tmp = _tf.NamedTemporaryFile(suffix=".jpg", delete=False, dir=".")
            _tmp_path = _tmp.name
            pil_img.save(_tmp, format="JPEG", quality=95)
            _tmp.close()
            clean_buf = io.BytesIO()  # kept for del below
            # Use dlib's native loader to avoid numpy ABI issues
            import dlib as _dlib
            img_array = _dlib.load_rgb_image(_tmp_path)
            try:
                import os; os.unlink(_tmp_path)
            except Exception:
                pass
        except Exception as e:
            self.add_debug_log("error", f"Failed to load image: {e}")
            return []

        # Upsample 2x to detect smaller/distant faces from security cameras
        try:
            _detector = _dlib.get_frontal_face_detector()
            dlib_dets = _detector(img_array, 2)
            # Convert dlib rectangles to face_recognition format (top, right, bottom, left)
            face_locations = [
                (d.top(), d.right(), d.bottom(), d.left()) for d in dlib_dets
            ]
        except Exception as e:
            self.add_debug_log("error",
                               f"Legacy face detection failed for {camera_source}: {e}")
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
                                   f"(confidence: {confidence:.1%}) [InsightFace]",
                                   person_id=person_id,
                                   confidence=confidence)

                if confidence >= self.confidence_threshold:
                    result = self._process_attendance(
                        person_id=person_id,
                        name=match_result["name"],
                        phone=match_result["phone"],
                        confidence=confidence,
                        image_bytes=image_bytes,
                        face_location=(0, 0, 0, 0),
                        camera_source=camera_source,
                    )
                    if result:
                        results.append(result)
                else:
                    self.add_debug_log("low_confidence",
                                       f"Confidence {confidence:.1%} < "
                                       f"{self.confidence_threshold:.0%} threshold [InsightFace]",
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

    def _is_within_attendance_window(self) -> bool:
        """Check if the current time is within the attendance window."""
        now = datetime.now()
        start = now.replace(hour=ATTENDANCE_START_HOUR, minute=ATTENDANCE_START_MINUTE,
                            second=0, microsecond=0)
        end = now.replace(hour=ATTENDANCE_END_HOUR, minute=ATTENDANCE_END_MINUTE,
                          second=0, microsecond=0)
        return start <= now <= end

    def _is_notification_sent_today(self, person_id: str) -> bool:
        """Check if notification was already sent for this person today.

        Uses in-memory cache first for speed. On cache miss, falls back
        to the persistent database so dedup survives process restarts.
        """
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

    def _process_attendance(self, person_id: str, name: str, phone: str,
                            confidence: float, image_bytes: bytes,
                            face_location: tuple,
                            camera_source: str) -> dict | None:
        """Process an attendance detection: check time window, cooldown/daily dedup, log, and notify."""
        now = time.time()

        # Time window check: only mark attendance between 7:00 AM and 8:30 AM
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

        # Schedule async tasks — may be called from a thread pool,
        # so use run_coroutine_threadsafe instead of create_task
        try:
            loop = asyncio.get_running_loop()
            _schedule = lambda coro: asyncio.create_task(coro)
        except RuntimeError:
            # Running inside run_in_executor thread — no event loop here
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            _schedule = (
                lambda coro: asyncio.run_coroutine_threadsafe(coro, loop)
                if loop else None
            )

        # Sync attendance to cloud dashboard
        _schedule(self._sync_attendance_to_cloud(result, phone or ""))

        # Send WhatsApp notification ONCE per student per day
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
                    )
                )

        return result

    async def _send_whatsapp_notification(self, attendance_id: int,
                                           person_id: str,
                                           name: str, time_str: str,
                                           phone: str):
        """Send WhatsApp attendance notification via cloud bot API.

        Always uses the ppis_attendance_alert template for guaranteed
        delivery to BOTH parents — no 24-hour window dependency.
        Template messages are delivered regardless of whether the parent
        has ever messaged the bot.
        """
        api_url = self.whatsapp_api_url or "https://app-itszlsnn.fly.dev"
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {"Content-Type": "application/json"}
        if agent_secret:
            headers["X-Agent-Secret"] = agent_secret

        sent = False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Always use template — guaranteed delivery to any number
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
                else:
                    resp_text = ""
                    try:
                        resp_text = resp.text[:200]
                    except Exception:
                        pass
                    self.add_debug_log("whatsapp_failed",
                                       f"Failed for {phone}: {resp_text}")
        except Exception as e:
            self.add_debug_log("whatsapp_error",
                               f"Failed to send to {phone}: {type(e).__name__}: {e}")

    async def _sync_attendance_to_cloud(self, record: dict, parent_phones: str):
        """Report attendance record to cloud backend for dashboard display."""
        api_url = self.whatsapp_api_url or "https://app-itszlsnn.fly.dev"
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
                "logged_at": datetime.now().isoformat(),
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

    async def _send_camera_alert(self, cam_key: str, camera_label: str,
                                 error_count: int, alert_type: str = "offline"):
        """Send WhatsApp alert when a camera goes offline or recovers."""
        if not self._admin_phones:
            return
        api_url = self.whatsapp_api_url or "https://app-itszlsnn.fly.dev"
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
        # Request highest quality JPEG for face recognition
        url = (f"http://{ip}:{port}/ISAPI/Streaming/channels/{stream_channel}/picture"
               f"?snapShotImageType=JPEG&videoResolutionWidth=1920&videoResolutionHeight=1080")

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
                    # Non-exception failure: log and track like exceptions
                    cam_key = f"{ip}:{channel}"
                    self._camera_errors[cam_key] = self._camera_errors.get(cam_key, 0) + 1
                    ct = resp.headers.get("content-type", "unknown")
                    if attempt < max_retries - 1:
                        backoff = 2 ** attempt
                        self.add_debug_log(
                            "dvr_http_error",
                            f"{ip} ch{channel}: HTTP {resp.status_code} "
                            f"(content-type={ct}), retrying in {backoff}s")
                        await asyncio.sleep(backoff)
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
                    backoff = 2 ** attempt
                    await asyncio.sleep(backoff)
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

        # Also include entry gate cameras and special rooms (check ALL faces)
        _GATE_KEYWORDS = {"ENTRY", "ENTRANCE", "DISPERSAL", "ADMISSION", "RECEPTION"}
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
        try:
            self.classwise_running = True
            self._last_dvrs = dvrs
            self._last_camera_mapping = camera_mapping
            self.reload_faces()

            cameras = self.build_classroom_camera_list(camera_mapping, dvrs)
            classroom_cams = [c for c in cameras if c["grade"] is not None]
            gate_cams = [c for c in cameras if c["grade"] is None]

            # Reset all stats so counters don't accumulate across restarts
            self._classwise_stats = {
                "total_cameras": len(cameras),
                "cameras_scanned": 0,
                "current_camera": "",
                "cycle_count": 0,
                "last_cycle_duration": 0.0,
                "faces_detected_total": 0,
                "attendance_marked_today": 0,
                "errors": 0,
            }
            # Log gate camera details for debugging
            gate_labels = [c["label"] for c in gate_cams]
            self.add_debug_log(
                "classwise_started",
                f"Monitoring {len(classroom_cams)} classroom cameras + "
                f"{len(gate_cams)} entry gate cameras, "
                f"{len(self.known_faces)} total faces loaded, "
                f"{len(self._grade_face_cache)} grades with faces"
            )
            if gate_cams:
                logger.info(f"Gate/special cameras: {gate_labels}")

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
                            insightface_subset=None,
                        )
                        scanned += 1
                        faces_in_cycle += len(results)
                    except Exception as e:
                        self._classwise_stats["errors"] += 1
                        cycle_errors += 1
                        logger.error(f"Error scanning {cam['label']}: {e}")
                    await asyncio.sleep(0.5)

                # Free memory after gate cameras
                gc.collect()

                # Scan classroom cameras in batches to manage memory
                BATCH_SIZE = 10
                for batch_start in range(0, len(classroom_cams), BATCH_SIZE):
                    batch = classroom_cams[batch_start:batch_start + BATCH_SIZE]
                    for cam in batch:
                        if not self.classwise_running:
                            break
                        try:
                            grade = cam["grade"]
                            grade_faces = self.get_faces_for_grade(grade)
                            grade_faces_if = self.get_insightface_for_grade(grade)

                            if not grade_faces and not grade_faces_if:
                                scanned += 1
                                continue

                            self._classwise_stats["current_camera"] = cam["label"]
                            results = await self.scan_camera(
                                cam["dvr"], cam["channel"], cam["label"],
                                faces_subset=grade_faces,
                                insightface_subset=grade_faces_if,
                            )
                            scanned += 1
                            faces_in_cycle += len(results)
                        except Exception as e:
                            self._classwise_stats["errors"] += 1
                            cycle_errors += 1
                            logger.error(f"Error scanning {cam['label']}: {e}")
                        await asyncio.sleep(0.5)

                    # Free memory between batches
                    gc.collect()
                    await asyncio.sleep(1.0)

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

                # Check camera health and send alerts if needed
                if cycle % 3 == 0:
                    await self._check_camera_health_alerts()

                # Free memory between cycles and brief cooldown
                gc.collect()
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
        }
        if self.classwise_running:
            status["classwise_stats"] = self._classwise_stats.copy()
        return status

    def get_debug_logs(self, limit: int = 100) -> list[dict]:
        """Return recent debug logs."""
        return self.debug_logs[-limit:]


# Module-level singleton
engine = AttendanceEngine()

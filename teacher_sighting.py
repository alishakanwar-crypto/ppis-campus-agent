"""
Teacher Sighting & Visitor Tracker for Head Count Reconciliation.

Periodically captures frames from DVR cameras (Entry Gate 1 & 2,
Reception 1-4, Teacher Staff, Administration) and detects teacher
faces. Also counts visitors — any face detected on gate/reception
cameras that does not match a registered person (teacher, student,
staff) is counted as a visitor.

Sends sightings to the cloud backend for reconciliation
against TrueFace attendance records.

Does NOT mark attendance or send WhatsApp — that remains the
exclusive domain of TrueFace 3000 via the Selenium poller.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import time
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import httpx
import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import face_recognition
except ImportError:
    face_recognition = None

import face_db

logger = logging.getLogger("ppis-agent.sighting")

IST = timezone(timedelta(hours=5, minutes=30))

# Sighting window: 6:30 AM – 5:00 PM IST (covers full school day)
SIGHTING_START_HOUR = 6
SIGHTING_START_MIN = 30
SIGHTING_END_HOUR = 17
SIGHTING_END_MIN = 0

# Scan interval between sighting cycles (seconds)
SIGHTING_SCAN_INTERVAL = 60

# Cooldown: minimum seconds between sightings of the same teacher
# on the same camera
SIGHTING_COOLDOWN = 300  # 5 minutes

# Visitor cooldown: minimum seconds between counting a new visitor
# on the same camera (avoids re-counting the same unknown face)
VISITOR_COOLDOWN = 600  # 10 minutes

# Minimum face size (pixels) to consider for visitor alerts.
# 30px balances filtering car/gate false positives vs catching real distant faces.
MIN_VISITOR_FACE_SIZE = 30

# Maximum face distance for DVR teacher recognition.
# Lower = stricter (fewer false matches like misidentifying absent teachers).
# 0.45 distance ≈ 55% confidence minimum.
TEACHER_MATCH_DISTANCE = 0.45

# Cameras to monitor specifically for visitors (entry only — NOT dispersal/exit)
VISITOR_CAMERA_KEYWORDS = [
    "ENTRY", "ENTRANCE",  # Entry gates only (people entering)
    "RECEPTION",           # Reception cameras (indoor follow-up)
]

# Camera types to monitor for teacher sightings
SIGHTING_CAMERA_KEYWORDS = [
    "ENTRY", "ENTRANCE", "DISPERSAL",  # Entry gates
    "RECEPTION",                         # Reception cameras
    "TEACHER STAFF", "STAFF ROOM",       # Staff rooms
    "ADMINISTRATION", "ADMIN",           # Administration
    "ADMISSION",                         # Admission room
]


def _is_sighting_camera(location: str) -> bool:
    """Check if a camera location is relevant for teacher sighting."""
    loc_upper = location.upper()
    return any(kw in loc_upper for kw in SIGHTING_CAMERA_KEYWORDS)


def _is_visitor_camera(location: str) -> bool:
    """Check if a camera location is relevant for visitor counting."""
    loc_upper = location.upper()
    return any(kw in loc_upper for kw in VISITOR_CAMERA_KEYWORDS)


# Color name mapping from HSV ranges
_COLOR_RANGES = [
    ((0, 50, 50), (10, 255, 255), "red"),
    ((170, 50, 50), (180, 255, 255), "red"),
    ((11, 50, 50), (25, 255, 255), "orange"),
    ((26, 50, 50), (34, 255, 255), "yellow"),
    ((35, 50, 50), (85, 255, 255), "green"),
    ((86, 50, 50), (125, 255, 255), "blue"),
    ((126, 50, 50), (155, 255, 255), "purple"),
    ((156, 50, 50), (169, 255, 255), "pink"),
]


def _detect_outfit_color(bgr_image: np.ndarray, face_top: int, face_bottom: int,
                          face_left: int, face_right: int) -> dict:
    """Analyze the clothing region below a detected face to identify outfit color.

    Returns dict with dominant_color, colors list, and description.
    """
    if cv2 is None:
        return {"dominant_color": "unknown", "colors": [], "description": "unknown"}

    h, w = bgr_image.shape[:2]
    face_h = face_bottom - face_top
    face_w = face_right - face_left

    # Body region: below face, roughly 1.5x face height, wider than face
    body_top = min(face_bottom, h - 1)
    body_bottom = min(face_bottom + int(face_h * 1.5), h)
    body_left = max(0, face_left - int(face_w * 0.3))
    body_right = min(w, face_right + int(face_w * 0.3))

    if body_bottom - body_top < 10 or body_right - body_left < 10:
        return {"dominant_color": "unknown", "colors": [], "description": "unknown"}

    body_roi = bgr_image[body_top:body_bottom, body_left:body_right]
    hsv = cv2.cvtColor(body_roi, cv2.COLOR_BGR2HSV)
    total_pixels = hsv.shape[0] * hsv.shape[1]

    # Check for achromatic colors first (white, black, gray)
    gray_mask = hsv[:, :, 1] < 50  # low saturation
    bright = hsv[:, :, 2]
    black_pixels = int(np.sum(gray_mask & (bright < 60)))
    white_pixels = int(np.sum(gray_mask & (bright > 180)))
    gray_pixels = int(np.sum(gray_mask & (bright >= 60) & (bright <= 180)))

    color_counts: dict[str, int] = {}
    if black_pixels > 0:
        color_counts["black"] = black_pixels
    if white_pixels > 0:
        color_counts["white"] = white_pixels
    if gray_pixels > 0:
        color_counts["gray"] = gray_pixels

    # Check chromatic colors
    for lower, upper, color_name in _COLOR_RANGES:
        mask = cv2.inRange(hsv, np.array(lower), np.array(upper))
        count = int(cv2.countNonZero(mask))
        if count > 0:
            color_counts[color_name] = color_counts.get(color_name, 0) + count

    if not color_counts:
        return {"dominant_color": "unknown", "colors": [], "description": "unknown"}

    sorted_colors = sorted(color_counts.items(), key=lambda x: -x[1])
    dominant = sorted_colors[0][0]

    # Build color list with percentages (top 3)
    colors = []
    for name, count in sorted_colors[:3]:
        pct = round(count / total_pixels * 100, 1)
        if pct >= 5:
            colors.append({"color": name, "percentage": pct})

    # Build description
    if len(colors) == 1:
        description = colors[0]["color"]
    elif len(colors) >= 2:
        description = f"{colors[0]['color']} and {colors[1]['color']}"
    else:
        description = dominant

    return {
        "dominant_color": dominant,
        "colors": colors,
        "description": description,
    }


class TeacherSightingTracker:
    """Tracks teacher appearances on DVR cameras for head count reconciliation.
    Also counts visitors (unknown faces) on gate/reception cameras."""

    def __init__(self, cloud_url: str = "https://ppis-whatsapp-bot.fly.dev",
                 agent_secret: str = ""):
        self.cloud_url = cloud_url
        self.agent_secret = agent_secret
        self.running = False
        self._task: asyncio.Task | None = None
        self._dvr_clients: dict[str, httpx.AsyncClient] = {}
        self._teacher_encodings: dict[str, dict] = {}
        self._all_encodings: dict[str, dict] = {}  # all registered faces
        # Dedup: (person_id, camera_label) → last_sighting_timestamp
        self._last_sighting: dict[tuple[str, str], float] = {}
        # Visitor dedup: camera_label → list of (encoding, timestamp) for recent visitors
        self._recent_visitor_encodings: dict[str, list[tuple[np.ndarray, float]]] = {}
        # Daily sighting log for local tracking
        self._today: str = ""
        self._daily_sightings: list[dict] = []
        self._daily_visitor_sightings: list[dict] = []

    def load_teacher_faces(self):
        """Load face encodings for all teachers and all known persons."""
        all_faces = face_db.load_known_faces()
        self._teacher_encodings = {}
        self._all_encodings = all_faces  # keep all for visitor detection

        for pid, data in all_faces.items():
            if pid.startswith(("TEACHER_", "PRINCIPAL_")):
                self._teacher_encodings[pid] = data
        logger.info(f"[SIGHTING] Loaded {len(self._teacher_encodings)} teacher faces, "
                     f"{len(self._all_encodings)} total known faces")

    def _get_dvr_client(self, dvr: dict) -> httpx.AsyncClient:
        ip = dvr["ip"]
        if ip not in self._dvr_clients:
            self._dvr_clients[ip] = httpx.AsyncClient(
                timeout=15,
                auth=httpx.DigestAuth(dvr["username"], dvr["password"]),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
            )
        return self._dvr_clients[ip]

    async def _capture_frame(self, dvr: dict, channel: int) -> bytes | None:
        """Capture a single JPEG frame from a DVR camera via ISAPI."""
        ip = dvr["ip"]
        port = dvr.get("port", 80)
        client = self._get_dvr_client(dvr)
        stream_channel = channel * 100 + 1
        url = (f"http://{ip}:{port}/ISAPI/Streaming/channels/{stream_channel}/picture"
               f"?snapShotImageType=JPEG")
        try:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.headers.get(
                    "content-type", "").startswith("image"):
                return resp.content
        except Exception as e:
            logger.debug(f"[SIGHTING] Capture failed {ip} ch{channel}: {e}")
        return None

    def _decode_image(self, image_bytes: bytes):
        """Decode image bytes to RGB numpy array and detect face locations/encodings."""
        if face_recognition is None or cv2 is None:
            return None, [], []
        try:
            nparr = np.frombuffer(image_bytes, dtype=np.uint8)
            bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if bgr is None:
                return None, [], []
            img_array = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            img_array = np.ascontiguousarray(img_array, dtype=np.uint8)
        except Exception:
            return None, [], []

        try:
            # number_of_times_to_upsample=2 helps detect smaller/distant faces
            # on DVR cameras where subjects are far from the camera
            face_locations = face_recognition.face_locations(
                img_array, model="hog", number_of_times_to_upsample=2)
        except Exception:
            return img_array, [], []

        if not face_locations:
            return img_array, [], []

        try:
            face_encodings = face_recognition.face_encodings(img_array, face_locations)
        except Exception:
            return img_array, face_locations, []

        return img_array, face_locations, face_encodings

    def _detect_teachers(self, image_bytes: bytes) -> list[dict]:
        """Detect teacher faces in an image.

        Returns list of dicts with person_id, name, confidence, face_distance, outfit.
        """
        if not self._teacher_encodings:
            return []

        img_array, face_locations, face_encodings = self._decode_image(image_bytes)
        if not face_encodings:
            return []

        # Get BGR image for outfit detection
        bgr_img = None
        if cv2 is not None and img_array is not None:
            bgr_img = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)

        results = []
        for i, encoding in enumerate(face_encodings):
            best_match = None
            best_distance = 1.0

            for pid, data in self._teacher_encodings.items():
                known_encs = data.get("encodings", [])
                if not known_encs:
                    continue
                distances = face_recognition.face_distance(known_encs, encoding)
                min_dist = float(np.min(distances))
                if min_dist < best_distance:
                    best_distance = min_dist
                    best_match = pid

            if best_match and best_distance < TEACHER_MATCH_DISTANCE:
                data = self._teacher_encodings[best_match]
                result = {
                    "person_id": best_match,
                    "name": data.get("name", best_match),
                    "confidence": round(1.0 - best_distance, 3),
                    "face_distance": round(best_distance, 3),
                }

                # Detect outfit color from body region below face
                if bgr_img is not None and i < len(face_locations):
                    top, right, bottom, left = face_locations[i]
                    outfit = _detect_outfit_color(bgr_img, top, bottom, left, right)
                    result["outfit"] = outfit
                else:
                    result["outfit"] = {"dominant_color": "unknown", "colors": [], "description": "unknown"}

                results.append(result)

        return results

    def _detect_faces_with_visitors(self, image_bytes: bytes) -> tuple[list[dict], list[tuple[np.ndarray, str]]]:
        """Detect all faces in an image. Returns (known_teachers, unknown_faces).

        Matches each face against ALL registered persons (teachers, students, staff).
        Faces that don't match anyone are returned as (encoding, face_crop_b64) tuples.
        """
        if not self._all_encodings:
            return [], []

        img_array, face_locations, face_encodings = self._decode_image(image_bytes)
        if not face_encodings:
            return [], []

        # Get BGR image for outfit detection and face crops
        bgr_img = None
        if cv2 is not None and img_array is not None:
            bgr_img = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)

        teachers = []
        unknown_faces: list[tuple[np.ndarray, str]] = []

        for i, encoding in enumerate(face_encodings):
            best_match = None
            best_distance = 1.0

            # Check against ALL known faces (teachers + students + staff + chairman)
            for pid, data in self._all_encodings.items():
                known_encs = data.get("encodings", [])
                if not known_encs:
                    continue
                distances = face_recognition.face_distance(known_encs, encoding)
                min_dist = float(np.min(distances))
                if min_dist < best_distance:
                    best_distance = min_dist
                    best_match = pid

            if best_match and best_distance < TEACHER_MATCH_DISTANCE:
                # Known person — check if teacher
                if best_match.startswith(("TEACHER_", "PRINCIPAL_")):
                    data = self._teacher_encodings.get(best_match, self._all_encodings.get(best_match, {}))
                    result = {
                        "person_id": best_match,
                        "name": data.get("name", best_match),
                        "confidence": round(1.0 - best_distance, 3),
                        "face_distance": round(best_distance, 3),
                    }

                    # Detect outfit color from body region below face
                    if bgr_img is not None and i < len(face_locations):
                        top, right, bottom, left = face_locations[i]
                        outfit = _detect_outfit_color(bgr_img, top, bottom, left, right)
                        result["outfit"] = outfit
                    else:
                        result["outfit"] = {"dominant_color": "unknown", "colors": [], "description": "unknown"}

                    teachers.append(result)
                # else: known student/staff — skip (not a visitor)
            else:
                # Unknown face — send full frame with face highlighted
                # (face crops are too small on DVR cameras to be useful)
                crop_b64 = ""
                if bgr_img is not None and i < len(face_locations):
                    top, right, bottom, left = face_locations[i]
                    face_h = bottom - top
                    face_w = right - left
                    # Skip tiny faces — likely false positives (car, gate, etc.)
                    if face_h < MIN_VISITOR_FACE_SIZE or face_w < MIN_VISITOR_FACE_SIZE:
                        logger.info(f"[VISITOR] Skipping tiny face {face_w}x{face_h}px (min={MIN_VISITOR_FACE_SIZE})")
                        continue
                    # Draw rectangle around detected face on a copy
                    annotated = bgr_img.copy()
                    # Expand box to show head + upper body
                    pad_top = int(face_h * 0.5)
                    pad_bottom = int(face_h * 3.0)
                    pad_lr = int(face_w * 1.5)
                    h, w = annotated.shape[:2]
                    box_y1 = max(0, top - pad_top)
                    box_y2 = min(h, bottom + pad_bottom)
                    box_x1 = max(0, left - pad_lr)
                    box_x2 = min(w, right + pad_lr)
                    cv2.rectangle(annotated, (box_x1, box_y1), (box_x2, box_y2),
                                  (0, 0, 255), 3)
                    cv2.putText(annotated, "UNKNOWN", (box_x1, box_y1 - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
                    _, buf = cv2.imencode(".jpg", annotated,
                                          [cv2.IMWRITE_JPEG_QUALITY, 85])
                    crop_b64 = base64.b64encode(buf).decode()
                unknown_faces.append((encoding, crop_b64))

        return teachers, unknown_faces

    def _label_frame(self, frame_bytes: bytes) -> str | None:
        """Add a simple UNKNOWN PERSON DETECTED label to a camera frame.

        Returns base64 JPEG or None.
        """
        if cv2 is None:
            return None

        nparr = np.frombuffer(frame_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if bgr is None:
            return None

        cv2.putText(bgr, "UNKNOWN PERSON DETECTED", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)

        _, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buf).decode()

    def _is_new_visitor(self, encoding: np.ndarray, cam_label: str, now_ts: float) -> bool:
        """Check if a visitor encoding is distinct from recently seen visitors on this camera."""
        recent = self._recent_visitor_encodings.get(cam_label, [])
        # Prune expired entries
        recent = [(enc, ts) for enc, ts in recent if now_ts - ts < VISITOR_COOLDOWN]
        self._recent_visitor_encodings[cam_label] = recent

        if not recent:
            return True

        # Check if this face is similar to any recently seen visitor
        recent_encs = [enc for enc, _ in recent]
        distances = face_recognition.face_distance(recent_encs, encoding)
        if len(distances) > 0 and float(np.min(distances)) < 0.45:
            return False  # Same visitor seen recently
        return True

    async def _post_sightings(self, sightings: list[dict]):
        """Send teacher sightings to the cloud backend."""
        if not sightings:
            return

        headers = {"Content-Type": "application/json"}
        if self.agent_secret:
            headers["X-Agent-Secret"] = self.agent_secret

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.cloud_url}/api/gate/teacher-sighting",
                    json=sightings,
                    headers=headers,
                )
                if resp.status_code == 200:
                    logger.info(f"[SIGHTING] Posted {len(sightings)} teacher sighting(s)")
                else:
                    logger.warning(f"[SIGHTING] POST failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"[SIGHTING] POST failed: {e}")

    async def _post_visitor_sightings(self, visitors: list[dict]):
        """Send visitor sightings to the cloud backend."""
        if not visitors:
            return

        headers = {"Content-Type": "application/json"}
        if self.agent_secret:
            headers["X-Agent-Secret"] = self.agent_secret

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.cloud_url}/api/gate/visitor-sighting",
                    json=visitors,
                    headers=headers,
                )
                if resp.status_code == 200:
                    logger.info(f"[VISITOR] Posted {len(visitors)} visitor sighting(s)")
                else:
                    logger.warning(f"[VISITOR] POST failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"[VISITOR] POST failed: {e}")

    def _is_in_sighting_window(self) -> bool:
        now = datetime.now(IST)
        cur_mins = now.hour * 60 + now.minute
        start_mins = SIGHTING_START_HOUR * 60 + SIGHTING_START_MIN
        end_mins = SIGHTING_END_HOUR * 60 + SIGHTING_END_MIN
        return start_mins <= cur_mins < end_mins

    def _is_weekday(self) -> bool:
        return datetime.now(IST).weekday() < 5

    async def scan_cameras_for_sightings(self, dvrs: list[dict],
                                          camera_mapping: dict) -> tuple[list[dict], list[dict]]:
        """Scan relevant cameras and detect teachers + visitors.
        Returns (new_teacher_sightings, new_visitor_sightings)."""
        if not self._teacher_encodings:
            return [], []

        now = datetime.now(IST)
        now_ts = time.time()
        today = now.strftime("%Y-%m-%d")

        # Reset daily state if day changed
        if today != self._today:
            self._today = today
            self._last_sighting.clear()
            self._daily_sightings.clear()
            self._daily_visitor_sightings.clear()
            self._recent_visitor_encodings.clear()

        new_sightings = []
        new_visitors = []
        _pending_visitors = []

        # Diagnostic: count cameras scanned, frames captured, faces found
        cams_eligible = 0
        cams_scanned = 0
        frames_captured = 0
        total_faces_found = 0

        for location, cam_data in camera_mapping.items():
            is_teacher_cam = _is_sighting_camera(location)
            is_visitor_cam = _is_visitor_camera(location)
            if not is_teacher_cam and not is_visitor_cam:
                continue

            all_cams = cam_data.get("all_cameras", [])
            cams_to_scan = all_cams if all_cams else [cam_data]
            cams_eligible += len(cams_to_scan)

            for cam in cams_to_scan:
                dvr_idx = cam.get("dvr_index", 0)
                channel = cam.get("channel", 1)
                if dvr_idx >= len(dvrs):
                    continue

                cams_scanned += 1
                dvr = dvrs[dvr_idx]
                frame = await self._capture_frame(dvr, channel)
                if frame is None:
                    continue
                frames_captured += 1

                cam_label = f"{location} (DVR {dvr_idx + 1} Ch {channel})"

                if is_visitor_cam:
                    # On visitor-eligible cameras: detect teachers + visitors
                    teachers, unknown_encs = self._detect_faces_with_visitors(frame)
                else:
                    # On teacher-only cameras (staff rooms, admin): only detect teachers
                    teachers = self._detect_teachers(frame)
                    unknown_encs = []

                n_faces = len(teachers) + len(unknown_encs)
                total_faces_found += n_faces
                if n_faces > 0:
                    logger.info(f"[SIGHTING] {cam_label}: {len(teachers)} known, "
                                f"{len(unknown_encs)} unknown face(s)")

                # Process teacher detections
                for det in teachers:
                    pid = det["person_id"]
                    key = (pid, cam_label)

                    last = self._last_sighting.get(key, 0)
                    if now_ts - last < SIGHTING_COOLDOWN:
                        continue
                    self._last_sighting[key] = now_ts

                    outfit = det.get("outfit", {})
                    sighting = {
                        "person_id": pid,
                        "name": det["name"],
                        "camera": cam_label,
                        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "date": today,
                        "confidence": det["confidence"],
                        "outfit_color": outfit.get("dominant_color", "unknown"),
                        "outfit_colors": outfit.get("colors", []),
                        "outfit_description": outfit.get("description", "unknown"),
                    }
                    new_sightings.append(sighting)
                    self._daily_sightings.append(sighting)

                    logger.info(
                        f"[SIGHTING] {det['name']} on {cam_label} "
                        f"(conf={det['confidence']:.2f}, "
                        f"outfit={outfit.get('description', 'unknown')})")

                # Process visitor (unknown) detections — collect for delayed recapture
                for enc, crop_b64 in unknown_encs:
                    if not self._is_new_visitor(enc, cam_label, now_ts):
                        continue
                    self._recent_visitor_encodings.setdefault(cam_label, []).append(
                        (enc, now_ts))

                    # Store DVR info + face encoding for delayed recapture
                    _pending_visitors.append({
                        "cam_label": cam_label,
                        "dvr": dvr,
                        "channel": channel,
                        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "date": today,
                        "initial_snapshot": crop_b64,
                        "encoding": enc,
                    })
                    logger.info(f"[VISITOR] Unknown face on {cam_label} — queued for delayed recapture")

        logger.info(f"[SIGHTING] Scan complete: {cams_eligible} eligible, "
                     f"{cams_scanned} scanned, {frames_captured} frames, "
                     f"{total_faces_found} faces, {len(new_sightings)} teachers, "
                     f"{len(_pending_visitors)} visitors")

        # Delayed recapture: wait for person to walk further inside, then
        # pull a fresh snapshot from the same camera + Reception cameras
        if _pending_visitors:
            delay = 15
            logger.info(f"[VISITOR] Waiting {delay}s for {len(_pending_visitors)} "
                        f"visitor(s) to walk inside before recapture...")
            await asyncio.sleep(delay)

            # Build list of Reception cameras for follow-up captures
            reception_cams = []
            for location, cam_data in camera_mapping.items():
                if "RECEPTION" in location.upper():
                    all_cams = cam_data.get("all_cameras", [])
                    for cam in (all_cams if all_cams else [cam_data]):
                        dvr_idx = cam.get("dvr_index", 0)
                        ch = cam.get("channel", 1)
                        if dvr_idx < len(dvrs):
                            reception_cams.append((location, dvrs[dvr_idx], ch))

            for pv in _pending_visitors:
                dvr = pv["dvr"]
                channel = pv["channel"]
                cam_label = pv["cam_label"]
                snapshot = pv["initial_snapshot"]
                best_source = "initial"

                # Try delayed recapture from same camera
                fresh_frame = await self._capture_frame(dvr, channel)
                if fresh_frame is not None:
                    labelled = self._label_frame(fresh_frame)
                    if labelled is not None:
                        snapshot = labelled
                        best_source = "delayed-gate"

                # Also capture from Reception cameras for a closer indoor shot
                reception_snapshots = []
                for rec_loc, rec_dvr, rec_ch in reception_cams:
                    rec_frame = await self._capture_frame(rec_dvr, rec_ch)
                    if rec_frame is not None:
                        nparr = np.frombuffer(rec_frame, dtype=np.uint8)
                        bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if bgr is not None:
                            _, buf = cv2.imencode(".jpg", bgr,
                                                  [cv2.IMWRITE_JPEG_QUALITY, 85])
                            reception_snapshots.append((rec_loc, base64.b64encode(buf).decode()))

                # Pick first Reception snapshot that captured successfully
                if reception_snapshots:
                    rec_loc, rec_snap = reception_snapshots[0]
                    logger.info(f"[VISITOR] Reception follow-up from {rec_loc}")
                    # Send both: gate snapshot as primary, reception as extra
                    visitor_reception = {
                        "camera": f"{rec_loc} (follow-up)",
                        "timestamp": pv["timestamp"],
                        "date": pv["date"],
                        "snapshot": rec_snap,
                    }
                    new_visitors.append(visitor_reception)
                    self._daily_visitor_sightings.append(visitor_reception)

                logger.info(f"[VISITOR] Recapture: {cam_label} (source={best_source}, "
                            f"reception_followups={len(reception_snapshots)})")

                visitor = {
                    "camera": cam_label,
                    "timestamp": pv["timestamp"],
                    "date": pv["date"],
                    "snapshot": snapshot,
                }
                new_visitors.append(visitor)
                self._daily_visitor_sightings.append(visitor)

        return new_sightings, new_visitors

    async def sighting_monitoring_loop(self, dvrs: list[dict],
                                        camera_mapping: dict):
        """Background loop: periodically scan cameras for teacher sightings."""
        self.running = True
        self.load_teacher_faces()

        # Count eligible cameras for diagnostics
        n_teacher_cams = sum(1 for loc in camera_mapping if _is_sighting_camera(loc))
        n_visitor_cams = sum(1 for loc in camera_mapping if _is_visitor_camera(loc))
        visitor_cam_names = [loc for loc in camera_mapping if _is_visitor_camera(loc)]

        logger.info(f"[SIGHTING] Started. Window: {SIGHTING_START_HOUR}:{SIGHTING_START_MIN:02d}"
                     f"-{SIGHTING_END_HOUR}:{SIGHTING_END_MIN:02d} IST. "
                     f"Teachers: {len(self._teacher_encodings)}, "
                     f"All faces: {len(self._all_encodings)}, "
                     f"DVRs: {len(dvrs)}, "
                     f"Teacher cams: {n_teacher_cams}, Visitor cams: {n_visitor_cams}")
        logger.info(f"[SIGHTING] Visitor-eligible cameras: {visitor_cam_names}")

        cycle = 0
        while self.running:
            cycle += 1
            try:
                if not self._is_weekday():
                    if cycle % 120 == 1:
                        logger.info("[SIGHTING] Weekend — sighting paused")
                    await asyncio.sleep(60)
                    continue

                if not self._is_in_sighting_window():
                    if cycle % 120 == 1:
                        now = datetime.now(IST)
                        logger.info(f"[SIGHTING] Outside window ({now.strftime('%H:%M')} IST)")
                    await asyncio.sleep(30)
                    continue

                sightings, visitors = await self.scan_cameras_for_sightings(dvrs, camera_mapping)
                if sightings:
                    await self._post_sightings(sightings)
                if visitors:
                    await self._post_visitor_sightings(visitors)

                # Reload faces periodically
                if cycle % 30 == 0:
                    self.load_teacher_faces()

            except Exception as e:
                logger.error(f"[SIGHTING] Scan cycle error: {e}")

            await asyncio.sleep(SIGHTING_SCAN_INTERVAL)

        logger.info("[SIGHTING] Stopped")

    def start(self, dvrs: list[dict], camera_mapping: dict):
        """Start sighting monitoring as a background task."""
        if self._task and not self._task.done():
            logger.info("[SIGHTING] Already running")
            return
        self._task = asyncio.create_task(
            self.sighting_monitoring_loop(dvrs, camera_mapping))
        logger.info("[SIGHTING] Background task created")

    def stop(self):
        """Stop sighting monitoring."""
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[SIGHTING] Stopped")

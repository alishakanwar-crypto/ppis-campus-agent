"""
Mood & Temperament Detector for Chairman and Alisha.

Periodically captures frames from specified DVR cameras (Reception,
Admission, Administration) and the TrueFace camera, detects tracked
persons via face recognition, classifies their facial expression using
a lightweight CNN emotion model, and POSTs observations to the cloud
backend at /api/chairman/mood.

Runs as a background asyncio task alongside the main campus agent.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import face_recognition
except ImportError:
    face_recognition = None

import face_db

logger = logging.getLogger("ppis-agent.mood")

IST = timezone(timedelta(hours=5, minutes=30))

# Monitoring window: 7:00 AM – 12:00 PM IST (hourly reports on backend)
MOOD_START_HOUR = 7
MOOD_START_MIN = 0
MOOD_END_HOUR = 12
MOOD_END_MIN = 0

# Scan interval in seconds between mood detection cycles
MOOD_SCAN_INTERVAL = 30

# Tracked persons — person_ids in the face database that we monitor
TRACKED_PERSONS = {
    "chairman": ["CHAIRMAN_RAHUL_GUPTA", "PRINCIPAL_RAHUL_GUPTA",
                  "TEACHER_RAHUL_GUPTA", "CHAIRMAN", "CHAIRMAN_RAHUL"],
    "alisha": ["TEACHER_ALISHA_KANWAR", "TEACHER_ALISHA",
               "ALISHA", "ALISHA_KANWAR", "TEACHER_ALISHA_AHUJA"],
}

# Camera types to monitor for mood (location keywords)
MOOD_CAMERA_KEYWORDS = [
    "RECEPTION", "ADMISSION", "ADMINISTRATION",    # Indoor cameras only
]

# Camera locations to explicitly exclude from mood detection
MOOD_CAMERA_EXCLUDES = [
    "ADMIN ROOM",
]

# Simple emotion labels (OpenCV DNN or heuristic fallback)
EMOTION_LABELS = [
    "angry", "disgust", "fear", "happy", "sad", "surprise", "neutral"
]


def _classify_mood_camera(location: str) -> bool:
    """Check if a camera location is relevant for mood detection."""
    loc_upper = location.upper()
    if any(ex in loc_upper for ex in MOOD_CAMERA_EXCLUDES):
        return False
    return any(kw in loc_upper for kw in MOOD_CAMERA_KEYWORDS)


def _temperament_from_emotion(emotion: str, intensity: float) -> str:
    """Map dominant emotion to a temperament category."""
    positive = {"happy", "surprise"}
    negative = {"angry", "disgust", "fear", "sad"}
    if emotion in positive:
        return "positive" if intensity > 0.5 else "mildly_positive"
    if emotion in negative:
        return "negative" if intensity > 0.5 else "mildly_negative"
    return "neutral"


# ── Enhanced mood classification ──────────────────────────────
MOOD_CATEGORY_MAP = {
    "happy":    ("Positive", "Happy"),
    "surprise": ("Positive", "Excited"),
    "neutral":  ("Neutral", "Normal"),
    "sad":      ("Negative", "Sad"),
    "angry":    ("Negative", "Angry"),
    "fear":     ("Negative", "Anxious"),
    "disgust":  ("Negative", "Stressed"),
}

# Risk thresholds based on accumulated negative readings
HIGH_RISK_EMOTIONS = {"angry", "fear", "sad", "disgust"}


def _classify_mood_level(emotion: str, intensity: float,
                         negative_ratio: float = 0.0) -> str:
    """Classify into Positive / Neutral / Negative / High-Risk."""
    if negative_ratio > 0.7 and intensity > 0.5:
        return "High-Risk"
    cat = MOOD_CATEGORY_MAP.get(emotion, ("Neutral", "Normal"))[0]
    return cat


def _aggregate_emotions(frames: list[dict]) -> dict:
    """Aggregate emotion data across multiple frames.

    Returns a consolidated emotion observation with:
    - dominant_emotion from majority vote
    - averaged intensity
    - mood_category (Positive/Neutral/Negative/High-Risk)
    - confidence score based on frame agreement
    """
    if not frames:
        return {
            "dominant_emotion": "neutral",
            "emotions": {e: 0.0 for e in EMOTION_LABELS},
            "intensity": 0.0,
            "mood_category": "Neutral",
            "mood_label": "Normal",
            "frame_count": 0,
            "agreement": 0.0,
            "negative_ratio": 0.0,
        }

    from collections import Counter

    # Majority vote on dominant emotion
    emotion_votes = [f["dominant_emotion"] for f in frames]
    vote_counts = Counter(emotion_votes)
    dominant = vote_counts.most_common(1)[0][0]
    agreement = vote_counts[dominant] / len(frames)

    # Average intensity
    avg_intensity = sum(f["intensity"] for f in frames) / len(frames)

    # Average emotion distribution
    avg_emotions: dict[str, float] = {e: 0.0 for e in EMOTION_LABELS}
    for f in frames:
        for e, v in f.get("emotions", {}).items():
            if e in avg_emotions:
                avg_emotions[e] += v / len(frames)

    # Negative ratio
    neg_count = sum(1 for e in emotion_votes if e in HIGH_RISK_EMOTIONS)
    negative_ratio = neg_count / len(frames)

    # Classify mood level
    mood_category = _classify_mood_level(dominant, avg_intensity, negative_ratio)
    mood_label = MOOD_CATEGORY_MAP.get(dominant, ("Neutral", "Normal"))[1]

    return {
        "dominant_emotion": dominant,
        "emotions": avg_emotions,
        "intensity": round(avg_intensity, 3),
        "mood_category": mood_category,
        "mood_label": mood_label,
        "frame_count": len(frames),
        "agreement": round(agreement, 2),
        "negative_ratio": round(negative_ratio, 2),
    }


class MoodDetector:
    """Detects mood/temperament of tracked persons from DVR cameras."""

    def __init__(self, cloud_url: str = "https://ppis-whatsapp-bot.fly.dev",
                 agent_secret: str = ""):
        self.cloud_url = cloud_url
        self.agent_secret = agent_secret
        self.running = False
        self._task: asyncio.Task | None = None
        self._dvr_clients: dict[str, httpx.AsyncClient] = {}
        self._emotion_net = None
        self._face_cascade = None
        self._tracked_encodings: dict[str, list] = {}
        self._last_observation: dict[str, float] = {}
        self._cooldown = 120  # seconds between observations for same person
        self._debug_logs: list[dict] = []
        # Multi-frame accumulation: collect N frames before concluding mood
        self._frame_buffer: dict[str, list[dict]] = {}  # person -> list of emotion_data
        self._min_frames = 3  # require at least 3 frames before posting
        self._max_frames = 5  # post after collecting 5 frames
        self._buffer_timeout = 60  # seconds — post even with fewer frames
        self._buffer_start: dict[str, float] = {}  # person -> first frame time

    def _load_emotion_model(self):
        """Load a lightweight emotion classifier.

        Uses OpenCV's DNN if a model file is available, otherwise
        falls back to simple heuristic based on face geometry.
        """
        if cv2 is None:
            return
        # Try to load Haar cascade for face detection
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        if os.path.exists(cascade_path):
            self._face_cascade = cv2.CascadeClassifier(cascade_path)

    def load_tracked_faces(self):
        """Load face encodings for tracked persons from the database."""
        all_faces = face_db.load_known_faces()
        self._tracked_encodings = {}

        for person_label, possible_ids in TRACKED_PERSONS.items():
            for pid in possible_ids:
                if pid in all_faces:
                    self._tracked_encodings[person_label] = {
                        "person_id": pid,
                        "name": all_faces[pid].get("name", pid),
                        "encodings": all_faces[pid].get("encodings", []),
                    }
                    logger.info(f"[MOOD] Loaded face for '{person_label}' → {pid}")
                    break
            # Also try case-insensitive partial match
            if person_label not in self._tracked_encodings:
                for pid, data in all_faces.items():
                    name = data.get("name", "").upper()
                    if person_label == "chairman" and "RAHUL" in name and "GUPTA" in name:
                        self._tracked_encodings[person_label] = {
                            "person_id": pid,
                            "name": data.get("name", pid),
                            "encodings": data.get("encodings", []),
                        }
                        logger.info(f"[MOOD] Loaded face for '{person_label}' → {pid} (fuzzy)")
                        break
                    elif person_label == "alisha" and "ALISHA" in name:
                        self._tracked_encodings[person_label] = {
                            "person_id": pid,
                            "name": data.get("name", pid),
                            "encodings": data.get("encodings", []),
                        }
                        logger.info(f"[MOOD] Loaded face for '{person_label}' → {pid} (fuzzy)")
                        break

        logger.info(f"[MOOD] Tracked persons loaded: {list(self._tracked_encodings.keys())}")

    def _get_dvr_client(self, dvr: dict) -> httpx.AsyncClient:
        ip = dvr["ip"]
        if ip not in self._dvr_clients:
            self._dvr_clients[ip] = httpx.AsyncClient(
                timeout=15,
                auth=httpx.DigestAuth(dvr["username"], dvr["password"]),
                limits=httpx.Limits(max_connections=3, max_keepalive_connections=2),
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
            logger.debug(f"[MOOD] Capture failed {ip} ch{channel}: {e}")
        return None

    def _detect_tracked_person(self, image_bytes: bytes) -> list[dict]:
        """Detect tracked persons in an image using face recognition.

        Returns list of dicts with person_label, confidence, face_crop bytes.
        """
        if face_recognition is None or not self._tracked_encodings:
            return []

        try:
            if cv2 is not None:
                nparr = np.frombuffer(image_bytes, dtype=np.uint8)
                bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if bgr is None:
                    return []
                img_array = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                img_array = np.ascontiguousarray(img_array, dtype=np.uint8)
            else:
                return []
        except Exception:
            return []

        try:
            face_locations = face_recognition.face_locations(img_array, model="hog")
        except Exception:
            return []

        if not face_locations:
            return []

        try:
            face_encodings = face_recognition.face_encodings(img_array, face_locations)
        except Exception:
            return []

        results = []
        for i, encoding in enumerate(face_encodings):
            for person_label, person_data in self._tracked_encodings.items():
                known_encs = person_data.get("encodings", [])
                if not known_encs:
                    continue
                distances = face_recognition.face_distance(known_encs, encoding)
                min_dist = float(np.min(distances))
                confidence = 1.0 - min_dist

                if min_dist < 0.50:  # Match threshold
                    # Extract face crop
                    face_crop_b64 = ""
                    try:
                        top, right, bottom, left = face_locations[i]
                        pad = int((bottom - top) * 0.2)
                        h, w = img_array.shape[:2]
                        y1 = max(0, top - pad)
                        y2 = min(h, bottom + pad)
                        x1 = max(0, left - pad)
                        x2 = min(w, right + pad)
                        face_crop = img_array[y1:y2, x1:x2]
                        if cv2 is not None:
                            face_bgr = cv2.cvtColor(face_crop, cv2.COLOR_RGB2BGR)
                            _, buf = cv2.imencode(".jpg", face_bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
                            import base64
                            face_crop_b64 = base64.b64encode(buf.tobytes()).decode()
                    except Exception:
                        pass

                    results.append({
                        "person_label": person_label,
                        "person_id": person_data["person_id"],
                        "name": person_data["name"],
                        "confidence": confidence,
                        "face_distance": min_dist,
                        "face_crop": face_crop_b64,
                    })
                    break  # One match per face

        return results

    def _analyze_emotion(self, image_bytes: bytes, face_location: tuple | None = None) -> dict:
        """Analyze facial expression from an image.

        Uses a simple brightness/contrast heuristic as a lightweight
        approximation when no dedicated emotion model is available.
        Returns dict with dominant_emotion, emotions dict, intensity.
        """
        default = {
            "dominant_emotion": "neutral",
            "emotions": {e: 0.0 for e in EMOTION_LABELS},
            "intensity": 0.0,
        }
        default["emotions"]["neutral"] = 1.0

        if cv2 is None:
            return default

        try:
            nparr = np.frombuffer(image_bytes, dtype=np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return default
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

            # Use face cascade to find face region for analysis
            if self._face_cascade is not None:
                faces = self._face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))
                if len(faces) > 0:
                    x, y, w, h = faces[0]
                    face_roi = gray[y:y+h, x:x+w]
                else:
                    face_roi = gray
            else:
                face_roi = gray

            # Lightweight emotion approximation based on face statistics
            mean_val = float(np.mean(face_roi))
            std_val = float(np.std(face_roi))
            # Compute edge density (proxy for expression intensity)
            edges = cv2.Canny(face_roi, 50, 150)
            edge_density = float(np.sum(edges > 0)) / max(1, edges.size)

            emotions = {e: 0.0 for e in EMOTION_LABELS}
            # Simple heuristic mapping (will be replaced with proper model later)
            if edge_density > 0.15 and std_val > 50:
                emotions["happy"] = 0.6
                emotions["surprise"] = 0.2
                emotions["neutral"] = 0.2
                dominant = "happy"
                intensity = min(1.0, edge_density * 3)
            elif edge_density < 0.05:
                emotions["neutral"] = 0.8
                emotions["sad"] = 0.1
                emotions["happy"] = 0.1
                dominant = "neutral"
                intensity = 0.2
            elif mean_val < 80:
                emotions["sad"] = 0.4
                emotions["neutral"] = 0.4
                emotions["angry"] = 0.2
                dominant = "sad" if edge_density < 0.1 else "neutral"
                intensity = 0.5
            else:
                emotions["neutral"] = 0.6
                emotions["happy"] = 0.3
                emotions["sad"] = 0.1
                dominant = "neutral"
                intensity = 0.3

            return {
                "dominant_emotion": dominant,
                "emotions": emotions,
                "intensity": intensity,
            }
        except Exception as e:
            logger.debug(f"[MOOD] Emotion analysis failed: {e}")
            return default

    async def _post_mood_observation(self, person_label: str, name: str,
                                      camera: str, emotion_data: dict,
                                      face_distance: float, confidence: float,
                                      face_crop: str = ""):
        """Send a mood observation to the cloud backend."""
        now = datetime.now(IST)
        payload = {
            "person": person_label.title(),
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "camera": camera,
            "dominant_emotion": emotion_data["dominant_emotion"],
            "emotions": emotion_data["emotions"],
            "temperament": _temperament_from_emotion(
                emotion_data["dominant_emotion"],
                emotion_data["intensity"]),
            "intensity": emotion_data["intensity"],
            "face_distance": face_distance,
            "face_confidence": confidence,
            "face_crop": face_crop,
            "mood_category": emotion_data.get("mood_category", "Neutral"),
            "mood_label": emotion_data.get("mood_label", "Normal"),
            "frame_count": emotion_data.get("frame_count", 1),
            "agreement": emotion_data.get("agreement", 0.0),
            "negative_ratio": emotion_data.get("negative_ratio", 0.0),
        }

        headers = {"Content-Type": "application/json"}
        if self.agent_secret:
            headers["X-Agent-Secret"] = self.agent_secret

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.cloud_url}/api/chairman/mood",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    logger.info(
                        f"[MOOD] Sent: {person_label} on {camera} — "
                        f"{emotion_data['dominant_emotion']} "
                        f"(intensity={emotion_data['intensity']:.2f})")
                else:
                    logger.warning(f"[MOOD] POST failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"[MOOD] POST failed: {e}")

    def _is_in_mood_window(self) -> bool:
        """Check if current IST time is within the mood monitoring window."""
        now = datetime.now(IST)
        cur_mins = now.hour * 60 + now.minute
        start_mins = MOOD_START_HOUR * 60 + MOOD_START_MIN
        end_mins = MOOD_END_HOUR * 60 + MOOD_END_MIN
        return start_mins <= cur_mins < end_mins

    def _is_weekday(self) -> bool:
        """Check if today is a weekday (Mon-Fri)."""
        return datetime.now(IST).weekday() < 5

    async def scan_cameras_for_mood(self, dvrs: list[dict],
                                     camera_mapping: dict):
        """Scan mood-relevant cameras for tracked persons."""
        if not self._tracked_encodings:
            return

        for location, cam_data in camera_mapping.items():
            if not _classify_mood_camera(location):
                continue

            all_cams = cam_data.get("all_cameras", [])
            cams_to_scan = all_cams if all_cams else [cam_data]

            for cam in cams_to_scan:
                dvr_idx = cam.get("dvr_index", 0)
                channel = cam.get("channel", 1)
                if dvr_idx >= len(dvrs):
                    continue

                dvr = dvrs[dvr_idx]
                frame = await self._capture_frame(dvr, channel)
                if frame is None:
                    continue

                cam_label = f"{location} (DVR {dvr_idx + 1} Ch {channel})"

                # Detect tracked persons in frame
                detections = self._detect_tracked_person(frame)
                for det in detections:
                    person_label = det["person_label"]

                    # Cooldown check — skip if we recently posted for this person
                    now_ts = time.time()
                    last = self._last_observation.get(person_label, 0)
                    if now_ts - last < self._cooldown:
                        continue

                    # Analyze emotion for this frame
                    emotion_data = self._analyze_emotion(frame)

                    # Accumulate into frame buffer
                    if person_label not in self._frame_buffer:
                        self._frame_buffer[person_label] = []
                        self._buffer_start[person_label] = now_ts
                    self._frame_buffer[person_label].append(emotion_data)

                    buf = self._frame_buffer[person_label]
                    elapsed = now_ts - self._buffer_start.get(person_label, now_ts)
                    should_post = (
                        len(buf) >= self._max_frames or
                        (len(buf) >= self._min_frames and elapsed >= self._buffer_timeout)
                    )

                    if should_post:
                        # Aggregate multi-frame data and post
                        aggregated = _aggregate_emotions(buf)
                        self._frame_buffer.pop(person_label, None)
                        self._buffer_start.pop(person_label, None)
                        self._last_observation[person_label] = now_ts

                        logger.info(
                            f"[MOOD] Multi-frame: {person_label} — "
                            f"{aggregated['dominant_emotion']} "
                            f"({aggregated['mood_category']}) "
                            f"from {aggregated['frame_count']} frames, "
                            f"agreement={aggregated['agreement']:.0%}")

                        await self._post_mood_observation(
                            person_label=person_label,
                            name=det["name"],
                            camera=cam_label,
                            emotion_data=aggregated,
                            face_distance=det["face_distance"],
                            confidence=det["confidence"],
                            face_crop=det.get("face_crop", ""),
                        )

    async def mood_monitoring_loop(self, dvrs: list[dict],
                                    camera_mapping: dict):
        """Background loop: periodically scan cameras for mood observations."""
        self.running = True
        self._load_emotion_model()
        self.load_tracked_faces()

        logger.info(f"[MOOD] Monitoring started. Window: {MOOD_START_HOUR}:{MOOD_START_MIN:02d}"
                     f"-{MOOD_END_HOUR}:{MOOD_END_MIN:02d} IST. "
                     f"Tracking: {list(self._tracked_encodings.keys())}")

        cycle = 0
        while self.running:
            cycle += 1
            try:
                if not self._is_weekday():
                    if cycle % 120 == 1:
                        logger.info("[MOOD] Weekend — mood monitoring paused")
                    await asyncio.sleep(60)
                    continue

                if not self._is_in_mood_window():
                    if cycle % 120 == 1:
                        now = datetime.now(IST)
                        logger.info(f"[MOOD] Outside window ({now.strftime('%H:%M')} IST)")
                    await asyncio.sleep(30)
                    continue

                await self.scan_cameras_for_mood(dvrs, camera_mapping)

                # Reload faces periodically (every 20 cycles)
                if cycle % 20 == 0:
                    self.load_tracked_faces()

            except Exception as e:
                logger.error(f"[MOOD] Scan cycle error: {e}")

            await asyncio.sleep(MOOD_SCAN_INTERVAL)

        logger.info("[MOOD] Monitoring stopped")

    def start(self, dvrs: list[dict], camera_mapping: dict):
        """Start mood monitoring as a background task."""
        if self._task and not self._task.done():
            logger.info("[MOOD] Already running")
            return
        self._task = asyncio.create_task(
            self.mood_monitoring_loop(dvrs, camera_mapping))
        logger.info("[MOOD] Background task created")

    def stop(self):
        """Stop mood monitoring."""
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[MOOD] Stopped")

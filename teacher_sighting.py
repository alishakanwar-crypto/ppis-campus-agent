"""
Teacher Sighting Tracker for Head Count Reconciliation.

Periodically captures frames from DVR cameras (Entry Gate 1 & 2,
Reception 1-4, Teacher Staff, Administration) and detects teacher
faces. Sends sightings to the cloud backend for reconciliation
against TrueFace attendance records.

Does NOT mark attendance or send WhatsApp — that remains the
exclusive domain of TrueFace 3000 via the Selenium poller.
"""

from __future__ import annotations

import asyncio
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

# Camera types to monitor for teacher sightings
SIGHTING_CAMERA_KEYWORDS = [
    "ENTRY", "ENTRANCE", "DISPERSAL",  # Entry gates
    "RECEPTION",                         # Reception cameras
    "TEACHER STAFF", "STAFF ROOM",       # Staff rooms
    "ADMINISTRATION", "ADMIN",           # Administration
]


def _is_sighting_camera(location: str) -> bool:
    """Check if a camera location is relevant for teacher sighting."""
    loc_upper = location.upper()
    return any(kw in loc_upper for kw in SIGHTING_CAMERA_KEYWORDS)


class TeacherSightingTracker:
    """Tracks teacher appearances on DVR cameras for head count reconciliation."""

    def __init__(self, cloud_url: str = "https://ppis-whatsapp-bot.fly.dev",
                 agent_secret: str = ""):
        self.cloud_url = cloud_url
        self.agent_secret = agent_secret
        self.running = False
        self._task: asyncio.Task | None = None
        self._dvr_clients: dict[str, httpx.AsyncClient] = {}
        self._teacher_encodings: dict[str, dict] = {}
        # Dedup: (person_id, camera_label) → last_sighting_timestamp
        self._last_sighting: dict[tuple[str, str], float] = {}
        # Daily sighting log for local tracking
        self._today: str = ""
        self._daily_sightings: list[dict] = []

    def load_teacher_faces(self):
        """Load face encodings for all teachers from the database."""
        all_faces = face_db.load_known_faces()
        self._teacher_encodings = {}

        for pid, data in all_faces.items():
            if pid.startswith(("TEACHER_", "PRINCIPAL_")):
                self._teacher_encodings[pid] = data
        logger.info(f"[SIGHTING] Loaded {len(self._teacher_encodings)} teacher faces")

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

    def _detect_teachers(self, image_bytes: bytes) -> list[dict]:
        """Detect teacher faces in an image.

        Returns list of dicts with person_id, name, confidence, face_distance.
        """
        if face_recognition is None or not self._teacher_encodings:
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
        for encoding in face_encodings:
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

            if best_match and best_distance < 0.50:
                data = self._teacher_encodings[best_match]
                results.append({
                    "person_id": best_match,
                    "name": data.get("name", best_match),
                    "confidence": round(1.0 - best_distance, 3),
                    "face_distance": round(best_distance, 3),
                })

        return results

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

    def _is_in_sighting_window(self) -> bool:
        now = datetime.now(IST)
        cur_mins = now.hour * 60 + now.minute
        start_mins = SIGHTING_START_HOUR * 60 + SIGHTING_START_MIN
        end_mins = SIGHTING_END_HOUR * 60 + SIGHTING_END_MIN
        return start_mins <= cur_mins < end_mins

    def _is_weekday(self) -> bool:
        return datetime.now(IST).weekday() < 5

    async def scan_cameras_for_sightings(self, dvrs: list[dict],
                                          camera_mapping: dict) -> list[dict]:
        """Scan relevant cameras and detect teachers. Returns new sightings."""
        if not self._teacher_encodings:
            return []

        now = datetime.now(IST)
        now_ts = time.time()
        today = now.strftime("%Y-%m-%d")

        # Reset daily state if day changed
        if today != self._today:
            self._today = today
            self._last_sighting.clear()
            self._daily_sightings.clear()

        new_sightings = []

        for location, cam_data in camera_mapping.items():
            if not _is_sighting_camera(location):
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
                detections = self._detect_teachers(frame)

                for det in detections:
                    pid = det["person_id"]
                    key = (pid, cam_label)

                    last = self._last_sighting.get(key, 0)
                    if now_ts - last < SIGHTING_COOLDOWN:
                        continue
                    self._last_sighting[key] = now_ts

                    sighting = {
                        "person_id": pid,
                        "name": det["name"],
                        "camera": cam_label,
                        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "date": today,
                        "confidence": det["confidence"],
                    }
                    new_sightings.append(sighting)
                    self._daily_sightings.append(sighting)

                    logger.info(
                        f"[SIGHTING] {det['name']} on {cam_label} "
                        f"(conf={det['confidence']:.2f})")

        return new_sightings

    async def sighting_monitoring_loop(self, dvrs: list[dict],
                                        camera_mapping: dict):
        """Background loop: periodically scan cameras for teacher sightings."""
        self.running = True
        self.load_teacher_faces()

        logger.info(f"[SIGHTING] Started. Window: {SIGHTING_START_HOUR}:{SIGHTING_START_MIN:02d}"
                     f"-{SIGHTING_END_HOUR}:{SIGHTING_END_MIN:02d} IST. "
                     f"Teachers: {len(self._teacher_encodings)}")

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

                sightings = await self.scan_cameras_for_sightings(dvrs, camera_mapping)
                if sightings:
                    await self._post_sightings(sightings)

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

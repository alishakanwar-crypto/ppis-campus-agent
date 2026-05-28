"""
Mood & Temperament Monitor (Multi-Person)
==========================================
Captures frames from reception, administration and admission cameras,
identifies tracked persons via face recognition, analyzes facial
expressions, and tracks mood/temperament throughout the day.

Sends mood data to the cloud backend for daily report generation.

Usage:
    python chairman_mood.py          # Run in foreground
    python chairman_mood.py --test   # Quick connectivity + face match test

Tracked persons:
    Chairman  — chairman_ref.jpg
    Alisha    — alisha_ref.jpg, alisha_ref2.jpg

Cameras:
    Reception C1:      DVR 2 (192.168.0.12) Channel 54
    Reception C2:      DVR 2 (192.168.0.12) Channel 55
    Reception C3:      DVR 2 (192.168.0.12) Channel 53
    Reception C4:      DVR 2 (192.168.0.12) Channel 52
    Administration:    DVR 3 (192.168.0.14) Channel 23
    Admission Room C1: DVR 2 (192.168.0.12) Channel 57

All timestamps use IST (Asia/Kolkata, UTC+05:30).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cv2
import httpx
import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DVR_PORT = int(os.environ.get("MOOD_DVR_PORT", "80"))
DVR_DEFAULT_USER = "admin"
DVR_CREDS: dict[str, dict[str, str]] = {}

MOOD_CAMERAS = [
    {"channel": 54, "name": "Reception C1",      "dvr_ip": "192.168.0.12"},
    {"channel": 55, "name": "Reception C2",      "dvr_ip": "192.168.0.12"},
    {"channel": 53, "name": "Reception C3",      "dvr_ip": "192.168.0.12"},
    {"channel": 52, "name": "Reception C4",      "dvr_ip": "192.168.0.12"},
    {"channel": 23, "name": "Administration",    "dvr_ip": "192.168.0.14"},
    {"channel": 57, "name": "Admission Room C1", "dvr_ip": "192.168.0.12"},
]

CLOUD_API = os.environ.get(
    "MOOD_CLOUD_API",
    "https://ppis-whatsapp-bot.fly.dev/api/chairman/mood",
)

POLL_INTERVAL = int(os.environ.get("MOOD_POLL_SECONDS", "3"))

# School hours for monitoring (IST)
MONITOR_START_HOUR = 7
MONITOR_START_MIN = 0
MONITOR_END_HOUR = 17  # 5:00 PM
MONITOR_END_MIN = 0

IST = timezone(timedelta(hours=5, minutes=30))

FACE_MATCH_TOLERANCE = float(os.environ.get("MOOD_FACE_TOLERANCE", "0.62"))

# Tracked persons: list of {name, ref_photos: [Path, ...]}
BASE_DIR = Path(__file__).parent
TRACKED_PERSONS = [
    {
        "name": "Chairman",
        "ref_photos": [BASE_DIR / "chairman_ref.jpg"],
    },
    {
        "name": "Alisha",
        "ref_photos": [
            BASE_DIR / "alisha_ref.jpg",
            BASE_DIR / "alisha_ref2.jpg",
        ],
    },
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("chairman_mood.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("chairman_mood")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

running = True


def _handle_signal(sig, frame):
    global running
    logger.info("Received signal %s — shutting down", sig)
    running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# DVR Snapshot Capture (same pattern as gate_counter.py)
# ---------------------------------------------------------------------------

def capture_frame(channel: int, dvr_ip: str = "192.168.0.12") -> np.ndarray | None:
    """Capture a JPEG frame from a DVR camera."""
    stream_channel = channel * 100 + 1
    url = (
        f"http://{dvr_ip}:{DVR_PORT}/ISAPI/Streaming/channels/"
        f"{stream_channel}/picture?snapShotImageType=JPEG"
    )

    creds = DVR_CREDS.get(dvr_ip, {})
    dvr_user = creds.get("user", DVR_DEFAULT_USER)
    dvr_pass = creds.get("pass", "")

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, auth=httpx.DigestAuth(dvr_user, dvr_pass))
            if resp.status_code == 401:
                resp = client.get(url, auth=httpx.BasicAuth(dvr_user, dvr_pass))

            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                img_array = np.frombuffer(resp.content, dtype=np.uint8)
                frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                return frame
            else:
                logger.warning(
                    "Frame capture failed: ch%d HTTP %d", channel, resp.status_code,
                )
    except Exception as e:
        logger.error("Frame capture error ch%d: %s", channel, e)

    return None


# ---------------------------------------------------------------------------
# DVR Credentials (same pattern as gate_counter.py)
# ---------------------------------------------------------------------------

def _load_creds_from_dvr_list(dvrs: list[dict]) -> int:
    needed_ips = {cam["dvr_ip"] for cam in MOOD_CAMERAS}
    loaded = 0
    for dvr in dvrs:
        ip = dvr.get("ip", "")
        if ip in needed_ips and ip not in DVR_CREDS:
            pw = dvr.get("password", "")
            if pw:
                DVR_CREDS[ip] = {
                    "user": dvr.get("username", DVR_DEFAULT_USER),
                    "pass": pw,
                }
                loaded += 1
    return loaded


def load_dvr_passwords() -> None:
    cloud_url = "https://ppis-whatsapp-bot.fly.dev/api/agent-config/full"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(cloud_url)
            if resp.status_code == 200:
                data = resp.json()
                n = _load_creds_from_dvr_list(data.get("dvrs", []))
                if n:
                    logger.info("Loaded %d DVR credential(s) from cloud config", n)
    except Exception as e:
        logger.warning("Could not fetch cloud config: %s", e)

    missing = {cam["dvr_ip"] for cam in MOOD_CAMERAS} - set(DVR_CREDS.keys())
    if missing:
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            n = _load_creds_from_dvr_list(cfg.get("dvrs", []))
            if n:
                logger.info("Loaded %d DVR credential(s) from config.json", n)

    for ip in {cam["dvr_ip"] for cam in MOOD_CAMERAS}:
        if ip in DVR_CREDS:
            logger.info("DVR %s: credentials OK", ip)
        else:
            logger.warning("DVR %s: NO PASSWORD FOUND", ip)


# ---------------------------------------------------------------------------
# Multi-Person Face Recognition
# ---------------------------------------------------------------------------

class PersonDetector:
    """Detects and identifies tracked persons using face_recognition (dlib)."""

    def __init__(self, persons: list[dict], tolerance: float = 0.5):
        self.persons = persons
        self.tolerance = tolerance
        self.ref_encodings: dict[str, list[np.ndarray]] = {}

    def load(self) -> bool:
        import face_recognition

        loaded_any = False
        for person in self.persons:
            name = person["name"]
            encodings = []
            for ref_path in person["ref_photos"]:
                if not ref_path.exists():
                    logger.warning("Reference photo not found for %s: %s", name, ref_path)
                    continue
                img = face_recognition.load_image_file(str(ref_path))
                encs = face_recognition.face_encodings(img)
                if encs:
                    encodings.append(encs[0])
                    logger.info("Loaded reference face for %s: %s", name, ref_path.name)
                else:
                    logger.warning("No face found in %s for %s", ref_path.name, name)

            if encodings:
                self.ref_encodings[name] = encodings
                loaded_any = True
            else:
                logger.error("No valid reference faces for %s", name)

        return loaded_any

    def find_persons(self, frame: np.ndarray) -> list[dict]:
        """Find tracked persons in a frame.

        Returns list of dicts: {person, location, bbox, distance}
        """
        import face_recognition

        if not self.ref_encodings:
            return []

        # Upscale frame for better detection of distant/small faces
        scale = 2
        h, w = frame.shape[:2]
        upscaled = cv2.resize(frame, (w * scale, h * scale), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(upscaled, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb, number_of_times_to_upsample=2, model="hog")
        if not locations:
            return []

        logger.debug("Found %d face(s) in frame (%dx%d upscaled)", len(locations), w * scale, h * scale)

        encodings = face_recognition.face_encodings(rgb, locations)
        matches = []
        for loc, enc in zip(locations, encodings):
            best_person = None
            best_dist = float("inf")
            all_dists = {}

            for person_name, ref_encs in self.ref_encodings.items():
                distances = face_recognition.face_distance(ref_encs, enc)
                min_dist = float(min(distances))
                all_dists[person_name] = min_dist
                if min_dist <= self.tolerance and min_dist < best_dist:
                    best_person = person_name
                    best_dist = min_dist

            if best_person is None and all_dists:
                closest = min(all_dists, key=all_dists.get)
                logger.info("Face detected but no match (closest: %s dist=%.3f, threshold=%.2f)",
                           closest, all_dists[closest], self.tolerance)

            if best_person is not None:
                top, right, bottom, left = loc
                # Scale bbox back to original frame coordinates
                matches.append({
                    "person": best_person,
                    "location": (top // scale, right // scale, bottom // scale, left // scale),
                    "bbox": (left // scale, top // scale, (right - left) // scale, (bottom - top) // scale),
                    "distance": best_dist,
                })

        return matches


# ---------------------------------------------------------------------------
# Expression Analysis
# ---------------------------------------------------------------------------

class ExpressionAnalyzer:
    """Analyze facial expressions using DeepFace."""

    def __init__(self):
        self._initialized = False

    def _ensure_init(self):
        if not self._initialized:
            from deepface import DeepFace
            self._df = DeepFace
            self._initialized = True

    def analyze(self, frame: np.ndarray, face_bbox: tuple) -> dict:
        """Analyze expression of a face region.

        Args:
            frame: Full BGR frame
            face_bbox: (x, y, w, h) of the face

        Returns:
            {"dominant_emotion": str, "emotions": {str: float}, "confidence": float}
        """
        self._ensure_init()

        x, y, w, h = face_bbox
        pad = int(max(w, h) * 0.3)
        fh, fw = frame.shape[:2]
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(fw, x + w + pad)
        y2 = min(fh, y + h + pad)
        face_crop = frame[y1:y2, x1:x2]

        if face_crop.size == 0:
            return {"dominant_emotion": "unknown", "emotions": {}, "confidence": 0.0}

        try:
            results = self._df.analyze(
                face_crop,
                actions=["emotion"],
                enforce_detection=False,
                silent=True,
            )
            if results and isinstance(results, list):
                r = results[0]
                emotions = {k: float(v) for k, v in r["emotion"].items()}
                return {
                    "dominant_emotion": r["dominant_emotion"],
                    "emotions": emotions,
                    "confidence": float(r.get("face_confidence", 0.0)),
                }
        except Exception as e:
            logger.warning("Expression analysis failed: %s", e)

        return {"dominant_emotion": "unknown", "emotions": {}, "confidence": 0.0}


# ---------------------------------------------------------------------------
# Temperament Scoring
# ---------------------------------------------------------------------------

TEMPERAMENT_MAP = {
    "happy": "calm",
    "neutral": "neutral",
    "sad": "stressed",
    "angry": "agitated",
    "fear": "stressed",
    "surprise": "neutral",
    "disgust": "irritable",
}


def classify_temperament(emotions: dict) -> str:
    if not emotions:
        return "unknown"
    dominant = max(emotions, key=emotions.get)
    return TEMPERAMENT_MAP.get(dominant, "neutral")


def compute_expression_intensity(emotions: dict) -> float:
    neutral = emotions.get("neutral", 100.0)
    return max(0.0, 100.0 - neutral)


# ---------------------------------------------------------------------------
# Face Crop for Report
# ---------------------------------------------------------------------------

def crop_face_jpeg(frame: np.ndarray, bbox: tuple, quality: int = 80) -> str:
    x, y, w, h = bbox
    pad = int(max(w, h) * 0.4)
    fh, fw = frame.shape[:2]
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(fw, x + w + pad)
    y2 = min(fh, y + h + pad)
    crop = frame[y1:y2, x1:x2]

    if crop.size == 0:
        return ""

    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf.tobytes()).decode("ascii")


# ---------------------------------------------------------------------------
# Cloud API
# ---------------------------------------------------------------------------

def send_mood_event(event: dict) -> bool:
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(CLOUD_API, json=event)
            if resp.status_code in (200, 201):
                return True
            else:
                logger.warning("Cloud API error: %d %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Cloud API send failed: %s", e)
    return False


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def is_monitoring_time() -> bool:
    now = datetime.now(IST)
    start = now.replace(hour=MONITOR_START_HOUR, minute=MONITOR_START_MIN, second=0)
    end = now.replace(hour=MONITOR_END_HOUR, minute=MONITOR_END_MIN, second=0)
    return start <= now <= end


def run_mood_monitor():
    logger.info("=" * 60)
    logger.info("Mood & Temperament Monitor starting (multi-person)")
    logger.info("Tracked persons: %s", ", ".join(p["name"] for p in TRACKED_PERSONS))
    logger.info("Cameras: %s", ", ".join(c["name"] for c in MOOD_CAMERAS))
    logger.info("Poll interval: %d seconds", POLL_INTERVAL)
    logger.info("Monitoring: %02d:%02d - %02d:%02d IST",
                MONITOR_START_HOUR, MONITOR_START_MIN,
                MONITOR_END_HOUR, MONITOR_END_MIN)
    logger.info("=" * 60)

    load_dvr_passwords()
    needed_ips = {c["dvr_ip"] for c in MOOD_CAMERAS}
    missing = needed_ips - set(DVR_CREDS.keys())
    if missing:
        logger.error("Missing DVR credentials for: %s", ", ".join(missing))
        sys.exit(1)

    detector = PersonDetector(TRACKED_PERSONS, tolerance=FACE_MATCH_TOLERANCE)
    if not detector.load():
        logger.error("Cannot start without at least one reference face")
        sys.exit(1)

    analyzer = ExpressionAnalyzer()

    poll_count = 0
    daily_detections: dict[str, int] = {p["name"]: 0 for p in TRACKED_PERSONS}
    current_date = datetime.now(IST).strftime("%Y-%m-%d")
    pending_events: list[dict] = []

    while running:
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")

        if today != current_date:
            logger.info("Date changed: %s -> %s. Detections yesterday: %s",
                        current_date, today, daily_detections)
            current_date = today
            daily_detections = {p["name"]: 0 for p in TRACKED_PERSONS}

        if not is_monitoring_time():
            if poll_count > 0:
                logger.info("Outside monitoring hours. Today's detections: %s", daily_detections)
                poll_count = 0
            time.sleep(30)
            continue

        poll_count += 1

        for cam in MOOD_CAMERAS:
            frame = capture_frame(cam["channel"], cam["dvr_ip"])
            if frame is None:
                continue

            matches = detector.find_persons(frame)
            if not matches:
                continue

            # Group by person — take best match per person
            person_best: dict[str, dict] = {}
            for m in matches:
                pname = m["person"]
                if pname not in person_best or m["distance"] < person_best[pname]["distance"]:
                    person_best[pname] = m

            for person_name, best in person_best.items():
                bbox = best["bbox"]

                expr = analyzer.analyze(frame, bbox)
                if expr["dominant_emotion"] == "unknown":
                    continue

                temperament = classify_temperament(expr["emotions"])
                intensity = compute_expression_intensity(expr["emotions"])
                face_crop = crop_face_jpeg(frame, bbox)

                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

                event = {
                    "person": person_name,
                    "timestamp": timestamp,
                    "camera": cam["name"],
                    "dominant_emotion": expr["dominant_emotion"],
                    "emotions": expr["emotions"],
                    "temperament": temperament,
                    "intensity": round(intensity, 1),
                    "face_distance": round(best["distance"], 3),
                    "face_confidence": round(expr["confidence"], 2),
                    "face_crop": face_crop,
                }
                pending_events.append(event)
                daily_detections[person_name] = daily_detections.get(person_name, 0) + 1

                logger.info(
                    "%s detected on %s — Mood: %s (%.1f%%) | Temperament: %s | Intensity: %.1f",
                    person_name, cam["name"], expr["dominant_emotion"],
                    expr["emotions"].get(expr["dominant_emotion"], 0),
                    temperament, intensity,
                )

        # Send pending events to cloud (retry failed ones next cycle)
        if pending_events:
            failed = []
            for evt in pending_events:
                if not send_mood_event(evt):
                    failed.append(evt)
            pending_events = failed

        if poll_count % 100 == 0:
            logger.info("Poll #%d — Today's detections: %s", poll_count, daily_detections)

        time.sleep(POLL_INTERVAL)

    logger.info("Mood monitor stopped. Total detections today: %s", daily_detections)


# ---------------------------------------------------------------------------
# Quick test mode
# ---------------------------------------------------------------------------

def test_connectivity():
    logger.info("Testing mood monitor (multi-person)...")
    load_dvr_passwords()

    for cam in MOOD_CAMERAS:
        dvr_ip = cam["dvr_ip"]
        if dvr_ip not in DVR_CREDS:
            logger.error("%s: SKIPPED — no credentials for DVR %s", cam["name"], dvr_ip)
            continue
        frame = capture_frame(cam["channel"], dvr_ip)
        if frame is not None:
            logger.info("%s (DVR %s): OK — Frame %dx%d",
                        cam["name"], dvr_ip, frame.shape[1], frame.shape[0])
        else:
            logger.error("%s (DVR %s): FAILED — Could not capture frame", cam["name"], dvr_ip)

    logger.info("Testing face recognition for tracked persons...")
    detector = PersonDetector(TRACKED_PERSONS, tolerance=FACE_MATCH_TOLERANCE)
    if detector.load():
        for name, encs in detector.ref_encodings.items():
            logger.info("%s: OK — %d reference encoding(s)", name, len(encs))
    else:
        logger.error("Face recognition: FAILED — no reference faces loaded")
        return

    logger.info("Testing expression analysis...")
    analyzer = ExpressionAnalyzer()
    for person in TRACKED_PERSONS:
        ref_path = person["ref_photos"][0]
        if not ref_path.exists():
            continue
        ref_img = cv2.imread(str(ref_path))
        if ref_img is None:
            continue
        import face_recognition
        rgb = cv2.cvtColor(ref_img, cv2.COLOR_BGR2RGB)
        locs = face_recognition.face_locations(rgb)
        if locs:
            top, right, bottom, left = locs[0]
            bbox = (left, top, right - left, bottom - top)
            expr = analyzer.analyze(ref_img, bbox)
            logger.info("%s expression test — Dominant: %s", person["name"], expr["dominant_emotion"])
            for emo, score in sorted(expr["emotions"].items(), key=lambda x: -x[1]):
                logger.info("  %s: %.1f%%", emo, score)

    logger.info("Cloud API: %s", CLOUD_API)
    logger.info("Test complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mood & Temperament Monitor (Multi-Person)")
    parser.add_argument("--test", action="store_true", help="Quick connectivity + detection test")
    args = parser.parse_args()

    if args.test:
        test_connectivity()
    else:
        run_mood_monitor()

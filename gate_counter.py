"""
Gate Head Count Counter
=======================
Captures frames from ENTRY GATE cameras on the school DVR,
detects people using YOLOv8-nano, tracks them across frames,
and counts entries with attire color.

Sends events to the cloud backend for reconciliation with
TrueFace face-recognition attendance.

Usage:
    python gate_counter.py          # Run in foreground
    python gate_counter.py --test   # Quick connectivity test

Cameras:
    ENTRY GATE-1: DVR 3 (192.168.0.14) Channel 20
    ENTRY GATE-2: DVR 3 (192.168.0.14) Channel 16

All timestamps use IST (Asia/Kolkata, UTC+05:30).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import math
import os
import signal
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cv2
import httpx
import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DVR_IP = os.environ.get("GATE_DVR_IP", "192.168.0.14")
DVR_PORT = int(os.environ.get("GATE_DVR_PORT", "80"))
DVR_USER = os.environ.get("GATE_DVR_USER", "admin")
DVR_PASS = os.environ.get("GATE_DVR_PASS", "")

GATE_CAMERAS = [
    {"channel": 20, "name": "ENTRY GATE-1"},
    {"channel": 16, "name": "ENTRY GATE-2"},
]

CLOUD_API = os.environ.get(
    "GATE_CLOUD_API",
    "https://ppis-whatsapp-bot.fly.dev/api/gate/entry",
)

POLL_INTERVAL = int(os.environ.get("GATE_POLL_SECONDS", "5"))

# School hours for gate monitoring (IST)
MONITOR_START_HOUR = 6   # 6:00 AM
MONITOR_START_MIN = 0
MONITOR_END_HOUR = 17    # 5:00 PM
MONITOR_END_MIN = 0

IST = timezone(timedelta(hours=5, minutes=30))

# YOLO model path (will be downloaded on first run)
MODEL_DIR = Path(__file__).parent / "models"
MODEL_DIR.mkdir(exist_ok=True)
YOLO_MODEL = os.environ.get("GATE_YOLO_MODEL", "yolov8n")

# Person detection confidence threshold
CONFIDENCE_THRESHOLD = float(os.environ.get("GATE_CONF_THRESHOLD", "0.5"))

# Tracker settings
MAX_DISAPPEARED = 15  # frames before removing a tracked person
MAX_DISTANCE = 100    # max pixel distance for centroid matching

# Virtual line position (fraction of frame height, 0.0=top, 1.0=bottom)
# People crossing this line from top to bottom = "IN"
LINE_POSITION = float(os.environ.get("GATE_LINE_POSITION", "0.5"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gate_counter.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("gate_counter")

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
# DVR Snapshot Capture (Hikvision ISAPI)
# ---------------------------------------------------------------------------

def capture_gate_frame(channel: int) -> np.ndarray | None:
    """Capture a JPEG frame from the DVR gate camera and return as numpy array."""
    stream_channel = channel * 100 + 1
    url = (
        f"http://{DVR_IP}:{DVR_PORT}/ISAPI/Streaming/channels/"
        f"{stream_channel}/picture?snapShotImageType=JPEG"
    )

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, auth=httpx.DigestAuth(DVR_USER, DVR_PASS))
            if resp.status_code == 401:
                resp = client.get(url, auth=httpx.BasicAuth(DVR_USER, DVR_PASS))

            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                img_array = np.frombuffer(resp.content, dtype=np.uint8)
                frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                return frame
            else:
                logger.warning(
                    "Gate frame capture failed: ch%d HTTP %d content-type=%s",
                    channel, resp.status_code,
                    resp.headers.get("content-type", "unknown"),
                )
    except Exception as e:
        logger.error("Gate frame capture error ch%d: %s", channel, e)

    return None


# ---------------------------------------------------------------------------
# Color Extraction
# ---------------------------------------------------------------------------

COLOR_NAMES = {
    "red": [(0, 70, 50), (10, 255, 255)],
    "red2": [(170, 70, 50), (180, 255, 255)],
    "orange": [(10, 70, 50), (25, 255, 255)],
    "yellow": [(25, 70, 50), (35, 255, 255)],
    "green": [(35, 70, 50), (85, 255, 255)],
    "blue": [(85, 70, 50), (130, 255, 255)],
    "purple": [(130, 70, 50), (170, 255, 255)],
}


def extract_dominant_color(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> str:
    """Extract the dominant clothing color from a person's bounding box.

    Uses the upper 2/3 of the bounding box (torso area) for color detection.
    Returns a color name string like "red", "blue", "white", "black", etc.
    """
    x1, y1, x2, y2 = bbox
    h = y2 - y1
    # Use upper 2/3 (torso, not legs)
    torso_y2 = y1 + int(h * 0.67)
    crop = frame[y1:torso_y2, x1:x2]

    if crop.size == 0:
        return "unknown"

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    # Check for achromatic colors first (white, black, gray)
    mean_v = np.mean(hsv[:, :, 2])
    mean_s = np.mean(hsv[:, :, 1])

    if mean_v < 50:
        return "black"
    if mean_v > 200 and mean_s < 40:
        return "white"
    if mean_s < 40:
        return "gray"

    # Check chromatic colors
    best_color = "unknown"
    best_count = 0

    for color_name, (lower, upper) in COLOR_NAMES.items():
        lower_arr = np.array(lower)
        upper_arr = np.array(upper)
        mask = cv2.inRange(hsv, lower_arr, upper_arr)
        count = cv2.countNonZero(mask)
        if count > best_count:
            best_count = count
            best_color = color_name

    # Merge red and red2
    if best_color == "red2":
        best_color = "red"

    return best_color


def crop_person_jpeg(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> str:
    """Crop the person from the frame and return as base64-encoded JPEG."""
    x1, y1, x2, y2 = bbox
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return ""
    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return base64.b64encode(buf).decode("ascii")


# ---------------------------------------------------------------------------
# Centroid Tracker with Line Crossing
# ---------------------------------------------------------------------------

class CentroidTracker:
    """Track people across frames using centroid matching.

    Detects when a tracked person crosses a virtual horizontal line,
    recording the crossing direction (IN = top-to-bottom, OUT = bottom-to-top).
    """

    def __init__(self, max_disappeared: int = 15, max_distance: float = 100.0,
                 line_y: int = 0):
        self.next_id = 0
        self.objects: OrderedDict[int, np.ndarray] = OrderedDict()  # id -> centroid
        self.bboxes: dict[int, tuple[int, int, int, int]] = {}  # id -> bbox
        self.disappeared: dict[int, int] = {}
        self.prev_centroids: dict[int, np.ndarray] = {}  # id -> previous centroid
        self.max_disappeared = max_disappeared
        self.max_distance = max_distance
        self.line_y = line_y
        self.crossings: list[dict] = []  # direction crossings this update

    def set_line_y(self, y: int):
        self.line_y = y

    def register(self, centroid: np.ndarray, bbox: tuple[int, int, int, int]):
        self.objects[self.next_id] = centroid
        self.bboxes[self.next_id] = bbox
        self.disappeared[self.next_id] = 0
        self.prev_centroids[self.next_id] = centroid.copy()
        self.next_id += 1

    def deregister(self, object_id: int):
        del self.objects[object_id]
        del self.bboxes[object_id]
        del self.disappeared[object_id]
        self.prev_centroids.pop(object_id, None)

    def update(self, detections: list[tuple[tuple[int, int, int, int], float]]) -> list[dict]:
        """Update tracker with new detections.

        Args:
            detections: list of (bbox, confidence) tuples

        Returns:
            list of crossing events: [{"id": int, "direction": "IN"|"OUT", "bbox": tuple}]
        """
        self.crossings = []

        if len(detections) == 0:
            for oid in list(self.disappeared.keys()):
                self.disappeared[oid] += 1
                if self.disappeared[oid] > self.max_disappeared:
                    self.deregister(oid)
            return self.crossings

        input_centroids = []
        input_bboxes = []
        for bbox, _conf in detections:
            x1, y1, x2, y2 = bbox
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            input_centroids.append(np.array([cx, cy]))
            input_bboxes.append(bbox)

        if len(self.objects) == 0:
            for i in range(len(input_centroids)):
                self.register(input_centroids[i], input_bboxes[i])
        else:
            object_ids = list(self.objects.keys())
            object_centroids = list(self.objects.values())

            # Compute distance matrix
            D = np.zeros((len(object_centroids), len(input_centroids)))
            for i, oc in enumerate(object_centroids):
                for j, ic in enumerate(input_centroids):
                    D[i, j] = np.linalg.norm(oc - ic)

            # Match using greedy nearest-neighbor
            rows = D.min(axis=1).argsort()
            cols = D.argmin(axis=1)[rows]

            used_rows: set[int] = set()
            used_cols: set[int] = set()

            for row, col in zip(rows, cols):
                if row in used_rows or col in used_cols:
                    continue
                if D[row, col] > self.max_distance:
                    continue

                oid = object_ids[row]
                self.prev_centroids[oid] = self.objects[oid].copy()
                self.objects[oid] = input_centroids[col]
                self.bboxes[oid] = input_bboxes[col]
                self.disappeared[oid] = 0

                # Check line crossing
                prev_y = self.prev_centroids[oid][1]
                curr_y = input_centroids[col][1]
                if prev_y < self.line_y <= curr_y:
                    self.crossings.append({
                        "id": oid,
                        "direction": "IN",
                        "bbox": input_bboxes[col],
                    })
                elif prev_y > self.line_y >= curr_y:
                    self.crossings.append({
                        "id": oid,
                        "direction": "OUT",
                        "bbox": input_bboxes[col],
                    })

                used_rows.add(row)
                used_cols.add(col)

            # Handle unmatched existing objects (disappeared)
            for row in range(len(object_ids)):
                if row not in used_rows:
                    oid = object_ids[row]
                    self.disappeared[oid] += 1
                    if self.disappeared[oid] > self.max_disappeared:
                        self.deregister(oid)

            # Handle unmatched new detections (register)
            for col in range(len(input_centroids)):
                if col not in used_cols:
                    self.register(input_centroids[col], input_bboxes[col])

        return self.crossings


# ---------------------------------------------------------------------------
# YOLO Person Detector
# ---------------------------------------------------------------------------

class PersonDetector:
    """YOLOv8-nano person detector."""

    def __init__(self):
        self.model = None

    def load(self):
        """Load YOLOv8-nano model (downloads on first run)."""
        try:
            from ultralytics import YOLO
            model_path = MODEL_DIR / f"{YOLO_MODEL}.pt"
            if model_path.exists():
                self.model = YOLO(str(model_path))
            else:
                self.model = YOLO(f"{YOLO_MODEL}.pt")
                # Save to models dir
                import shutil
                default_path = Path(f"{YOLO_MODEL}.pt")
                if default_path.exists():
                    shutil.move(str(default_path), str(model_path))
            logger.info("YOLO model loaded: %s", YOLO_MODEL)
        except ImportError:
            logger.error("ultralytics not installed. Run: pip install ultralytics")
            sys.exit(1)

    def detect(self, frame: np.ndarray) -> list[tuple[tuple[int, int, int, int], float]]:
        """Detect people in a frame.

        Returns:
            list of (bbox, confidence) where bbox = (x1, y1, x2, y2)
        """
        if self.model is None:
            return []

        results = self.model(frame, verbose=False, classes=[0])  # class 0 = person
        detections = []

        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf < CONFIDENCE_THRESHOLD:
                    continue
                x1, y1, x2, y2 = box.xyxy[0].int().tolist()
                detections.append(((x1, y1, x2, y2), conf))

        return detections


# ---------------------------------------------------------------------------
# Cloud API
# ---------------------------------------------------------------------------

def send_gate_event(events: list[dict]) -> bool:
    """Send gate entry events to the cloud backend."""
    if not events:
        return True

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(CLOUD_API, json=events)
            if resp.status_code == 200:
                logger.info("Sent %d gate event(s) to cloud — OK", len(events))
                return True
            else:
                logger.warning("Cloud API returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Cloud API error: %s", e)

    return False


# ---------------------------------------------------------------------------
# DVR Password Loader
# ---------------------------------------------------------------------------

def _load_from_dvr_list(dvrs: list[dict]) -> bool:
    """Try to find DVR_IP in a list of DVR dicts and set credentials."""
    global DVR_PASS, DVR_USER
    for dvr in dvrs:
        if dvr.get("ip") == DVR_IP:
            DVR_PASS = dvr.get("password", "")
            usr = dvr.get("username", "")
            if usr:
                DVR_USER = usr
            return bool(DVR_PASS)
    return False


def load_dvr_password() -> str:
    """Load DVR password — tries cloud config first, then local config.json."""
    global DVR_PASS

    # 1. Try cloud config (same endpoint main.py uses)
    cloud_url = "https://ppis-whatsapp-bot.fly.dev/api/agent-config/full"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(cloud_url)
            if resp.status_code == 200:
                data = resp.json()
                dvrs = data.get("dvrs", [])
                if _load_from_dvr_list(dvrs):
                    logger.info("DVR password loaded from cloud config for %s", DVR_IP)
                    return DVR_PASS
    except Exception as e:
        logger.warning("Could not fetch cloud config: %s", e)

    # 2. Fall back to local config.json
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        if _load_from_dvr_list(cfg.get("dvrs", [])):
            logger.info("DVR password loaded from config.json for %s", DVR_IP)
            return DVR_PASS

    logger.warning("No DVR password found for %s", DVR_IP)
    return DVR_PASS


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def is_monitoring_time() -> bool:
    """Check if current IST time is within school monitoring hours."""
    now = datetime.now(IST)
    start = now.replace(hour=MONITOR_START_HOUR, minute=MONITOR_START_MIN, second=0)
    end = now.replace(hour=MONITOR_END_HOUR, minute=MONITOR_END_MIN, second=0)
    return start <= now <= end


def run_gate_counter():
    """Main gate counting loop."""
    logger.info("=" * 60)
    logger.info("Gate Head Count Counter starting")
    logger.info("DVR: %s:%d", DVR_IP, DVR_PORT)
    logger.info("Cameras: %s", ", ".join(c["name"] for c in GATE_CAMERAS))
    logger.info("Poll interval: %d seconds", POLL_INTERVAL)
    logger.info("Monitoring: %02d:%02d - %02d:%02d IST",
                MONITOR_START_HOUR, MONITOR_START_MIN,
                MONITOR_END_HOUR, MONITOR_END_MIN)
    logger.info("=" * 60)

    # Load DVR password from shared config
    load_dvr_password()

    if not DVR_PASS:
        logger.error("No DVR password configured. Set GATE_DVR_PASS or check config.json")
        sys.exit(1)

    # Initialize detector
    detector = PersonDetector()
    detector.load()

    # One tracker per camera
    trackers: dict[str, CentroidTracker] = {}
    for cam in GATE_CAMERAS:
        trackers[cam["name"]] = CentroidTracker(
            max_disappeared=MAX_DISAPPEARED,
            max_distance=MAX_DISTANCE,
        )

    # Daily counters
    daily_in: dict[str, int] = {c["name"]: 0 for c in GATE_CAMERAS}
    daily_out: dict[str, int] = {c["name"]: 0 for c in GATE_CAMERAS}
    current_date = datetime.now(IST).strftime("%Y-%m-%d")
    poll_count = 0
    pending_events: list[dict] = []

    while running:
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")

        # Reset daily counters on date change
        if today != current_date:
            logger.info(
                "Date changed: %s -> %s. Previous totals: IN=%s OUT=%s",
                current_date, today, daily_in, daily_out,
            )
            current_date = today
            daily_in = {c["name"]: 0 for c in GATE_CAMERAS}
            daily_out = {c["name"]: 0 for c in GATE_CAMERAS}
            for cam in GATE_CAMERAS:
                trackers[cam["name"]] = CentroidTracker(
                    max_disappeared=MAX_DISAPPEARED,
                    max_distance=MAX_DISTANCE,
                )

        # Only monitor during school hours
        if not is_monitoring_time():
            if poll_count > 0:
                logger.info(
                    "Outside monitoring hours. Day totals: IN=%s OUT=%s",
                    daily_in, daily_out,
                )
                poll_count = 0
            time.sleep(30)
            continue

        poll_count += 1

        for cam in GATE_CAMERAS:
            cam_name = cam["name"]
            channel = cam["channel"]

            frame = capture_gate_frame(channel)
            if frame is None:
                continue

            # Set virtual line at configured position
            frame_h = frame.shape[0]
            line_y = int(frame_h * LINE_POSITION)
            trackers[cam_name].set_line_y(line_y)

            # Detect people
            detections = detector.detect(frame)

            # Update tracker and get crossings
            crossings = trackers[cam_name].update(detections)

            for crossing in crossings:
                direction = crossing["direction"]
                bbox = crossing["bbox"]

                if direction == "IN":
                    daily_in[cam_name] += 1
                else:
                    daily_out[cam_name] += 1

                # Extract attire color
                attire_color = extract_dominant_color(frame, bbox)

                # Crop person for the report
                person_crop = crop_person_jpeg(frame, bbox)

                timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

                event = {
                    "timestamp": timestamp,
                    "camera": cam_name,
                    "direction": direction,
                    "attire_color": attire_color,
                    "person_crop": person_crop,
                    "daily_in": daily_in[cam_name],
                    "daily_out": daily_out[cam_name],
                }
                pending_events.append(event)

                logger.info(
                    "%s: %s crossing at %s — %s attire — Day total IN=%d OUT=%d",
                    cam_name, direction, timestamp, attire_color,
                    daily_in[cam_name], daily_out[cam_name],
                )

        # Send pending events to cloud in batches
        if pending_events:
            ok = send_gate_event(pending_events)
            if ok:
                pending_events = []
            # If failed, keep pending and retry next cycle

        # Periodic status log
        if poll_count % 60 == 0:  # Every ~5 minutes
            total_in = sum(daily_in.values())
            total_out = sum(daily_out.values())
            logger.info(
                "Poll #%d — Day totals: IN=%d OUT=%d — Per camera: IN=%s OUT=%s",
                poll_count, total_in, total_out, daily_in, daily_out,
            )

        time.sleep(POLL_INTERVAL)

    # Shutdown
    total_in = sum(daily_in.values())
    total_out = sum(daily_out.values())
    logger.info(
        "Gate counter stopped. Final totals: IN=%d OUT=%d", total_in, total_out,
    )


# ---------------------------------------------------------------------------
# Quick test mode
# ---------------------------------------------------------------------------

def test_connectivity():
    """Quick test: capture one frame from each gate camera."""
    logger.info("Testing gate camera connectivity...")
    load_dvr_password()

    for cam in GATE_CAMERAS:
        frame = capture_gate_frame(cam["channel"])
        if frame is not None:
            logger.info(
                "%s: OK — Frame %dx%d", cam["name"], frame.shape[1], frame.shape[0],
            )
        else:
            logger.error("%s: FAILED — Could not capture frame", cam["name"])

    logger.info("Cloud API: %s", CLOUD_API)
    logger.info("Test complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gate Head Count Counter")
    parser.add_argument("--test", action="store_true", help="Quick connectivity test")
    args = parser.parse_args()

    if args.test:
        test_connectivity()
    else:
        run_gate_counter()

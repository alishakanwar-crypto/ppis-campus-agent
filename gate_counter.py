"""
Gate Head Count Counter
=======================
Captures frames from ENTRY GATE and RECEPTION cameras on school DVRs,
detects people and vehicles using YOLOv8-nano, tracks them across frames,
and counts entries with attire color. Vehicles (cars, buses, trucks,
motorcycles) are counted separately from people.

Sends events to the cloud backend for reconciliation with
TrueFace face-recognition attendance.

Usage:
    python gate_counter.py          # Run in foreground
    python gate_counter.py --test   # Quick connectivity test

Cameras:
    ENTRY GATE-1: DVR 3 (192.168.0.14) Channel 20
    ENTRY GATE-2: DVR 3 (192.168.0.14) Channel 16
    GALLERY MID: DVR 1 (192.168.0.11) Channel 17
    Reception C1: DVR 2 (192.168.0.12) Channel 54
    Reception C2: DVR 2 (192.168.0.12) Channel 55
    Reception C3: DVR 2 (192.168.0.12) Channel 53
    Reception C4: DVR 2 (192.168.0.12) Channel 52
    DISPERSAL EXIT: DVR 2 (192.168.0.12) Channel 8
    Basement Main Gate: DVR 4 (192.168.0.13) Channel 12
    Basement R/W First Strs: DVR 4 (192.168.0.13) Channel 42
    Basement R/W Middle Strs: DVR 4 (192.168.0.13) Channel 6
    Basement L/W Middle Strs: DVR 4 (192.168.0.13) Channel 20
    Basement Generator Right Exit: DVR 4 (192.168.0.13) Channel 25
    Basement Cam 2: DVR 4 (192.168.0.13) Channel 21
    Basement Cam 5: DVR 4 (192.168.0.13) Channel 37
    Basement Cam 8: DVR 4 (192.168.0.13) Channel 10
    Basement Cam 10: DVR 4 (192.168.0.13) Channel 19
    Basement Electricity: DVR 4 (192.168.0.13) Channel 11
    ENTRY GATE-OUTSIDE (CP Plus): standalone IP camera 192.168.0.215 (no DVR)

The CP Plus camera (model CP-UNC-VE21ZL4P-VMD) is mounted OUTSIDE the school for
pedestrian head counting. It is a direct IP camera (Dahua-OEM), captured via the
Dahua HTTP snapshot CGI with an RTSP fallback. Its head count feeds the same
cloud reconciliation used for the DVR gate cameras, so unknown faces are
classified (Parent / Student / Staff / Third-party-Vendor), logged with entry
time + attire color, and cross-referenced against face-recognition attendance.

All timestamps use IST (Asia/Kolkata, UTC+05:30).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import cv2
import httpx
import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DVR_PORT = int(os.environ.get("GATE_DVR_PORT", "80"))
DVR_DEFAULT_USER = "admin"

# Per-DVR credentials: {ip: {"user": ..., "pass": ...}}
DVR_CREDS: dict[str, dict[str, str]] = {}

GATE_CAMERAS = [
    {"channel": 20, "name": "ENTRY GATE-1",    "dvr_ip": "192.168.0.14"},
    {"channel": 16, "name": "ENTRY GATE-2",    "dvr_ip": "192.168.0.14"},
    {"channel": 17, "name": "GALLERY MID",     "dvr_ip": "192.168.0.11"},
    {"channel": 54, "name": "Reception C1",    "dvr_ip": "192.168.0.12"},
    {"channel": 55, "name": "Reception C2",    "dvr_ip": "192.168.0.12"},
    {"channel": 53, "name": "Reception C3",    "dvr_ip": "192.168.0.12"},
    {"channel": 52, "name": "Reception C4",    "dvr_ip": "192.168.0.12"},
    {"channel":  8, "name": "DISPERSAL EXIT",  "dvr_ip": "192.168.0.12"},
    # Basement cameras (DVR 4)
    {"channel": 12, "name": "Basement Main Gate",             "dvr_ip": "192.168.0.13"},
    {"channel": 42, "name": "Basement R/W First Strs",        "dvr_ip": "192.168.0.13"},
    {"channel":  6, "name": "Basement R/W Middle Strs",       "dvr_ip": "192.168.0.13"},
    {"channel": 20, "name": "Basement L/W Middle Strs",       "dvr_ip": "192.168.0.13"},
    {"channel": 25, "name": "Basement Generator Right Exit",  "dvr_ip": "192.168.0.13"},
    {"channel": 21, "name": "Basement Cam 2",                 "dvr_ip": "192.168.0.13"},
    {"channel": 37, "name": "Basement Cam 5",                 "dvr_ip": "192.168.0.13"},
    {"channel": 10, "name": "Basement Cam 8",                 "dvr_ip": "192.168.0.13"},
    {"channel": 19, "name": "Basement Cam 10",                "dvr_ip": "192.168.0.13"},
    {"channel": 11, "name": "Basement Electricity",           "dvr_ip": "192.168.0.13"},
]

# ---------------------------------------------------------------------------
# Standalone CP Plus IP camera(s) — pedestrian entry OUTSIDE the school gate.
#
# Unlike the DVR channels above, these are direct IP cameras (no NVR in front).
# CP Plus network cameras (e.g. model CP-UNC-VE21ZL4P-VMD) are Dahua-OEM, so we
# capture frames via the Dahua HTTP snapshot CGI and fall back to RTSP.
#
# The camera NAME intentionally contains "ENTRY GATE" so the cloud
# reconciliation (_classify_visitor) treats it as a main-gate pedestrian entry:
# unknown faces during school hours are classified as Parents, and its head
# count is cross-referenced against face-recognition attendance.
# ---------------------------------------------------------------------------

CPPLUS_CAMERAS = [
    {
        "name": os.environ.get("CPPLUS_GATE_NAME", "ENTRY GATE-OUTSIDE (CP Plus)"),
        "ip": os.environ.get("CPPLUS_GATE_IP", "192.168.0.215"),
        # Username is case-sensitive on Dahua/CP Plus firmware; alternates are
        # tried automatically on 401.
        "user": os.environ.get("CPPLUS_GATE_USER", "admin"),
        # Password is loaded at runtime (env override → shared DVR password),
        # never hard-coded here. See _resolve_cpplus_password().
        "pass": os.environ.get("CPPLUS_GATE_PASS", ""),
        "type": "cpplus",
        # Optional per-camera virtual-line override (falls back to LINE_POSITION)
        "line_position": float(os.environ["CPPLUS_GATE_LINE_POSITION"])
        if os.environ.get("CPPLUS_GATE_LINE_POSITION")
        else None,
    },
]

# Alternate usernames to retry on 401 (Dahua username is case-sensitive)
CPPLUS_USER_ALTERNATES = ["admin", "Admin"]

# The main stream preserves small and overlapping people during crowded periods.
# Set to 1 only on PCs that cannot sustain the main-stream inference load.
CPPLUS_RTSP_SUBTYPE = int(os.environ.get("CPPLUS_RTSP_SUBTYPE", "0"))

# Bound HTTP snapshot + RTSP connect/read time so an unreachable camera never
# blocks the whole poll loop.
CPPLUS_CONNECT_TIMEOUT_SEC = float(os.environ.get("CPPLUS_CONNECT_TIMEOUT_SEC", "3"))
CPPLUS_HTTP_TIMEOUT_SEC = float(os.environ.get("CPPLUS_HTTP_TIMEOUT_SEC", "8"))
CPPLUS_RTSP_TIMEOUT_SEC = int(os.environ.get("CPPLUS_RTSP_TIMEOUT_SEC", "5"))

# The CP Plus outside gate is head-counted by its own dedicated worker thread
# that reads a continuous stream (not the slow ~15-20s DVR poll cycle) so people
# who cross quickly are sampled more reliably. This throttles the worker's
# processing rate to bound CPU; ~5 FPS gives several frames for a 1-2s pass.
CPPLUS_TARGET_FPS = float(os.environ.get("CPPLUS_TARGET_FPS", "5"))
CPPLUS_CONFIDENCE_THRESHOLD = float(
    os.environ.get("CPPLUS_CONFIDENCE_THRESHOLD", "0.25")
)
CPPLUS_LINE_HYSTERESIS = float(os.environ.get("CPPLUS_LINE_HYSTERESIS", "0.04"))
CPPLUS_TRACK_MAX_GAP_SECONDS = float(
    os.environ.get("CPPLUS_TRACK_MAX_GAP_SECONDS", "5")
)
CPPLUS_LINE_AXIS = os.environ.get("CPPLUS_LINE_AXIS", "vertical").strip().lower()
if CPPLUS_LINE_AXIS not in {"horizontal", "vertical"}:
    CPPLUS_LINE_AXIS = "vertical"

# Record the main RTSP stream on the school PC, then recount each completed
# hour. Onboard SD playback remains a secondary source and live detections are
# the fallback if local coverage is incomplete.
CPPLUS_LOCAL_RECORDING_ENABLED = os.environ.get(
    "CPPLUS_LOCAL_RECORDING_ENABLED", "1",
) not in ("0", "false", "False", "no", "NO")
CPPLUS_LOCAL_RECORDING_DIR = Path(
    os.environ.get(
        "CPPLUS_LOCAL_RECORDING_DIR",
        str(Path(__file__).parent / "cpplus_recordings"),
    )
)
CPPLUS_CROSSING_AUDIT_DIR = Path(
    os.environ.get(
        "CPPLUS_CROSSING_AUDIT_DIR",
        str(Path(__file__).parent / "face_audit_results"),
    )
)
_CPPLUS_CROSSING_AUDIT_LOCK = threading.Lock()
CPPLUS_CROSSING_AUDIT_MARKER = (
    CPPLUS_CROSSING_AUDIT_DIR / ".c1_face_audit_active"
)
CPPLUS_LOCAL_RECORDING_FPS = max(
    0.1, float(os.environ.get("CPPLUS_LOCAL_RECORDING_FPS", "10")),
)
CPPLUS_LOCAL_SEGMENT_MINUTES = max(
    1, int(os.environ.get("CPPLUS_LOCAL_SEGMENT_MINUTES", "5")),
)
CPPLUS_LOCAL_RETENTION_DAYS = max(
    1, int(os.environ.get("CPPLUS_LOCAL_RETENTION_DAYS", "2")),
)
CPPLUS_LOCAL_MIN_FREE_GB = max(
    0.0, float(os.environ.get("CPPLUS_LOCAL_MIN_FREE_GB", "5")),
)
CPPLUS_LOCAL_COVERAGE_TOLERANCE_SECONDS = max(
    0, int(os.environ.get("CPPLUS_LOCAL_COVERAGE_TOLERANCE_SECONDS", "15")),
)
CPPLUS_REPLAY_ENABLED = os.environ.get("CPPLUS_REPLAY_ENABLED", "1") not in (
    "0", "false", "False", "no", "NO",
)
CPPLUS_REPLAY_DELAY_MINUTES = int(os.environ.get("CPPLUS_REPLAY_DELAY_MINUTES", "2"))
CPPLUS_REPLAY_RETRY_MINUTES = int(os.environ.get("CPPLUS_REPLAY_RETRY_MINUTES", "10"))
CPPLUS_NATIVE_HISTORY_GRACE_MINUTES = max(
    0, int(os.environ.get("CPPLUS_NATIVE_HISTORY_GRACE_MINUTES", "60")),
)
CPPLUS_NATIVE_HISTORY_RETRY_SECONDS = max(
    1.0, float(os.environ.get("CPPLUS_NATIVE_HISTORY_RETRY_SECONDS", "60")),
)
# Two frames per second retains several observations per walkway crossing.
# Native 640px YOLO inference reduces replay latency on the CPU-only school PC.
CPPLUS_REPLAY_SAMPLE_FPS = float(os.environ.get("CPPLUS_REPLAY_SAMPLE_FPS", "2"))
CPPLUS_REPLAY_IMAGE_SIZE = int(os.environ.get("CPPLUS_REPLAY_IMAGE_SIZE", "640"))
CPPLUS_SD_REPLAY_ENABLED = os.environ.get(
    "CPPLUS_SD_REPLAY_ENABLED", "0"
).lower() in ("1", "true", "yes")
CPPLUS_RECORDING_CHANNEL = int(os.environ.get("CPPLUS_RECORDING_CHANNEL", "1"))
CPPLUS_RECORDING_PORT = int(os.environ.get("CPPLUS_RECORDING_PORT", "80"))
CPPLUS_REPLAY_STATE_FILE = Path(__file__).parent / "cpplus_replay_state.json"
CPPLUS_SEGMENT_REPLAY_ENABLED = os.environ.get(
    "CPPLUS_SEGMENT_REPLAY_ENABLED", "1",
).lower() not in ("0", "false", "no")
CPPLUS_SEGMENT_REPLAY_POLL_SECONDS = max(
    2.0, float(os.environ.get("CPPLUS_SEGMENT_REPLAY_POLL_SECONDS", "10")),
)
CPPLUS_SEGMENT_REPLAY_STATE_FILE = (
    Path(__file__).parent / "cpplus_segment_replay_state.json"
)
CPPLUS_NATIVE_SUMMARY_ENABLED = os.environ.get(
    "CPPLUS_NATIVE_SUMMARY_ENABLED", "1",
).lower() not in ("0", "false", "no")
CPPLUS_NATIVE_SUMMARY_POLL_SECONDS = max(
    1.0, float(os.environ.get("CPPLUS_NATIVE_SUMMARY_POLL_SECONDS", "2")),
)
CPPLUS_NATIVE_SUMMARY_STATE_FILE = (
    Path(__file__).parent / "cpplus_native_summary_state.json"
)
CPPLUS_NATIVE_SUMMARY_PENDING_FILE = (
    Path(__file__).parent / "cpplus_native_summary_pending.json"
)

# Campus side of the CP Plus entry-zone boundary. Tracks may approach the
# boundary horizontally, vertically, or diagonally; only outside-to-campus
# transitions are IN. The supplied recording places the campus side on the
# right of a vertical boundary.
CPPLUS_IN_TOP_TO_BOTTOM = os.environ.get("CPPLUS_IN_TOP_TO_BOTTOM", "1") not in (
    "0", "false", "False", "no", "NO",
)
CPPLUS_IN_LEFT_TO_RIGHT = os.environ.get("CPPLUS_IN_LEFT_TO_RIGHT", "1") not in (
    "0", "false", "False", "no", "NO",
)


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in (
        "0", "false", "no",
    )


# ---------------------------------------------------------------------------
# CP Plus C1 anonymous event interface
#
# The outside gate (C1) is ANONYMOUS ONLY: no names, faces, or biometrics.
# Behaviour/health signals never modify official head-count totals; they are
# posted to a dedicated non-additive endpoint (mirrors candidate-boundary
# events) and are safe to ignore on the backend.
# ---------------------------------------------------------------------------

# When true, C1 live crossing events omit the person face crop so nothing
# biometric leaves the school PC. Default on to honour the anonymous-only rule.
CPPLUS_C1_ANONYMOUS = _env_flag("CPPLUS_C1_ANONYMOUS", "1")

# Non-additive signal channel for queue/wrong-way/loitering/after-hours/
# vehicle/camera-health/replay-discrepancy events.
C1_SIGNAL_API = os.environ.get(
    "C1_SIGNAL_API",
    "https://ppis-whatsapp-bot.fly.dev/api/gate/c1-signal",
)
CPPLUS_SIGNALS_ENABLED = _env_flag("CPPLUS_SIGNALS_ENABLED", "1")

# Queue / congestion: sustained simultaneous occupancy in view.
CPPLUS_QUEUE_THRESHOLD = max(1, int(os.environ.get("CPPLUS_QUEUE_THRESHOLD", "6")))
CPPLUS_QUEUE_MIN_INTERVAL_SEC = max(
    1.0, float(os.environ.get("CPPLUS_QUEUE_MIN_INTERVAL_SEC", "60")),
)

# Wrong-way: the direction expected for normal C1 flow ("IN" or "OUT").
CPPLUS_EXPECTED_FLOW = (
    os.environ.get("CPPLUS_EXPECTED_FLOW", "IN").strip().upper()
)
if CPPLUS_EXPECTED_FLOW not in {"IN", "OUT"}:
    CPPLUS_EXPECTED_FLOW = "IN"

# Loitering / dwell: seconds a track may linger before a signal is raised.
CPPLUS_LOITER_SECONDS = max(
    1.0, float(os.environ.get("CPPLUS_LOITER_SECONDS", "60")),
)

# After-hours activity (default OFF — enabling changes the monitoring window).
CPPLUS_AFTER_HOURS_ENABLED = _env_flag("CPPLUS_AFTER_HOURS_ENABLED", "0")
CPPLUS_AFTER_HOURS_POLL_SEC = max(
    2.0, float(os.environ.get("CPPLUS_AFTER_HOURS_POLL_SEC", "10")),
)

# Vehicle classification/count/dwell on C1 (default OFF — extra CPU).
CPPLUS_VEHICLE_ENABLED = _env_flag("CPPLUS_VEHICLE_ENABLED", "0")
CPPLUS_VEHICLE_DWELL_SECONDS = max(
    1.0, float(os.environ.get("CPPLUS_VEHICLE_DWELL_SECONDS", "120")),
)

# Camera health thresholds.
CPPLUS_HEALTH_ENABLED = _env_flag("CPPLUS_HEALTH_ENABLED", "1")
CPPLUS_HEALTH_OFFLINE_FRAMES = max(
    1, int(os.environ.get("CPPLUS_HEALTH_OFFLINE_FRAMES", "10")),
)
CPPLUS_HEALTH_FROZEN_FRAMES = max(
    2, int(os.environ.get("CPPLUS_HEALTH_FROZEN_FRAMES", "30")),
)
CPPLUS_HEALTH_BLUR_VARIANCE = max(
    0.0, float(os.environ.get("CPPLUS_HEALTH_BLUR_VARIANCE", "40")),
)
CPPLUS_HEALTH_DARK_LUMA = max(
    0.0, float(os.environ.get("CPPLUS_HEALTH_DARK_LUMA", "20")),
)
CPPLUS_HEALTH_BAD_FRAMES = max(
    1, int(os.environ.get("CPPLUS_HEALTH_BAD_FRAMES", "20")),
)
# Mean per-pixel difference (0-255, on a 32x32 grayscale) from the learned
# scene baseline that indicates the camera has been physically moved/re-aimed.
CPPLUS_HEALTH_MOVED_DIFF = max(
    0.0, float(os.environ.get("CPPLUS_HEALTH_MOVED_DIFF", "45")),
)

# Allow disabling the CP Plus outside-gate camera without a code change
CPPLUS_ENABLED = os.environ.get("CPPLUS_GATE_ENABLED", "1") not in ("0", "false", "False")
if not CPPLUS_ENABLED:
    CPPLUS_CAMERAS = []

# All people head-count cameras: DVR channels + standalone CP Plus IP camera(s)
HEADCOUNT_CAMERAS = GATE_CAMERAS + CPPLUS_CAMERAS

CLOUD_API = os.environ.get(
    "GATE_CLOUD_API",
    "https://ppis-whatsapp-bot.fly.dev/api/gate/entry",
)
CPPLUS_RECOUNT_API = os.environ.get(
    "CPPLUS_RECOUNT_API",
    "https://ppis-whatsapp-bot.fly.dev/api/gate/cpplus-hourly-recount",
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

# Detection confidence thresholds
CONFIDENCE_THRESHOLD = float(os.environ.get("GATE_CONF_THRESHOLD", "0.5"))
VEHICLE_CONF_THRESHOLD = float(os.environ.get("GATE_VEHICLE_CONF", "0.45"))

# YOLOv8 COCO classes for vehicles
VEHICLE_CLASSES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

# Only detect vehicles on entry gate cameras (not indoor cameras)
VEHICLE_CAMERAS = {"ENTRY GATE-1", "ENTRY GATE-2", "DISPERSAL EXIT"}

# Cloud API for vehicle events
VEHICLE_CLOUD_API = os.environ.get(
    "GATE_VEHICLE_API",
    "https://ppis-whatsapp-bot.fly.dev/api/gate/vehicle-entry",
)

CANDIDATE_BOUNDARY_API = os.environ.get(
    "GATE_CANDIDATE_BOUNDARY_API",
    "https://ppis-whatsapp-bot.fly.dev/api/gate/candidate-boundary-event",
)
CANDIDATE_BOUNDARY_CAMERAS = {
    "ENTRY GATE-1": "C2",
    "ENTRY GATE-2": "C2",
    "Basement Main Gate": "C4",
}

# Tracker settings
MAX_DISAPPEARED = 15  # frames before removing a tracked person
MAX_DISTANCE = 100    # max pixel distance for centroid matching

# Default virtual-line position as a fraction of the selected frame axis.
LINE_POSITION = float(os.environ.get("GATE_LINE_POSITION", "0.5"))

# Entry/exit camera classification for direction assignment
_EXIT_CAMERAS = {"DISPERSAL EXIT"}


def _camera_direction(cam_name: str) -> str:
    """Determine entry direction based on camera name.

    All cameras default to IN except explicit exit cameras.
    This covers Entry Gates, Basement cameras, and Reception cameras.
    """
    if cam_name in _EXIT_CAMERAS:
        return "OUT"
    return "IN"

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

# DVR IPs where ISAPI auth is broken but RTSP works
_RTSP_FALLBACK_IPS: set[str] = {"192.168.0.13"}  # DVR 4


def _capture_gate_frame_rtsp(channel: int, dvr_ip: str) -> np.ndarray | None:
    """Capture a single frame via RTSP (fallback for DVRs with broken ISAPI)."""
    creds = DVR_CREDS.get(dvr_ip, {})
    dvr_user = creds.get("user", DVR_DEFAULT_USER)
    dvr_pass = creds.get("pass", "")
    stream_channel = channel * 100 + 1
    safe_pwd = dvr_pass.replace("@", "%40")
    rtsp_url = f"rtsp://{dvr_user}:{safe_pwd}@{dvr_ip}:554/Streaming/Channels/{stream_channel}"
    try:
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            logger.warning("RTSP fallback failed to open %s ch%d", dvr_ip, channel)
            return None
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            logger.info("RTSP fallback captured frame from %s ch%d", dvr_ip, channel)
            return frame
    except Exception as e:
        logger.error("RTSP fallback error %s ch%d: %s", dvr_ip, channel, e)
    return None


def capture_gate_frame(channel: int, dvr_ip: str = "192.168.0.14") -> np.ndarray | None:
    """Capture a JPEG frame from a DVR camera and return as numpy array."""
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
                    "Gate frame capture failed: ch%d HTTP %d content-type=%s",
                    channel, resp.status_code,
                    resp.headers.get("content-type", "unknown"),
                )
                # RTSP fallback for DVRs with broken ISAPI auth
                if dvr_ip in _RTSP_FALLBACK_IPS:
                    return _capture_gate_frame_rtsp(channel, dvr_ip)
    except Exception as e:
        logger.error("Gate frame capture error ch%d: %s", channel, e)
        if dvr_ip in _RTSP_FALLBACK_IPS:
            return _capture_gate_frame_rtsp(channel, dvr_ip)

    return None


def _resolve_cpplus_password(cam: dict) -> str:
    """Return the password for a CP Plus camera without hard-coding secrets.

    Priority: explicit env-configured value on the camera → the shared password
    used by the school DVRs (all admin/<same pass>), which is loaded from cloud
    config / config.json at runtime. Returns "" if nothing is available.
    """
    if cam.get("pass"):
        return cam["pass"]
    # All school DVRs share one admin password; the outside gate camera uses the
    # same. Reuse whatever was loaded into DVR_CREDS to avoid committing it.
    passwords = [c.get("pass", "") for c in DVR_CREDS.values() if c.get("pass")]
    if passwords:
        # Most common (they should all be identical)
        return max(set(passwords), key=passwords.count)
    return ""


def capture_cpplus_frame(cam: dict) -> np.ndarray | None:
    """Capture a JPEG frame from a standalone CP Plus (Dahua-OEM) IP camera.

    Tries the Dahua HTTP snapshot CGI first (with digest, then basic auth,
    retrying alternate usernames on 401), then falls back to an RTSP grab.
    Returns a decoded BGR numpy frame or None.
    """
    ip = cam["ip"]
    password = _resolve_cpplus_password(cam)
    if not password:
        logger.warning("CP Plus %s: no password available — skipping", ip)
        return None

    configured_user = cam.get("user", "admin")
    users = [configured_user] + [u for u in CPPLUS_USER_ALTERNATES if u != configured_user]

    snapshot_urls = [
        f"http://{ip}/cgi-bin/snapshot.cgi?channel=1",
        f"http://{ip}/cgi-bin/snapshot.cgi",
    ]

    # 1) HTTP snapshot CGI. Use a short connect timeout so an unreachable camera
    # fails fast, and only retry alternate usernames on an actual 401 (auth
    # issue) rather than multiplying connect timeouts on network errors.
    http_timeout = httpx.Timeout(CPPLUS_HTTP_TIMEOUT_SEC, connect=CPPLUS_CONNECT_TIMEOUT_SEC)
    try:
        with httpx.Client(timeout=http_timeout) as client:
            for url in snapshot_urls:
                for user in users:
                    resp = client.get(url, auth=httpx.DigestAuth(user, password))
                    if resp.status_code == 401:
                        resp = client.get(url, auth=httpx.BasicAuth(user, password))
                    if (
                        resp.status_code == 200
                        and resp.headers.get("content-type", "").startswith("image")
                    ):
                        img_array = np.frombuffer(resp.content, dtype=np.uint8)
                        frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
                        if frame is not None:
                            cam["user"] = user  # remember the working username
                            return frame
                    if resp.status_code == 401:
                        continue  # wrong username — try the next candidate
                    logger.debug("CP Plus %s: %s HTTP %d", ip, url, resp.status_code)
                    break  # non-auth HTTP error — no point trying other users
    except httpx.HTTPError as e:
        # Connection refused / timeout — camera unreachable via HTTP; try RTSP.
        logger.debug("CP Plus %s snapshot unreachable: %s", ip, e)

    # 2) RTSP fallback (sub-stream is enough for head-count detection)
    # Bound the FFmpeg connect/read time so an unreachable camera can never
    # block the whole poll loop (timeout is in microseconds).
    timeout_us = str(CPPLUS_RTSP_TIMEOUT_SEC * 1_000_000)
    prev_ffmpeg_opts = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
    # "stimeout" (older FFmpeg) and "timeout" (newer FFmpeg) are both socket
    # timeouts in microseconds; set both so the build in use honours one.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        f"rtsp_transport;tcp|stimeout;{timeout_us}|timeout;{timeout_us}"
    )
    try:
        for user in users:
            rtsp_url = (
                f"rtsp://{user}:{password}@{ip}:554/cam/realmonitor"
                f"?channel=1&subtype={CPPLUS_RTSP_SUBTYPE}"
            )
            cap = None
            try:
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, CPPLUS_RTSP_TIMEOUT_SEC * 1000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, CPPLUS_RTSP_TIMEOUT_SEC * 1000)
                ok, frame = cap.read()
                if ok and frame is not None:
                    cam["user"] = user
                    return frame
            except Exception as e:
                logger.debug("CP Plus %s RTSP error: %s", ip, e)
            finally:
                if cap is not None:
                    cap.release()
    finally:
        if prev_ffmpeg_opts is None:
            os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
        else:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = prev_ffmpeg_opts

    logger.warning("CP Plus %s: all capture methods failed", ip)
    return None


def capture_camera_frame(cam: dict) -> np.ndarray | None:
    """Capture a frame from any head-count camera (DVR channel or CP Plus)."""
    if cam.get("type") == "cpplus":
        return capture_cpplus_frame(cam)
    return capture_gate_frame(cam["channel"], cam["dvr_ip"])


def open_cpplus_stream(cam: dict):
    """Open a persistent, low-latency RTSP stream to the CP Plus camera.

    Returns an opened cv2.VideoCapture (buffer size 1 so reads stay current) or
    None if no username/password combination can open the stream.
    """
    ip = cam["ip"]
    password = _resolve_cpplus_password(cam)
    if not password:
        logger.warning("CP Plus %s: no password available for stream", ip)
        return None

    configured_user = cam.get("user", "admin")
    users = [configured_user] + [u for u in CPPLUS_USER_ALTERNATES if u != configured_user]

    timeout_us = str(CPPLUS_RTSP_TIMEOUT_SEC * 1_000_000)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        f"rtsp_transport;tcp|stimeout;{timeout_us}|timeout;{timeout_us}"
    )
    for user in users:
        rtsp_url = (
            f"rtsp://{user}:{password}@{ip}:554/cam/realmonitor"
            f"?channel=1&subtype={CPPLUS_RTSP_SUBTYPE}"
        )
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, CPPLUS_RTSP_TIMEOUT_SEC * 1000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, CPPLUS_RTSP_TIMEOUT_SEC * 1000)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        if cap.isOpened():
            ok, _ = cap.read()
            if ok:
                cam["user"] = user  # remember the working username
                logger.info("CP Plus %s: RTSP stream opened (user=%s)", ip, user)
                return cap
        cap.release()
    logger.warning("CP Plus %s: could not open RTSP stream (will use HTTP snapshots)", ip)
    return None


def _cpplus_crossing_audit_active(
    marker_path: Path = CPPLUS_CROSSING_AUDIT_MARKER,
) -> bool:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        return float(marker["expires_at_epoch"]) > time.time()
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _append_cpplus_crossing_audit(
    events: list[dict],
    output_dir: Path = CPPLUS_CROSSING_AUDIT_DIR,
) -> Path | None:
    if not events:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"c1_crossings_{datetime.now(IST).strftime('%Y%m%d')}.jsonl"
    is_new = not path.exists()
    with _CPPLUS_CROSSING_AUDIT_LOCK, path.open("a", encoding="utf-8") as handle:
        for event in events:
            payload = {
                key: event[key]
                for key in (
                    "event_id",
                    "timestamp",
                    "camera",
                    "direction",
                    "tracker_id",
                    "attire_color",
                    "bbox_height_ratio",
                    "bbox_width_ratio",
                    "daily_in",
                    "daily_out",
                )
                if key in event
            }
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    if is_new:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path


def _append_cpplus_tracker_trace(
    samples: list[dict],
    output_dir: Path = CPPLUS_CROSSING_AUDIT_DIR,
) -> Path | None:
    if not samples:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"c1_tracker_trace_{datetime.now(IST).strftime('%Y%m%d')}.jsonl"
    is_new = not path.exists()
    with _CPPLUS_CROSSING_AUDIT_LOCK, path.open("a", encoding="utf-8") as handle:
        for sample in samples:
            payload = {
                key: sample[key]
                for key in (
                    "timestamp",
                    "tracker_id",
                    "line_axis",
                    "line_position_ratio",
                    "line_hysteresis_ratio",
                    "anchor_x_ratio",
                    "anchor_y_ratio",
                    "line_side",
                    "bbox_height_ratio",
                    "bbox_width_ratio",
                )
                if key in sample
            }
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    if is_new:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path


# ---------------------------------------------------------------------------
# C1 anonymous event helpers
# ---------------------------------------------------------------------------

def stable_event_id(
    camera: str, date_ymd: str, tracker_id: int, ordinal: int,
) -> str:
    """Deterministic, anonymous id for a C1 crossing.

    Derived only from non-PII inputs so the same physical crossing yields the
    same id from the live loop, the audit ledger, and any replay correlation.
    """
    raw = f"{camera}|{date_ymd}|{tracker_id}|{ordinal}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# Live per-hour IN counts recorded by the live worker so the replay verifier
# can compute a live-vs-verified discrepancy without changing official totals.
_CPPLUS_LIVE_HOURLY_LOCK = threading.Lock()
_cpplus_live_hourly_in: dict[str, int] = {}


def _record_live_hourly_in(now: datetime) -> None:
    key = now.replace(minute=0, second=0, microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with _CPPLUS_LIVE_HOURLY_LOCK:
        _cpplus_live_hourly_in[key] = _cpplus_live_hourly_in.get(key, 0) + 1


def _get_live_hourly_in(hour_start: datetime) -> int | None:
    key = hour_start.strftime("%Y-%m-%d %H:%M:%S")
    with _CPPLUS_LIVE_HOURLY_LOCK:
        return _cpplus_live_hourly_in.get(key)


def _build_c1_signal(
    camera: str,
    signal_type: str,
    data: dict,
    timestamp: datetime,
    event_id: str | None = None,
) -> dict:
    """Build a non-additive, anonymous C1 signal event.

    ``verification_only`` marks the event as never affecting official totals.
    The payload intentionally carries no images, names, or biometrics.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=IST)
    return {
        "event_id": event_id or uuid4().hex,
        "timestamp": timestamp.astimezone(IST).strftime("%d-%m-%Y %H:%M:%S IST"),
        "camera": camera,
        "type": signal_type,
        "verification_only": True,
        "data": data,
    }


def send_c1_signal_events(events: list[dict]) -> bool:
    """Send anonymous, non-additive C1 signal events to the cloud."""
    if not events:
        return True
    if not CPPLUS_SIGNALS_ENABLED:
        return True
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                C1_SIGNAL_API, json=events, headers=_agent_secret_headers(),
            )
            if response.status_code == 200:
                logger.info("Sent %d C1 signal event(s) — OK", len(events))
                return True
            logger.warning(
                "C1 signal API returned %d: %s",
                response.status_code, response.text[:200],
            )
    except Exception as exc:
        logger.error("C1 signal API error: %s", exc)
    return False


def _frame_blur_variance(frame: np.ndarray) -> float:
    """Variance of the Laplacian — low values indicate a blurred frame."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _frame_mean_luma(frame: np.ndarray) -> float:
    """Mean brightness — very low values indicate a blocked/covered lens."""
    return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())


def _frame_signature(frame: np.ndarray) -> str:
    """Cheap perceptual signature for frozen-frame detection."""
    small = cv2.resize(frame, (16, 16), interpolation=cv2.INTER_AREA)
    return hashlib.md5(small.tobytes()).hexdigest()


class CameraHealthMonitor:
    """Detect offline/frozen/blurred/blocked/moved conditions.

    Emits at most one signal per health state until the camera recovers, so a
    persistent fault does not flood the backend. Returns a health state string
    when a NEW fault (or a recovery) is observed, otherwise ``None``.
    """

    def __init__(
        self,
        offline_frames: int = CPPLUS_HEALTH_OFFLINE_FRAMES,
        frozen_frames: int = CPPLUS_HEALTH_FROZEN_FRAMES,
        blur_variance: float = CPPLUS_HEALTH_BLUR_VARIANCE,
        dark_luma: float = CPPLUS_HEALTH_DARK_LUMA,
        bad_frames: int = CPPLUS_HEALTH_BAD_FRAMES,
        moved_diff: float = CPPLUS_HEALTH_MOVED_DIFF,
    ):
        self.offline_frames = offline_frames
        self.frozen_frames = frozen_frames
        self.blur_variance = blur_variance
        self.dark_luma = dark_luma
        self.bad_frames = bad_frames
        self.moved_diff = moved_diff
        self._missing = 0
        self._same = 0
        self._blur = 0
        self._dark = 0
        self._moved = 0
        self._last_signature: str | None = None
        self._baseline: np.ndarray | None = None
        self._active: str | None = None

    @staticmethod
    def _scene(frame: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(
            np.float32
        )

    def _fire(self, state: str | None) -> str | None:
        if state == self._active:
            return None
        self._active = state
        return state or "healthy"

    def observe(self, frame: np.ndarray | None) -> str | None:
        if frame is None:
            self._missing += 1
            if self._missing >= self.offline_frames:
                return self._fire("offline")
            return None
        self._missing = 0

        signature = _frame_signature(frame)
        if signature == self._last_signature:
            self._same += 1
        else:
            self._same = 0
        self._last_signature = signature
        if self._same >= self.frozen_frames:
            return self._fire("frozen")

        if _frame_mean_luma(frame) < self.dark_luma:
            self._dark += 1
        else:
            self._dark = 0
        if self._dark >= self.bad_frames:
            return self._fire("blocked")

        if _frame_blur_variance(frame) < self.blur_variance:
            self._blur += 1
        else:
            self._blur = 0
        if self._blur >= self.bad_frames:
            return self._fire("blurred")

        scene = self._scene(frame)
        if self._baseline is None:
            self._baseline = scene
        else:
            diff = float(np.abs(scene - self._baseline).mean())
            if diff > self.moved_diff:
                self._moved += 1
            else:
                self._moved = 0
                # Track slow scene drift (lighting, seasons) when stable.
                self._baseline = 0.95 * self._baseline + 0.05 * scene
            if self._moved >= self.bad_frames:
                self._baseline = scene
                self._moved = 0
                return self._fire("moved")

        return self._fire(None)


class DwellTracker:
    """Track per-object first-seen time to flag loitering, fired once each."""

    def __init__(self, threshold_seconds: float):
        self.threshold_seconds = threshold_seconds
        self._first_seen: dict[int, float] = {}
        self._fired: set[int] = set()

    def update(
        self, active_ids: list[int], now_monotonic: float,
    ) -> list[tuple[int, float]]:
        active = set(active_ids)
        for oid in active:
            self._first_seen.setdefault(oid, now_monotonic)
        fired: list[tuple[int, float]] = []
        for oid in active:
            if oid in self._fired:
                continue
            dwell = now_monotonic - self._first_seen[oid]
            if dwell >= self.threshold_seconds:
                self._fired.add(oid)
                fired.append((oid, dwell))
        for oid in list(self._first_seen):
            if oid not in active:
                self._first_seen.pop(oid, None)
                self._fired.discard(oid)
        return fired


def run_cpplus_worker(cam: dict):
    """Dedicated head-count loop for the CP Plus outside gate camera.

    The main DVR poll loop samples every camera only once per ~15-20s cycle,
    which misses people who cross the outside gate quickly. This worker keeps a
    persistent RTSP stream (falling back to fast HTTP snapshots if RTSP can't
    open) and processes frames continuously, throttled to CPPLUS_TARGET_FPS, so
    every person is caught. Each completed line crossing is counted once and
    streamed to the cloud. Runs in its own thread.
    """
    cam_name = cam["name"]
    detector = PersonDetector()
    detector.load()
    tracker = CentroidTracker(
        max_disappeared=max(
            MAX_DISAPPEARED,
            int(CPPLUS_TARGET_FPS * CPPLUS_TRACK_MAX_GAP_SECONDS),
        ),
        max_distance=MAX_DISTANCE,
        anchor_y="bottom",
        line_axis=CPPLUS_LINE_AXIS,
    )
    daily_in = 0
    daily_out = 0
    current_date = datetime.now(IST).strftime("%Y-%m-%d")
    cap = None
    min_interval = 1.0 / CPPLUS_TARGET_FPS if CPPLUS_TARGET_FPS > 0 else 0.0
    reconnect_backoff = 2.0
    tracker_trace_last_sample: dict[int, float] = {}

    # Anonymous, non-additive signal state (queue/wrong-way/loitering/health/
    # vehicle). None of these ever change the official head-count totals.
    crossing_ordinal: dict[int, int] = {}
    health_monitor = CameraHealthMonitor()
    dwell_tracker = DwellTracker(CPPLUS_LOITER_SECONDS)
    last_queue_signal = 0.0
    vehicle_tracker: CentroidTracker | None = None
    vehicle_dwell: DwellTracker | None = None
    if CPPLUS_VEHICLE_ENABLED:
        vehicle_tracker = CentroidTracker(
            max_disappeared=max(
                MAX_DISAPPEARED,
                int(CPPLUS_TARGET_FPS * CPPLUS_TRACK_MAX_GAP_SECONDS),
            ),
            max_distance=MAX_DISTANCE * 2,
            anchor_y="bottom",
            line_axis=CPPLUS_LINE_AXIS,
        )
        vehicle_dwell = DwellTracker(CPPLUS_VEHICLE_DWELL_SECONDS)
    seen_vehicle_ids: set[int] = set()

    logger.info("CP Plus worker started for %s (target %.1f FPS)", cam_name, CPPLUS_TARGET_FPS)

    while running:
        # Only monitor during school hours; drop the stream when idle.
        if not is_monitoring_time():
            if cap is not None:
                cap.release()
                cap = None
            time.sleep(30)
            continue

        # Reset counters/tracker at date change (mirrors the main loop).
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if today != current_date:
            logger.info("CP Plus worker date change %s -> %s (prev IN=%d OUT=%d)",
                        current_date, today, daily_in, daily_out)
            current_date = today
            daily_in = 0
            daily_out = 0
            tracker_trace_last_sample.clear()
            crossing_ordinal.clear()
            seen_vehicle_ids.clear()
            tracker = CentroidTracker(
                max_disappeared=max(
                    MAX_DISAPPEARED,
                    int(CPPLUS_TARGET_FPS * CPPLUS_TRACK_MAX_GAP_SECONDS),
                ),
                max_distance=MAX_DISTANCE,
                anchor_y="bottom",
                line_axis=CPPLUS_LINE_AXIS,
            )

        loop_start = time.monotonic()

        # Acquire a frame: prefer the persistent RTSP stream; if it can't be
        # opened or a read fails, fall back to a single HTTP snapshot so the
        # head-count keeps working through any RTSP outage.
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
                cap = None
            cap = open_cpplus_stream(cam)

        frame = None
        if cap is not None:
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                cap = None
                frame = None
        if frame is None:
            frame = capture_cpplus_frame(cam)
        if frame is None:
            if CPPLUS_HEALTH_ENABLED:
                state = health_monitor.observe(None)
                if state and state != "healthy":
                    send_c1_signal_events([
                        _build_c1_signal(
                            cam_name, "camera_health",
                            {"state": state, "detector": "capture"},
                            datetime.now(IST),
                        )
                    ])
            time.sleep(reconnect_backoff)
            continue

        frame_h, frame_w = frame.shape[:2]
        line_dimension = frame_w if CPPLUS_LINE_AXIS == "vertical" else frame_h
        tracker.set_line(
            int(line_dimension * (cam.get("line_position") or LINE_POSITION)),
            hysteresis=int(line_dimension * CPPLUS_LINE_HYSTERESIS),
        )
        tracker.max_distance = max(MAX_DISTANCE, frame_w * MAX_DISTANCE / 640.0)

        try:
            detections = detector.detect(
                frame,
                confidence_threshold=CPPLUS_CONFIDENCE_THRESHOLD,
            )
        except Exception as e:
            logger.error("CP Plus %s: detection error: %s", cam_name, e)
            detections = []
        # Raw IN means negative-to-positive across the selected line axis.
        crossings = tracker.update(detections)

        events: list[dict] = []
        audit_events: list[dict] = []
        signal_events: list[dict] = []
        now = datetime.now(IST)

        # Camera-health assessment on a real frame (frozen/blurred/blocked/moved).
        if CPPLUS_HEALTH_ENABLED:
            health_state = health_monitor.observe(frame)
            if health_state and health_state != "healthy":
                signal_events.append(_build_c1_signal(
                    cam_name, "camera_health",
                    {"state": health_state, "detector": "frame"},
                    now,
                ))

        crossing_audit_active = _cpplus_crossing_audit_active()
        if crossing_audit_active:
            sample_time = time.monotonic()
            trace_samples: list[dict] = []
            for tracker_id, anchor in tracker.objects.items():
                if tracker.disappeared.get(tracker_id, 0) > 0:
                    continue
                if sample_time - tracker_trace_last_sample.get(tracker_id, 0) < 1.0:
                    continue
                x1, y1, x2, y2 = tracker.bboxes[tracker_id]
                line_coordinate = tracker._line_coordinate(anchor)
                trace_samples.append({
                    "timestamp": now.strftime("%d-%m-%Y %H:%M:%S IST"),
                    "tracker_id": tracker_id,
                    "line_axis": tracker.line_axis,
                    "line_position_ratio": round(tracker.line_y / line_dimension, 4),
                    "line_hysteresis_ratio": round(
                        tracker.line_hysteresis / line_dimension, 4
                    ),
                    "anchor_x_ratio": round(float(anchor[0]) / frame_w, 4),
                    "anchor_y_ratio": round(float(anchor[1]) / frame_h, 4),
                    "line_side": tracker._line_side(line_coordinate),
                    "bbox_height_ratio": round(max(0, y2 - y1) / frame_h, 4),
                    "bbox_width_ratio": round(max(0, x2 - x1) / frame_w, 4),
                })
                tracker_trace_last_sample[tracker_id] = sample_time
            if trace_samples:
                try:
                    _append_cpplus_tracker_trace(trace_samples)
                except OSError as exc:
                    logger.warning("Could not append C1 tracker trace: %s", exc)
        for cr in crossings:
            raw_dir = cr["direction"]
            positive_direction_is_in = (
                CPPLUS_IN_LEFT_TO_RIGHT
                if CPPLUS_LINE_AXIS == "vertical"
                else CPPLUS_IN_TOP_TO_BOTTOM
            )
            if positive_direction_is_in:
                person_dir = raw_dir
            else:
                person_dir = "OUT" if raw_dir == "IN" else "IN"

            if person_dir == "IN":
                daily_in += 1
                _record_live_hourly_in(now)
            else:
                daily_out += 1

            bbox = cr.get("bbox")
            if bbox is None:
                continue
            attire_color = extract_dominant_color(frame, bbox)
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            x1, y1, x2, y2 = bbox
            # Stable, anonymous id derived from (camera, date, track, ordinal)
            # so the same crossing is identifiable across audit and replay.
            ordinal = crossing_ordinal.get(cr["id"], 0)
            crossing_ordinal[cr["id"]] = ordinal + 1
            event_id = stable_event_id(cam_name, current_date, cr["id"], ordinal)
            audit_events.append({
                "event_id": event_id,
                "timestamp": now.strftime("%d-%m-%Y %H:%M:%S IST"),
                "camera": cam_name,
                "direction": person_dir,
                "tracker_id": cr["id"],
                "attire_color": attire_color,
                "bbox_height_ratio": round(max(0, y2 - y1) / frame_h, 4),
                "bbox_width_ratio": round(max(0, x2 - x1) / frame_w, 4),
                "daily_in": daily_in,
                "daily_out": daily_out,
            })
            event = {
                "event_id": event_id,
                "timestamp": ts,
                "camera": cam_name,
                "direction": person_dir,
                "attire_color": attire_color,
                "daily_in": daily_in,
                "daily_out": daily_out,
            }
            # C1 is anonymous-only by default: no face crop leaves the PC.
            # The hi-res snapshot is only produced when explicitly allowed.
            if not CPPLUS_C1_ANONYMOUS:
                if person_dir == "IN":
                    event["person_crop"] = crop_person_hires_cpplus(cam, frame, bbox)
                else:
                    event["person_crop"] = crop_person_jpeg(frame, bbox)
            events.append(event)

            # Wrong-way movement: crossing against the expected C1 flow.
            if person_dir != CPPLUS_EXPECTED_FLOW:
                signal_events.append(_build_c1_signal(
                    cam_name, "wrong_way",
                    {"observed": person_dir, "expected": CPPLUS_EXPECTED_FLOW},
                    now, event_id=event_id,
                ))

            logger.info("%s: %s person #%d at %s — %s attire — Day IN=%d OUT=%d",
                        cam_name, person_dir, cr["id"], ts, attire_color, daily_in, daily_out)

        # Queue / congestion: sustained simultaneous occupancy in the frame.
        active_ids = [
            oid for oid in tracker.objects
            if tracker.disappeared.get(oid, 0) == 0
        ]
        occupancy = len(active_ids)
        if (
            occupancy >= CPPLUS_QUEUE_THRESHOLD
            and time.monotonic() - last_queue_signal >= CPPLUS_QUEUE_MIN_INTERVAL_SEC
        ):
            last_queue_signal = time.monotonic()
            signal_events.append(_build_c1_signal(
                cam_name, "queue",
                {"occupancy": occupancy, "threshold": CPPLUS_QUEUE_THRESHOLD},
                now,
            ))

        # Loitering / dwell: a person lingering longer than the threshold.
        for oid, dwell in dwell_tracker.update(active_ids, time.monotonic()):
            signal_events.append(_build_c1_signal(
                cam_name, "loitering",
                {
                    "tracker_id": oid,
                    "dwell_sec": round(dwell, 1),
                    "threshold_sec": CPPLUS_LOITER_SECONDS,
                },
                now,
            ))

        # Vehicle classification / count / dwell (opt-in).
        if CPPLUS_VEHICLE_ENABLED and vehicle_tracker is not None:
            try:
                v_detections = detector.detect_vehicles(frame)
            except Exception as exc:
                logger.error("CP Plus %s: vehicle detection error: %s", cam_name, exc)
                v_detections = []
            v_type_map = {d[0]: d[2] for d in v_detections}
            vehicle_tracker.update([(d[0], d[1]) for d in v_detections])
            v_active = [
                oid for oid in vehicle_tracker.objects
                if vehicle_tracker.disappeared.get(oid, 0) == 0
            ]
            for oid in v_active:
                if oid in seen_vehicle_ids:
                    continue
                seen_vehicle_ids.add(oid)
                v_type = v_type_map.get(vehicle_tracker.bboxes.get(oid), "vehicle")
                signal_events.append(_build_c1_signal(
                    cam_name, "vehicle",
                    {"tracker_id": oid, "vehicle_type": v_type},
                    now,
                ))
            if vehicle_dwell is not None:
                for oid, dwell in vehicle_dwell.update(v_active, time.monotonic()):
                    v_type = v_type_map.get(
                        vehicle_tracker.bboxes.get(oid), "vehicle",
                    )
                    signal_events.append(_build_c1_signal(
                        cam_name, "vehicle_dwell",
                        {
                            "tracker_id": oid,
                            "vehicle_type": v_type,
                            "dwell_sec": round(dwell, 1),
                            "threshold_sec": CPPLUS_VEHICLE_DWELL_SECONDS,
                        },
                        now,
                    ))

        if audit_events and crossing_audit_active:
            try:
                _append_cpplus_crossing_audit(audit_events)
            except OSError as exc:
                logger.warning("Could not append C1 crossing audit: %s", exc)
        if events:
            send_gate_event(events)
        if signal_events:
            send_c1_signal_events(signal_events)

        # Throttle to the target processing rate.
        elapsed = time.monotonic() - loop_start
        if min_interval > elapsed:
            time.sleep(min_interval - elapsed)

    if cap is not None:
        cap.release()
    logger.info("CP Plus worker stopped for %s (final IN=%d OUT=%d)",
                cam_name, daily_in, daily_out)


def run_cpplus_after_hours_worker(cam: dict) -> None:
    """Watch the C1 camera OUTSIDE monitoring hours for any person activity.

    The main worker sleeps outside 06:00-17:00 IST, so after-hours motion is
    otherwise never seen. This low-rate loop only runs when monitoring is
    closed and emits an anonymous, non-additive ``after_hours`` signal when a
    person is detected (no images/faces, never touches official totals).
    """
    cam_name = cam["name"]
    detector: PersonDetector | None = None
    last_signal = 0.0
    logger.info("CP Plus after-hours worker started for %s", cam_name)

    while running:
        if is_monitoring_time():
            time.sleep(CPPLUS_AFTER_HOURS_POLL_SEC)
            continue

        frame = capture_cpplus_frame(cam)
        if frame is None:
            time.sleep(CPPLUS_AFTER_HOURS_POLL_SEC)
            continue

        if detector is None:
            detector = PersonDetector()
            detector.load()
        try:
            detections = detector.detect(
                frame, confidence_threshold=CPPLUS_CONFIDENCE_THRESHOLD,
            )
        except Exception as exc:
            logger.error("CP Plus %s after-hours detection error: %s", cam_name, exc)
            detections = []

        now = time.monotonic()
        if detections and now - last_signal >= CPPLUS_AFTER_HOURS_POLL_SEC:
            last_signal = now
            send_c1_signal_events([_build_c1_signal(
                cam_name, "after_hours",
                {"person_count": len(detections)},
                datetime.now(IST),
            )])
            logger.info(
                "%s: after-hours activity — %d person(s) detected",
                cam_name, len(detections),
            )
        time.sleep(CPPLUS_AFTER_HOURS_POLL_SEC)

    logger.info("CP Plus after-hours worker stopped for %s", cam_name)


def _local_recording_name(start: datetime, end: datetime) -> str:
    fmt = "%Y%m%dT%H%M%S%f"
    return f"cpplus_{start.strftime(fmt)}__{end.strftime(fmt)}.mp4"


def _parse_local_recording_path(path: Path) -> tuple[datetime, datetime] | None:
    name = path.name
    if not name.startswith("cpplus_") or not name.endswith(".mp4"):
        return None
    try:
        start_text, end_text = name[len("cpplus_"):-len(".mp4")].split("__", 1)
        fmt = "%Y%m%dT%H%M%S%f"
        return (
            datetime.strptime(start_text, fmt).replace(tzinfo=IST),
            datetime.strptime(end_text, fmt).replace(tzinfo=IST),
        )
    except (ValueError, TypeError):
        return None


def _cleanup_cpplus_local_recordings(now: datetime) -> None:
    CPPLUS_LOCAL_RECORDING_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = now.timestamp() - CPPLUS_LOCAL_RETENTION_DAYS * 24 * 60 * 60
    for path in CPPLUS_LOCAL_RECORDING_DIR.glob("cpplus_*.mp4"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            logger.warning("Could not clean CP Plus recording %s", path.name)
    for path in CPPLUS_LOCAL_RECORDING_DIR.glob("*.part.mp4"):
        try:
            path.unlink()
        except OSError:
            pass


def _local_recording_segments_for_hour(
    hour_start: datetime, hour_end: datetime,
) -> list[tuple[datetime, datetime, Path]]:
    segments = []
    for path in CPPLUS_LOCAL_RECORDING_DIR.glob("cpplus_*__*.mp4"):
        interval = _parse_local_recording_path(path)
        if interval is None:
            continue
        start, end = interval
        if end > hour_start and start < hour_end:
            segments.append((start, end, path))
    return sorted(segments, key=lambda item: item[0])


def _local_recordings_for_hour(
    hour_start: datetime, hour_end: datetime,
) -> list[Path] | None:
    segments = _local_recording_segments_for_hour(hour_start, hour_end)
    if not segments:
        return None

    tolerance = timedelta(seconds=CPPLUS_LOCAL_COVERAGE_TOLERANCE_SECONDS)
    covered_until = hour_start
    selected: list[Path] = []
    for start, end, path in segments:
        if end <= covered_until:
            continue
        if start > covered_until + tolerance:
            return None
        selected.append(path)
        covered_until = max(covered_until, end)
        if covered_until >= hour_end - tolerance:
            return selected
    return None


def _finalize_cpplus_local_segment(
    writer, part_path: Path | None, start: datetime | None, end: datetime,
) -> None:
    if writer is not None:
        writer.release()
    if part_path is None or start is None or not part_path.exists():
        return
    try:
        if part_path.stat().st_size <= 1024:
            part_path.unlink(missing_ok=True)
            return
        final_path = CPPLUS_LOCAL_RECORDING_DIR / _local_recording_name(start, end)
        part_path.replace(final_path)
        logger.info(
            "CP Plus local recording saved %s-%s (%d bytes)",
            start.strftime("%H:%M:%S"), end.strftime("%H:%M:%S"),
            final_path.stat().st_size,
        )
    except OSError as exc:
        logger.warning("Could not finalize CP Plus local recording: %s", exc)


def run_cpplus_local_recorder(cam: dict) -> None:
    CPPLUS_LOCAL_RECORDING_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_cpplus_local_recordings(datetime.now(IST))
    cap = None
    writer = None
    part_path: Path | None = None
    segment_start: datetime | None = None
    segment_end: datetime | None = None
    next_write_at = 0.0
    next_disk_check_at = 0.0
    disk_has_space = True
    logger.info(
        "CP Plus local recorder started (%.1f FPS, %d-minute segments, %d-day retention)",
        CPPLUS_LOCAL_RECORDING_FPS, CPPLUS_LOCAL_SEGMENT_MINUTES,
        CPPLUS_LOCAL_RETENTION_DAYS,
    )

    try:
        while running:
            now = datetime.now(IST)
            if not is_monitoring_time():
                _finalize_cpplus_local_segment(writer, part_path, segment_start, now)
                writer = None
                part_path = None
                segment_start = None
                segment_end = None
                if cap is not None:
                    cap.release()
                    cap = None
                _cleanup_cpplus_local_recordings(now)
                time.sleep(30)
                continue

            monotonic_now = time.monotonic()
            if monotonic_now >= next_disk_check_at:
                disk_has_space = shutil.disk_usage(
                    CPPLUS_LOCAL_RECORDING_DIR,
                ).free >= CPPLUS_LOCAL_MIN_FREE_GB * 1024 ** 3
                next_disk_check_at = monotonic_now + 60
            if not disk_has_space:
                _finalize_cpplus_local_segment(
                    writer, part_path, segment_start, now,
                )
                writer = None
                part_path = None
                segment_start = None
                segment_end = None
                logger.error(
                    "CP Plus local recording paused: less than %.1f GB free",
                    CPPLUS_LOCAL_MIN_FREE_GB,
                )
                _cleanup_cpplus_local_recordings(now)
                time.sleep(60)
                continue

            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                cap = open_cpplus_stream(cam)
                if cap is None:
                    time.sleep(2)
                    continue

            ok, frame = cap.read()
            if not ok or frame is None:
                _finalize_cpplus_local_segment(
                    writer, part_path, segment_start, datetime.now(IST),
                )
                writer = None
                part_path = None
                segment_start = None
                segment_end = None
                cap.release()
                cap = None
                continue

            now = datetime.now(IST)
            if segment_end is not None and now >= segment_end:
                _finalize_cpplus_local_segment(
                    writer, part_path, segment_start, segment_end,
                )
                writer = None
                part_path = None
                segment_start = None
                segment_end = None

            if writer is None:
                minute = (
                    now.minute // CPPLUS_LOCAL_SEGMENT_MINUTES
                ) * CPPLUS_LOCAL_SEGMENT_MINUTES
                boundary = now.replace(minute=minute, second=0, microsecond=0)
                segment_end = boundary + timedelta(minutes=CPPLUS_LOCAL_SEGMENT_MINUTES)
                segment_start = now
                part_path = CPPLUS_LOCAL_RECORDING_DIR / (
                    f"cpplus_{now.strftime('%Y%m%dT%H%M%S%f')}.part.mp4"
                )
                height, width = frame.shape[:2]
                writer = cv2.VideoWriter(
                    str(part_path), cv2.VideoWriter_fourcc(*"mp4v"),
                    CPPLUS_LOCAL_RECORDING_FPS, (width, height),
                )
                if not writer.isOpened():
                    logger.error("CP Plus local recorder could not open MP4 writer")
                    writer.release()
                    writer = None
                    part_path.unlink(missing_ok=True)
                    part_path = None
                    time.sleep(10)
                    continue
                next_write_at = time.monotonic()

            monotonic_now = time.monotonic()
            if monotonic_now >= next_write_at:
                writer.write(frame)
                next_write_at = monotonic_now + 1.0 / CPPLUS_LOCAL_RECORDING_FPS
    except Exception as exc:
        logger.exception("CP Plus local recorder failed: %s", exc)
    finally:
        _finalize_cpplus_local_segment(
            writer, part_path, segment_start, datetime.now(IST),
        )
        if cap is not None:
            cap.release()
        logger.info("CP Plus local recorder stopped")


def _cpplus_rpc_login(
    client: httpx.Client, base_url: str, user: str, password: str,
) -> str | None:
    challenge = client.post(
        f"{base_url}/RPC2_Login",
        json={
            "method": "global.login",
            "params": {
                "userName": user,
                "password": "",
                "clientType": "Web3.0",
                "loginType": "Direct",
            },
            "id": 1,
        },
    )
    if challenge.status_code != 200:
        return None
    try:
        challenge_data = challenge.json()
    except ValueError:
        return None
    params = challenge_data.get("params") or {}
    session = challenge_data.get("session")
    realm = params.get("realm")
    random = params.get("random")
    if session is None or not realm or not random:
        return None

    password_hash = hashlib.md5(
        f"{user}:{realm}:{password}".encode("utf-8")
    ).hexdigest().upper()
    response_hash = hashlib.md5(
        f"{user}:{random}:{password_hash}".encode("utf-8")
    ).hexdigest().upper()
    response = client.post(
        f"{base_url}/RPC2_Login",
        json={
            "method": "global.login",
            "params": {
                "userName": user,
                "password": response_hash,
                "clientType": "Web3.0",
                "loginType": "Direct",
                "authorityType": "Default",
                "passwordType": "Default",
                "realm": realm,
                "random": random,
            },
            "id": 2,
            "session": session,
        },
        headers={"Cookie": f"DhWebClientSessionID={session}"},
    )
    if response.status_code != 200:
        return None
    try:
        response_data = response.json()
    except ValueError:
        return None
    if response_data.get("result") is not True:
        return None
    return str(response_data.get("session", session))


def _cpplus_rpc_call(
    client: httpx.Client,
    base_url: str,
    session: str,
    method: str,
    params: dict | None = None,
    object_id: object | None = None,
) -> object | None:
    payload: dict[str, object] = {
        "method": method,
        "params": params,
        "id": 3,
        "session": session,
    }
    if object_id is not None:
        payload["object"] = object_id
    response = client.post(
        f"{base_url}/RPC2",
        json=payload,
        headers={"Cookie": f"DhWebClientSessionID={session}"},
    )
    if response.status_code != 200:
        return None
    try:
        response_data = response.json()
    except ValueError:
        return None
    result = response_data.get("result")
    if result is False or result is None:
        return None
    response_params = response_data.get("params")
    if response_params:
        return response_params
    return result


def _cpplus_native_hourly_count(
    client: httpx.Client,
    base_url: str,
    session: str,
    hour_start: datetime,
    hour_end: datetime,
) -> int | None:
    server = _cpplus_rpc_call(
        client,
        base_url,
        session,
        "videoStatServer.factory.instance",
        {"channel": 0},
    )
    if isinstance(server, dict):
        server = server.get("instanceID") or server.get("object")
    if server is None:
        return None

    token: object | None = None
    try:
        search = _cpplus_rpc_call(
            client,
            base_url,
            session,
            "videoStatServer.startFind",
            {
                "condition": {
                    "StartTime": hour_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "EndTime": hour_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "Granularity": "Hour",
                    "RuleType": "NumberStat",
                    "PtzPresetId": 0,
                    "AreaID": [1],
                },
            },
            server,
        )
        if not isinstance(search, dict):
            return None
        token = search.get("token")
        total_count = search.get("totalCount")
        if token is None or not isinstance(total_count, int) or total_count <= 0:
            return None

        results = _cpplus_rpc_call(
            client,
            base_url,
            session,
            "videoStatServer.doFind",
            {"token": token, "beginNumber": 0, "count": total_count},
            server,
        )
        infos = results.get("info", []) if isinstance(results, dict) else []
        if isinstance(infos, dict):
            infos = [infos]
        counts = [
            info.get("EnteredSubtotal")
            for info in infos
            if isinstance(info, dict)
            and info.get("RuleName", "NumberStat") == "NumberStat"
            and info.get("StartTime") == hour_start.strftime("%Y-%m-%d %H:%M:%S")
        ]
        if not counts or any(not isinstance(count, int) for count in counts):
            return None
        return sum(counts)
    finally:
        if token is not None:
            _cpplus_rpc_call(
                client,
                base_url,
                session,
                "videoStatServer.stopFind",
                {"token": token},
                server,
            )


def _fetch_cpplus_native_hourly_count(
    cam: dict,
    hour_start: datetime,
    hour_end: datetime,
) -> int | None:
    password = _resolve_cpplus_password(cam)
    if not password:
        return None
    base_url = f"http://{cam['ip']}:{CPPLUS_RECORDING_PORT}"
    timeout = httpx.Timeout(15.0, connect=CPPLUS_CONNECT_TIMEOUT_SEC)
    try:
        with httpx.Client(timeout=timeout) as client:
            session = _cpplus_rpc_login(
                client, base_url, str(cam.get("user", "admin")), password,
            )
            if session is None:
                return None
            try:
                return _cpplus_native_hourly_count(
                    client, base_url, session, hour_start, hour_end,
                )
            finally:
                _cpplus_rpc_call(
                    client, base_url, session, "global.logout", {},
                )
    except httpx.HTTPError as exc:
        logger.warning("CP Plus native people-count query failed: %s", exc)
        return None


def _parse_cpplus_native_summary(
    response_text: str,
) -> tuple[int, int, int | None] | None:
    values = {}
    for line in response_text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    if values.get("summary.RuleName") != "NumberStat":
        return None
    try:
        entered_hour = values.get("summary.EnteredSubtotal.Hour")
        return (
            int(values["summary.EnteredSubtotal.Today"]),
            int(values["summary.ExitedSubtotal.Today"]),
            int(entered_hour) if entered_hour is not None else None,
        )
    except (KeyError, ValueError):
        return None


def _fetch_cpplus_native_summary(
    cam: dict,
) -> tuple[int, int, int | None] | None:
    password = _resolve_cpplus_password(cam)
    if not password:
        return None
    base_url = f"http://{cam['ip']}:{CPPLUS_RECORDING_PORT}"
    users = list(dict.fromkeys((str(cam.get("user", "admin")), *CPPLUS_USER_ALTERNATES)))
    timeout = httpx.Timeout(CPPLUS_HTTP_TIMEOUT_SEC, connect=CPPLUS_CONNECT_TIMEOUT_SEC)
    try:
        with httpx.Client(timeout=timeout) as client:
            for user in users:
                for auth in (
                    httpx.DigestAuth(user, password),
                    httpx.BasicAuth(user, password),
                ):
                    response = client.get(
                        f"{base_url}/cgi-bin/videoStatServer.cgi",
                        params={"action": "getSummary", "channel": "0"},
                        auth=auth,
                    )
                    if response.status_code != 200:
                        continue
                    summary = _parse_cpplus_native_summary(response.text)
                    if summary is not None:
                        cam["user"] = user
                        return summary
    except httpx.HTTPError as exc:
        logger.warning("CP Plus native summary query failed: %s", exc)
    return None


def _load_cpplus_native_summary_state() -> dict:
    try:
        state = json.loads(
            CPPLUS_NATIVE_SUMMARY_STATE_FILE.read_text(encoding="utf-8")
        )
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cpplus_native_summary_state(state: dict) -> None:
    temp_path = CPPLUS_NATIVE_SUMMARY_STATE_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temp_path.replace(CPPLUS_NATIVE_SUMMARY_STATE_FILE)


def _load_cpplus_native_summary_pending() -> list[dict]:
    try:
        pending = json.loads(
            CPPLUS_NATIVE_SUMMARY_PENDING_FILE.read_text(encoding="utf-8")
        )
        if not isinstance(pending, list):
            return []
        return [item for item in pending if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def _save_cpplus_native_summary_pending(pending: list[dict]) -> None:
    temp_path = CPPLUS_NATIVE_SUMMARY_PENDING_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(pending, sort_keys=True), encoding="utf-8")
    temp_path.replace(CPPLUS_NATIVE_SUMMARY_PENDING_FILE)


def _queue_cpplus_native_pending(
    pending: list[dict], hour_start: datetime, hour_end: datetime, in_count: int,
) -> list[dict]:
    """Record a completed hour that still needs uploading, de-duplicated by
    hour_start so retries stay idempotent against the backend upsert."""
    key = hour_start.strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "hour_start": key,
        "hour_end": hour_end.strftime("%Y-%m-%d %H:%M:%S"),
        "in_count": in_count,
    }
    updated = [item for item in pending if item.get("hour_start") != key]
    updated.append(entry)
    return updated


def _flush_cpplus_native_pending(pending: list[dict]) -> list[dict]:
    """Retry every queued completed hour; keep only the ones that still fail."""
    remaining = []
    for item in pending:
        try:
            hour_start = datetime.strptime(
                item["hour_start"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=IST)
            hour_end = datetime.strptime(
                item["hour_end"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=IST)
            in_count = int(item["in_count"])
        except (KeyError, TypeError, ValueError):
            continue
        uploaded = _post_cpplus_recount(
            hour_start, hour_end, in_count, 0, "camera_native_counter",
        )
        logger.info(
            "CP Plus live native count %s-%s: IN=%d uploaded=%s",
            hour_start.strftime("%H:%M"),
            hour_end.strftime("%H:%M"),
            in_count,
            uploaded,
        )
        if not uploaded:
            remaining.append(item)
    return remaining


def _cpplus_native_summary_transition(
    state: dict,
    now: datetime,
    entered_today: int,
    entered_hour: int | None = None,
) -> tuple[dict, tuple[datetime, datetime, int] | None]:
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    current_key = current_hour.strftime("%Y-%m-%d %H:%M:%S")
    date = now.strftime("%Y-%m-%d")
    within_boundary = now.minute == 0 and now.second <= 10

    valid_hour_subtotal = (
        entered_hour is not None
        and 0 <= entered_hour <= entered_today
    )
    # The camera's current-hour subtotal gives the exact cumulative value at the
    # hour boundary even when the first successful poll arrives late.
    hour_baseline = (
        entered_today - entered_hour
        if valid_hour_subtotal and entered_hour is not None
        else entered_today
    )

    if state.get("date") == date and state.get("hour_start") == current_key:
        updated = dict(state)
        updated["entered_end"] = entered_today
        if valid_hour_subtotal:
            updated["entered_today"] = hour_baseline
            updated["complete"] = True
        return (state if updated == state else updated), None

    completed = None
    previous_key = state.get("hour_start")
    if state.get("date") == date and isinstance(previous_key, str):
        try:
            previous_hour = datetime.strptime(
                previous_key, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=IST)
            baseline = int(state["entered_today"])
        except (KeyError, TypeError, ValueError):
            previous_hour = None
            baseline = 0
        exact_end = valid_hour_subtotal or within_boundary
        end_value = hour_baseline if valid_hour_subtotal else entered_today
        if (
            previous_hour is not None
            and state.get("complete") is True
            and current_hour - previous_hour == timedelta(hours=1)
            and exact_end
            and end_value >= baseline
        ):
            completed = (
                previous_hour,
                current_hour,
                end_value - baseline,
            )

    next_state = {
        "date": date,
        "hour_start": current_key,
        "entered_today": hour_baseline,
        "entered_end": entered_today,
        "complete": valid_hour_subtotal or within_boundary,
    }
    return next_state, completed


def run_cpplus_native_summary_worker(cam: dict) -> None:
    state = _load_cpplus_native_summary_state()
    pending = _load_cpplus_native_summary_pending()
    logger.info("CP Plus live native-summary worker started")
    while running:
        now = datetime.now(IST)
        summary = _fetch_cpplus_native_summary(cam)
        if summary is not None:
            next_state, completed = _cpplus_native_summary_transition(
                state, now, summary[0], summary[2],
            )
            if completed is not None:
                hour_start, hour_end, in_count = completed
                pending = _queue_cpplus_native_pending(
                    pending, hour_start, hour_end, in_count,
                )
                # Persist the completed interval before advancing state. A crash
                # between these writes can only re-queue the same idempotent hour.
                _save_cpplus_native_summary_pending(pending)
            if next_state != state:
                state = next_state
                _save_cpplus_native_summary_state(state)
        # Always attempt to drain any completed hour that has not been accepted
        # yet, so a transient upload failure is retried instead of lost.
        if pending:
            remaining = _flush_cpplus_native_pending(pending)
            if remaining != pending:
                pending = remaining
                _save_cpplus_native_summary_pending(pending)
        time.sleep(CPPLUS_NATIVE_SUMMARY_POLL_SECONDS)


def _find_cpplus_rpc_recording_paths(
    client: httpx.Client,
    base_url: str,
    session: str,
    channel: int,
    hour_start: datetime,
    hour_end: datetime,
) -> list[str]:
    finder = _cpplus_rpc_call(
        client, base_url, session, "mediaFileFind.factory.create",
    )
    if isinstance(finder, dict):
        finder = finder.get("instanceID") or finder.get("object")
    if finder is None:
        return []

    try:
        started = _cpplus_rpc_call(
            client,
            base_url,
            session,
            "mediaFileFind.findFile",
            {
                "condition": {
                    "Channel": channel,
                    "Types": ["dav", "mp4"],
                    "Order": "Ascent",
                    "Flags": ["Timing", "Event", "Manual", "Marker"],
                    "StartTime": hour_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "EndTime": hour_end.strftime("%Y-%m-%d %H:%M:%S"),
                },
            },
            finder,
        )
        if started is None:
            return []

        paths: list[str] = []
        for _ in range(20):
            batch = _cpplus_rpc_call(
                client,
                base_url,
                session,
                "mediaFileFind.findNextFile",
                {"count": 100},
                finder,
            )
            infos = batch.get("infos", []) if isinstance(batch, dict) else []
            paths.extend(
                str(info["FilePath"]) for info in infos if info.get("FilePath")
            )
            if len(infos) < 100:
                break
        return list(dict.fromkeys(paths))
    finally:
        _cpplus_rpc_call(
            client, base_url, session, "mediaFileFind.close", {}, finder,
        )
        _cpplus_rpc_call(
            client, base_url, session, "mediaFileFind.destroy", {}, finder,
        )


def _download_cpplus_rpc_file(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    target: Path,
    keepalive: Callable[[], object | None] | None = None,
) -> bool:
    target.unlink(missing_ok=True)
    expected_total: int | None = None
    stalled_attempts = 0
    chunk_size = 8 * 1024 * 1024

    for _ in range(512):
        if keepalive is not None:
            keepalive()
        offset = target.stat().st_size if target.exists() else 0
        request_offset = offset
        request_headers = {
            **headers,
            "Range": f"bytes={offset}-{offset + chunk_size - 1}",
        }
        response_completed = False
        try:
            with client.stream("GET", url, headers=request_headers) as response:
                if response.status_code == 416 and offset > 1024:
                    return True
                if response.status_code not in (200, 206):
                    target.unlink(missing_ok=True)
                    return False

                append = response.status_code == 206 and offset > 0
                if not append:
                    offset = 0
                content_range = response.headers.get("content-range", "")
                _, separator, total_text = content_range.rpartition("/")
                if separator and total_text.isdigit():
                    expected_total = int(total_text)
                elif response.status_code == 200:
                    content_length = response.headers.get("content-length", "")
                    if content_length.isdigit():
                        expected_total = int(content_length)

                with target.open("ab" if append else "wb") as recording:
                    for chunk in response.iter_bytes():
                        recording.write(chunk)
                response_completed = True
        except httpx.HTTPError:
            pass

        downloaded_size = target.stat().st_size if target.exists() else 0
        if expected_total is not None and downloaded_size >= expected_total:
            return downloaded_size > 1024
        if response_completed and expected_total is None:
            return downloaded_size > 1024
        if downloaded_size > request_offset:
            stalled_attempts = 0
        else:
            stalled_attempts += 1
            if stalled_attempts >= 3:
                target.unlink(missing_ok=True)
                return False

    target.unlink(missing_ok=True)
    return False


def _cpplus_recording_path_interval(
    camera_path: str,
    recording_date: datetime,
) -> tuple[datetime, datetime] | None:
    time_range = Path(camera_path).name.split("[", 1)[0]
    parts = time_range.split("-", 1)
    if len(parts) != 2:
        return None
    try:
        start_time = datetime.strptime(parts[0], "%H.%M.%S").time()
        end_time = datetime.strptime(parts[1], "%H.%M.%S").time()
    except ValueError:
        return None
    start = datetime.combine(
        recording_date.date(), start_time, tzinfo=recording_date.tzinfo,
    )
    end = datetime.combine(
        recording_date.date(), end_time, tzinfo=recording_date.tzinfo,
    )
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _cpplus_recordings_cover_hour(
    camera_paths: list[str],
    hour_start: datetime,
    hour_end: datetime,
) -> bool:
    intervals = [
        interval
        for camera_path in camera_paths
        if (interval := _cpplus_recording_path_interval(
            camera_path, hour_start,
        )) is not None
    ]
    if len(intervals) != len(camera_paths):
        return False
    cursor = hour_start
    tolerance = timedelta(seconds=CPPLUS_LOCAL_COVERAGE_TOLERANCE_SECONDS)
    for start, end in sorted(intervals):
        if end <= cursor:
            continue
        if start > cursor + tolerance:
            return False
        cursor = max(cursor, end)
        if cursor >= hour_end - tolerance:
            return True
    return False


def _download_cpplus_rpc_recordings(
    client: httpx.Client,
    base_url: str,
    user: str,
    password: str,
    channels: list[int],
    hour_start: datetime,
    hour_end: datetime,
    output_path: Path,
) -> list[Path] | None:
    session = _cpplus_rpc_login(client, base_url, user, password)
    if session is None:
        return None

    headers = {"Cookie": f"DhWebClientSessionID={session}"}
    try:
        for channel in channels:
            paths = _find_cpplus_rpc_recording_paths(
                client, base_url, session, channel, hour_start, hour_end,
            )
            if not paths:
                continue
            downloaded: list[Path] = []
            downloaded_camera_paths: list[str] = []
            for index, camera_path in enumerate(paths):
                suffix = Path(camera_path).suffix or ".dav"
                target = output_path.with_name(
                    f"{output_path.stem}_{index:03d}{suffix}"
                )
                encoded_path = quote(camera_path, safe="/[]@()._-")
                for loadfile_path in ("/RPC_Loadfile", "/RPC2_Loadfile"):
                    if _download_cpplus_rpc_file(
                        client,
                        f"{base_url}{loadfile_path}{encoded_path}",
                        headers,
                        target,
                        keepalive=lambda: _cpplus_rpc_call(
                            client,
                            base_url,
                            session,
                            "global.keepAlive",
                            {"timeout": 300, "active": True},
                        ),
                    ):
                        downloaded.append(target)
                        downloaded_camera_paths.append(camera_path)
                        break
            if downloaded and _cpplus_recordings_cover_hour(
                downloaded_camera_paths, hour_start, hour_end,
            ):
                logger.info(
                    "CP Plus SD recording downloaded through camera playback "
                    "session for %s-%s (%d file(s), channel %d)",
                    hour_start.strftime("%H:%M"),
                    hour_end.strftime("%H:%M"), len(downloaded), channel,
                )
                return downloaded
            for downloaded_path in downloaded:
                downloaded_path.unlink(missing_ok=True)
            if downloaded:
                logger.warning(
                    "CP Plus camera playback files did not cover the complete "
                    "hour %s-%s; trying fallback recording",
                    hour_start.strftime("%H:%M"),
                    hour_end.strftime("%H:%M"),
                )
        return None
    finally:
        try:
            _cpplus_rpc_call(
                client, base_url, session, "global.logout", {},
            )
        except httpx.HTTPError:
            pass


def _parse_cpplus_recording_paths(response_text: str) -> list[str]:
    items: dict[int, dict[str, str]] = {}
    for line in response_text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key.startswith("items["):
            continue
        index_end = key.find("]")
        if index_end < 7 or "." not in key[index_end:]:
            continue
        try:
            index = int(key[6:index_end])
        except ValueError:
            continue
        field = key[index_end + 2:]
        items.setdefault(index, {})[field] = value.strip()
    return [
        item["FilePath"] for _, item in sorted(items.items())
        if item.get("FilePath")
    ]


def _find_cpplus_recording_paths(
    client: httpx.Client,
    base_url: str,
    auth: httpx.Auth,
    channel: int,
    hour_start: datetime,
    hour_end: datetime,
) -> list[str]:
    finder_id = ""
    try:
        response = client.get(
            f"{base_url}/cgi-bin/mediaFileFind.cgi",
            params={"action": "factory.create"},
            auth=auth,
        )
        if response.status_code != 200 or "=" not in response.text:
            return []
        finder_id = response.text.strip().splitlines()[0].partition("=")[2].strip()
        if not finder_id:
            return []
        response = client.get(
            f"{base_url}/cgi-bin/mediaFileFind.cgi",
            params={
                "action": "findFile",
                "object": finder_id,
                "condition.Channel": str(channel),
                "condition.StartTime": hour_start.strftime("%Y-%m-%d %H:%M:%S"),
                "condition.EndTime": hour_end.strftime("%Y-%m-%d %H:%M:%S"),
                "condition.Types[0]": "dav",
                "condition.Types[1]": "mp4",
            },
            auth=auth,
        )
        if response.status_code != 200 or not response.text.strip().startswith("OK"):
            return []

        paths: list[str] = []
        for _ in range(20):
            response = client.get(
                f"{base_url}/cgi-bin/mediaFileFind.cgi",
                params={
                    "action": "findNextFile",
                    "object": finder_id,
                    "count": "100",
                },
                auth=auth,
            )
            if response.status_code != 200:
                break
            batch = _parse_cpplus_recording_paths(response.text)
            paths.extend(batch)
            first_line = response.text.strip().splitlines()[0] if response.text.strip() else ""
            try:
                found = int(first_line.partition("=")[2])
            except ValueError:
                found = len(batch)
            if found < 100:
                break
        return list(dict.fromkeys(paths))
    finally:
        if finder_id:
            for action in ("close", "destroy"):
                try:
                    client.get(
                        f"{base_url}/cgi-bin/mediaFileFind.cgi",
                        params={"action": action, "object": finder_id},
                        auth=auth,
                    )
                except httpx.HTTPError:
                    pass


def _download_cpplus_recording(
    cam: dict, hour_start: datetime, hour_end: datetime, output_path: Path,
) -> list[Path] | None:
    password = _resolve_cpplus_password(cam)
    if not password:
        logger.warning("CP Plus recording replay: no camera password available")
        return None

    configured_user = cam.get("user", "admin")
    users = [configured_user] + [
        user for user in CPPLUS_USER_ALTERNATES if user != configured_user
    ]
    channels = list(dict.fromkeys((CPPLUS_RECORDING_CHANNEL, max(0, CPPLUS_RECORDING_CHANNEL - 1))))
    base_url = f"http://{cam['ip']}:{CPPLUS_RECORDING_PORT}"
    timeout = httpx.Timeout(180.0, connect=CPPLUS_CONNECT_TIMEOUT_SEC)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for user in users:
                try:
                    downloaded = _download_cpplus_rpc_recordings(
                        client,
                        base_url,
                        user,
                        password,
                        channels,
                        hour_start,
                        hour_end,
                        output_path,
                    )
                except (httpx.HTTPError, OSError) as exc:
                    logger.warning(
                        "CP Plus camera playback session failed for %s-%s: %s",
                        hour_start.strftime("%H:%M"),
                        hour_end.strftime("%H:%M"), exc,
                    )
                    downloaded = None
                if downloaded:
                    cam["user"] = user
                    return downloaded

                for auth in (
                    httpx.DigestAuth(user, password),
                    httpx.BasicAuth(user, password),
                ):
                    for channel in channels:
                        paths = _find_cpplus_recording_paths(
                            client, base_url, auth, channel, hour_start, hour_end,
                        )
                        if not paths:
                            continue
                        downloaded: list[Path] = []
                        for index, camera_path in enumerate(paths):
                            suffix = Path(camera_path).suffix or ".dav"
                            target = output_path.with_name(
                                f"{output_path.stem}_{index:03d}{suffix}"
                            )
                            encoded_path = quote(camera_path, safe="/[]@()._-")
                            url = f"{base_url}/cgi-bin/RPC_Loadfile{encoded_path}"
                            with client.stream("GET", url, auth=auth) as response:
                                if response.status_code != 200:
                                    continue
                                size = 0
                                with target.open("wb") as recording:
                                    for chunk in response.iter_bytes():
                                        recording.write(chunk)
                                        size += len(chunk)
                                if size > 1024:
                                    downloaded.append(target)
                                else:
                                    target.unlink(missing_ok=True)
                        if downloaded:
                            cam["user"] = user
                            logger.info(
                                "CP Plus SD recording downloaded for %s-%s "
                                "(%d file(s), channel %d)",
                                hour_start.strftime("%H:%M"),
                                hour_end.strftime("%H:%M"), len(downloaded), channel,
                            )
                            return downloaded
    except (httpx.HTTPError, OSError) as exc:
        logger.warning(
            "CP Plus SD recording download failed for %s-%s: %s",
            hour_start.strftime("%H:%M"), hour_end.strftime("%H:%M"), exc,
        )
    return None


def _build_cpplus_replay_tracker() -> CentroidTracker:
    return CentroidTracker(
        max_disappeared=max(
            MAX_DISAPPEARED,
            int(CPPLUS_REPLAY_SAMPLE_FPS * CPPLUS_TRACK_MAX_GAP_SECONDS),
        ),
        max_distance=MAX_DISTANCE,
        anchor_y="bottom",
        line_axis=CPPLUS_LINE_AXIS,
    )


def _count_cpplus_recording_paths_with_tracker(
    recording_paths: list[Path],
    cam: dict,
    detector: "PersonDetector",
    tracker: CentroidTracker,
    recording_windows: dict[str, tuple[float, float]] | None = None,
) -> tuple[int, int] | None:
    in_count = 0
    processed_frames = 0

    for recording_path in recording_paths:
        cap = cv2.VideoCapture(str(recording_path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            logger.warning("CP Plus recording could not be decoded: %s", recording_path.name)
            return None
        source_fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(source_fps) or source_fps <= 0:
            source_fps = CPPLUS_LOCAL_RECORDING_FPS
        frame_stride = max(1, round(source_fps / CPPLUS_REPLAY_SAMPLE_FPS))
        frame_number = 0
        window = (recording_windows or {}).get(str(recording_path))
        try:
            while running:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frame_number += 1
                frame_seconds = frame_number / source_fps
                if window is not None and frame_seconds >= window[1]:
                    break
                count_crossings = window is None or frame_seconds >= window[0]
                if frame_number % frame_stride:
                    continue

                frame_h, frame_w = frame.shape[:2]
                line_dimension = (
                    frame_w if CPPLUS_LINE_AXIS == "vertical" else frame_h
                )
                tracker.set_line(
                    int(line_dimension * (cam.get("line_position") or LINE_POSITION)),
                    hysteresis=int(line_dimension * CPPLUS_LINE_HYSTERESIS),
                )
                tracker.max_distance = max(
                    MAX_DISTANCE, frame_w * MAX_DISTANCE / 640.0,
                )
                detections = detector.detect(
                    frame,
                    confidence_threshold=CPPLUS_CONFIDENCE_THRESHOLD,
                    image_size=CPPLUS_REPLAY_IMAGE_SIZE,
                )
                processed_frames += 1
                for crossing in tracker.update(detections):
                    raw_direction = crossing["direction"]
                    positive_direction_is_in = (
                        CPPLUS_IN_LEFT_TO_RIGHT
                        if CPPLUS_LINE_AXIS == "vertical"
                        else CPPLUS_IN_TOP_TO_BOTTOM
                    )
                    direction = raw_direction if positive_direction_is_in else (
                        "OUT" if raw_direction == "IN" else "IN"
                    )
                    if direction == "IN" and count_crossings:
                        in_count += 1
        finally:
            cap.release()

    if processed_frames == 0:
        logger.warning("CP Plus recording contained no decodable frames")
        return None
    return in_count, processed_frames


def count_cpplus_recordings(
    recording_paths: list[Path], cam: dict, detector: "PersonDetector",
) -> tuple[int, int] | None:
    return _count_cpplus_recording_paths_with_tracker(
        recording_paths, cam, detector, _build_cpplus_replay_tracker(),
    )


def count_cpplus_recording(
    recording_path: Path, cam: dict, detector: "PersonDetector",
) -> tuple[int, int] | None:
    return count_cpplus_recordings([recording_path], cam, detector)


def _load_cpplus_replay_state() -> dict[str, dict]:
    try:
        data = json.loads(CPPLUS_REPLAY_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(key): value for key, value in data.items()
                if isinstance(value, dict)
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_cpplus_replay_state(state: dict[str, dict]) -> None:
    temp_path = CPPLUS_REPLAY_STATE_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(CPPLUS_REPLAY_STATE_FILE)


def _agent_secret_headers() -> dict[str, str]:
    secret = os.environ.get("AGENT_SECRET", "")
    if not secret:
        try:
            config = json.loads(
                (Path(__file__).parent / "config.json").read_text(encoding="utf-8")
            )
            if isinstance(config, dict):
                secret = str(config.get("agent_secret", ""))
        except (OSError, json.JSONDecodeError):
            pass
    return {"X-Agent-Secret": secret} if secret else {}


def _post_cpplus_recount(
    hour_start: datetime,
    hour_end: datetime,
    in_count: int,
    processed_frames: int,
    source: str,
) -> bool:
    payload = {
        "date": hour_start.strftime("%Y-%m-%d"),
        "hour_start": hour_start.strftime("%Y-%m-%d %H:%M:%S"),
        "hour_end": hour_end.strftime("%Y-%m-%d %H:%M:%S"),
        "in_count": in_count,
        "processed_frames": processed_frames,
        "source": source,
    }
    try:
        response = httpx.post(
            CPPLUS_RECOUNT_API,
            json=payload,
            headers=_agent_secret_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("CP Plus recording recount upload failed: %s", exc)
        return False


def _emit_replay_discrepancy(
    cam: dict,
    hour_start: datetime,
    hour_end: datetime,
    verified: int,
    source: str,
) -> None:
    """Emit an anonymous, verification-only live-vs-verified discrepancy.

    Never modifies official totals — the recount itself is the authoritative
    verified figure; this signal only surfaces the gap for review.
    """
    live = _get_live_hourly_in(hour_start)
    if live is None:
        return
    send_c1_signal_events([
        _build_c1_signal(
            cam["name"],
            "replay_discrepancy",
            {
                "hour_start": hour_start.strftime("%Y-%m-%d %H:%M:%S"),
                "hour_end": hour_end.strftime("%Y-%m-%d %H:%M:%S"),
                "live": live,
                "verified": verified,
                "delta": verified - live,
                "verified_source": source,
            },
            hour_end,
        )
    ])


def _completed_replay_hours(now: datetime) -> list[tuple[datetime, datetime]]:
    day_start = now.replace(
        hour=MONITOR_START_HOUR, minute=MONITOR_START_MIN, second=0, microsecond=0,
    )
    day_end = now.replace(
        hour=MONITOR_END_HOUR, minute=MONITOR_END_MIN, second=0, microsecond=0,
    )
    available_until = min(now - timedelta(minutes=CPPLUS_REPLAY_DELAY_MINUTES), day_end)
    completed_end = available_until.replace(minute=0, second=0, microsecond=0)
    hours = []
    hour_start = day_start
    while hour_start + timedelta(hours=1) <= completed_end:
        hours.append((hour_start, hour_start + timedelta(hours=1)))
        hour_start += timedelta(hours=1)
    return list(reversed(hours))


def _cpplus_native_history_grace_open(
    now: datetime, hour_end: datetime,
) -> bool:
    return now < hour_end + timedelta(
        minutes=CPPLUS_NATIVE_HISTORY_GRACE_MINUTES,
    )


def run_cpplus_replay_worker(cam: dict) -> None:
    state = _load_cpplus_replay_state()
    retry_after: dict[str, float] = {}
    detector: PersonDetector | None = None
    logger.info("CP Plus onboard-recording replay worker started")

    while running:
        now = datetime.now(IST)
        for hour_start, hour_end in _completed_replay_hours(now):
            state_key = hour_start.strftime("%Y-%m-%d %H:%M:%S")
            saved = state.get(state_key)
            if saved:
                if not saved.get("uploaded") and _post_cpplus_recount(
                    hour_start,
                    hour_end,
                    int(saved["in_count"]),
                    int(saved["processed_frames"]),
                    str(saved.get("source", "school_pc_recording")),
                ):
                    saved["uploaded"] = True
                    _save_cpplus_replay_state(state)
                continue
            if time.monotonic() < retry_after.get(state_key, 0.0):
                continue

            native_count = _fetch_cpplus_native_hourly_count(
                cam, hour_start, hour_end,
            )
            if native_count is not None:
                uploaded = _post_cpplus_recount(
                    hour_start,
                    hour_end,
                    native_count,
                    0,
                    "camera_native_counter",
                )
                logger.info(
                    "CP Plus native people count %s-%s: IN=%d uploaded=%s",
                    hour_start.strftime("%H:%M"),
                    hour_end.strftime("%H:%M"),
                    native_count,
                    uploaded,
                )
                if uploaded:
                    state[state_key] = {
                        "in_count": native_count,
                        "processed_frames": 0,
                        "uploaded": True,
                        "source": "camera_native_counter",
                    }
                    _save_cpplus_replay_state(state)
                    _emit_replay_discrepancy(
                        cam, hour_start, hour_end, native_count,
                        "camera_native_counter",
                    )
                    continue
                logger.warning(
                    "CP Plus native count was rejected; trying recording recount for %s-%s",
                    hour_start.strftime("%H:%M"),
                    hour_end.strftime("%H:%M"),
                )
            elif _cpplus_native_history_grace_open(now, hour_end):
                retry_after[state_key] = (
                    time.monotonic() + CPPLUS_NATIVE_HISTORY_RETRY_SECONDS
                )
                logger.info(
                    "CP Plus native history pending for %s-%s; retrying before replay",
                    hour_start.strftime("%H:%M"),
                    hour_end.strftime("%H:%M"),
                )
                continue

            file_handle, file_name = tempfile.mkstemp(
                prefix="cpplus_replay_", suffix=".dav",
            )
            os.close(file_handle)
            recording_path = Path(file_name)
            local_paths = _local_recordings_for_hour(hour_start, hour_end)
            downloaded_paths: list[Path] = []
            source = "camera_sd_recording"
            try:
                if CPPLUS_SD_REPLAY_ENABLED:
                    downloaded_paths = _download_cpplus_recording(
                        cam, hour_start, hour_end, recording_path,
                    ) or []
                replay_paths = downloaded_paths or local_paths
                if not replay_paths:
                    retry_after[state_key] = (
                        time.monotonic() + CPPLUS_REPLAY_RETRY_MINUTES * 60
                    )
                    logger.warning(
                        "CP Plus replay unavailable for %s-%s: camera SD playback failed "
                        "and local coverage is incomplete; live count remains in use",
                        hour_start.strftime("%H:%M"), hour_end.strftime("%H:%M"),
                    )
                    continue
                if not downloaded_paths:
                    source = "school_pc_recording"
                if detector is None:
                    detector = PersonDetector()
                    detector.load()
                result = count_cpplus_recordings(replay_paths, cam, detector)
                if result is None:
                    retry_after[state_key] = (
                        time.monotonic() + CPPLUS_REPLAY_RETRY_MINUTES * 60
                    )
                    continue
                in_count, processed_frames = result
                uploaded = _post_cpplus_recount(
                    hour_start, hour_end, in_count, processed_frames, source,
                )
                state[state_key] = {
                    "in_count": in_count,
                    "processed_frames": processed_frames,
                    "uploaded": uploaded,
                    "source": source,
                }
                _save_cpplus_replay_state(state)
                _emit_replay_discrepancy(
                    cam, hour_start, hour_end, in_count, source,
                )
                logger.info(
                    "CP Plus recording recount %s-%s: IN=%d frames=%d source=%s uploaded=%s",
                    hour_start.strftime("%H:%M"), hour_end.strftime("%H:%M"),
                    in_count, processed_frames, source, uploaded,
                )
            except Exception as exc:
                logger.exception("CP Plus recording recount failed: %s", exc)
                retry_after[state_key] = (
                    time.monotonic() + CPPLUS_REPLAY_RETRY_MINUTES * 60
                )
            finally:
                recording_path.unlink(missing_ok=True)
                for downloaded_path in downloaded_paths:
                    downloaded_path.unlink(missing_ok=True)
        time.sleep(30)

    logger.info("CP Plus onboard-recording replay worker stopped")


def _load_cpplus_segment_replay_state() -> dict[str, dict]:
    try:
        data = json.loads(
            CPPLUS_SEGMENT_REPLAY_STATE_FILE.read_text(encoding="utf-8")
        )
        if isinstance(data, dict):
            return {
                str(key): value for key, value in data.items()
                if isinstance(value, dict)
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_cpplus_segment_replay_state(state: dict[str, dict]) -> None:
    temp_path = CPPLUS_SEGMENT_REPLAY_STATE_FILE.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8",
    )
    temp_path.replace(CPPLUS_SEGMENT_REPLAY_STATE_FILE)


def _segment_replay_hours(now: datetime) -> list[tuple[datetime, datetime]]:
    day_start = now.replace(
        hour=MONITOR_START_HOUR, minute=MONITOR_START_MIN,
        second=0, microsecond=0,
    )
    day_end = now.replace(
        hour=MONITOR_END_HOUR, minute=MONITOR_END_MIN,
        second=0, microsecond=0,
    )
    if now < day_start:
        return []
    current_start = min(now, day_end - timedelta(microseconds=1)).replace(
        minute=0, second=0, microsecond=0,
    )
    current_hour = (current_start, current_start + timedelta(hours=1))
    previous_start = current_start - timedelta(hours=1)
    if previous_start >= day_start:
        return [(previous_start, current_start), current_hour]
    return [current_hour]


def run_cpplus_segment_replay_worker(cam: dict) -> None:
    state = _load_cpplus_segment_replay_state()
    accumulators: dict[str, dict] = {}
    detector: PersonDetector | None = None
    tolerance = timedelta(seconds=CPPLUS_LOCAL_COVERAGE_TOLERANCE_SECONDS)
    logger.info("CP Plus five-minute segment replay worker started")

    while running:
        now = datetime.now(IST)
        for hour_start, hour_end in _segment_replay_hours(now):
            state_key = hour_start.strftime("%Y-%m-%d %H:%M:%S")
            saved = state.get(state_key)
            if saved is not None:
                if not saved.get("uploaded") and _post_cpplus_recount(
                    hour_start,
                    hour_end,
                    int(saved["in_count"]),
                    int(saved["processed_frames"]),
                    "school_pc_segment_recording",
                ):
                    saved["uploaded"] = True
                    _save_cpplus_segment_replay_state(state)
                continue

            accumulator = accumulators.setdefault(
                state_key,
                {
                    "tracker": _build_cpplus_replay_tracker(),
                    "processed_paths": set(),
                    "covered_until": hour_start,
                    "in_count": 0,
                    "processed_frames": 0,
                },
            )
            segments = _local_recording_segments_for_hour(hour_start, hour_end)
            for start, end, path in segments:
                path_key = str(path)
                if path_key in accumulator["processed_paths"]:
                    continue
                if start > accumulator["covered_until"] + tolerance:
                    break
                if detector is None:
                    detector = PersonDetector()
                    detector.load()
                window_start = max(0.0, (hour_start - start).total_seconds())
                window_end = min(
                    (end - start).total_seconds(),
                    (hour_end - start).total_seconds(),
                )
                result = _count_cpplus_recording_paths_with_tracker(
                    [path],
                    cam,
                    detector,
                    accumulator["tracker"],
                    {str(path): (window_start, window_end)},
                )
                if result is None:
                    break
                in_count, processed_frames = result
                accumulator["in_count"] += in_count
                accumulator["processed_frames"] += processed_frames
                accumulator["processed_paths"].add(path_key)
                accumulator["covered_until"] = max(
                    accumulator["covered_until"], end,
                )
                logger.info(
                    "CP Plus in-hour segment checked %s-%s: cumulative IN=%d frames=%d",
                    start.strftime("%H:%M:%S"), end.strftime("%H:%M:%S"),
                    accumulator["in_count"], accumulator["processed_frames"],
                )

            if (
                now >= hour_end
                and accumulator["covered_until"] >= hour_end - tolerance
            ):
                uploaded = _post_cpplus_recount(
                    hour_start,
                    hour_end,
                    accumulator["in_count"],
                    accumulator["processed_frames"],
                    "school_pc_segment_recording",
                )
                state[state_key] = {
                    "in_count": accumulator["in_count"],
                    "processed_frames": accumulator["processed_frames"],
                    "uploaded": uploaded,
                }
                _save_cpplus_segment_replay_state(state)
                accumulators.pop(state_key, None)
                logger.info(
                    "CP Plus in-hour recording check %s-%s: IN=%d frames=%d uploaded=%s",
                    hour_start.strftime("%H:%M"), hour_end.strftime("%H:%M"),
                    state[state_key]["in_count"],
                    state[state_key]["processed_frames"], uploaded,
                )
        time.sleep(CPPLUS_SEGMENT_REPLAY_POLL_SECONDS)

    logger.info("CP Plus five-minute segment replay worker stopped")


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


def crop_person_hires_cpplus(
    cam: dict, lo_frame: np.ndarray, bbox: tuple[int, int, int, int]
) -> str:
    """Return a base64 JPEG person crop suitable for a face snapshot.

    Detection runs on the low-res RTSP sub-stream (fast, catches quick
    passers), but that crop is far too small for the cloud's frontal-face
    gate to resolve a face. So grab a full-resolution snapshot (the Dahua/
    CP Plus HTTP snapshot.cgi returns the main-stream resolution), scale the
    sub-stream bbox up to it, and crop with head-room padding. Falls back to
    the low-res crop if the snapshot can't be captured.
    """
    try:
        hi = capture_cpplus_frame(cam)
        if hi is None:
            return crop_person_jpeg(lo_frame, bbox)
        lh, lw = lo_frame.shape[:2]
        hh, hw = hi.shape[:2]
        if lw <= 0 or lh <= 0:
            return crop_person_jpeg(lo_frame, bbox)
        sx, sy = hw / lw, hh / lh
        x1, y1, x2, y2 = bbox
        X1, Y1, X2, Y2 = int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)
        bw, bh = max(1, X2 - X1), max(1, Y2 - Y1)
        # Extra head-room on top (face is near the top of a person box) and a
        # little around the sides so a slightly-moved person stays in frame.
        pad_x = int(bw * 0.30)
        pad_top = int(bh * 0.35)
        pad_bot = int(bh * 0.10)
        X1 = max(0, X1 - pad_x)
        X2 = min(hw, X2 + pad_x)
        Y1 = max(0, Y1 - pad_top)
        Y2 = min(hh, Y2 + pad_bot)
        crop = hi[Y1:Y2, X1:X2]
        if crop.size == 0:
            return crop_person_jpeg(lo_frame, bbox)
        _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return base64.b64encode(buf).decode("ascii")
    except Exception as e:
        logger.debug("CP Plus hi-res crop failed (%s); using low-res", e)
        return crop_person_jpeg(lo_frame, bbox)


def snapshot_vehicle_jpeg(frame: np.ndarray, bbox: tuple[int, int, int, int],
                          vehicle_type: str) -> str:
    """Draw bounding box on vehicle and return annotated crop as base64 JPEG."""
    x1, y1, x2, y2 = bbox
    h, w = frame.shape[:2]
    # Add padding around vehicle for context
    pad = int(max(x2 - x1, y2 - y1) * 0.3)
    cx1 = max(0, x1 - pad)
    cy1 = max(0, y1 - pad)
    cx2 = min(w, x2 + pad)
    cy2 = min(h, y2 + pad)
    crop = frame[cy1:cy2, cx1:cx2].copy()
    if crop.size == 0:
        return ""
    # Draw bounding box on the crop
    bx1 = x1 - cx1
    by1 = y1 - cy1
    bx2 = x2 - cx1
    by2 = y2 - cy1
    cv2.rectangle(crop, (bx1, by1), (bx2, by2), (0, 255, 0), 2)
    label = vehicle_type.upper()
    cv2.putText(crop, label, (bx1, by1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return base64.b64encode(buf).decode("ascii")


# ---------------------------------------------------------------------------
# Centroid Tracker with Line Crossing
# ---------------------------------------------------------------------------

class CentroidTracker:
    """Track people and detect crossings of a horizontal or vertical line."""

    def __init__(self, max_disappeared: int = 15, max_distance: float = 100.0,
                 line_y: int = 0, anchor_y: str = "centroid",
                 line_axis: str = "horizontal"):
        self.next_id = 0
        self.objects: OrderedDict[int, np.ndarray] = OrderedDict()  # id -> anchor point
        self.bboxes: dict[int, tuple[int, int, int, int]] = {}  # id -> bbox
        self.disappeared: dict[int, int] = {}
        self.stable_sides: dict[int, int] = {}
        self.max_disappeared = max_disappeared
        self.max_distance = max_distance
        self.line_y = line_y
        self.line_hysteresis = 0
        self.anchor_y = anchor_y
        self.line_axis = line_axis
        self.crossings: list[dict] = []  # direction crossings this update

    def set_line(self, coordinate: int, hysteresis: int = 0):
        self.line_y = coordinate
        self.line_hysteresis = max(0, hysteresis)

    def set_line_y(self, y: int, hysteresis: int = 0):
        self.line_axis = "horizontal"
        self.set_line(y, hysteresis)

    def _line_coordinate(self, point: np.ndarray) -> int:
        return int(point[0] if self.line_axis == "vertical" else point[1])

    def _line_side(self, coordinate: int) -> int:
        if coordinate < self.line_y - self.line_hysteresis:
            return -1
        if coordinate > self.line_y + self.line_hysteresis:
            return 1
        return 0

    def register(self, centroid: np.ndarray, bbox: tuple[int, int, int, int]):
        self.objects[self.next_id] = centroid
        self.bboxes[self.next_id] = bbox
        self.disappeared[self.next_id] = 0
        side = self._line_side(self._line_coordinate(centroid))
        if side:
            self.stable_sides[self.next_id] = side
        self.next_id += 1

    def deregister(self, object_id: int):
        del self.objects[object_id]
        del self.bboxes[object_id]
        del self.disappeared[object_id]
        self.stable_sides.pop(object_id, None)

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
            cy = y2 if self.anchor_y == "bottom" else (y1 + y2) // 2
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
                self.objects[oid] = input_centroids[col]
                self.bboxes[oid] = input_bboxes[col]
                self.disappeared[oid] = 0

                previous_side = self.stable_sides.get(oid)
                current_side = self._line_side(
                    self._line_coordinate(input_centroids[col])
                )
                if current_side and previous_side and current_side != previous_side:
                    self.crossings.append({
                        "id": oid,
                        "direction": "IN" if current_side > previous_side else "OUT",
                        "bbox": input_bboxes[col],
                    })
                if current_side:
                    self.stable_sides[oid] = current_side

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
    """YOLOv8-nano person and vehicle detector."""

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

    def detect(
        self,
        frame: np.ndarray,
        confidence_threshold: float | None = None,
        image_size: int | None = None,
    ) -> list[tuple[tuple[int, int, int, int], float]]:
        """Detect people in a frame.

        Returns:
            list of (bbox, confidence) where bbox = (x1, y1, x2, y2)
        """
        if self.model is None:
            return []

        threshold = (
            CONFIDENCE_THRESHOLD
            if confidence_threshold is None
            else confidence_threshold
        )
        if image_size is None:
            results = self.model(
                frame, verbose=False, classes=[0], conf=threshold,
            )
        else:
            results = self.model(
                frame, verbose=False, classes=[0], conf=threshold,
                imgsz=image_size,
            )
        detections = []

        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf < threshold:
                    continue
                x1, y1, x2, y2 = box.xyxy[0].int().tolist()
                detections.append(((x1, y1, x2, y2), conf))

        return detections

    def detect_vehicles(self, frame: np.ndarray) -> list[tuple[tuple[int, int, int, int], float, str]]:
        """Detect vehicles in a frame.

        Returns:
            list of (bbox, confidence, vehicle_type) tuples
        """
        if self.model is None:
            return []

        vehicle_cls = list(VEHICLE_CLASSES.keys())
        results = self.model(frame, verbose=False, classes=vehicle_cls)
        detections = []

        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf < VEHICLE_CONF_THRESHOLD:
                    continue
                cls_id = int(box.cls[0])
                x1, y1, x2, y2 = box.xyxy[0].int().tolist()
                detections.append(((x1, y1, x2, y2), conf, VEHICLE_CLASSES.get(cls_id, "vehicle")))

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


def send_vehicle_event(events: list[dict]) -> bool:
    """Send vehicle entry events to the cloud backend."""
    if not events:
        return True

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(VEHICLE_CLOUD_API, json=events)
            if resp.status_code == 200:
                logger.info("Sent %d vehicle event(s) to cloud — OK", len(events))
                return True
            else:
                logger.warning("Vehicle API returned %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Vehicle API error: %s", e)

    return False


def _build_candidate_boundary_event(
    camera: str,
    raw_direction: str,
    line_position: float,
    timestamp: datetime,
) -> dict:
    if camera not in CANDIDATE_BOUNDARY_CAMERAS:
        raise ValueError("Unknown candidate boundary camera")
    if raw_direction not in {"IN", "OUT"} or not 0 < line_position < 1:
        raise ValueError("Invalid candidate boundary crossing")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=IST)
    image_direction = (
        "TOP_TO_BOTTOM" if raw_direction == "IN" else "BOTTOM_TO_TOP"
    )
    return {
        "event_id": uuid4().hex,
        "timestamp": timestamp.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "boundary": CANDIDATE_BOUNDARY_CAMERAS[camera],
        "camera": camera,
        "image_direction": image_direction,
        "line_position": line_position,
    }


def send_candidate_boundary_events(events: list[dict]) -> bool:
    """Send audit-only candidate crossings without changing headcount."""
    if not events:
        return True

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                CANDIDATE_BOUNDARY_API,
                json=events,
                headers=_agent_secret_headers(),
            )
            if response.status_code == 200:
                logger.info(
                    "Sent %d candidate boundary event(s) to cloud — OK",
                    len(events),
                )
                return True
            logger.warning(
                "Candidate boundary API returned %d: %s",
                response.status_code,
                response.text[:200],
            )
    except Exception as exc:
        logger.error("Candidate boundary API error: %s", exc)

    return False


# ---------------------------------------------------------------------------
# DVR Password Loader
# ---------------------------------------------------------------------------

def _load_creds_from_dvr_list(dvrs: list[dict]) -> int:
    """Load credentials for all monitored DVR IPs into DVR_CREDS. Returns count loaded."""
    needed_ips = {cam["dvr_ip"] for cam in GATE_CAMERAS}
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
    """Load DVR passwords for all monitored cameras — cloud first, then local."""
    needed_ips = {cam["dvr_ip"] for cam in GATE_CAMERAS}

    # 1. Try cloud config
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

    # 2. Fall back to local config.json for any missing
    missing = needed_ips - set(DVR_CREDS.keys())
    if missing:
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            n = _load_creds_from_dvr_list(cfg.get("dvrs", []))
            if n:
                logger.info("Loaded %d DVR credential(s) from config.json", n)

    # Report status
    for ip in needed_ips:
        if ip in DVR_CREDS:
            logger.info("DVR %s: credentials OK", ip)
        else:
            logger.warning("DVR %s: NO PASSWORD FOUND", ip)


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
    dvr_ips = sorted({c["dvr_ip"] for c in GATE_CAMERAS})
    logger.info("DVRs: %s", ", ".join(dvr_ips))
    logger.info("Cameras: %s", ", ".join(c["name"] for c in HEADCOUNT_CAMERAS))
    for cam in CPPLUS_CAMERAS:
        logger.info("CP Plus outside-gate camera: %s @ %s", cam["name"], cam["ip"])
    logger.info("Poll interval: %d seconds", POLL_INTERVAL)
    logger.info("Monitoring: %02d:%02d - %02d:%02d IST",
                MONITOR_START_HOUR, MONITOR_START_MIN,
                MONITOR_END_HOUR, MONITOR_END_MIN)
    logger.info("=" * 60)

    # Load DVR passwords from shared config
    load_dvr_passwords()

    needed_ips = {c["dvr_ip"] for c in GATE_CAMERAS}
    missing = needed_ips - set(DVR_CREDS.keys())
    if missing:
        logger.warning("Missing DVR credentials for: %s — skipping those cameras", ", ".join(missing))

    # Initialize detector
    detector = PersonDetector()
    detector.load()

    # The CP Plus outside gate runs in its own continuous-capture worker
    # thread(s) so fast passers are never missed (the DVR loop below only
    # samples once per ~15-20s cycle). Those cameras are skipped in the main
    # loop to avoid double-counting.
    cpplus_threads: list[threading.Thread] = []
    for cam in CPPLUS_CAMERAS:
        t = threading.Thread(
            target=run_cpplus_worker, args=(cam,), daemon=True,
            name=f"cpplus-{cam['name']}",
        )
        t.start()
        cpplus_threads.append(t)
        if CPPLUS_LOCAL_RECORDING_ENABLED:
            recorder_thread = threading.Thread(
                target=run_cpplus_local_recorder, args=(cam,), daemon=True,
                name=f"cpplus-recorder-{cam['name']}",
            )
            recorder_thread.start()
            cpplus_threads.append(recorder_thread)
        if CPPLUS_NATIVE_SUMMARY_ENABLED:
            native_thread = threading.Thread(
                target=run_cpplus_native_summary_worker, args=(cam,), daemon=True,
                name=f"cpplus-native-{cam['name']}",
            )
            native_thread.start()
            cpplus_threads.append(native_thread)
        if CPPLUS_REPLAY_ENABLED:
            replay_thread = threading.Thread(
                target=run_cpplus_replay_worker, args=(cam,), daemon=True,
                name=f"cpplus-replay-{cam['name']}",
            )
            replay_thread.start()
            cpplus_threads.append(replay_thread)
        if CPPLUS_SEGMENT_REPLAY_ENABLED and CPPLUS_LOCAL_RECORDING_ENABLED:
            segment_thread = threading.Thread(
                target=run_cpplus_segment_replay_worker,
                args=(cam,), daemon=True,
                name=f"cpplus-segment-replay-{cam['name']}",
            )
            segment_thread.start()
            cpplus_threads.append(segment_thread)
        if CPPLUS_AFTER_HOURS_ENABLED:
            after_hours_thread = threading.Thread(
                target=run_cpplus_after_hours_worker, args=(cam,), daemon=True,
                name=f"cpplus-after-hours-{cam['name']}",
            )
            after_hours_thread.start()
            cpplus_threads.append(after_hours_thread)

    # One tracker per camera (for people) — DVR channels + CP Plus outside gate
    trackers: dict[str, CentroidTracker] = {}
    for cam in HEADCOUNT_CAMERAS:
        trackers[cam["name"]] = CentroidTracker(
            max_disappeared=MAX_DISAPPEARED,
            max_distance=MAX_DISTANCE,
        )

    # Appearance-based counting: track which person IDs have been counted
    counted_person_ids: dict[str, set[int]] = {c["name"]: set() for c in HEADCOUNT_CAMERAS}

    # Vehicle trackers (only for entry gate cameras)
    vehicle_trackers: dict[str, CentroidTracker] = {}
    for cam_name in VEHICLE_CAMERAS:
        vehicle_trackers[cam_name] = CentroidTracker(
            max_disappeared=MAX_DISAPPEARED,
            max_distance=MAX_DISTANCE * 2,  # vehicles move faster
        )

    # Daily counters
    daily_in: dict[str, int] = {c["name"]: 0 for c in HEADCOUNT_CAMERAS}
    daily_out: dict[str, int] = {c["name"]: 0 for c in HEADCOUNT_CAMERAS}
    daily_vehicles_in: dict[str, int] = {n: 0 for n in VEHICLE_CAMERAS}
    daily_vehicles_out: dict[str, int] = {n: 0 for n in VEHICLE_CAMERAS}
    counted_vehicle_ids: dict[str, set[int]] = {n: set() for n in VEHICLE_CAMERAS}
    current_date = datetime.now(IST).strftime("%Y-%m-%d")
    poll_count = 0
    pending_events: list[dict] = []
    pending_vehicle_events: list[dict] = []
    pending_candidate_boundary_events: list[dict] = []

    while running:
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")

        # Reset daily counters on date change
        if today != current_date:
            logger.info(
                "Date changed: %s -> %s. Previous totals: IN=%s OUT=%s Vehicles IN=%s",
                current_date, today, daily_in, daily_out, daily_vehicles_in,
            )
            current_date = today
            daily_in = {c["name"]: 0 for c in HEADCOUNT_CAMERAS}
            daily_out = {c["name"]: 0 for c in HEADCOUNT_CAMERAS}
            daily_vehicles_in = {n: 0 for n in VEHICLE_CAMERAS}
            daily_vehicles_out = {n: 0 for n in VEHICLE_CAMERAS}
            counted_vehicle_ids = {n: set() for n in VEHICLE_CAMERAS}
            counted_person_ids = {c["name"]: set() for c in HEADCOUNT_CAMERAS}
            for cam in HEADCOUNT_CAMERAS:
                trackers[cam["name"]] = CentroidTracker(
                    max_disappeared=MAX_DISAPPEARED,
                    max_distance=MAX_DISTANCE,
                )
            for cam_name in VEHICLE_CAMERAS:
                vehicle_trackers[cam_name] = CentroidTracker(
                    max_disappeared=MAX_DISAPPEARED,
                    max_distance=MAX_DISTANCE * 2,
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

        for cam in HEADCOUNT_CAMERAS:
            cam_name = cam["name"]

            # CP Plus is handled by its dedicated worker thread (see above).
            if cam.get("type") == "cpplus":
                continue

            frame = capture_camera_frame(cam)
            if frame is None:
                continue

            # Set virtual line at configured position (per-camera override wins)
            frame_h = frame.shape[0]
            cam_line_position = cam.get("line_position") or LINE_POSITION
            line_y = int(frame_h * cam_line_position)
            trackers[cam_name].set_line_y(line_y)

            # Detect people
            detections = detector.detect(frame)

            # Update tracker (maintains person identity across frames)
            crossings = trackers[cam_name].update(detections)
            if cam_name in CANDIDATE_BOUNDARY_CAMERAS:
                for crossing in crossings:
                    event = _build_candidate_boundary_event(
                        cam_name,
                        crossing["direction"],
                        cam_line_position,
                        now,
                    )
                    pending_candidate_boundary_events.append(event)
                    logger.info(
                        "%s %s audit crossing: %s",
                        event["boundary"],
                        cam_name,
                        event["image_direction"],
                    )

            # Appearance-based counting: count NEW people on first detection
            # (replaces line-crossing which fails with 5-second snapshot gaps)
            direction = _camera_direction(cam_name)
            for obj_id in list(trackers[cam_name].objects.keys()):
                if obj_id not in counted_person_ids[cam_name]:
                    counted_person_ids[cam_name].add(obj_id)

                    if direction == "IN":
                        daily_in[cam_name] += 1
                    else:
                        daily_out[cam_name] += 1

                    bbox = trackers[cam_name].bboxes.get(obj_id)
                    if bbox is None:
                        continue

                    attire_color = extract_dominant_color(frame, bbox)
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
                        "%s: %s person #%d at %s — %s attire — Day total IN=%d OUT=%d",
                        cam_name, direction, obj_id, timestamp, attire_color,
                        daily_in[cam_name], daily_out[cam_name],
                    )

            # Detect vehicles on entry gate cameras
            if cam_name in VEHICLE_CAMERAS:
                v_detections = detector.detect_vehicles(frame)
                if v_detections:
                    # Convert to tracker format (bbox, conf) — ignore vehicle_type for tracking
                    v_track_input = [((d[0]), d[1]) for d in v_detections]
                    # Build a map from bbox to vehicle_type for lookup after crossing
                    v_type_map = {d[0]: d[2] for d in v_detections}

                    vehicle_trackers[cam_name].set_line_y(line_y)
                    v_crossings = vehicle_trackers[cam_name].update(v_track_input)

                    # Count line crossings (when vehicles drive through)
                    for vc in v_crossings:
                        v_dir = vc["direction"]
                        v_bbox = vc["bbox"]
                        v_type = v_type_map.get(v_bbox, "vehicle")
                        v_id = vc["id"]

                        if v_id not in counted_vehicle_ids[cam_name]:
                            counted_vehicle_ids[cam_name].add(v_id)
                            if v_dir == "IN":
                                daily_vehicles_in[cam_name] += 1
                            else:
                                daily_vehicles_out[cam_name] += 1

                            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
                            v_snapshot = snapshot_vehicle_jpeg(frame, v_bbox, v_type)
                            v_event = {
                                "timestamp": timestamp,
                                "camera": cam_name,
                                "direction": v_dir,
                                "vehicle_type": v_type,
                                "snapshot": v_snapshot,
                                "daily_in": daily_vehicles_in[cam_name],
                                "daily_out": daily_vehicles_out[cam_name],
                            }
                            pending_vehicle_events.append(v_event)

                            logger.info(
                                "%s: VEHICLE %s (%s) at %s — Day vehicles IN=%d OUT=%d",
                                cam_name, v_dir, v_type, timestamp,
                                daily_vehicles_in[cam_name], daily_vehicles_out[cam_name],
                            )

                    # Also count NEW vehicle appearances (even without line crossing)
                    for obj_id in vehicle_trackers[cam_name].objects:
                        if obj_id not in counted_vehicle_ids[cam_name]:
                            # New vehicle appeared — count as IN on entry gates
                            counted_vehicle_ids[cam_name].add(obj_id)
                            # Determine type from current detection
                            obj_centroid = vehicle_trackers[cam_name].objects[obj_id]
                            v_type = "vehicle"
                            for det in v_detections:
                                bbox = det[0]
                                cx = (bbox[0] + bbox[2]) // 2
                                cy = (bbox[1] + bbox[3]) // 2
                                if abs(cx - obj_centroid[0]) < 50 and abs(cy - obj_centroid[1]) < 50:
                                    v_type = det[2]
                                    break

                            direction = "IN" if "ENTRY" in cam_name else "OUT" if "EXIT" in cam_name else "IN"
                            if direction == "IN":
                                daily_vehicles_in[cam_name] += 1
                            else:
                                daily_vehicles_out[cam_name] += 1

                            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
                            # Find best matching bbox for snapshot
                            v_snap_bbox = None
                            for det in v_detections:
                                dbbox = det[0]
                                dcx = (dbbox[0] + dbbox[2]) // 2
                                dcy = (dbbox[1] + dbbox[3]) // 2
                                if abs(dcx - obj_centroid[0]) < 80 and abs(dcy - obj_centroid[1]) < 80:
                                    v_snap_bbox = dbbox
                                    break
                            v_snapshot = snapshot_vehicle_jpeg(frame, v_snap_bbox, v_type) if v_snap_bbox else ""
                            v_event = {
                                "timestamp": timestamp,
                                "camera": cam_name,
                                "direction": direction,
                                "vehicle_type": v_type,
                                "snapshot": v_snapshot,
                                "daily_in": daily_vehicles_in[cam_name],
                                "daily_out": daily_vehicles_out[cam_name],
                            }
                            pending_vehicle_events.append(v_event)

                            logger.info(
                                "%s: VEHICLE %s (%s, appearance) at %s — Day vehicles IN=%d OUT=%d",
                                cam_name, direction, v_type, timestamp,
                                daily_vehicles_in[cam_name], daily_vehicles_out[cam_name],
                            )

        # Send pending events to cloud in batches
        if pending_events:
            ok = send_gate_event(pending_events)
            if ok:
                pending_events = []
            # If failed, keep pending and retry next cycle

        if pending_vehicle_events:
            ok = send_vehicle_event(pending_vehicle_events)
            if ok:
                pending_vehicle_events = []

        if pending_candidate_boundary_events:
            ok = send_candidate_boundary_events(pending_candidate_boundary_events)
            if ok:
                pending_candidate_boundary_events = []

        # Periodic status log
        if poll_count % 60 == 0:  # Every ~5 minutes
            total_in = sum(daily_in.values())
            total_out = sum(daily_out.values())
            total_v_in = sum(daily_vehicles_in.values())
            total_v_out = sum(daily_vehicles_out.values())
            logger.info(
                "Poll #%d — Day totals: IN=%d OUT=%d Vehicles IN=%d OUT=%d",
                poll_count, total_in, total_out, total_v_in, total_v_out,
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
    """Quick test: capture one frame from each camera."""
    logger.info("Testing camera connectivity...")
    load_dvr_passwords()

    for cam in GATE_CAMERAS:
        dvr_ip = cam["dvr_ip"]
        if dvr_ip not in DVR_CREDS:
            logger.error("%s: SKIPPED — no credentials for DVR %s", cam["name"], dvr_ip)
            continue
        frame = capture_gate_frame(cam["channel"], dvr_ip)
        if frame is not None:
            logger.info(
                "%s (DVR %s): OK — Frame %dx%d",
                cam["name"], dvr_ip, frame.shape[1], frame.shape[0],
            )
        else:
            logger.error("%s (DVR %s): FAILED — Could not capture frame", cam["name"], dvr_ip)

    # Standalone CP Plus outside-gate camera(s)
    for cam in CPPLUS_CAMERAS:
        frame = capture_cpplus_frame(cam)
        if frame is not None:
            logger.info(
                "%s (CP Plus %s): OK — Frame %dx%d (user=%s)",
                cam["name"], cam["ip"], frame.shape[1], frame.shape[0],
                cam.get("user"),
            )
        else:
            logger.error(
                "%s (CP Plus %s): FAILED — Could not capture frame",
                cam["name"], cam["ip"],
            )

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

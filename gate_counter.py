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
import io
import json
import logging
import math
import os
import signal
import sys
import threading
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

DVR_PORT = int(os.environ.get("GATE_DVR_PORT", "80"))
DVR_DEFAULT_USER = "admin"

# Per-DVR credentials: {ip: {"user": ..., "pass": ...}}
DVR_CREDS: dict[str, dict[str, str]] = {}

GATE_CAMERAS = [
    {"channel": 20, "name": "ENTRY GATE-1",    "dvr_ip": "192.168.0.14"},
    {"channel": 16, "name": "ENTRY GATE-2",    "dvr_ip": "192.168.0.14"},
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

# RTSP sub-stream (lower resolution) is enough for head-count detection
CPPLUS_RTSP_SUBTYPE = int(os.environ.get("CPPLUS_RTSP_SUBTYPE", "1"))

# Bound HTTP snapshot + RTSP connect/read time so an unreachable camera never
# blocks the whole poll loop.
CPPLUS_CONNECT_TIMEOUT_SEC = float(os.environ.get("CPPLUS_CONNECT_TIMEOUT_SEC", "3"))
CPPLUS_HTTP_TIMEOUT_SEC = float(os.environ.get("CPPLUS_HTTP_TIMEOUT_SEC", "8"))
CPPLUS_RTSP_TIMEOUT_SEC = int(os.environ.get("CPPLUS_RTSP_TIMEOUT_SEC", "5"))

# The CP Plus outside gate is head-counted by its own dedicated worker thread
# that reads a continuous stream (not the slow ~15-20s DVR poll cycle) so people
# who cross quickly are never missed. This throttles the worker's processing
# rate to bound CPU; ~5 FPS gives several detections even for a fast 1-2s pass.
CPPLUS_TARGET_FPS = float(os.environ.get("CPPLUS_TARGET_FPS", "5"))

# Direction of travel that counts as ENTERING for the CP Plus outside gate.
# The CentroidTracker labels a virtual-line crossing "IN" when the person moves
# top-to-bottom in the frame and "OUT" bottom-to-top. If the camera is mounted
# so that people ENTER the school moving up the frame instead, set
# CPPLUS_IN_TOP_TO_BOTTOM=0 to swap IN/OUT — no code change needed.
CPPLUS_IN_TOP_TO_BOTTOM = os.environ.get("CPPLUS_IN_TOP_TO_BOTTOM", "1") not in (
    "0", "false", "False", "no", "NO",
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

# Tracker settings
MAX_DISAPPEARED = 15  # frames before removing a tracked person
MAX_DISTANCE = 100    # max pixel distance for centroid matching

# Virtual line position (kept for backward compat, but appearance-based
# counting is now the primary method for people)
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


def _open_cpplus_stream(cam: dict):
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


def run_cpplus_worker(cam: dict):
    """Dedicated head-count loop for the CP Plus outside gate camera.

    The main DVR poll loop samples every camera only once per ~15-20s cycle,
    which misses people who cross the outside gate quickly. This worker keeps a
    persistent RTSP stream (falling back to fast HTTP snapshots if RTSP can't
    open) and processes frames continuously, throttled to CPPLUS_TARGET_FPS, so
    every person is caught. Each NEW tracked person is counted once
    (appearance-based) and streamed to the cloud. Runs in its own thread.
    """
    cam_name = cam["name"]
    detector = PersonDetector()
    detector.load()
    tracker = CentroidTracker(max_disappeared=MAX_DISAPPEARED, max_distance=MAX_DISTANCE)
    # Count each (tracked-person, direction) crossing once so a single person
    # walking through the gate is not counted on every frame.
    counted_crossings: set[tuple[int, str]] = set()
    daily_in = 0
    daily_out = 0
    current_date = datetime.now(IST).strftime("%Y-%m-%d")
    cap = None
    min_interval = 1.0 / CPPLUS_TARGET_FPS if CPPLUS_TARGET_FPS > 0 else 0.0
    reconnect_backoff = 2.0

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
            counted_crossings = set()
            tracker = CentroidTracker(max_disappeared=MAX_DISAPPEARED, max_distance=MAX_DISTANCE)

        loop_start = time.monotonic()

        # Acquire a frame: prefer the persistent RTSP stream; if it can't be
        # opened or a read fails, fall back to a single HTTP snapshot so the
        # head-count keeps working through any RTSP outage.
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
                cap = None
            cap = _open_cpplus_stream(cam)

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
            time.sleep(reconnect_backoff)
            continue

        frame_h = frame.shape[0]
        line_y = int(frame_h * (cam.get("line_position") or LINE_POSITION))
        tracker.set_line_y(line_y)

        try:
            detections = detector.detect(frame)
        except Exception as e:
            logger.error("CP Plus %s: detection error: %s", cam_name, e)
            detections = []
        # Directional counting: the tracker reports each virtual-line crossing
        # with a raw direction (top-to-bottom = "IN", bottom-to-top = "OUT").
        # We map that to the real ENTER/EXIT direction via CPPLUS_IN_TOP_TO_BOTTOM
        # so people leaving the school are recorded as OUT (and deducted from
        # the head-count), not miscounted as another entry.
        crossings = tracker.update(detections)

        events: list[dict] = []
        now = datetime.now(IST)
        for cr in crossings:
            raw_dir = cr["direction"]  # "IN" = top->bottom, "OUT" = bottom->top
            if CPPLUS_IN_TOP_TO_BOTTOM:
                person_dir = raw_dir
            else:
                person_dir = "OUT" if raw_dir == "IN" else "IN"

            key = (cr["id"], person_dir)
            if key in counted_crossings:
                continue
            counted_crossings.add(key)

            if person_dir == "IN":
                daily_in += 1
            else:
                daily_out += 1

            bbox = cr.get("bbox")
            if bbox is None:
                continue
            attire_color = extract_dominant_color(frame, bbox)
            # Snapshot uses a full-resolution crop (see docstring) so the cloud
            # frontal-face gate can actually resolve a face; only IN crossings
            # trigger a snapshot, so only pay the hi-res grab for those.
            if person_dir == "IN":
                person_crop = crop_person_hires_cpplus(cam, frame, bbox)
            else:
                person_crop = crop_person_jpeg(frame, bbox)
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            events.append({
                "timestamp": ts,
                "camera": cam_name,
                "direction": person_dir,
                "attire_color": attire_color,
                "person_crop": person_crop,
                "daily_in": daily_in,
                "daily_out": daily_out,
            })
            logger.info("%s: %s person #%d at %s — %s attire — Day IN=%d OUT=%d",
                        cam_name, person_dir, cr["id"], ts, attire_color, daily_in, daily_out)

        if events:
            send_gate_event(events)

        # Throttle to the target processing rate.
        elapsed = time.monotonic() - loop_start
        if min_interval > elapsed:
            time.sleep(min_interval - elapsed)

    if cap is not None:
        cap.release()
    logger.info("CP Plus worker stopped for %s (final IN=%d OUT=%d)",
                cam_name, daily_in, daily_out)


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
            trackers[cam_name].update(detections)

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

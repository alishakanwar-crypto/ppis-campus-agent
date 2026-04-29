"""
PPIS Campus Agent — Local Windows application that connects to Hikvision DVRs
on the school LAN and communicates with the cloud bot via WebSocket.

Features:
- Web-based local UI for DVR configuration, Excel upload, camera mapping
- Hikvision ISAPI integration for snapshot capture
- WebSocket client to cloud bot for receiving snapshot requests
- On-demand child photo capture and delivery
"""

import asyncio
import base64
import io
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from attendance_engine import engine as attendance_engine
import face_db

try:
    from PIL import Image
except ImportError:
    Image = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ppis-agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CLOUD_API_BASE = "https://app-itszlsnn.fly.dev"
CONFIG_FILE = Path(__file__).parent / "config.json"
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)


async def fetch_config_from_cloud() -> dict | None:
    """Fetch full config from the cloud-hosted SQLite database.
    Returns None if cloud is unreachable."""
    url = f"{CLOUD_API_BASE}/api/agent-config/full"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                logger.info(
                    f"Fetched config from cloud: "
                    f"{len(data.get('dvrs', []))} DVRs, "
                    f"{len(data.get('camera_mapping', {}))} camera mappings"
                )
                return data
            logger.warning(f"Cloud config API returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not fetch cloud config: {e}")
    return None


def load_config_local() -> dict:
    """Load config from local config.json (fallback)."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {
        "cloud_bot_url": "wss://app-itszlsnn.fly.dev/ws/agent",
        "agent_secret": os.environ.get("AGENT_SECRET", ""),
        "dvrs": [],
        "camera_mapping": {},
        "snapshot_dir": "snapshots",
        "local_port": 8897,
    }


def save_config(cfg: dict):
    """Save config to local config.json (cache for offline use)."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


async def sync_faces_from_cloud() -> int:
    """Download registered face images from cloud and register locally.

    Returns the number of faces synced.
    """
    url = f"{CLOUD_API_BASE}/api/face/images"
    agent_secret = os.environ.get("AGENT_SECRET", "")
    headers = {"X-Agent-Secret": agent_secret} if agent_secret else {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"Cloud face sync: API returned {resp.status_code}")
                return 0
            faces = resp.json()
            if not faces:
                logger.info("Cloud face sync: no faces registered in cloud")
                return 0

            import database as db_mod
            synced = 0
            # Fetch existing faces once before the loop (avoid N+1 queries)
            existing = db_mod.get_all_face_encodings()
            existing_keys = {
                (r["person_id"], r.get("angle", ""))
                for r in existing
            }
            for face_data in faces:
                person_id = face_data["person_id"]
                angle = face_data["angle"]
                if (person_id, angle) in existing_keys:
                    continue

                # Decode image and register locally
                image_bytes = base64.b64decode(face_data["image_base64"])
                result = face_db.register_face(
                    person_id=person_id,
                    name=face_data["name"],
                    role=face_data["role"],
                    phone=face_data["phone"],
                    angle=angle,
                    image_bytes=image_bytes,
                )
                if result.get("success"):
                    synced += 1
                    existing_keys.add((person_id, angle))
                    logger.info(f"Cloud face sync: registered {face_data['name']} ({person_id}) angle={angle}")
                else:
                    logger.warning(f"Cloud face sync: failed to register {person_id}: {result.get('error')}")

            logger.info(f"Cloud face sync complete: {synced} new face(s) synced")
            return synced
    except Exception as e:
        logger.warning(f"Cloud face sync failed: {e}")
        return 0


async def load_config() -> dict:
    """Load config: try cloud first, fall back to local config.json."""
    cloud_cfg = await fetch_config_from_cloud()
    if cloud_cfg and cloud_cfg.get("dvrs"):
        # Merge cloud data into a usable config dict
        cfg = {
            "cloud_bot_url": cloud_cfg.get("cloud_bot_url", "wss://app-itszlsnn.fly.dev/ws/agent"),
            "agent_secret": cloud_cfg.get("agent_secret", os.environ.get("AGENT_SECRET", "")),
            "dvrs": cloud_cfg.get("dvrs", []),
            "camera_mapping": cloud_cfg.get("camera_mapping", {}),
            "local_port": int(cloud_cfg.get("settings", {}).get("local_port", 8897)),
        }
        # Cache locally for offline fallback
        save_config(cfg)
        logger.info("Config loaded from cloud DB (cached locally)")
        return cfg
    # Fallback to local
    logger.info("Using local config.json (cloud unavailable or empty)")
    return load_config_local()


# Config will be loaded async in lifespan; use local as placeholder
config = load_config_local()

# ---------------------------------------------------------------------------
# Hikvision ISAPI — Snapshot capture
# ---------------------------------------------------------------------------

def compress_jpeg(data: bytes, max_bytes: int = 200_000, quality_start: int = 70) -> bytes:
    """Compress a JPEG image to fit within max_bytes.
    
    Uses Pillow if available, otherwise returns original data.
    """
    if Image is None or len(data) <= max_bytes:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        # Resize if very large (>1920px on any side)
        max_dim = 1920
        if img.width > max_dim or img.height > max_dim:
            ratio = min(max_dim / img.width, max_dim / img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        # Try decreasing quality until it fits
        quality = quality_start
        while quality >= 20:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            result = buf.getvalue()
            if len(result) <= max_bytes:
                logger.info(f"Compressed image: {len(data)} -> {len(result)} bytes (q={quality})")
                return result
            quality -= 10
        # Return best effort
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=20, optimize=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"Image compression failed: {e}, using original")
        return data


async def capture_snapshot(dvr: dict, channel: int) -> bytes | None:
    """Capture a JPEG snapshot from a Hikvision NVR via ISAPI.

    Hikvision DS-9664NI-ST / DS-7632NXI-K2 supports:
      GET /ISAPI/Streaming/channels/{channel}01/picture
    where channel is 1-based (1..64 per NVR).

    Returns JPEG bytes or None on failure.
    """
    ip = dvr["ip"]
    port = dvr.get("port", 80)
    user = dvr["username"]
    pwd = dvr["password"]

    # Hikvision uses channelNo * 100 + 1 for main stream snapshot
    stream_channel = channel * 100 + 1
    # Request highest quality JPEG via ISAPI params
    url = (f"http://{ip}:{port}/ISAPI/Streaming/channels/{stream_channel}/picture"
           f"?snapShotImageType=JPEG&videoResolutionWidth=1920&videoResolutionHeight=1080")

    logger.info(f"Capturing snapshot from {ip} channel {channel} (stream {stream_channel})")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Try digest auth first (Hikvision default), then basic
            resp = await client.get(url, auth=httpx.DigestAuth(user, pwd))
            if resp.status_code == 401:
                resp = await client.get(url, auth=httpx.BasicAuth(user, pwd))

            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                logger.info(f"Snapshot captured: {len(resp.content)} bytes from {ip} ch{channel}")
                return resp.content
            else:
                logger.error(
                    f"Snapshot failed from {ip} ch{channel}: "
                    f"HTTP {resp.status_code}, content-type={resp.headers.get('content-type', 'unknown')}"
                )
                # Try alternate URL format (sub-stream: channel*100 + 2)
                alt_stream = channel * 100 + 2
                alt_url = f"http://{ip}:{port}/ISAPI/Streaming/channels/{alt_stream}/picture"
                resp2 = await client.get(alt_url, auth=httpx.DigestAuth(user, pwd))
                if resp2.status_code == 200 and resp2.headers.get("content-type", "").startswith("image"):
                    logger.info(f"Snapshot captured (sub-stream): {len(resp2.content)} bytes")
                    return resp2.content

                # Try without /ISAPI prefix
                alt_url2 = f"http://{ip}:{port}/Streaming/channels/{stream_channel}/picture"
                resp3 = await client.get(alt_url2, auth=httpx.DigestAuth(user, pwd))
                if resp3.status_code == 200 and resp3.headers.get("content-type", "").startswith("image"):
                    logger.info(f"Snapshot captured (alt2 URL): {len(resp3.content)} bytes")
                    return resp3.content

                return None
    except Exception as e:
        logger.error(f"Snapshot error from {ip} ch{channel}: {e}")
        return None


async def test_dvr_connection(dvr: dict) -> dict:
    """Test connection to a DVR and return status info."""
    ip = dvr["ip"]
    port = dvr.get("port", 80)
    user = dvr["username"]
    pwd = dvr["password"]

    url = f"http://{ip}:{port}/ISAPI/System/deviceInfo"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, auth=httpx.DigestAuth(user, pwd))
            if resp.status_code == 200:
                return {"status": "connected", "ip": ip, "response": resp.text[:500]}
            elif resp.status_code == 401:
                return {"status": "auth_failed", "ip": ip, "error": "Invalid username/password"}
            else:
                return {"status": "error", "ip": ip, "error": f"HTTP {resp.status_code}"}
    except httpx.ConnectError:
        return {"status": "unreachable", "ip": ip, "error": "Cannot connect — DVR may be offline or IP is wrong"}
    except Exception as e:
        return {"status": "error", "ip": ip, "error": str(e)}


def find_camera_for_classroom(classroom: str) -> tuple[dict, int, str] | None:
    """Look up the DVR, channel number, and description for a given classroom (returns best/first camera)."""
    result = find_all_cameras_for_classroom(classroom)
    if result:
        return result[0]  # Return first (best) camera
    return None


def find_all_cameras_for_classroom(classroom: str) -> list[tuple[dict, int, str]] | None:
    """Look up ALL DVR cameras for a given classroom.

    Returns a list of (dvr_dict, channel, description) tuples for all cameras
    (C1 and C2) mapped to this classroom. Returns None if no cameras found.

    camera_mapping structure:
    {
        "GRADE 3C": {
            "dvr_index": 1, "channel": 17, "description": "G3C C1",
            "all_cameras": [
                {"dvr_index": 1, "channel": 17, "description": "G3C C1", "cam_type": "C1"},
                {"dvr_index": 1, "channel": 13, "description": "G3C C2", "cam_type": "C2"}
            ]
        }
    }
    """
    import re
    mapping = config.get("camera_mapping", {})
    classroom_upper = classroom.strip().upper()
    dvrs = config.get("dvrs", [])

    def _resolve_entry(val: dict) -> list[tuple[dict, int, str]]:
        """Resolve a mapping entry to a list of (dvr, channel, description) tuples."""
        results = []
        all_cams = val.get("all_cameras", [])
        if all_cams:
            for cam in all_cams:
                dvr_idx = cam.get("dvr_index", 0)
                channel = cam.get("channel", 1)
                desc = cam.get("description", "")
                if 0 <= dvr_idx < len(dvrs):
                    results.append((dvrs[dvr_idx], channel, desc))
        else:
            # Single camera entry (no all_cameras field)
            dvr_idx = val.get("dvr_index", 0)
            channel = val.get("channel", 1)
            desc = val.get("description", "")
            if 0 <= dvr_idx < len(dvrs):
                results.append((dvrs[dvr_idx], channel, desc))
        return results or None

    def _find_val(target: str) -> dict | None:
        """Find the mapping value using fuzzy matching."""
        # 1. Direct match (case-insensitive)
        for key, val in mapping.items():
            if key.strip().upper() == target:
                return val
        # 2. Whitespace-normalized match
        clean = re.sub(r'\s+', '', target)
        for key, val in mapping.items():
            key_clean = re.sub(r'\s+', '', key.strip().upper())
            if key_clean == clean:
                return val
        # 3. Strip section letter: "GRADE 6A" → "GRADE 6"
        m = re.match(r'^(GRADE\s*\d{1,2})\s*[A-D]$', target)
        if m:
            grade_no_section_clean = re.sub(r'\s+', '', m.group(1).strip())
            for key, val in mapping.items():
                key_clean = re.sub(r'\s+', '', key.strip().upper())
                if key_clean == grade_no_section_clean:
                    logger.info(f"Fuzzy camera match: {target} -> {key} (section stripped)")
                    return val
        # 4. Grade without section: "GRADE 9" → find "GRADE 9A" if it's the only match
        m3 = re.match(r'^GRADE\s*(\d{1,2})$', target)
        if m3:
            grade_num = m3.group(1)
            candidates = []
            for key, val in mapping.items():
                km = re.match(r'^GRADE\s*' + grade_num + r'\s*[A-D]$', key.strip().upper())
                if km:
                    candidates.append((key, val))
            if len(candidates) == 1:
                logger.info(f"Fuzzy camera match: {target} -> {candidates[0][0]} (section inferred)")
                return candidates[0][1]
            elif candidates:
                # Multiple sections exist — pick section A as default
                for key, val in candidates:
                    if key.strip().upper().endswith('A'):
                        logger.info(f"Fuzzy camera match: {target} -> {key} (default section A)")
                        return val
                logger.info(f"Fuzzy camera match: {target} -> {candidates[0][0]} (first of {len(candidates)})")
                return candidates[0][1]
        # 5. Strip number from Nursery/Prep: "NURSERY 4" → "NURSERY"
        m2 = re.match(r'^(NURSERY|NUR|PREP)\s*[-]?\s*\d+$', target)
        if m2:
            base_name = m2.group(1)
            for key, val in mapping.items():
                if key.strip().upper() == base_name:
                    logger.info(f"Fuzzy camera match: {target} -> {key} (number stripped)")
                    return val
        return None

    val = _find_val(classroom_upper)
    if val:
        resolved = _resolve_entry(val)
        if resolved:
            return resolved

    logger.warning(f"No camera mapping found for: {classroom!r}")
    return None


# ---------------------------------------------------------------------------
# WebSocket client — connects to cloud bot
# ---------------------------------------------------------------------------

ws_connection = None
ws_task = None

async def websocket_client():
    """Persistent WebSocket connection to the cloud bot.
    Receives snapshot requests and sends back images."""
    global ws_connection
    url = config.get("cloud_bot_url", "wss://app-itszlsnn.fly.dev/ws/agent")
    secret = config.get("agent_secret", os.environ.get("AGENT_SECRET", ""))

    while True:
        try:
            logger.info(f"Connecting to cloud bot WebSocket: {url}")
            async with websockets.connect(
                url,
                extra_headers={"X-Agent-Secret": secret},
                ping_interval=30,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,  # 10 MB max message size
            ) as ws:
                ws_connection = ws
                logger.info("Connected to cloud bot WebSocket")

                # Send hello
                await ws.send(json.dumps({
                    "type": "agent_hello",
                    "dvr_count": len(config.get("dvrs", [])),
                    "camera_count": len(config.get("camera_mapping", {})),
                }))

                async for message in ws:
                    try:
                        data = json.loads(message)
                        msg_type = data.get("type", "")

                        if msg_type == "snapshot_request":
                            classroom = data.get("classroom", "")
                            request_id = data.get("request_id", "")
                            logger.info(f"Snapshot request for classroom: {classroom} (req: {request_id})")
                            await handle_snapshot_request(ws, classroom, request_id)

                        elif msg_type == "ping":
                            await ws.send(json.dumps({"type": "pong"}))

                        elif msg_type == "test_connection":
                            dvr_idx = data.get("dvr_index", 0)
                            dvrs = config.get("dvrs", [])
                            if 0 <= dvr_idx < len(dvrs):
                                result = await test_dvr_connection(dvrs[dvr_idx])
                                await ws.send(json.dumps({"type": "test_result", **result}))

                        elif msg_type == "update_camera_mapping":
                            new_mapping = data.get("camera_mapping", {})
                            if new_mapping:
                                config["camera_mapping"] = new_mapping
                                save_config(config)
                                logger.info(f"Camera mapping updated remotely: {len(new_mapping)} entries")
                                await ws.send(json.dumps({
                                    "type": "mapping_updated",
                                    "success": True,
                                    "count": len(new_mapping),
                                }))
                            else:
                                await ws.send(json.dumps({
                                    "type": "mapping_updated",
                                    "success": False,
                                    "error": "Empty mapping data",
                                }))

                        elif msg_type == "update_dvrs":
                            new_dvrs = data.get("dvrs", [])
                            if new_dvrs:
                                config["dvrs"] = new_dvrs
                                save_config(config)
                                logger.info(f"DVRs updated remotely: {len(new_dvrs)} entries")
                                await ws.send(json.dumps({
                                    "type": "dvrs_updated",
                                    "success": True,
                                    "count": len(new_dvrs),
                                }))
                            else:
                                await ws.send(json.dumps({
                                    "type": "dvrs_updated",
                                    "success": False,
                                    "error": "Empty DVR data",
                                }))

                        else:
                            logger.warning(f"Unknown WS message type: {msg_type}")

                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON from cloud bot: {message[:200]}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}. Reconnecting in 5s...")
            ws_connection = None
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
            ws_connection = None

        await asyncio.sleep(5)


async def handle_snapshot_request(ws, classroom: str, request_id: str):
    """Handle a snapshot request from the cloud bot.

    Captures from ALL cameras (C1 and C2) for the classroom.
    Sends each image as a separate WebSocket message to avoid size limits.
    Protocol:
      1. snapshot_image  (one per captured image, sent individually)
      2. snapshot_complete (final message with total count)
    Falls back to legacy single-message format if only 1 image captured.
    """
    all_cameras = find_all_cameras_for_classroom(classroom)

    if not all_cameras:
        await ws.send(json.dumps({
            "type": "snapshot_response",
            "request_id": request_id,
            "success": False,
            "error": f"No camera mapped for classroom: {classroom}",
        }))
        return

    logger.info(f"Capturing from {len(all_cameras)} camera(s) for {classroom}")

    # Capture exactly 2 photos: one from C1, one from C2 (no extras)
    raw_images = []  # list of (bytes, filename, description)
    for dvr, channel, desc in all_cameras[:2]:  # Max 2 cameras (C1 + C2)
        snapshot = await capture_snapshot(dvr, channel)
        if snapshot:
            ts = int(time.time())
            cam_label = desc.split()[-1] if desc else f"ch{channel}"
            filename = f"{classroom.replace(' ', '_')}_{cam_label}_{ts}.jpg"
            filepath = SNAPSHOT_DIR / filename
            with open(filepath, "wb") as f:
                f.write(snapshot)
            raw_images.append((snapshot, filename, desc))
            logger.info(f"Snapshot captured: {filename} ({len(snapshot)} bytes) - {desc}")
        else:
            logger.warning(f"Failed to capture from DVR {dvr['ip']} channel {channel} ({desc})")

    if not raw_images:
        await ws.send(json.dumps({
            "type": "snapshot_response",
            "request_id": request_id,
            "success": False,
            "error": f"Failed to capture snapshot from any camera for {classroom}",
        }))
        return

    total = len(raw_images)
    logger.info(f"Sending {total} snapshot(s) for {classroom} individually")

    # Send each image as a separate message (avoids WebSocket size limits)
    for idx, (raw_data, filename, desc) in enumerate(raw_images):
        compressed = compress_jpeg(raw_data)
        b64 = base64.b64encode(compressed).decode("ascii")
        await ws.send(json.dumps({
            "type": "snapshot_image",
            "request_id": request_id,
            "classroom": classroom,
            "image_index": idx,
            "image_total": total,
            "filename": filename,
            "image_base64": b64,
            "size_bytes": len(compressed),
            "description": desc,
        }))
        logger.info(f"Sent image {idx+1}/{total}: {filename} ({len(compressed)} bytes) - {desc}")

    # Send completion message
    await ws.send(json.dumps({
        "type": "snapshot_complete",
        "request_id": request_id,
        "success": True,
        "classroom": classroom,
        "image_count": total,
    }))
    logger.info(f"Sent snapshot_complete for {classroom}: {total} image(s)")


# ---------------------------------------------------------------------------
# FastAPI — Local web UI
# ---------------------------------------------------------------------------

async def _auto_start_classwise():
    """Auto-start classwise monitoring after a brief delay.

    This ensures the system is always-on without manual intervention.
    """
    await asyncio.sleep(5)  # Let other startup tasks finish
    if attendance_engine.classwise_running or attendance_engine.running:
        logger.info("Monitoring already active — skipping auto-start")
        return

    dvrs = config.get("dvrs", [])
    camera_mapping = config.get("camera_mapping", {})
    if not dvrs or not camera_mapping:
        logger.warning("Auto-start skipped: no DVRs or camera mapping configured")
        return

    attendance_engine.test_mode = False
    attendance_engine.confidence_threshold = 0.30
    attendance_engine.reload_faces()
    attendance_engine.classwise_running = True
    attendance_engine._classwise_task = asyncio.create_task(
        attendance_engine.classwise_monitoring_loop(dvrs, camera_mapping)
    )
    logger.info("AUTO-START: Classwise attendance monitoring started automatically")


async def _health_watchdog():
    """Background watchdog that monitors system health and auto-recovers.

    Checks every 60 seconds:
    - Is classwise monitoring running? If not, restart it.
    - Are cameras responding?
    - Is the notification system reachable?
    - Periodically sync new faces from cloud.
    """
    face_sync_counter = 0
    while True:
        await asyncio.sleep(60)
        try:
            # --- Check 1: Classwise monitoring alive ---
            if attendance_engine._health.get("auto_start_enabled", True):
                if not attendance_engine.classwise_running and not attendance_engine.running:
                    dvrs = config.get("dvrs", [])
                    camera_mapping = config.get("camera_mapping", {})
                    if dvrs and camera_mapping:
                        logger.warning("WATCHDOG: Classwise monitoring stopped — restarting")
                        attendance_engine._health["total_recoveries"] += 1
                        attendance_engine.test_mode = False
                        attendance_engine.confidence_threshold = 0.30
                        attendance_engine.reload_faces()
                        attendance_engine.classwise_running = True
                        attendance_engine._classwise_task = asyncio.create_task(
                            attendance_engine.classwise_monitoring_loop(dvrs, camera_mapping)
                        )

            # --- Check 2: Notification system reachable ---
            try:
                api_url = attendance_engine.whatsapp_api_url or "https://app-itszlsnn.fly.dev"
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(f"{api_url}/debug/version")
                    if resp.status_code == 200:
                        attendance_engine._health["notification_system"] = "ok"
                    else:
                        attendance_engine._health["notification_system"] = "degraded"
            except Exception:
                attendance_engine._health["notification_system"] = "error"

            # --- Check 3: Periodic face sync from cloud (every 5 min) ---
            face_sync_counter += 1
            if face_sync_counter >= 5:
                face_sync_counter = 0
                synced = await sync_faces_from_cloud()
                if synced > 0:
                    attendance_engine.reload_faces()
                    logger.info(f"WATCHDOG: Synced {synced} new face(s) from cloud")

            attendance_engine._health["last_health_check"] = (
                __import__("datetime").datetime.now().isoformat()
            )
        except Exception as e:
            logger.error(f"WATCHDOG error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ws_task, config
    # Initialize database (creates tables including attendance tables)
    import database as db_mod
    db_mod.init_db()
    # Load config from cloud DB (falls back to local config.json)
    config = await load_config()
    # ALWAYS enforce the correct cloud bot URL (prevents stale config.json issues)
    config["cloud_bot_url"] = "wss://app-itszlsnn.fly.dev/ws/agent"
    logger.info(
        f"Config loaded: {len(config.get('dvrs', []))} DVRs, "
        f"{len(config.get('camera_mapping', {}))} camera mappings"
    )
    # Sync face registrations from cloud DB (downloads images, computes encodings)
    await sync_faces_from_cloud()
    # Pre-load registered faces into attendance engine
    attendance_engine.reload_faces()
    # Start WebSocket client in background
    ws_task = asyncio.create_task(websocket_client())
    # Auto-start classwise monitoring (24/7 always-on)
    asyncio.create_task(_auto_start_classwise())
    # Start health watchdog (auto-recovery, face sync, system checks)
    asyncio.create_task(_health_watchdog())
    logger.info("PPIS Campus Agent started (24/7 mode with auto-recovery)")
    yield
    # Shutdown
    attendance_engine.stop()
    if ws_task:
        ws_task.cancel()
    logger.info("PPIS Campus Agent stopped")


app = FastAPI(title="PPIS Campus Agent", lifespan=lifespan)

# Ensure static directories exist
(Path(__file__).parent / "static").mkdir(exist_ok=True)
(Path(__file__).parent / "face_images").mkdir(exist_ok=True)
(Path(__file__).parent / "attendance_snapshots").mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.mount("/snapshots", StaticFiles(directory=str(SNAPSHOT_DIR)), name="snapshots")
app.mount("/face_images", StaticFiles(directory=str(Path(__file__).parent / "face_images")), name="face_images")
app.mount("/attendance_snapshots", StaticFiles(directory=str(Path(__file__).parent / "attendance_snapshots")), name="attendance_snapshots")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Main dashboard page."""
    return get_dashboard_html()


@app.get("/api/config")
async def get_config():
    """Return current config (without passwords)."""
    safe_dvrs = []
    for d in config.get("dvrs", []):
        safe_dvrs.append({
            "name": d.get("name", ""),
            "ip": d.get("ip", ""),
            "port": d.get("port", 80),
            "username": d.get("username", ""),
            "channels": d.get("channels", 64),
        })
    return {
        "dvrs": safe_dvrs,
        "camera_mapping": config.get("camera_mapping", {}),
        "cloud_bot_url": config.get("cloud_bot_url", ""),
        "config_source": "cloud" if config.get("_from_cloud") else "local",
        "ws_connected": ws_connection is not None and ws_connection.open if ws_connection else False,
    }


@app.post("/api/dvr/save")
async def save_dvr_config(request: Request):
    """Save DVR configuration locally and sync to cloud.

    Preserves existing passwords when incoming DVR entries have empty passwords
    (the frontend strips passwords for display security).
    """
    body = await request.json()
    dvrs = body.get("dvrs", [])

    # Merge: preserve stored passwords when incoming password is empty
    # Match by (ip, port) key instead of index to handle DVR reordering/deletion
    existing_dvrs = config.get("dvrs", [])
    existing_pw_map = {}
    for d in existing_dvrs:
        key = (d.get("ip", ""), d.get("port", 80))
        existing_pw_map[key] = d.get("password", "")
    for new_dvr in dvrs:
        if not new_dvr.get("password"):
            key = (new_dvr.get("ip", ""), new_dvr.get("port", 80))
            if key in existing_pw_map:
                new_dvr["password"] = existing_pw_map[key]

    config["dvrs"] = dvrs
    save_config(config)
    # Sync to cloud DB
    cloud_synced = False
    try:
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {"X-Agent-Secret": agent_secret} if agent_secret else {}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{CLOUD_API_BASE}/api/agent-config/dvrs",
                json={"dvrs": dvrs},
                headers=headers,
            )
            cloud_synced = resp.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to sync DVRs to cloud: {e}")
    return {"status": "ok", "dvr_count": len(dvrs), "cloud_synced": cloud_synced}


@app.post("/api/dvr/test/{dvr_index}")
async def test_dvr(dvr_index: int):
    """Test connection to a specific DVR."""
    dvrs = config.get("dvrs", [])
    if dvr_index < 0 or dvr_index >= len(dvrs):
        return JSONResponse({"status": "error", "error": "Invalid DVR index"}, status_code=400)
    result = await test_dvr_connection(dvrs[dvr_index])
    return result


@app.post("/api/snapshot/{classroom}")
async def take_snapshot(classroom: str):
    """Manually capture a snapshot for a classroom."""
    result = find_camera_for_classroom(classroom)
    if not result:
        return JSONResponse(
            {"status": "error", "error": f"No camera mapped for: {classroom}"},
            status_code=404,
        )

    dvr, channel, _desc = result
    snapshot = await capture_snapshot(dvr, channel)
    if not snapshot:
        return JSONResponse(
            {"status": "error", "error": "Failed to capture snapshot"},
            status_code=500,
        )

    ts = int(time.time())
    filename = f"{classroom.replace(' ', '_')}_{ts}.jpg"
    filepath = SNAPSHOT_DIR / filename
    with open(filepath, "wb") as f:
        f.write(snapshot)

    return {"status": "ok", "filename": filename, "size_bytes": len(snapshot)}


def _parse_camera_xls(file_path: str | Path) -> dict:
    """Parse the PPIS camera Excel (.xls) with side-by-side NVR layout.

    The Excel has this structure:
      Cols A-D: NVR 1 data (S.NO, CAMERA NAME, CAMERA LOCATION, SOUND)
      Col E: empty separator
      Cols F-I: NVR 2 data (S.NO, CAMERA NAME, CAMERA LOCATION, SOUND)
    NVR 3 starts further down in cols A-D after NVR 1 ends.

    Returns a dict mapping camera_name -> {dvr_index, channel, location, sound}.
    """
    import re
    import xlrd

    wb = xlrd.open_workbook(str(file_path))
    ws = wb.sheet_by_index(0)

    mapping = {}
    current_nvr = None  # Will be set when we encounter "NVR NUMBER" rows
    nvr_sections = []  # list of (nvr_number, start_col, header_row)

    # First pass: find NVR section headers
    for r in range(ws.nrows):
        for c in range(ws.ncols):
            val = str(ws.cell_value(r, c)).strip().upper()
            if "NVR NUMBER" in val or "DVR NUMBER" in val:
                # Extract NVR number (e.g. "NVR NUMBER:- 1" -> 1)
                nums = re.findall(r'\d+', val)
                nvr_num = int(nums[0]) if nums else len(nvr_sections) + 1
                nvr_sections.append({"nvr": nvr_num, "col": c, "header_row": r})

    logger.info(f"Found {len(nvr_sections)} NVR sections: {nvr_sections}")

    # Second pass: for each NVR section, find the data header row and parse cameras
    for section in nvr_sections:
        start_col = section["col"]
        nvr_num = section["nvr"]
        header_row = section["header_row"]

        # Find the header row with "S.NO" / "CAMERA NAME" below the NVR header
        sno_col = None
        name_col = None
        loc_col = None
        sound_col = None

        for r in range(header_row, min(header_row + 5, ws.nrows)):
            for c in range(max(0, start_col - 1), min(ws.ncols, start_col + 5)):
                val = str(ws.cell_value(r, c)).strip().upper()
                if val in ("S.NO.", "S.NO", "SNO", "SR.NO"):
                    sno_col = c
                elif "CAMERA NAME" in val:
                    name_col = c
                elif "CAMERA LOCATION" in val or "LOCATION" in val:
                    loc_col = c
                elif "SOUND" in val:
                    sound_col = c

            if sno_col is not None and name_col is not None:
                data_start_row = r + 1
                break
        else:
            logger.warning(f"Could not find data headers for NVR {nvr_num}")
            continue

        # Parse camera rows
        for r in range(data_start_row, ws.nrows):
            sno_val = str(ws.cell_value(r, sno_col)).strip()
            if not sno_val or sno_val == "":
                # Check if we hit a new NVR section header
                row_text = " ".join(str(ws.cell_value(r, c)).strip() for c in range(max(0, start_col - 1), min(ws.ncols, start_col + 5)))
                if "NVR NUMBER" in row_text.upper() or "DVR NUMBER" in row_text.upper():
                    break
                continue

            # Extract channel number from S.NO
            try:
                channel = int(float(sno_val))
            except (ValueError, TypeError):
                continue

            cam_name = str(ws.cell_value(r, name_col)).strip() if name_col is not None else ""
            if not cam_name:
                continue

            location = str(ws.cell_value(r, loc_col)).strip() if loc_col is not None else ""
            sound = str(ws.cell_value(r, sound_col)).strip().upper() if sound_col is not None else ""

            mapping[cam_name] = {
                "dvr_index": nvr_num - 1,  # 0-based (NVR 1 -> index 0)
                "channel": channel,
                "location": location,
                "sound": sound == "YES",
                "description": f"{cam_name} ({location})",
            }

    return mapping


def _build_classroom_mapping(raw_mapping: dict) -> dict:
    """Convert raw camera mapping to classroom-focused mapping.

    Extracts classroom names from camera names like 'GRADE 10  CAM 2' -> 'Grade 10'.
    Groups multiple cameras per classroom and picks the best one (prefers CAM 1, with sound).
    """
    import re

    classroom_cameras = {}  # classroom -> list of camera entries

    for cam_name, info in raw_mapping.items():
        upper = cam_name.upper().strip()

        # Remove "CAM X" suffix first to isolate the classroom part
        classroom_part = re.sub(r'\s*CAM\s*\d+\s*$', '', upper).strip()
        # Collapse multiple spaces
        classroom_part = re.sub(r'\s+', ' ', classroom_part)

        # Check if this is a classroom camera
        match = re.match(
            r'((?:GRADE|NURSERY|NUR|PREP)\s+\d+\s*[A-C]?|(?:POPSICLES?|NURSERY|NUR|PREP))$',
            classroom_part,
        )

        if match:
            classroom = match.group(1).strip()
            # Normalize: "GRADE 10" -> "Grade 10", "NURSERY 3" -> "Nursery 3"
            classroom = classroom.title()
        else:
            # Not a classroom camera (library, sports room, etc.)
            # Still include it with the cleaned camera name as key
            classroom = classroom_part.title()

        if classroom not in classroom_cameras:
            classroom_cameras[classroom] = []
        classroom_cameras[classroom].append({
            "cam_name": cam_name,
            **info,
        })

    # Pick the best camera per classroom
    final_mapping = {}
    for classroom, cameras in classroom_cameras.items():
        # Prefer: CAM 1 > CAM 2, with sound > without sound
        best = sorted(cameras, key=lambda c: (
            "CAM 1" in c["cam_name"].upper(),  # True sorts after False, so negate
            c.get("sound", False),
        ), reverse=True)[0]

        final_mapping[classroom] = {
            "dvr_index": best["dvr_index"],
            "channel": best["channel"],
            "description": best["description"],
            "all_cameras": [
                {"name": c["cam_name"], "channel": c["channel"], "dvr_index": c["dvr_index"], "description": c.get("description", "")}
                for c in cameras
            ],
        }

    return final_mapping


@app.post("/api/mapping/upload")
async def upload_mapping(file: UploadFile = File(...)):
    """Upload an Excel file with camera-to-classroom mapping.

    Supports both .xls (old format) and .xlsx (new format).
    Auto-detects PPIS camera Excel layout with side-by-side NVR sections.
    """
    fname = file.filename or ""
    if not fname.endswith((".xlsx", ".xls")):
        return JSONResponse(
            {"status": "error", "error": "Please upload an .xlsx or .xls file"},
            status_code=400,
        )

    content = await file.read()
    ext = ".xls" if fname.endswith(".xls") else ".xlsx"
    upload_path = Path(__file__).parent / f"camera_mapping{ext}"
    with open(upload_path, "wb") as f:
        f.write(content)

    try:
        if ext == ".xls":
            # Use xlrd for old .xls format — auto-detect PPIS NVR layout
            raw_mapping = _parse_camera_xls(upload_path)
            mapping = _build_classroom_mapping(raw_mapping)
        else:
            # Use openpyxl for .xlsx
            import openpyxl
            wb = openpyxl.load_workbook(upload_path, data_only=True)
            ws = wb.active
            mapping = {}
            headers = [str(cell.value or "").strip().lower() for cell in ws[1]]
            classroom_col = None
            dvr_col = None
            channel_col = None
            desc_col = None

            for i, h in enumerate(headers):
                if "class" in h or "room" in h:
                    classroom_col = i
                elif "dvr" in h or "nvr" in h:
                    dvr_col = i
                elif "channel" in h or "camera" in h or "ch" == h:
                    channel_col = i
                elif "desc" in h or "note" in h or "label" in h:
                    desc_col = i

            if classroom_col is None or channel_col is None:
                # Fallback: try PPIS format detection on .xlsx too
                return JSONResponse(
                    {"status": "error", "error": "Excel must have columns: Classroom/Room and Channel/Camera. Or use the PPIS .xls format."},
                    status_code=400,
                )

            for row in ws.iter_rows(min_row=2, values_only=True):
                classroom = str(row[classroom_col] or "").strip()
                if not classroom:
                    continue
                dvr_num = int(row[dvr_col] or 1) if dvr_col is not None else 1
                channel = int(row[channel_col] or 1)
                desc = str(row[desc_col] or "") if desc_col is not None else ""
                mapping[classroom] = {
                    "dvr_index": dvr_num - 1,
                    "channel": channel,
                    "description": desc or classroom,
                }

        config["camera_mapping"] = mapping
        save_config(config)

        # Separate classroom cameras from non-classroom cameras for display
        classroom_keys = [k for k in mapping if any(
            kw in k.upper() for kw in ("GRADE", "NURSERY", "NUR", "PREP", "POPSICLE")
        )]
        other_keys = [k for k in mapping if k not in classroom_keys]

        return {
            "status": "ok",
            "mappings_loaded": len(mapping),
            "classroom_cameras": len(classroom_keys),
            "other_cameras": len(other_keys),
            "classrooms": sorted(classroom_keys),
            "other_locations": sorted(other_keys),
        }
    except Exception as e:
        logger.exception(f"Failed to parse camera mapping Excel: {e}")
        return JSONResponse(
            {"status": "error", "error": f"Failed to parse Excel: {str(e)}"},
            status_code=400,
        )


@app.post("/api/mapping/save")
async def save_mapping(request: Request):
    """Save camera mapping locally and sync to cloud."""
    body = await request.json()
    mapping = body.get("mapping", {})
    config["camera_mapping"] = mapping
    save_config(config)
    # Sync to cloud DB
    cloud_synced = False
    try:
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {"X-Agent-Secret": agent_secret} if agent_secret else {}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{CLOUD_API_BASE}/api/agent-config/camera-mapping",
                json={"camera_mapping": mapping},
                headers=headers,
            )
            cloud_synced = resp.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to sync camera mapping to cloud: {e}")
    return {"status": "ok", "mappings_saved": len(mapping), "cloud_synced": cloud_synced}


@app.get("/api/snapshots")
async def list_snapshots():
    """List recent snapshots."""
    files = sorted(SNAPSHOT_DIR.glob("*.jpg"), key=os.path.getmtime, reverse=True)[:50]
    return [{"filename": f.name, "size": f.stat().st_size, "time": f.stat().st_mtime} for f in files]


# ---------------------------------------------------------------------------
# Face Recognition Attendance API
# ---------------------------------------------------------------------------

@app.post("/api/face/register")
async def register_face(
    image: UploadFile = File(...),
    person_id: str = Form(...),
    name: str = Form(...),
    role: str = Form(""),
    phone: str = Form(""),
    angle: str = Form("front"),
):
    """Register a face from an uploaded image."""
    image_bytes = await image.read()
    result = face_db.register_face(
        person_id=person_id,
        name=name,
        role=role,
        phone=phone,
        angle=angle,
        image_bytes=image_bytes,
    )
    if not result["success"]:
        return JSONResponse(result, status_code=400)
    # Reload known faces in the engine
    attendance_engine.reload_faces()
    # Sync to cloud DB
    cloud_synced = False
    try:
        agent_secret = os.environ.get("AGENT_SECRET", "")
        headers = {"X-Agent-Secret": agent_secret} if agent_secret else {}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{CLOUD_API_BASE}/api/face/register",
                data={"person_id": person_id, "name": name, "role": role, "phone": phone, "angle": angle},
                files={"image": ("face.jpg", image_bytes, "image/jpeg")},
                headers=headers,
            )
            cloud_synced = resp.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to sync face to cloud: {e}")
    result["cloud_synced"] = cloud_synced
    return result


@app.get("/api/face/registered")
async def list_registered_faces():
    """List all registered persons."""
    return face_db.get_registered_list()


@app.delete("/api/face/{person_id}")
async def delete_registered_face(person_id: str):
    """Delete all face encodings for a person."""
    deleted = face_db.delete_person(person_id)
    attendance_engine.reload_faces()
    return {"deleted": deleted, "person_id": person_id}


@app.post("/api/face/migrate-insightface")
async def migrate_faces_to_insightface():
    """Re-encode all existing face photos with InsightFace (512-d ArcFace).

    Reads saved face images from disk and creates InsightFace embeddings
    alongside existing face_recognition encodings. Safe to run multiple
    times — skips faces that already have InsightFace encodings.
    """
    result = face_db.migrate_to_insightface()
    if result.get("success"):
        attendance_engine.reload_faces()
    return result


@app.post("/api/attendance/start")
async def start_attendance_monitoring(request: Request):
    """Start the face recognition attendance monitoring loop."""
    body = await request.json() if (request.headers.get("content-type") or "").startswith("application/json") else {}

    if attendance_engine.running:
        return {"status": "already_running"}
    if attendance_engine.classwise_running:
        return {"status": "error", "error": "Classwise monitoring is running. Stop it first."}

    # Configure engine
    attendance_engine.test_mode = body.get("test_mode", True)
    attendance_engine.test_person_id = body.get("test_person_id", "TEST001")
    attendance_engine.confidence_threshold = body.get("confidence_threshold", 0.30)
    attendance_engine.scan_interval = body.get("scan_interval", 3.0)
    attendance_engine.whatsapp_phone = body.get("whatsapp_phone", "")

    entrance = body.get("entrance_camera", None)
    dvrs = config.get("dvrs", [])

    attendance_engine.reload_faces()
    attendance_engine.running = True  # Set immediately to prevent duplicate starts
    attendance_engine._task = asyncio.create_task(
        attendance_engine.monitoring_loop(dvrs, entrance)
    )

    return {
        "status": "started",
        "config": attendance_engine.get_status(),
    }


@app.post("/api/attendance/start-classwise")
async def start_classwise_monitoring(request: Request):
    """Start classroom-wise face recognition on ALL classroom cameras.

    Each camera only checks students assigned to that class.
    Entry gate cameras check ALL registered faces.
    """
    body = await request.json() if (request.headers.get("content-type") or "").startswith("application/json") else {}

    if attendance_engine.classwise_running:
        return {"status": "already_running"}
    if attendance_engine.running:
        return {"status": "error", "error": "Single-camera monitoring is running. Stop it first."}

    attendance_engine.test_mode = False  # Classwise mode is always production
    attendance_engine.confidence_threshold = body.get("confidence_threshold", 0.30)
    attendance_engine.scan_interval = body.get("scan_interval", 3.0)

    dvrs = config.get("dvrs", [])
    camera_mapping = config.get("camera_mapping", {})

    if not dvrs:
        return {"status": "error", "error": "No DVRs configured"}
    if not camera_mapping:
        return {"status": "error", "error": "No camera mapping loaded"}

    cameras = attendance_engine.build_classroom_camera_list(camera_mapping, dvrs)
    classroom_cams = [c for c in cameras if c["grade"] is not None]
    gate_cams = [c for c in cameras if c["grade"] is None]

    attendance_engine.reload_faces()
    attendance_engine.classwise_running = True
    attendance_engine._classwise_task = asyncio.create_task(
        attendance_engine.classwise_monitoring_loop(dvrs, camera_mapping)
    )

    return {
        "status": "started",
        "classroom_cameras": len(classroom_cams),
        "gate_cameras": len(gate_cams),
        "total_faces": len(attendance_engine.known_faces),
        "grades_with_faces": len(attendance_engine._grade_face_cache),
        "config": attendance_engine.get_status(),
    }


@app.get("/api/attendance/classwise-cameras")
async def list_classwise_cameras():
    """List all classroom cameras with their grade assignments."""
    dvrs = config.get("dvrs", [])
    camera_mapping = config.get("camera_mapping", {})
    cameras = attendance_engine.build_classroom_camera_list(camera_mapping, dvrs)

    grade_face_counts = {
        g: len(v) for g, v in attendance_engine._grade_face_cache.items()
    }

    result = []
    for cam in cameras:
        grade = cam["grade"]
        result.append({
            "location": cam["location"],
            "grade": grade,
            "dvr_index": cam["dvr_index"],
            "channel": cam["channel"],
            "label": cam["label"],
            "faces_for_grade": grade_face_counts.get(grade, 0) if grade else len(attendance_engine.known_faces),
            "is_gate": cam.get("is_gate", grade is None),
        })

    return {
        "cameras": result,
        "total_classroom": sum(1 for c in result if not c["is_gate"]),
        "total_gate": sum(1 for c in result if c["is_gate"]),
        "grades_with_faces": grade_face_counts,
    }


@app.post("/api/attendance/stop")
async def stop_attendance_monitoring():
    """Stop attendance monitoring and disable auto-restart by the watchdog."""
    attendance_engine.stop()
    return {"status": "stopped", "auto_start_enabled": False}


@app.post("/api/attendance/resend-notification")
async def resend_notification(person_id: str):
    """Clear notification dedup for a student so their next detection re-sends."""
    today = date.today().isoformat()
    cleared = []
    if attendance_engine._notification_sent.get(person_id) == today:
        del attendance_engine._notification_sent[person_id]
        cleared.append("notification_sent")
    if attendance_engine.daily_marked.get(person_id) == today:
        del attendance_engine.daily_marked[person_id]
        cleared.append("daily_marked")
    return {"person_id": person_id, "cleared": cleared}


@app.get("/api/attendance/status")
async def get_attendance_status():
    """Get attendance engine status."""
    return attendance_engine.get_status()


@app.get("/api/health")
async def health_check():
    """System health check — returns status of all subsystems."""
    health = attendance_engine._health.copy()
    health["classwise_running"] = attendance_engine.classwise_running
    health["single_cam_running"] = attendance_engine.running
    health["registered_faces"] = len(attendance_engine.known_faces)
    health["attendance_marked_today"] = sum(
        1 for d in attendance_engine.daily_marked.values()
        if d == __import__("datetime").date.today().isoformat()
    )
    health["cameras_with_errors"] = len(attendance_engine._camera_errors)

    # Overall status
    statuses = [health["camera_feed"], health["recognition_engine"],
                health["notification_system"]]
    if "error" in statuses:
        health["overall"] = "error"
    elif "degraded" in statuses:
        health["overall"] = "degraded"
    else:
        health["overall"] = "ok"

    return health


@app.get("/api/attendance/logs")
async def get_attendance_logs(limit: int = 100, person_id: str | None = None):
    """Get attendance log entries from database."""
    import database as db_mod
    return db_mod.get_attendance_log(limit=limit, person_id=person_id)


@app.get("/api/attendance/debug")
async def get_debug_logs(limit: int = 100):
    """Get real-time debug logs from the attendance engine."""
    return attendance_engine.get_debug_logs(limit)


@app.post("/api/attendance/recognize")
async def recognize_single_image(image: UploadFile = File(...)):
    """Run face recognition on a single uploaded image (manual test)."""
    image_bytes = await image.read()
    attendance_engine.reload_faces()
    results = attendance_engine.recognize_faces_in_image(
        image_bytes, camera_source="manual_upload"
    )
    return {
        "results": results,
        "debug_logs": attendance_engine.get_debug_logs(20),
    }


@app.get("/api/attendance/today-summary")
async def get_today_summary():
    """Get today's attendance summary grouped by classroom."""
    import database as db_mod
    return {
        "total_marked": db_mod.get_today_attendance_count(),
        "by_classroom": db_mod.get_today_attendance_summary(),
    }


@app.get("/api/attendance/unrecognized")
async def get_unrecognized_faces(limit: int = 50, all: bool = False):
    """Get unrecognized faces flagged for manual review."""
    import database as db_mod
    return db_mod.get_unrecognized_faces(limit=limit, unreviewed_only=not all)


# ---------------------------------------------------------------------------
# Dashboard HTML (embedded for simplicity — no external templates needed)
# ---------------------------------------------------------------------------

def get_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PPIS Campus Agent</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Tahoma, sans-serif; background: #f0f2f5; color: #333; }
.header { background: linear-gradient(135deg, #1a237e, #283593); color: white; padding: 20px 30px; display: flex; align-items: center; gap: 15px; }
.header h1 { font-size: 24px; font-weight: 600; }
.header .status { margin-left: auto; padding: 6px 16px; border-radius: 20px; font-size: 13px; font-weight: 500; }
.status.connected { background: #43a047; }
.status.disconnected { background: #e53935; }
.container { max-width: 1200px; margin: 20px auto; padding: 0 20px; }
.tabs { display: flex; gap: 4px; margin-bottom: 20px; }
.tab { padding: 10px 24px; background: white; border: 1px solid #ddd; border-radius: 8px 8px 0 0; cursor: pointer; font-weight: 500; }
.tab.active { background: #1a237e; color: white; border-color: #1a237e; }
.panel { display: none; background: white; border-radius: 0 8px 8px 8px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
.panel.active { display: block; }
.card { border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.card h3 { margin-bottom: 12px; color: #1a237e; }
.form-row { display: flex; gap: 12px; margin-bottom: 12px; align-items: center; }
.form-row label { min-width: 100px; font-weight: 500; }
.form-row input, .form-row select { flex: 1; padding: 8px 12px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
button { padding: 8px 20px; background: #1a237e; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500; }
button:hover { background: #283593; }
button.test { background: #43a047; }
button.test:hover { background: #388e3c; }
button.danger { background: #e53935; }
.mapping-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
.mapping-table th, .mapping-table td { padding: 8px 12px; border: 1px solid #e0e0e0; text-align: left; }
.mapping-table th { background: #f5f5f5; font-weight: 600; }
.upload-zone { border: 2px dashed #ccc; border-radius: 8px; padding: 40px; text-align: center; cursor: pointer; transition: all 0.2s; }
.upload-zone:hover { border-color: #1a237e; background: #f8f9ff; }
.snapshot-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-top: 12px; }
.snapshot-card { border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; }
.snapshot-card img { width: 100%; height: 150px; object-fit: cover; }
.snapshot-card .info { padding: 8px; font-size: 12px; color: #666; }
.alert { padding: 12px 16px; border-radius: 6px; margin-bottom: 12px; }
.alert.success { background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7; }
.alert.error { background: #ffebee; color: #c62828; border: 1px solid #ef9a9a; }
.alert.info { background: #e3f2fd; color: #1565c0; border: 1px solid #90caf9; }
#alert-box { position: fixed; top: 20px; right: 20px; z-index: 1000; min-width: 300px; }
</style>
</head>
<body>

<div class="header">
    <div>🏫</div>
    <h1>PPIS Campus Agent</h1>
    <div class="status disconnected" id="ws-status">Disconnected</div>
</div>

<div id="alert-box"></div>

<div class="container">
    <div class="tabs">
        <div class="tab active" onclick="switchTab('attendance')">Attendance</div>
        <div class="tab" onclick="switchTab('register')">Face Registration</div>
        <div class="tab" onclick="switchTab('dvr')">DVR Configuration</div>
        <div class="tab" onclick="switchTab('mapping')">Camera Mapping</div>
        <div class="tab" onclick="switchTab('snapshots')">Snapshots</div>
        <div class="tab" onclick="switchTab('logs')">Logs</div>
    </div>

    <!-- Attendance Panel -->
    <div class="panel active" id="panel-attendance">
        <h2 style="margin-bottom:16px">Face Recognition Attendance</h2>

        <div class="card">
            <h3>Monitoring Controls</h3>
            <div class="form-row">
                <label>Status:</label>
                <span id="att-status" style="font-weight:bold;color:#e53935">Stopped</span>
            </div>
            <div class="form-row">
                <label>Test Mode:</label>
                <select id="att-test-mode"><option value="true" selected>ON (TEST001 only)</option><option value="false">OFF (All persons)</option></select>
            </div>
            <div class="form-row">
                <label>Test Person ID:</label>
                <input type="text" id="att-test-pid" value="TEST001">
            </div>
            <div class="form-row">
                <label>Confidence:</label>
                <input type="number" id="att-threshold" value="85" min="50" max="100" step="1">%
            </div>
            <div class="form-row">
                <label>Scan Interval:</label>
                <input type="number" id="att-interval" value="3" min="1" max="30" step="1">s
            </div>
            <div class="form-row">
                <label>WhatsApp #:</label>
                <input type="text" id="att-phone" placeholder="e.g. +91XXXXXXXXXX">
            </div>
            <div class="form-row">
                <label>DVR Index:</label>
                <input type="number" id="att-dvr-idx" value="0" min="0">
            </div>
            <div class="form-row">
                <label>Channel:</label>
                <input type="number" id="att-channel" value="1" min="1">
            </div>
            <button onclick="startAttendance()" style="background:#43a047">Start Monitoring</button>
            <button onclick="stopAttendance()" class="danger" style="margin-left:8px">Stop</button>
            <button onclick="refreshAttStatus()" style="margin-left:8px">Refresh Status</button>
        </div>

        <div class="card" style="border-left:4px solid #1565c0;background:#e3f2fd">
            <h3 style="color:#1565c0">Classroom-wise Attendance (All Cameras)</h3>
            <p style="margin-bottom:12px;color:#555">
                Scan ALL classroom cameras simultaneously. Each camera only checks students assigned to that class.
                Entry gate cameras check all registered faces.
            </p>
            <div class="form-row">
                <label>Confidence:</label>
                <input type="number" id="cw-threshold" value="40" min="20" max="100" step="1">%
            </div>
            <button onclick="startClasswise()" style="background:#1565c0;color:white">
                Start All Classrooms
            </button>
            <button onclick="stopAttendance()" class="danger" style="margin-left:8px">Stop All</button>
            <button onclick="loadClasswiseCameras()" style="margin-left:8px">View Camera List</button>
            <div id="classwise-status" style="margin-top:12px;font-family:monospace;font-size:13px;white-space:pre-wrap;background:#fff;padding:12px;border-radius:6px;display:none"></div>
            <div id="classwise-cameras" style="margin-top:12px;display:none">
                <table class="mapping-table">
                    <thead><tr><th>Location</th><th>Grade</th><th>DVR</th><th>Channel</th><th>Faces</th><th>Type</th></tr></thead>
                    <tbody id="cw-cameras-body"></tbody>
                </table>
            </div>
        </div>

        <div class="card">
            <h3>Manual Test - Upload Image</h3>
            <p style="margin-bottom:12px;color:#666">Upload an image to test face recognition without live camera feed.</p>
            <input type="file" id="test-image-upload" accept="image/*">
            <button onclick="testRecognize()" style="margin-top:8px">Recognize Faces</button>
            <div id="recognize-result" style="margin-top:12px;font-family:monospace;font-size:13px;white-space:pre-wrap"></div>
        </div>

        <div class="card">
            <h3>Attendance Log</h3>
            <button onclick="loadAttendanceLogs()">Refresh Logs</button>
            <table class="mapping-table" style="margin-top:12px" id="att-log-table">
                <thead><tr><th>Name</th><th>ID</th><th>Time</th><th>Status</th><th>Confidence</th><th>Camera</th><th>WhatsApp</th></tr></thead>
                <tbody id="att-log-body"></tbody>
            </table>
        </div>

        <div class="card">
            <h3>Debug Logs</h3>
            <button onclick="loadDebugLogs()">Refresh</button>
            <div id="debug-log-container" style="background:#1e1e1e;color:#d4d4d4;padding:16px;border-radius:8px;height:300px;overflow-y:auto;font-family:monospace;font-size:12px;margin-top:12px">
                <p>No debug logs yet. Start monitoring to see real-time events.</p>
            </div>
        </div>
    </div>

    <!-- Face Registration Panel -->
    <div class="panel" id="panel-register">
        <h2 style="margin-bottom:16px">Face Registration</h2>

        <div class="card">
            <h3>Register New Face</h3>
            <p style="margin-bottom:12px;color:#666">Upload face images from multiple angles (front, left, right) for better recognition accuracy.</p>
            <div class="form-row"><label>Person ID:</label><input type="text" id="reg-pid" value="TEST001" placeholder="e.g. TEST001"></div>
            <div class="form-row"><label>Name:</label><input type="text" id="reg-name" placeholder="Your Name"></div>
            <div class="form-row"><label>Role:</label><input type="text" id="reg-role" value="Test User" placeholder="e.g. Student, Teacher"></div>
            <div class="form-row"><label>Phone:</label><input type="text" id="reg-phone" placeholder="+91XXXXXXXXXX"></div>
            <div class="form-row"><label>Angle:</label>
                <select id="reg-angle">
                    <option value="front">Front</option>
                    <option value="left">Left</option>
                    <option value="right">Right</option>
                </select>
            </div>
            <div class="form-row"><label>Image:</label><input type="file" id="reg-image" accept="image/*"></div>
            <button onclick="registerFace()">Register Face</button>
            <div id="reg-result" style="margin-top:12px"></div>
        </div>

        <div class="card">
            <h3>Registered Persons</h3>
            <button onclick="loadRegistered()">Refresh</button>
            <table class="mapping-table" style="margin-top:12px">
                <thead><tr><th>Person ID</th><th>Name</th><th>Role</th><th>Phone</th><th>Faces</th><th>Angles</th><th>Action</th></tr></thead>
                <tbody id="registered-body"></tbody>
            </table>
        </div>
    </div>

    <!-- DVR Configuration Panel -->
    <div class="panel" id="panel-dvr">
        <h2 style="margin-bottom:16px">DVR Configuration</h2>
        <div id="dvr-list"></div>
        <button onclick="addDvr()" style="margin-top:12px">+ Add DVR</button>
        <button onclick="saveDvrs()" style="margin-top:12px; margin-left:8px">Save Configuration</button>
    </div>

    <!-- Camera Mapping Panel -->
    <div class="panel" id="panel-mapping">
        <h2 style="margin-bottom:16px">Camera-to-Classroom Mapping</h2>

        <div class="card">
            <h3>Upload Excel Mapping</h3>
            <p style="margin-bottom:12px; color:#666">Upload an Excel file with columns: <b>Classroom</b>, <b>DVR</b> (1/2/3), <b>Channel</b>, <b>Description</b> (optional)</p>
            <div class="upload-zone" onclick="document.getElementById('excel-upload').click()">
                <p>📁 Click to upload Excel file (.xlsx)</p>
                <p style="font-size:12px; color:#999; margin-top:8px">or drag and drop here</p>
            </div>
            <input type="file" id="excel-upload" accept=".xlsx,.xls" style="display:none" onchange="uploadExcel(this)">
        </div>

        <div class="card">
            <h3>Current Mapping</h3>
            <div id="mapping-table-container">
                <p style="color:#999">No mapping loaded. Upload an Excel file above.</p>
            </div>
        </div>

        <div class="card">
            <h3>Add Manual Entry</h3>
            <div class="form-row">
                <label>Classroom:</label>
                <input type="text" id="map-classroom" placeholder="e.g. Grade 3C">
            </div>
            <div class="form-row">
                <label>DVR #:</label>
                <select id="map-dvr"><option value="1">DVR 1</option><option value="2">DVR 2</option><option value="3">DVR 3</option></select>
            </div>
            <div class="form-row">
                <label>Channel:</label>
                <input type="number" id="map-channel" min="1" max="64" value="1">
            </div>
            <div class="form-row">
                <label>Description:</label>
                <input type="text" id="map-desc" placeholder="Optional description">
            </div>
            <button onclick="addMapping()">Add Mapping</button>
        </div>
    </div>

    <!-- Snapshots Panel -->
    <div class="panel" id="panel-snapshots">
        <h2 style="margin-bottom:16px">Snapshots</h2>
        <div class="form-row">
            <label>Classroom:</label>
            <input type="text" id="snap-classroom" placeholder="e.g. Grade 3C">
            <button onclick="takeSnapshot()">📷 Capture Snapshot</button>
        </div>
        <div class="snapshot-grid" id="snapshot-grid"></div>
    </div>

    <!-- Logs Panel -->
    <div class="panel" id="panel-logs">
        <h2 style="margin-bottom:16px">Activity Logs</h2>
        <div id="log-container" style="background:#1e1e1e; color:#d4d4d4; padding:16px; border-radius:8px; height:400px; overflow-y:auto; font-family:monospace; font-size:13px;">
            <p>Agent started. Waiting for events...</p>
        </div>
    </div>
</div>

<script>
let currentMapping = {};
let dvrs = [];

function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('panel-' + tab).classList.add('active');
    if (tab === 'snapshots') loadSnapshots();
    if (tab === 'attendance') { refreshAttStatus(); loadAttendanceLogs(); }
    if (tab === 'register') loadRegistered();
}

function showAlert(msg, type) {
    const box = document.getElementById('alert-box');
    const el = document.createElement('div');
    el.className = 'alert ' + type;
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), 5000);
}

function addLog(msg) {
    const container = document.getElementById('log-container');
    const ts = new Date().toLocaleTimeString();
    container.innerHTML += `<p>[${ts}] ${msg}</p>`;
    container.scrollTop = container.scrollHeight;
}

// --- DVR ---
function renderDvrs() {
    const list = document.getElementById('dvr-list');
    list.innerHTML = '';
    dvrs.forEach((d, i) => {
        list.innerHTML += `
        <div class="card">
            <h3>${d.name || 'DVR ' + (i+1)}</h3>
            <div class="form-row"><label>Name:</label><input value="${d.name||''}" onchange="dvrs[${i}].name=this.value"></div>
            <div class="form-row"><label>IP Address:</label><input value="${d.ip||''}" onchange="dvrs[${i}].ip=this.value"></div>
            <div class="form-row"><label>Port:</label><input type="number" value="${d.port||80}" onchange="dvrs[${i}].port=parseInt(this.value)"></div>
            <div class="form-row"><label>Username:</label><input value="${d.username||''}" onchange="dvrs[${i}].username=this.value"></div>
            <div class="form-row"><label>Password:</label><input type="password" value="${d.password||''}" onchange="dvrs[${i}].password=this.value"></div>
            <div class="form-row"><label>Channels:</label><input type="number" value="${d.channels||64}" onchange="dvrs[${i}].channels=parseInt(this.value)"></div>
            <button class="test" onclick="testDvr(${i})">Test Connection</button>
            <button class="danger" onclick="dvrs.splice(${i},1);renderDvrs()" style="margin-left:8px">Remove</button>
            <span id="dvr-test-${i}" style="margin-left:12px;font-size:13px"></span>
        </div>`;
    });
}

function addDvr() {
    dvrs.push({name:'DVR '+(dvrs.length+1), ip:'192.168.0.11', port:80, username:'admin', password:'', channels:64});
    renderDvrs();
}

async function saveDvrs() {
    const resp = await fetch('/api/dvr/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({dvrs})});
    const data = await resp.json();
    showAlert('DVR configuration saved (' + data.dvr_count + ' DVRs)', 'success');
    addLog('DVR configuration saved');
}

async function testDvr(i) {
    const el = document.getElementById('dvr-test-' + i);
    el.textContent = 'Testing...';
    el.style.color = '#666';
    // Need to save first so backend has the password
    await fetch('/api/dvr/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({dvrs})});
    const resp = await fetch('/api/dvr/test/' + i, {method:'POST'});
    const data = await resp.json();
    if (data.status === 'connected') {
        el.textContent = '✓ Connected';
        el.style.color = 'green';
        addLog('DVR ' + (i+1) + ' (' + dvrs[i].ip + '): Connected');
    } else {
        el.textContent = '✗ ' + (data.error || data.status);
        el.style.color = 'red';
        addLog('DVR ' + (i+1) + ' (' + dvrs[i].ip + '): ' + data.error);
    }
}

// --- Mapping ---
function renderMapping() {
    const container = document.getElementById('mapping-table-container');
    const keys = Object.keys(currentMapping);
    if (keys.length === 0) {
        container.innerHTML = '<p style="color:#999">No mapping loaded.</p>';
        return;
    }
    let html = '<table class="mapping-table"><thead><tr><th>Classroom</th><th>DVR</th><th>Channel</th><th>Description</th><th>Action</th></tr></thead><tbody>';
    keys.forEach(k => {
        const m = currentMapping[k];
        html += `<tr><td>${k}</td><td>DVR ${(m.dvr_index||0)+1}</td><td>${m.channel}</td><td>${m.description||''}</td><td><button class="danger" onclick="deleteMapping('${k}')">Delete</button></td></tr>`;
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

function addMapping() {
    const classroom = document.getElementById('map-classroom').value.trim();
    const dvrNum = parseInt(document.getElementById('map-dvr').value);
    const channel = parseInt(document.getElementById('map-channel').value);
    const desc = document.getElementById('map-desc').value.trim();
    if (!classroom) { showAlert('Please enter a classroom name', 'error'); return; }
    currentMapping[classroom] = {dvr_index: dvrNum-1, channel, description: desc || classroom};
    renderMapping();
    saveMapping();
    document.getElementById('map-classroom').value = '';
    document.getElementById('map-desc').value = '';
    showAlert('Mapping added: ' + classroom + ' → DVR ' + dvrNum + ' Ch' + channel, 'success');
}

async function deleteMapping(key) {
    delete currentMapping[key];
    renderMapping();
    await saveMapping();
    showAlert('Mapping removed: ' + key, 'info');
}

async function saveMapping() {
    await fetch('/api/mapping/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mapping: currentMapping})});
}

async function uploadExcel(input) {
    const file = input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    const resp = await fetch('/api/mapping/upload', {method:'POST', body: form});
    const data = await resp.json();
    if (data.status === 'ok') {
        showAlert('Loaded ' + data.mappings_loaded + ' camera mappings from Excel', 'success');
        addLog('Camera mapping uploaded: ' + data.mappings_loaded + ' entries');
        await loadConfig();
    } else {
        showAlert('Error: ' + data.error, 'error');
    }
}

// --- Snapshots ---
async function takeSnapshot() {
    const classroom = document.getElementById('snap-classroom').value.trim();
    if (!classroom) { showAlert('Enter a classroom name', 'error'); return; }
    showAlert('Capturing snapshot for ' + classroom + '...', 'info');
    const resp = await fetch('/api/snapshot/' + encodeURIComponent(classroom), {method:'POST'});
    const data = await resp.json();
    if (data.status === 'ok') {
        showAlert('Snapshot captured: ' + data.filename, 'success');
        addLog('Snapshot: ' + classroom + ' → ' + data.filename);
        loadSnapshots();
    } else {
        showAlert('Error: ' + data.error, 'error');
    }
}

async function loadSnapshots() {
    const resp = await fetch('/api/snapshots');
    const data = await resp.json();
    const grid = document.getElementById('snapshot-grid');
    grid.innerHTML = '';
    data.forEach(s => {
        const date = new Date(s.time * 1000).toLocaleString();
        grid.innerHTML += `<div class="snapshot-card"><img src="/snapshots/${s.filename}" alt="${s.filename}"><div class="info">${s.filename}<br>${date}<br>${Math.round(s.size/1024)}KB</div></div>`;
    });
}

// --- Face Registration ---
async function registerFace() {
    const pid = document.getElementById('reg-pid').value.trim();
    const name = document.getElementById('reg-name').value.trim();
    const role = document.getElementById('reg-role').value.trim();
    const phone = document.getElementById('reg-phone').value.trim();
    const angle = document.getElementById('reg-angle').value;
    const fileInput = document.getElementById('reg-image');
    if (!pid || !name) { showAlert('Person ID and Name are required', 'error'); return; }
    if (!fileInput.files[0]) { showAlert('Please select an image', 'error'); return; }
    const form = new FormData();
    form.append('person_id', pid);
    form.append('name', name);
    form.append('role', role);
    form.append('phone', phone);
    form.append('angle', angle);
    form.append('image', fileInput.files[0]);
    const resp = await fetch('/api/face/register', {method:'POST', body: form});
    const data = await resp.json();
    const el = document.getElementById('reg-result');
    if (data.success) {
        el.innerHTML = '<div class="alert success">Face registered: ' + name + ' (' + angle + ')</div>';
        showAlert('Face registered for ' + name + ' (' + angle + ')', 'success');
        loadRegistered();
    } else {
        el.innerHTML = '<div class="alert error">Error: ' + data.error + '</div>';
        showAlert('Registration failed: ' + data.error, 'error');
    }
}

async function loadRegistered() {
    const resp = await fetch('/api/face/registered');
    const data = await resp.json();
    const tbody = document.getElementById('registered-body');
    tbody.innerHTML = '';
    data.forEach(p => {
        tbody.innerHTML += '<tr><td>' + p.person_id + '</td><td>' + p.name + '</td><td>' + p.role + '</td><td>' + p.phone + '</td><td>' + p.face_count + '</td><td>' + (p.angles||'') + '</td><td><button class="danger" onclick="deletePerson(\\'' + p.person_id + '\\')">Delete</button></td></tr>';
    });
}

async function deletePerson(pid) {
    if (!confirm('Delete all face data for ' + pid + '?')) return;
    await fetch('/api/face/' + pid, {method:'DELETE'});
    showAlert('Deleted face data for ' + pid, 'info');
    loadRegistered();
}

// --- Attendance Monitoring ---
async function startAttendance() {
    const body = {
        test_mode: document.getElementById('att-test-mode').value === 'true',
        test_person_id: document.getElementById('att-test-pid').value.trim(),
        confidence_threshold: parseInt(document.getElementById('att-threshold').value) / 100.0,
        scan_interval: parseFloat(document.getElementById('att-interval').value),
        whatsapp_phone: document.getElementById('att-phone').value.trim(),
        entrance_camera: {
            dvr_index: parseInt(document.getElementById('att-dvr-idx').value),
            channel: parseInt(document.getElementById('att-channel').value),
            label: 'Entrance'
        }
    };
    const resp = await fetch('/api/attendance/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const data = await resp.json();
    showAlert('Attendance monitoring: ' + data.status, data.status === 'started' ? 'success' : 'info');
    refreshAttStatus();
}

async function stopAttendance() {
    await fetch('/api/attendance/stop', {method:'POST'});
    showAlert('Attendance monitoring stopped', 'info');
    refreshAttStatus();
}

async function refreshAttStatus() {
    try {
        const resp = await fetch('/api/attendance/status');
        const data = await resp.json();
        const el = document.getElementById('att-status');
        if (data.classwise_running) {
            el.textContent = 'CLASSWISE RUNNING (' + data.registered_persons + ' persons, ' + data.grades_with_faces + ' grades, ' + data.attendance_marked_today + ' marked today)';
            el.style.color = '#1565c0';
            // Update classwise status panel
            const cwEl = document.getElementById('classwise-status');
            cwEl.style.display = 'block';
            const s = data.classwise_stats || {};
            cwEl.innerHTML = '<b>Status:</b> RUNNING<br>' +
                '<b>Cameras:</b> ' + s.total_cameras + '<br>' +
                '<b>Current:</b> ' + (s.current_camera || '-') + '<br>' +
                '<b>Cycle:</b> #' + s.cycle_count + ' (' + s.last_cycle_duration + 's)<br>' +
                '<b>Faces detected:</b> ' + s.faces_detected_total + '<br>' +
                '<b>Attendance today:</b> ' + s.attendance_marked_today + '<br>' +
                '<b>Errors:</b> ' + s.errors;
        } else if (data.running) {
            el.textContent = 'Running (' + data.registered_persons + ' persons, ' + data.total_encodings + ' encodings)';
            el.style.color = '#43a047';
            document.getElementById('classwise-status').style.display = 'none';
        } else {
            el.textContent = 'Stopped';
            el.style.color = '#e53935';
            document.getElementById('classwise-status').style.display = 'none';
        }
    } catch(e) {}
}

async function loadAttendanceLogs() {
    const resp = await fetch('/api/attendance/logs?limit=50');
    const data = await resp.json();
    const tbody = document.getElementById('att-log-body');
    tbody.innerHTML = '';
    data.forEach(l => {
        tbody.innerHTML += '<tr><td>' + l.name + '</td><td>' + l.person_id + '</td><td>' + l.logged_at + '</td><td>' + l.status + '</td><td>' + (l.confidence * 100).toFixed(1) + '%</td><td>' + l.camera_source + '</td><td>' + (l.whatsapp_sent ? 'Sent' : '-') + '</td></tr>';
    });
}

async function loadDebugLogs() {
    const resp = await fetch('/api/attendance/debug?limit=100');
    const data = await resp.json();
    const container = document.getElementById('debug-log-container');
    container.innerHTML = '';
    data.forEach(l => {
        let color = '#d4d4d4';
        if (l.event === 'face_matched' || l.event === 'attendance_marked') color = '#4caf50';
        else if (l.event === 'error' || l.event === 'whatsapp_error') color = '#ef5350';
        else if (l.event === 'face_detected') color = '#42a5f5';
        else if (l.event === 'low_confidence') color = '#ffa726';
        container.innerHTML += '<p style="color:' + color + '">[' + l.timestamp + '] <b>' + l.event + '</b>: ' + l.details + (l.person_id ? ' (ID: ' + l.person_id + ')' : '') + (l.confidence > 0 ? ' [' + (l.confidence * 100).toFixed(1) + '%]' : '') + '</p>';
    });
    container.scrollTop = container.scrollHeight;
}

async function testRecognize() {
    const fileInput = document.getElementById('test-image-upload');
    if (!fileInput.files[0]) { showAlert('Select an image first', 'error'); return; }
    const form = new FormData();
    form.append('image', fileInput.files[0]);
    const el = document.getElementById('recognize-result');
    el.textContent = 'Processing...';
    const resp = await fetch('/api/attendance/recognize', {method:'POST', body: form});
    const data = await resp.json();
    let output = '=== Recognition Results ===\\n';
    if (data.results && data.results.length > 0) {
        data.results.forEach(r => {
            output += 'MATCH: ' + r.name + ' (ID: ' + r.person_id + ') - Confidence: ' + (r.confidence * 100).toFixed(1) + '% - Status: ' + r.status + '\\n';
        });
    } else {
        output += 'No matching faces found.\\n';
    }
    output += '\\n=== Debug Logs ===\\n';
    (data.debug_logs || []).forEach(l => {
        output += '[' + l.event + '] ' + l.details + '\\n';
    });
    el.textContent = output;
}

// --- Classwise Monitoring ---
async function startClasswise() {
    const body = {
        confidence_threshold: parseInt(document.getElementById('cw-threshold').value) / 100.0,
    };
    const resp = await fetch('/api/attendance/start-classwise', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const data = await resp.json();
    if (data.status === 'started') {
        showAlert('Classwise monitoring started: ' + data.classroom_cameras + ' classrooms + ' + data.gate_cameras + ' gates, ' + data.total_faces + ' faces', 'success');
    } else {
        showAlert('Classwise: ' + (data.error || data.status), 'error');
    }
    refreshAttStatus();
}

async function loadClasswiseCameras() {
    const resp = await fetch('/api/attendance/classwise-cameras');
    const data = await resp.json();
    const tbody = document.getElementById('cw-cameras-body');
    tbody.innerHTML = '';
    (data.cameras || []).forEach(c => {
        tbody.innerHTML += '<tr><td>' + c.location + '</td><td>' + (c.grade || 'ALL') + '</td><td>DVR ' + (c.dvr_index + 1) + '</td><td>' + c.channel + '</td><td>' + c.faces_for_grade + '</td><td>' + (c.is_gate ? 'Entry Gate' : 'Classroom') + '</td></tr>';
    });
    document.getElementById('classwise-cameras').style.display = 'block';
    showAlert('Found ' + data.total_classroom + ' classroom cameras + ' + data.total_gate + ' entry gate cameras', 'info');
}

// --- Init ---
async function loadConfig() {
    const resp = await fetch('/api/config');
    const data = await resp.json();
    dvrs = data.dvrs.map(d => ({...d, password: ''}));
    currentMapping = data.camera_mapping || {};
    renderDvrs();
    renderMapping();
    const statusEl = document.getElementById('ws-status');
    if (data.ws_connected) {
        statusEl.textContent = 'Connected to Cloud Bot';
        statusEl.className = 'status connected';
    } else {
        statusEl.textContent = 'Disconnected';
        statusEl.className = 'status disconnected';
    }
}

// Load config, attendance status, and refresh periodically
loadConfig();
refreshAttStatus();
loadAttendanceLogs();
loadRegistered();
setInterval(async () => {
    try {
        const resp = await fetch('/api/config');
        const data = await resp.json();
        const statusEl = document.getElementById('ws-status');
        if (data.ws_connected) {
            statusEl.textContent = 'Connected to Cloud Bot';
            statusEl.className = 'status connected';
        } else {
            statusEl.textContent = 'Disconnected';
            statusEl.className = 'status disconnected';
        }
    } catch(e) {}
    // Auto-refresh attendance status
    refreshAttStatus();
}, 5000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = config.get("local_port", 8897)
    logger.info(f"Starting PPIS Campus Agent on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

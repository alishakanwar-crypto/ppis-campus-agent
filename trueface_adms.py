"""
TrueFace 3000 ADMS Protocol Integration
========================================
Implements the ZKTeco ADMS push protocol to receive attendance events
from the TimeWatch TrueFace 3000 face recognition terminal.

Protocol flow:
1. Device polls GET /iclock/getrequest?SN=xxx → server responds "OK" or commands
2. Device pushes POST /iclock/cdata?SN=xxx&table=ATTLOG → attendance logs
3. Device pushes POST /iclock/cdata?SN=xxx&table=OPERLOG → user operations
4. Device confirms POST /iclock/devicecmd?SN=xxx → command acknowledgements

When an attendance event is received, we look up the teacher by PIN
and send a WhatsApp notification via the cloud API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger("ppis-agent.trueface")

# IST timezone offset
IST = timezone(timedelta(hours=5, minutes=30))

# File to store PIN → teacher mapping
TRUEFACE_USERS_FILE = Path(__file__).parent / "trueface_users.json"

# Command queue: commands to send back to device via /iclock/getrequest
_command_queue: list[str] = []

# Dedup: track who was already notified today
_notified_today: dict[str, str] = {}  # PIN → date string
_last_dedup_date: str = ""


router = APIRouter()


def _load_users() -> dict[str, dict]:
    """Load PIN → teacher mapping from JSON file.
    
    Format: {"1": {"name": "Alisha Ahuja", "phone": "918076455224", "person_id": "TEACHER_ALISHA_AHUJA"}, ...}
    """
    if TRUEFACE_USERS_FILE.exists():
        try:
            with open(TRUEFACE_USERS_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load trueface_users.json: {e}")
    return {}


def _save_users(users: dict[str, dict]):
    """Save PIN → teacher mapping to JSON file."""
    with open(TRUEFACE_USERS_FILE, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


def _clear_daily_dedup():
    """Reset dedup cache if the date has changed."""
    global _notified_today, _last_dedup_date
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != _last_dedup_date:
        _notified_today = {}
        _last_dedup_date = today


def add_user(pin: str, name: str, phone: str, person_id: str = ""):
    """Register a teacher in the PIN → info mapping."""
    users = _load_users()
    users[pin] = {
        "name": name,
        "phone": phone,
        "person_id": person_id or f"TEACHER_{name.upper().replace(' ', '_')}",
    }
    _save_users(users)
    logger.info(f"TrueFace user registered: PIN={pin}, name={name}, phone={phone}")


def remove_user(pin: str):
    """Remove a teacher from the mapping."""
    users = _load_users()
    if pin in users:
        del users[pin]
        _save_users(users)


def get_all_users() -> dict[str, dict]:
    """Get all registered TrueFace users."""
    return _load_users()


def queue_command(cmd: str):
    """Add a command to be sent to device on next getrequest poll."""
    _command_queue.append(cmd)


async def _send_teacher_notification(name: str, phone: str, time_str: str):
    """Send WhatsApp notification to teacher that attendance was marked."""
    api_url = os.environ.get("CLOUD_BOT_URL", "https://ppis-whatsapp-bot.fly.dev")
    agent_secret = os.environ.get("AGENT_SECRET", "")
    headers = {"Content-Type": "application/json"}
    if agent_secret:
        headers["X-Agent-Secret"] = agent_secret

    display_name = name.title() if name == name.upper() else name

    payload = {
        "phone": phone,
        "template_name": "ppis_teacher_present_text",
        "template_params": [display_name, time_str],
        "language_code": "en",
    }

    logger.info(f"[TRUEFACE] Sending WhatsApp to {phone} for {display_name} at {time_str}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{api_url}/api/send-whatsapp",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    logger.info(f"[TRUEFACE] WhatsApp sent to {phone} for {display_name}")
                    return True
            logger.warning(f"[TRUEFACE] WhatsApp failed for {phone}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"[TRUEFACE] WhatsApp error for {phone}: {e}")
    return False


def _parse_attlog(body: str) -> list[dict]:
    """Parse ATTLOG body from device.
    
    Format: PIN\tTimestamp\tStatus\tVerify\tWorkCode\tReserved1\tReserved2
    Example: 1\t2026-05-25 07:30:00\t0\t15\t0\t1\t0
    
    Status: 0=check-in, 1=check-out
    Verify: 1=fingerprint, 4=card, 15=face
    """
    records = []
    for line in body.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            record = {
                "pin": parts[0].strip(),
                "timestamp": parts[1].strip() if len(parts) > 1 else "",
                "status": parts[2].strip() if len(parts) > 2 else "0",
                "verify": parts[3].strip() if len(parts) > 3 else "0",
            }
            records.append(record)
    return records


# ============================================================
# ADMS Protocol Endpoints
# ============================================================

@router.get("/iclock/cdata")
async def iclock_cdata_get(request: Request):
    """Device initial handshake / info push (GET).
    
    Some devices send GET to /iclock/cdata on first connect with device info.
    Respond with OK to acknowledge.
    """
    sn = request.query_params.get("SN", "unknown")
    logger.info(f"[TRUEFACE] Device handshake from SN={sn}")
    return PlainTextResponse("OK")


@router.post("/iclock/cdata")
async def iclock_cdata_post(request: Request):
    """Receive attendance logs and operation logs from device.
    
    Query params:
        SN: device serial number
        table: ATTLOG (attendance) or OPERLOG (user operations)
        Stamp: sync stamp
    """
    sn = request.query_params.get("SN", "unknown")
    table = request.query_params.get("table", "").upper()
    
    body = await request.body()
    body_text = body.decode("utf-8", errors="replace")
    
    logger.info(f"[TRUEFACE] POST /iclock/cdata SN={sn} table={table} body_len={len(body_text)}")
    
    if table == "ATTLOG":
        # Attendance log — process and notify
        records = _parse_attlog(body_text)
        _clear_daily_dedup()
        users = _load_users()
        
        for record in records:
            pin = record["pin"]
            timestamp = record["timestamp"]
            
            logger.info(f"[TRUEFACE] Attendance: PIN={pin} time={timestamp} "
                       f"status={record['status']} verify={record['verify']}")
            
            # Check if user is registered in our mapping
            user = users.get(pin)
            if not user:
                logger.warning(f"[TRUEFACE] Unknown PIN={pin} — not in trueface_users.json")
                continue
            
            # Dedup: only notify once per day per person
            today = datetime.now(IST).strftime("%Y-%m-%d")
            if _notified_today.get(pin) == today:
                logger.info(f"[TRUEFACE] Already notified {user['name']} today, skipping")
                continue
            
            # Check if it's a weekend
            now = datetime.now(IST)
            if now.weekday() in (5, 6):  # Saturday=5, Sunday=6
                logger.info(f"[TRUEFACE] Weekend — skipping notification for {user['name']}")
                continue
            
            # Extract time for notification
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                time_str = dt.strftime("%I:%M %p")
            except (ValueError, TypeError):
                time_str = datetime.now(IST).strftime("%I:%M %p")
            
            # Send WhatsApp notification
            phone = user.get("phone", "")
            if phone:
                _notified_today[pin] = today
                asyncio.create_task(
                    _send_teacher_notification(user["name"], phone, time_str)
                )
            else:
                logger.warning(f"[TRUEFACE] No phone for {user['name']} (PIN={pin})")
    
    elif table == "OPERLOG":
        # User operation log (registration, deletion, etc.)
        logger.info(f"[TRUEFACE] OPERLOG: {body_text[:500]}")
    
    else:
        logger.info(f"[TRUEFACE] Unknown table={table}: {body_text[:200]}")
    
    return PlainTextResponse("OK")


@router.get("/iclock/getrequest")
async def iclock_getrequest(request: Request):
    """Device polls for pending commands.
    
    Respond with "OK" if no commands, or with command string.
    Commands format: C:{id}:{command}
    """
    sn = request.query_params.get("SN", "unknown")
    
    if _command_queue:
        cmd = _command_queue.pop(0)
        logger.info(f"[TRUEFACE] Sending command to SN={sn}: {cmd[:100]}")
        return PlainTextResponse(cmd)
    
    return PlainTextResponse("OK")


@router.post("/iclock/devicecmd")
async def iclock_devicecmd(request: Request):
    """Device confirms command execution."""
    sn = request.query_params.get("SN", "unknown")
    body = await request.body()
    body_text = body.decode("utf-8", errors="replace")
    logger.info(f"[TRUEFACE] Command ACK from SN={sn}: {body_text[:200]}")
    return PlainTextResponse("OK")


# ============================================================
# Management API (for campus agent internal use)
# ============================================================

@router.get("/api/trueface/users")
async def list_trueface_users():
    """List all registered TrueFace users."""
    return get_all_users()


@router.post("/api/trueface/users")
async def register_trueface_user(request: Request):
    """Register a new TrueFace user (PIN → teacher mapping).
    
    Body: {"pin": "1", "name": "Alisha Ahuja", "phone": "918076455224"}
    """
    data = await request.json()
    pin = str(data.get("pin", ""))
    name = data.get("name", "")
    phone = data.get("phone", "")
    person_id = data.get("person_id", "")
    
    if not pin or not name:
        return {"error": "pin and name are required"}
    
    add_user(pin, name, phone, person_id)
    return {"status": "ok", "pin": pin, "name": name}


@router.delete("/api/trueface/users/{pin}")
async def delete_trueface_user(pin: str):
    """Remove a TrueFace user mapping."""
    remove_user(pin)
    return {"status": "ok", "deleted": pin}


@router.get("/api/trueface/status")
async def trueface_status():
    """Get TrueFace integration status."""
    users = _load_users()
    return {
        "registered_users": len(users),
        "notified_today": len(_notified_today),
        "pending_commands": len(_command_queue),
        "users": users,
    }

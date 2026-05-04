# Devin Session Export — Face Recognition Attendance System

**Session ID:** `07121f882a3b4637847c2c34319cb414`
**Session URL:** https://app.devin.ai/sessions/07121f882a3b4637847c2c34319cb414
**Status:** Suspended (inactivity)
**Date:** April 2026

---

## Project Summary

Built a **face recognition attendance system** integrated with the PPIS Campus Agent and WhatsApp bot backend. The system captures faces from school CCTV/DVR cameras, matches them against a registered database, marks attendance, and sends WhatsApp notifications to parents/staff.

---

## Repositories Involved

### 1. ppis-campus-agent
- **GitHub:** https://github.com/alishakanwar-crypto/ppis-campus-agent
- **Purpose:** Local agent running on the school Windows PC, bridges Hikvision DVR hardware with the cloud bot
- **Location on school PC:** `C:\Users\DELL\ppis-campus-agent`
- **Run command:** `py -3.12 main.py` (requires Python 3.12, NOT 3.14)
- **Local URL:** http://localhost:8899

### 2. whatsapp-bot-backend
- **GitHub:** https://github.com/alishakanwar-crypto/whatsapp-bot-backend
- **Purpose:** Cloud bot handling WhatsApp messaging, DVR config storage, and now face data storage
- **Cloud deployment:** `app-itszlsnn.fly.dev` (Fly.io)
- **Hostinger instances:**
  - Original: http://62.72.12.208:5000 (code at `/opt/whatsapp-bot-api/`)
  - PPIS copy: http://62.72.12.208:5001 (code at `/opt/ppis/`)
  - PP International School copy: http://62.72.12.208:5002 (code at `/opt/pp-international-school/`)

---

## What Was Built

### Face Recognition Attendance Features
1. **Face Registration** — Upload face images from 3 angles (front/left/right), tagged with name, role, person ID
2. **Camera Monitoring** — Continuously captures frames from DVR entrance camera via Hikvision ISAPI, runs face detection + recognition
3. **Attendance Logic** — Marks "Present" when confidence >85%, 5-minute cooldown between entries, captures snapshot as proof
4. **WhatsApp Notifications** — Sends attendance confirmation via WhatsApp when a person is recognized
5. **Cloud Face Sync** — Register a face once in the cloud DB, all campus agents auto-download it on startup (no need to register on each PC separately)

### New Files Added to ppis-campus-agent
- `attendance_engine.py` — Core face detection, recognition, and attendance marking logic
- `face_db.py` — Local face database management and cloud sync
- `templates/index.html` — Updated UI with Attendance tab

### New Endpoints Added to whatsapp-bot-backend
- `POST /api/face/register` — Register a face in the cloud database
- `GET /api/face/images` — Download all registered face images (for agent sync)
- `POST /api/send-whatsapp` — Send WhatsApp message (used by attendance notifications), authenticated with `X-Agent-Secret` header

---

## PRs Created (All Merged)

| PR | Repo | Description |
|----|------|-------------|
| [#2](https://github.com/alishakanwar-crypto/ppis-campus-agent/pull/2) | ppis-campus-agent | Face recognition attendance system + cloud sync |
| [#3](https://github.com/alishakanwar-crypto/ppis-campus-agent/pull/3) | ppis-campus-agent | Updated agent to point to new cloud bot (`app-itszlsnn.fly.dev`) |
| [#4](https://github.com/alishakanwar-crypto/ppis-campus-agent/pull/4) | ppis-campus-agent | Fixed image format issue for cloud face sync |
| [#5](https://github.com/alishakanwar-crypto/whatsapp-bot-backend/pull/5) | whatsapp-bot-backend | Added `/api/send-whatsapp` endpoint + cloud face storage endpoints |

---

## Test Results

| Test | Result |
|------|--------|
| Face registration (valid image) | PASSED — `face_id:2, person_id:TEST001` |
| No-face image rejection | PASSED — `"No face detected in image"` |
| Manual face recognition | PASSED — **100% confidence match**, attendance logged |
| Status + logs + debug events | PASSED — full detection chain: face_detected → face_matched → attendance_marked |
| Face deletion + cleanup | PASSED — clean removal from DB |
| Alisha's photo recognition | PASSED — 100% confidence, marked Present at 07:26 AM |

---

## Infrastructure & Architecture

### How It Works (End-to-End Flow)
```
School Entry Gate Camera (Hikvision DVR)
        ↓ ISAPI HTTP request
Campus Agent (school PC, port 8899)
        ↓ captures frame, runs face_recognition
Face Match Found (confidence > 85%)
        ↓ marks attendance in local DB
        ↓ sends POST to cloud bot
Cloud Bot (app-itszlsnn.fly.dev)
        ↓ POST /api/send-whatsapp
WhatsApp Notification to parent/staff
```

### Cloud Bot (Fly.io)
- **URL:** `app-itszlsnn.fly.dev`
- **Contains:** DVR configs, camera mappings (3 DVRs, 89-91 cameras), face registrations
- **Agent config endpoint:** `GET /api/agent-config/full`
- **Face sync endpoint:** `GET /api/face/images`

### Campus Agent (School PC)
- **Location:** `C:\Users\DELL\ppis-campus-agent`
- **Python version:** 3.12 (3.14 does NOT work — dlib/face_recognition incompatible)
- **Dependencies requiring special install:**
  - dlib: `py -3.12 -m pip install https://github.com/z-mahmud22/Dlib_Windows_Python3.x/raw/refs/heads/main/dlib-19.24.99-cp312-cp312-win_amd64.whl`
  - setuptools: `py -3.12 -m pip install "setuptools==69.5.1"` (version 82+ removed pkg_resources)
  - face_recognition_models: `py -3.12 -m pip install git+https://github.com/ageitgey/face_recognition_models`

### DVR Hardware (School LAN)
- DVR IPs: 192.168.0.11, 192.168.0.12, 192.168.0.14
- Protocol: Hikvision ISAPI (HTTP Digest/Basic auth)
- **Important:** DVRs are only accessible from the school network — the cloud bot cannot reach them directly

---

## Registered Face Data

| Person ID | Name | Role | Phone |
|-----------|------|------|-------|
| ALISHA001 | Alisha | Teacher | 8076455224 |

---

## Known Issues & Where It Left Off

1. **Cloud face sync warning:** `"Unsupported image type, must be 8bit gray or RGB image"` — Fix was pushed in PR #4 but user hadn't pulled it on the school PC yet
2. **Port 8899 conflict:** Old agent process may still be running. Fix: `taskkill /F /IM python.exe` before starting new agent
3. **WhatsApp notification 404:** The `/api/send-whatsapp` endpoint was deployed to Fly.io but needs the WhatsApp API keys configured in the cloud bot's environment
4. **Python version:** School PC has Python 3.14 as default. Must always use `py -3.12` prefix for all commands

---

## Quick Start Commands (School PC)

### Start the agent:
```
cd C:\Users\DELL\ppis-campus-agent
taskkill /F /IM python.exe
git pull
py -3.12 -m pip install -r requirements.txt
py -3.12 main.py
```

### Register a new face via cloud API:
```
curl -X POST https://app-itszlsnn.fly.dev/api/face/register \
  -F "person_id=PERSON001" \
  -F "name=Person Name" \
  -F "role=Teacher" \
  -F "phone=9999999999" \
  -F "image=@photo.jpg"
```

### Check cloud bot health:
```
curl https://app-itszlsnn.fly.dev/healthz
```

---

## Related Sessions & Context

- **WhatsApp Bot Backend setup:** Session `d9e4daf668b24360ac6e1d8646d7b555` — security fixes, PPIS parallel instance, PP International School instance
- **Server credentials:** root@62.72.12.208 (password in separate session — CHANGE IT)
- **Fly.io:** Logged in via Google/GitHub OAuth through Devin's desktop (no API token saved)

---

## Next Steps (Not Yet Done)

1. Pull PR #4 fix on school PC: `cd C:\Users\DELL\ppis-campus-agent && git pull`
2. Test live at entry gate: start agent → walk past entry camera → verify recognition + attendance + WhatsApp notification
3. Configure WhatsApp API keys in cloud bot environment for notifications to actually send
4. Register more staff/student faces in the cloud database
5. Set up the agent to auto-start on school PC boot (Windows Task Scheduler or startup folder)

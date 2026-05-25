"""
TrueFace 3000 API Probe & Test
================================
Tests direct HTTP API access to the TrueFace device, bypassing the
Selenium/Chrome web scraping approach. If the API works reliably,
it can replace the browser-based poller entirely.

Usage:
    python trueface_api_test.py          # Probe all known API paths
    python trueface_api_test.py --poll   # Live-poll attendance logs via API

Device: TrueFace 3000 (ZKTeco-based)
IP: 192.168.1.112 (default, override with TRUEFACE_IP env var)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEVICE_IP = os.environ.get("TRUEFACE_IP", "192.168.1.112")
DEVICE_USER = os.environ.get("TRUEFACE_USER", "admin")
DEVICE_PASS = os.environ.get("TRUEFACE_PASS", "tipl9910")
DEVICE_PORT = int(os.environ.get("TRUEFACE_PORT", "80"))
BASE_URL = f"http://{DEVICE_IP}:{DEVICE_PORT}"

IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("trueface_api_test")

# ---------------------------------------------------------------------------
# Known ZKTeco / TrueFace API endpoints to probe
# ---------------------------------------------------------------------------

# ZKTeco devices commonly expose these APIs:
PROBE_PATHS = [
    # ICLOCK protocol (ZKTeco standard)
    "/iclock/cdata?action=options&language=83",
    "/iclock/getrequest",

    # Attendance log endpoints
    "/cdata",
    "/cdata?action=options",
    "/iclock/cdata",

    # ISAPI-style endpoints
    "/ISAPI/AccessControl/AcsEvent/search",
    "/ISAPI/AccessControl/UserInfo/search",
    "/ISAPI/System/deviceInfo",
    "/ISAPI/System/status",

    # ZKTeco push protocol
    "/iclock/push/options",
    "/iclock/devicecmd",

    # Common REST API paths
    "/api/attendance",
    "/api/attendance/logs",
    "/api/users",
    "/api/device/info",
    "/api/records",
    "/api/transaction/listatt",

    # ZKBioAccess / ZKBioSecurity endpoints
    "/ZKBioSecurity/api/accreport/getAccReport",
    "/ZKAccess/api/transaction/listatt",

    # ADMS endpoints
    "/adms/cdata",
    "/adms/getrequest",

    # iClock endpoints (standard ZKTeco)
    "/iclock/attlog",
    "/iclock/operlog",
    "/iclock/enrolluser",

    # Web UI API (Vue.js backend)
    "/api/record/query",
    "/api/record/list",
    "/api/user/list",
    "/api/device/info",
    "/action/FetchAttLog",
    "/action/FetchUserInfo",

    # Direct CGI
    "/cgi-bin/att.cgi",
    "/cgi-bin/records.cgi",

    # Root and common pages
    "/",
    "/index.html",
]

# Auth combinations to try
AUTH_METHODS = [
    ("none", None),
    ("basic", httpx.BasicAuth(DEVICE_USER, DEVICE_PASS)),
    ("digest", httpx.DigestAuth(DEVICE_USER, DEVICE_PASS)),
]


def probe_device():
    """Probe the TrueFace device for working API endpoints."""
    logger.info("=" * 60)
    logger.info("TrueFace API Probe")
    logger.info("Device: %s", BASE_URL)
    logger.info("Credentials: %s / %s", DEVICE_USER, DEVICE_PASS)
    logger.info("=" * 60)

    # Quick connectivity check
    logger.info("\n--- Connectivity Check ---")
    try:
        r = httpx.get(BASE_URL, timeout=5, follow_redirects=True)
        logger.info("Root: HTTP %d (%d bytes) content-type=%s",
                     r.status_code, len(r.content),
                     r.headers.get("content-type", "?"))
        if r.status_code == 200:
            # Check if it's a web UI
            if "html" in r.headers.get("content-type", ""):
                logger.info("  → HTML page (likely web UI)")
                # Look for Vue.js or API hints in the page
                page = r.text[:2000].lower()
                if "vue" in page:
                    logger.info("  → Vue.js detected")
                if "api" in page:
                    logger.info("  → API references found in page")
    except Exception as e:
        logger.error("Cannot reach device: %s", e)
        return

    # Probe all endpoints
    logger.info("\n--- Probing API Endpoints ---")
    working = []

    for path in PROBE_PATHS:
        url = f"{BASE_URL}{path}"

        for auth_name, auth in AUTH_METHODS:
            try:
                # Try GET first
                r = httpx.get(url, auth=auth, timeout=5, follow_redirects=False)

                if r.status_code == 401 and auth_name == "none":
                    continue  # Expected — needs auth
                if r.status_code == 401:
                    continue  # This auth method doesn't work

                ct = r.headers.get("content-type", "")
                body_preview = r.text[:200] if r.text else ""

                if r.status_code in (200, 201, 204):
                    logger.info("✓ GET %s [%s] → %d (%d bytes) %s",
                               path, auth_name, r.status_code, len(r.content), ct)
                    if body_preview:
                        logger.info("  Body: %s", body_preview[:150])
                    working.append({
                        "method": "GET",
                        "path": path,
                        "auth": auth_name,
                        "status": r.status_code,
                        "content_type": ct,
                        "body_preview": body_preview,
                    })
                    break  # Found working auth for this path
                elif r.status_code == 405:
                    # Method not allowed — try POST
                    rp = httpx.post(url, auth=auth, timeout=5,
                                    json={}, follow_redirects=False)
                    if rp.status_code in (200, 201, 204):
                        logger.info("✓ POST %s [%s] → %d (%d bytes)",
                                   path, auth_name, rp.status_code, len(rp.content))
                        working.append({
                            "method": "POST",
                            "path": path,
                            "auth": auth_name,
                            "status": rp.status_code,
                        })
                        break
                elif r.status_code not in (404, 403):
                    logger.info("? GET %s [%s] → %d", path, auth_name, r.status_code)

            except httpx.TimeoutException:
                if auth_name == "none":
                    continue
                logger.warning("  TIMEOUT: %s [%s]", path, auth_name)
                break
            except Exception:
                continue

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("RESULTS: %d working endpoint(s) found", len(working))
    logger.info("=" * 60)
    for w in working:
        logger.info("  %s %s [auth=%s] → HTTP %d",
                     w["method"], w["path"], w["auth"], w["status"])

    if not working:
        logger.info("\nNo API endpoints found. The device may only support:")
        logger.info("  1. Web UI (Selenium scraping — current approach)")
        logger.info("  2. ADMS push protocol (device pushes to our server)")
        logger.info("  3. SDK access (vendor-specific)")

    return working


def poll_via_api(endpoint: str, auth_name: str):
    """Live-poll attendance records via a discovered API endpoint."""
    logger.info("Polling %s%s every 3 seconds...", BASE_URL, endpoint)

    auth = None
    if auth_name == "basic":
        auth = httpx.BasicAuth(DEVICE_USER, DEVICE_PASS)
    elif auth_name == "digest":
        auth = httpx.DigestAuth(DEVICE_USER, DEVICE_PASS)

    seen = set()
    while True:
        try:
            url = f"{BASE_URL}{endpoint}"
            r = httpx.get(url, auth=auth, timeout=10)
            if r.status_code == 200:
                data = r.text
                try:
                    jdata = r.json()
                    if isinstance(jdata, list):
                        for item in jdata:
                            key = json.dumps(item, sort_keys=True)
                            if key not in seen:
                                seen.add(key)
                                logger.info("NEW: %s", json.dumps(item))
                    elif isinstance(jdata, dict):
                        records = jdata.get("data", jdata.get("records", jdata.get("rows", [])))
                        if isinstance(records, list):
                            for item in records:
                                key = json.dumps(item, sort_keys=True)
                                if key not in seen:
                                    seen.add(key)
                                    logger.info("NEW: %s", json.dumps(item))
                except json.JSONDecodeError:
                    # Not JSON — might be plain text log
                    for line in data.strip().split("\n"):
                        line = line.strip()
                        if line and line not in seen:
                            seen.add(line)
                            logger.info("NEW: %s", line)
        except Exception as e:
            logger.error("Poll error: %s", e)

        time.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TrueFace API Probe & Test")
    parser.add_argument("--poll", metavar="PATH",
                        help="Live-poll a specific API endpoint")
    parser.add_argument("--auth", default="none",
                        choices=["none", "basic", "digest"],
                        help="Auth method for --poll")
    args = parser.parse_args()

    if args.poll:
        poll_via_api(args.poll, args.auth)
    else:
        probe_device()

"""
TrueFace 3000 Auto-Poller
=========================
Runs as a background process on the school PC. Uses Selenium with headless
Chrome to login to the TrueFace web UI, navigate to Search Records, and
poll for new face-recognition events every 3 seconds. New events are sent
to the cloud API which handles arrival/departure tracking, WhatsApp
notifications, and daily Excel reports.

Usage:
    python trueface_poller.py          # Run in foreground
    python trueface_poller.py --test   # Quick connectivity test

The run_trueface.bat wrapper handles auto-restart on crash.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEVICE_IP = os.environ.get("TRUEFACE_IP", "192.168.1.112")
DEVICE_USER = os.environ.get("TRUEFACE_USER", "admin")
DEVICE_PASS = os.environ.get("TRUEFACE_PASS", "tipl9910")
DEVICE_URL = f"http://{DEVICE_IP}"

CLOUD_API = os.environ.get(
    "TRUEFACE_CLOUD_API",
    "https://ppis-whatsapp-bot.fly.dev/api/trueface/event",
)

POLL_INTERVAL = int(os.environ.get("TRUEFACE_POLL_SECONDS", "3"))
SCAN_DELAY = 1.5  # seconds to wait after clicking Query before reading table

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trueface_poller.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("trueface_poller")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

seen_keys: set[str] = set()
seen_date: str = ""
running = True


def _handle_signal(sig, frame):
    global running
    logger.info("Received signal %s — shutting down", sig)
    running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

def _create_driver():
    """Create a headless Chrome WebDriver."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,720")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-logging")
    opts.add_argument("--log-level=3")
    opts.add_argument("--ignore-certificate-errors")

    # Try to find chromedriver
    driver_path = None
    for candidate in [
        os.path.join(os.path.dirname(__file__), "chromedriver.exe"),
        os.path.join(os.path.dirname(__file__), "chromedriver"),
        "chromedriver",
    ]:
        if os.path.isfile(candidate):
            driver_path = candidate
            break

    if driver_path:
        service = Service(executable_path=driver_path)
        return webdriver.Chrome(service=service, options=opts)
    else:
        # Let Selenium find it automatically
        return webdriver.Chrome(options=opts)


def _login(driver):
    """Login to the TrueFace web UI."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    logger.info("Logging in to %s ...", DEVICE_URL)
    driver.get(DEVICE_URL)
    time.sleep(3)

    try:
        # Find username and password fields
        user_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[name='username'], input[placeholder*='user' i]"))
        )
        pass_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
        if not pass_inputs:
            logger.error("Could not find password field")
            return False

        user_input.clear()
        user_input.send_keys(DEVICE_USER)
        pass_inputs[0].clear()
        pass_inputs[0].send_keys(DEVICE_PASS)

        # Click login button
        login_btns = driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], button.login-btn, .login-btn, button")
        for btn in login_btns:
            text = btn.text.strip().lower()
            if text in ("login", "log in", "sign in", "ok", "submit", "\u767b\u5f55"):
                btn.click()
                break
        else:
            # Click the first button
            if login_btns:
                login_btns[0].click()

        time.sleep(3)

        # Check if login succeeded by looking for the page content
        if "login" in driver.current_url.lower() or "error" in driver.page_source.lower()[:500]:
            logger.warning("Login may have failed — checking page content...")

        logger.info("Login completed — current URL: %s", driver.current_url)
        return True

    except Exception as e:
        logger.error("Login failed: %s", e)
        return False


def _navigate_to_search_records(driver):
    """Navigate to the Search Records page.

    The TrueFace web UI sidebar has:
        System Log  (parent — click to expand)
          ├── System Log
          ├── Admin Log
          ├── Search Records  ← we need this
          └── Alarm Log
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    logger.info("Navigating to Search Records page...")
    time.sleep(2)

    # Step 1: Click "System Log" in the sidebar to expand submenu
    clicked_parent = False
    elements = driver.find_elements(By.CSS_SELECTOR, "a, span, div, li, p")
    for el in elements:
        try:
            text = el.text.strip()
            if text == "System Log" or text == "\u7cfb\u7edf\u65e5\u5fd7":
                el.click()
                time.sleep(1)
                logger.info("Clicked 'System Log' parent menu")
                clicked_parent = True
                break
        except Exception:
            continue

    if not clicked_parent:
        logger.warning("Could not find 'System Log' menu — trying direct navigation")

    # Step 2: Click "Search Records" in the expanded submenu
    time.sleep(1)
    elements = driver.find_elements(By.CSS_SELECTOR, "a, span, div, li, p")
    for el in elements:
        try:
            text = el.text.strip()
            if text == "Search Records" or text == "\u67e5\u8be2\u8bb0\u5f55":
                el.click()
                time.sleep(3)
                logger.info("Clicked 'Search Records' — current URL: %s", driver.current_url)
                return True
        except Exception:
            continue

    # Fallback: try direct URL hash navigation
    for path in [
        "#/SearchRecord", "#/searchRecord", "#/Record",
        "#/SystemLog/SearchRecord", "#/systemLog/searchRecord",
    ]:
        try:
            driver.get(f"{DEVICE_URL}/{path}")
            time.sleep(3)
            tables = driver.find_elements(By.TAG_NAME, "table")
            btns = [b for b in driver.find_elements(By.TAG_NAME, "button")
                    if b.text.strip().lower() in ("query", "search", "\u67e5\u8be2")]
            if tables or btns:
                logger.info("Found records page via %s", path)
                return True
        except Exception:
            continue

    logger.warning("Could not navigate to Search Records — will try polling from current page")
    return False


def _click_query(driver):
    """Click the Query/Search button to refresh records."""
    from selenium.webdriver.common.by import By

    buttons = driver.find_elements(By.TAG_NAME, "button")
    for btn in buttons:
        try:
            text = btn.text.strip().lower()
            if text in ("query", "search", "\u67e5\u8be2"):
                btn.click()
                return True
        except Exception:
            continue
    return False


def _extract_events(driver) -> list[dict]:
    """Extract face recognition events from the records table."""
    from selenium.webdriver.common.by import By

    events = []
    rows = driver.find_elements(By.CSS_SELECTOR, "table tr")

    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 8:
            continue

        uid = cells[1].text.strip() if len(cells) > 1 else ""
        name = cells[2].text.strip() if len(cells) > 2 else ""
        timestamp = cells[4].text.strip() if len(cells) > 4 else ""
        status = cells[5].text.strip() if len(cells) > 5 else ""
        method = cells[7].text.strip() if len(cells) > 7 else ""

        if status != "OK" or not uid:
            continue
        if method not in ("Face", "Fingerprint"):
            continue

        events.append({
            "pin": uid,
            "name": name,
            "timestamp": timestamp,
        })

    return events


# Events that failed to send — retry on next cycle
_pending_events: list[dict] = []


def _send_to_cloud(events: list[dict]) -> dict | None:
    """Send events to the cloud API with retry logic."""
    global _pending_events

    # Prepend any previously failed events
    if _pending_events:
        logger.info("Retrying %d previously failed event(s)...", len(_pending_events))
        events = _pending_events + events
        _pending_events = []

    for attempt in range(3):
        try:
            resp = httpx.post(
                CLOUD_API,
                json=events,
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error("Cloud API error: HTTP %d — %s", resp.status_code, resp.text[:200])
                if attempt < 2:
                    time.sleep(2)
                    continue
                return None
        except Exception as e:
            logger.error("Cloud API request failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(3)
                continue
            # Save events for retry on next poll cycle
            _pending_events.extend(events)
            logger.warning("Queued %d event(s) for retry on next cycle", len(events))
            return None
    return None


def _reset_daily():
    """Reset seen keys on new day."""
    global seen_keys, seen_date
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != seen_date:
        seen_keys = set()
        seen_date = today
        logger.info("New day: %s — reset seen events", today)


def _is_weekend() -> bool:
    return datetime.now(IST).weekday() in (5, 6)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def run_poller():
    """Main polling loop using Selenium."""
    global running

    logger.info("=" * 50)
    logger.info("TrueFace 3000 Auto-Poller starting")
    logger.info("Device: %s", DEVICE_URL)
    logger.info("Cloud API: %s", CLOUD_API)
    logger.info("Poll interval: %ds", POLL_INTERVAL)
    logger.info("=" * 50)

    driver = None
    consecutive_errors = 0
    max_errors = 10
    poll_count = 0
    SESSION_REFRESH_EVERY = 300  # Re-login every 300 polls (~15 min) to keep session alive

    while running:
        try:
            # Create driver if needed
            if driver is None:
                logger.info("Starting headless Chrome...")
                driver = _create_driver()
                if not _login(driver):
                    logger.error("Login failed — retrying in 30s")
                    driver.quit()
                    driver = None
                    time.sleep(30)
                    continue
                _navigate_to_search_records(driver)
                consecutive_errors = 0
                poll_count = 0

            # Periodic session refresh to prevent stale browser
            poll_count += 1
            if poll_count >= SESSION_REFRESH_EVERY:
                logger.info("Session refresh — restarting browser to prevent staleness")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None
                continue

            # Skip weekends
            if _is_weekend():
                time.sleep(60)
                continue

            _reset_daily()

            # Click Query to refresh
            query_ok = _click_query(driver)
            if not query_ok:
                logger.warning("Query button not found — refreshing page")
                _navigate_to_search_records(driver)
                time.sleep(2)
                _click_query(driver)
            time.sleep(SCAN_DELAY)

            # Extract events
            events = _extract_events(driver)

            # Filter to only new events
            new_events = []
            for evt in events:
                key = f"{evt['pin']}-{evt['timestamp']}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    new_events.append(evt)

            if new_events:
                logger.info("Sending %d new event(s) to cloud...", len(new_events))
                result = _send_to_cloud(new_events)
                if result:
                    for r in result.get("results", []):
                        status = r.get("status", "")
                        name = r.get("name", r.get("pin", ""))
                        t = r.get("time", "")
                        wa = r.get("whatsapp", "")
                        if status == "arrival":
                            logger.info(">>> ARRIVAL: %s at %s | WhatsApp: %s", name, t, wa)
                        elif status == "departure":
                            logger.info(">>> DEPARTURE: %s at %s | WhatsApp: %s", name, t, wa)
                        elif status == "updated_departure":
                            logger.info("Updated departure: %s at %s", name, t)
                        elif status == "skipped":
                            logger.info("Skipped: PIN %s (%s)", r.get("pin"), r.get("reason"))

            consecutive_errors = 0
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            running = False
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error("Poll error (#%d): %s", consecutive_errors, e)

            if consecutive_errors >= max_errors:
                logger.warning("Too many errors — restarting browser session")
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass
                driver = None
                consecutive_errors = 0
                time.sleep(10)
            else:
                time.sleep(POLL_INTERVAL)

    # Cleanup
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
    logger.info("TrueFace poller stopped.")


def test_connectivity():
    """Quick test: check device and cloud API connectivity."""
    print(f"Testing device at {DEVICE_URL} ...")
    try:
        r = httpx.get(DEVICE_URL, timeout=5, follow_redirects=True)
        print(f"  Device: HTTP {r.status_code} ({len(r.text)} bytes)")
    except Exception as e:
        print(f"  Device: FAILED — {e}")

    print(f"\nTesting cloud API at {CLOUD_API} ...")
    try:
        r = httpx.post(
            CLOUD_API,
            json={"pin": "test", "name": "Test", "timestamp": "2026-01-01 00:00:00"},
            timeout=10,
        )
        print(f"  Cloud: HTTP {r.status_code} — {r.text[:200]}")
    except Exception as e:
        print(f"  Cloud: FAILED — {e}")

    print("\nTesting Selenium/Chrome...")
    try:
        driver = _create_driver()
        driver.get(DEVICE_URL)
        print(f"  Chrome: OK — page title: {driver.title}")
        driver.quit()
    except Exception as e:
        print(f"  Chrome: FAILED — {e}")
        print("  Install: pip install selenium")
        print("  Also need chromedriver matching your Chrome version")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TrueFace 3000 Auto-Poller")
    parser.add_argument("--test", action="store_true", help="Test connectivity")
    args = parser.parse_args()

    if args.test:
        test_connectivity()
    else:
        run_poller()

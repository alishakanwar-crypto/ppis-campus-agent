"""
TrueFace 3000 Attendance Notifier.

Two modes:
  1. Browser mode (default): Paste trueface_notifier.js into Chrome Console
     on the device's Search Records page. It polls the table and sends
     WhatsApp notifications directly from the browser.

  2. Selenium mode: python trueface_poller.py --selenium
     Automates Chrome to read the device's Search Records page.
     Requires: pip install selenium

Run 'python trueface_poller.py --test' to send a test WhatsApp notification.
"""
import hashlib
import json
import logging
import re
import sys
import time
import httpx
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trueface")

IST = timezone(timedelta(hours=5, minutes=30))
DEVICE_IP = "192.168.1.112"
DEVICE_USER = "admin"
DEVICE_PASS = "tipl9910"
CLOUD_API = "https://ppis-whatsapp-bot.fly.dev/api/send-whatsapp"
POLL_INTERVAL = 30  # seconds

USERS_FILE = "trueface_users.json"
try:
    with open(USERS_FILE) as f:
        USERS = json.load(f)
    logger.info(f"Loaded {len(USERS)} users from {USERS_FILE}")
except Exception:
    USERS = {
        "1": {"name": "Alisha Ahuja", "phone": "918076455224", "person_id": "TEACHER_ALISHA_AHUJA"}
    }
    logger.info("Using default user list (Alisha only)")

_notified_today = {}
_last_date = ""


def _check_dedup(pin):
    global _last_date, _notified_today
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != _last_date:
        _notified_today.clear()
        _last_date = today
    if _notified_today.get(pin) == today:
        return True
    now = datetime.now(IST)
    if now.weekday() in (5, 6):
        logger.info("Weekend — skipping notification")
        return True
    return False


def _send_whatsapp(name, phone, time_str):
    try:
        resp = httpx.post(CLOUD_API, json={
            "to": phone,
            "template_name": "ppis_teacher_present_text",
            "language_code": "en",
            "body_params": [name, time_str],
        }, timeout=30)
        logger.info(f"WhatsApp -> {phone}: {resp.status_code} {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")
        return False


def _process_event(user_id, timestamp=None):
    pin = str(user_id)
    user = USERS.get(pin)
    if not user:
        logger.warning(f"Unknown user ID: {pin}")
        return
    name = user["name"]
    phone = user.get("phone", "")
    if not phone:
        logger.warning(f"No phone for {name}")
        return
    if _check_dedup(pin):
        logger.info(f"Already notified {name} today")
        return
    if timestamp:
        try:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            time_str = dt.strftime("%I:%M %p")
        except (ValueError, TypeError):
            time_str = datetime.now(IST).strftime("%I:%M %p")
    else:
        time_str = datetime.now(IST).strftime("%I:%M %p")

    logger.info(f">>> ATTENDANCE: {name} at {time_str} — sending WhatsApp to {phone}")
    _notified_today[pin] = datetime.now(IST).strftime("%Y-%m-%d")
    _send_whatsapp(name, phone, time_str)


def test_whatsapp():
    """Send a test WhatsApp notification for Alisha."""
    logger.info("Sending test WhatsApp notification...")
    time_str = datetime.now(IST).strftime("%I:%M %p")
    user = USERS.get("1", {})
    name = user.get("name", "Alisha Ahuja")
    phone = user.get("phone", "918076455224")
    _send_whatsapp(name, phone, time_str)


def selenium_mode():
    """Automate Chrome to read Search Records from the device web UI."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        logger.error("Selenium not installed. Run: pip install selenium")
        logger.info("Alternative: paste trueface_notifier.js into Chrome Console")
        return

    logger.info("Starting Chrome via Selenium...")
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-gpu")
    driver = webdriver.Chrome(options=options)
    seen_keys = set()

    try:
        # Step 1: Open device page
        driver.get(f"http://{DEVICE_IP}")
        logger.info("Opened device page, waiting for login form...")
        time.sleep(3)

        # Step 2: Login
        try:
            user_input = driver.find_element(By.CSS_SELECTOR, "input[type='text'], input[name='username'], #username")
            user_input.clear()
            user_input.send_keys(DEVICE_USER)
            pass_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password'], #password")
            pass_input.clear()
            pass_input.send_keys(DEVICE_PASS)
            login_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], .login-btn, button")
            login_btn.click()
            logger.info("Login submitted, waiting for dashboard...")
            time.sleep(5)
        except Exception as e:
            logger.warning(f"Login form not found or already logged in: {e}")

        # Step 3: Navigate to Search Records
        try:
            links = driver.find_elements(By.TAG_NAME, "a")
            for link in links:
                text = link.text.strip().lower()
                if "search" in text or "record" in text or "query" in text:
                    link.click()
                    logger.info(f"Clicked '{link.text.strip()}' link")
                    time.sleep(3)
                    break
            else:
                menus = driver.find_elements(By.CSS_SELECTOR, "li, .menu-item, [class*='menu']")
                for menu in menus:
                    text = menu.text.strip().lower()
                    if "search" in text or "record" in text:
                        menu.click()
                        logger.info(f"Clicked menu: '{menu.text.strip()}'")
                        time.sleep(3)
                        break
        except Exception as e:
            logger.warning(f"Could not find Search Records link: {e}")

        # Step 4: Poll loop
        logger.info("Starting poll loop...")
        while True:
            try:
                # Click Query button
                buttons = driver.find_elements(By.TAG_NAME, "button")
                for btn in buttons:
                    text = btn.text.strip().lower()
                    if text in ("query", "search", "查询"):
                        btn.click()
                        time.sleep(3)
                        break

                # Read table
                rows = driver.find_elements(By.CSS_SELECTOR, "table tr")
                for row in rows:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) < 8:
                        continue
                    user_id = cells[1].text.strip()
                    timestamp = cells[4].text.strip()
                    status = cells[5].text.strip()
                    method = cells[7].text.strip()

                    if status != "OK" or not user_id:
                        continue
                    if method not in ("Face", "Fingerprint"):
                        continue

                    key = f"{user_id}-{timestamp}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    _process_event(user_id, timestamp)

            except Exception as e:
                logger.error(f"Poll error: {e}")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        driver.quit()


def main():
    if "--test" in sys.argv:
        test_whatsapp()
        return

    if "--selenium" in sys.argv:
        selenium_mode()
        return

    # Default: print instructions for browser-based approach
    logger.info("=" * 50)
    logger.info("TrueFace 3000 Attendance Notifier")
    logger.info("=" * 50)
    logger.info("")
    logger.info("BROWSER MODE (recommended):")
    logger.info("  1. Open Chrome to http://192.168.1.112 and log in")
    logger.info("  2. Go to Search Records page")
    logger.info("  3. Click Query to load initial records")
    logger.info("  4. Press Ctrl+Shift+J to open Chrome Console")
    logger.info("  5. Paste the contents of trueface_notifier.js")
    logger.info("  6. Done! It polls every 30 seconds automatically")
    logger.info("")
    logger.info("OTHER MODES:")
    logger.info("  python trueface_poller.py --test       Send test WhatsApp")
    logger.info("  python trueface_poller.py --selenium   Automate Chrome")
    logger.info("")
    logger.info("Users loaded:")
    for pin, u in USERS.items():
        logger.info(f"  PIN {pin}: {u['name']} -> {u.get('phone', 'N/A')}")


if __name__ == "__main__":
    main()

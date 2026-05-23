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


_photo_debug_done = False


def _extract_snapshot_urls(driver) -> dict[str, str]:
    """Extract snapshot URLs from the Vue component data on all table rows.

    The TrueFace UI is a Vue.js app. Each table row's data includes a
    snapshot path like:
      /mnt/appdata1/userpic/SnapShot/2026-05-23/07/54/1660_99_100_20260523075434823.jpg

    We read this from Vue's internal data, then construct the full download URL.
    Returns a dict mapping "PIN-timestamp" keys to snapshot URLs.
    """
    try:
        url_map = driver.execute_script("""
            var result = {};
            // Try to get the Vue component that holds the table data
            var table = document.querySelector('.el-table');
            if (!table) return result;

            // Method 1: Access Vue component's data via __vue__
            var vue = table.__vue__;
            if (vue) {
                // Walk up to find the component with the data array
                var comp = vue;
                for (var i = 0; i < 10; i++) {
                    if (comp && comp.tableData) break;
                    if (comp && comp.$parent) comp = comp.$parent;
                    else break;
                }
                if (comp && comp.tableData) {
                    var data = comp.tableData;
                    for (var j = 0; j < data.length; j++) {
                        var row = data[j];
                        var pin = row.pin || row.PIN || row.userId || row.user_id || '';
                        var ts = row.time || row.timestamp || row.accessTime || '';
                        var pic = row.picturePath || row.picture || row.snapPath ||
                                  row.snap || row.photo || row.pic || row.filePath || '';
                        if (pin && pic) {
                            result[pin + '-' + ts] = pic;
                        }
                    }
                    return result;
                }
            }

            // Method 2: Try to find data in Vue 3 style (__vueParentComponent)
            var el = table.__vueParentComponent;
            if (el && el.proxy) {
                var proxy = el.proxy;
                var d = proxy.tableData || proxy.data || proxy.list || [];
                for (var k = 0; k < d.length; k++) {
                    var r = d[k];
                    var p = r.pin || r.PIN || r.userId || r.user_id || '';
                    var t = r.time || r.timestamp || r.accessTime || '';
                    var pic2 = r.picturePath || r.picture || r.snapPath ||
                               r.snap || r.photo || r.pic || r.filePath || '';
                    if (p && pic2) {
                        result[p + '-' + t] = pic2;
                    }
                }
                return result;
            }

            // Method 3: Scan all Vue instances on the page
            var allEls = document.querySelectorAll('*');
            for (var m = 0; m < allEls.length; m++) {
                var v = allEls[m].__vue__;
                if (!v) continue;
                var candidates = [v.tableData, v.data, v.list, v.records];
                for (var n = 0; n < candidates.length; n++) {
                    var arr = candidates[n];
                    if (!Array.isArray(arr) || arr.length === 0) continue;
                    var sample = arr[0];
                    if (!sample || typeof sample !== 'object') continue;
                    // Check if this looks like access log data
                    var keys = Object.keys(sample);
                    var hasPin = keys.some(function(k) { return k.toLowerCase().indexOf('pin') > -1 || k.toLowerCase().indexOf('userid') > -1; });
                    var hasPic = keys.some(function(k) { return k.toLowerCase().indexOf('pic') > -1 || k.toLowerCase().indexOf('snap') > -1 || k.toLowerCase().indexOf('photo') > -1 || k.toLowerCase().indexOf('path') > -1; });
                    if (hasPin && hasPic) {
                        for (var q = 0; q < arr.length; q++) {
                            var item = arr[q];
                            // Dump all keys for debug on first item
                            if (q === 0) {
                                result['__debug_keys__'] = Object.keys(item).join(',');
                                result['__debug_sample__'] = JSON.stringify(item).substring(0, 500);
                            }
                            var itemPin = item.pin || item.PIN || item.userId || item.user_id || '';
                            var itemTs = item.time || item.timestamp || item.accessTime || '';
                            var itemPic = '';
                            for (var r2 = 0; r2 < keys.length; r2++) {
                                var kk = keys[r2].toLowerCase();
                                if ((kk.indexOf('pic') > -1 || kk.indexOf('snap') > -1 || kk.indexOf('photo') > -1 || kk.indexOf('path') > -1 || kk.indexOf('file') > -1) && item[keys[r2]]) {
                                    itemPic = item[keys[r2]];
                                    break;
                                }
                            }
                            if (itemPin && itemPic) {
                                result[itemPin + '-' + itemTs] = itemPic;
                            }
                        }
                        return result;
                    }
                }
            }

            // Method 4: Just dump the first Vue component's data keys for debugging
            for (var z = 0; z < allEls.length; z++) {
                var vv = allEls[z].__vue__;
                if (!vv || !vv.$data) continue;
                var dkeys = Object.keys(vv.$data);
                if (dkeys.length > 0) {
                    result['__debug_vue_data_keys__'] = dkeys.join(',');
                    // Check each data key for arrays
                    for (var zz = 0; zz < dkeys.length; zz++) {
                        var val = vv.$data[dkeys[zz]];
                        if (Array.isArray(val) && val.length > 0 && typeof val[0] === 'object') {
                            result['__debug_array_' + dkeys[zz] + '_keys__'] = Object.keys(val[0]).join(',');
                            result['__debug_array_' + dkeys[zz] + '_sample__'] = JSON.stringify(val[0]).substring(0, 500);
                        }
                    }
                    break;
                }
            }

            return result;
        """)

        if url_map:
            # Log debug info on first call
            debug_keys = {k: v for k, v in url_map.items() if k.startswith("__debug")}
            if debug_keys:
                for k, v in debug_keys.items():
                    logger.info("Vue data %s: %s", k, v)

            # Filter to actual snapshot URLs
            return {k: v for k, v in url_map.items() if not k.startswith("__debug")}
    except Exception as e:
        logger.info("Vue data extraction failed: %s", e)

    return {}


def _fetch_snapshot_image(driver, snap_path: str) -> str:
    """Fetch the snapshot image from the device given its path.

    The path from Vue data might be relative like:
      /mnt/appdata1/userpic/SnapShot/2026-05-23/07/54/1660_99_100_20260523075434823.jpg
    We prepend the device URL and /RPC2_Loadfile prefix.
    """
    import base64

    base = DEVICE_URL.rstrip("/")

    # Build candidate URLs
    urls = []
    if snap_path.startswith("http"):
        urls.append(snap_path)
    else:
        # The device serves files via /RPC2_Loadfile prefix
        if snap_path.startswith("/mnt/"):
            urls.append(f"{base}/RPC2_Loadfile{snap_path}")
        elif snap_path.startswith("RPC2_Loadfile"):
            urls.append(f"{base}/{snap_path}")
        else:
            urls.append(f"{base}/RPC2_Loadfile/mnt/appdata1/userpic/{snap_path}")
            urls.append(f"{base}/RPC2_Loadfile/{snap_path}")
            urls.append(f"{base}/{snap_path}")

    for url in urls:
        try:
            resp = httpx.get(url, timeout=3, verify=False)
            if resp.status_code == 200 and len(resp.content) > 500:
                return base64.b64encode(resp.content).decode()
        except Exception:
            continue

    # Fallback: fetch via Selenium's authenticated session
    for url in urls:
        try:
            b64 = driver.execute_script("""
                var xhr = new XMLHttpRequest();
                xhr.open('GET', arguments[0], false);
                xhr.responseType = 'arraybuffer';
                xhr.send();
                if (xhr.status === 200 && xhr.response.byteLength > 500) {
                    var b = new Uint8Array(xhr.response), s = '';
                    for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
                    return btoa(s);
                }
                return '';
            """, url)
            if b64 and len(b64) > 100:
                return b64
        except Exception:
            continue

    return ""


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


def _attach_photos(driver, new_events: list[dict]) -> None:
    """Fetch live snapshots from the TrueFace device for new events.

    Reads the snapshot URL from the Vue.js component data (instant),
    then fetches the image via HTTP (~0.3s per teacher).
    Backend falls back to database photos if no snapshot is found.
    """
    global _photo_debug_done

    # Extract all snapshot URLs from Vue data in one JS call
    snap_urls = _extract_snapshot_urls(driver)

    if not _photo_debug_done and snap_urls:
        logger.info("Vue snapshot URLs found: %d entries", len(snap_urls))
        # Log first entry as sample
        for k, v in snap_urls.items():
            logger.info("  Sample: key=%s url=%s", k, str(v)[:120])
            break

    for evt in new_events:
        pin = evt.get("pin", "")
        ts = evt.get("timestamp", "")
        if not pin:
            continue

        # Try to match by PIN-timestamp key
        snap_path = snap_urls.get(f"{pin}-{ts}", "")

        # Also try just PIN match if exact key not found
        if not snap_path:
            for k, v in snap_urls.items():
                if k.startswith(f"{pin}-"):
                    snap_path = v
                    break

        if snap_path:
            photo_b64 = _fetch_snapshot_image(driver, snap_path)
            if not _photo_debug_done:
                _photo_debug_done = True
                logger.info(
                    "Live snapshot for %s (PIN=%s): %s (%d bytes) path=%s",
                    evt.get("name", "?"), pin,
                    "OK" if photo_b64 else "FETCH FAILED",
                    len(photo_b64) if photo_b64 else 0,
                    str(snap_path)[:100],
                )
            if photo_b64:
                evt["photo"] = photo_b64
                continue

        if not _photo_debug_done:
            _photo_debug_done = True
            logger.info(
                "Live snapshot for %s (PIN=%s): NO URL FOUND (backend will use DB photo)",
                evt.get("name", "?"), pin,
            )


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

            _reset_daily()

            # Click Query to refresh
            query_ok = _click_query(driver)
            if poll_count <= 3:
                logger.info("Poll #%d: Query click %s", poll_count, "OK" if query_ok else "FAILED")
            if not query_ok:
                logger.warning("Query button not found — refreshing page")
                _navigate_to_search_records(driver)
                time.sleep(2)
                _click_query(driver)
            time.sleep(SCAN_DELAY)

            # Extract events
            events = _extract_events(driver)

            # Verbose logging for first 5 polls and every 100th poll
            if poll_count <= 5 or (not events and poll_count % 100 == 0):
                from selenium.webdriver.common.by import By as _By
                rows = driver.find_elements(_By.CSS_SELECTOR, "table tr")
                # Sample first row's cell count and text for debugging
                sample = ""
                if rows:
                    cells = rows[0].find_elements(_By.TAG_NAME, "td")
                    th_cells = rows[0].find_elements(_By.TAG_NAME, "th")
                    sample = f", first_row: {len(cells)} td + {len(th_cells)} th"
                    if len(rows) > 1:
                        cells2 = rows[1].find_elements(_By.TAG_NAME, "td")
                        if cells2:
                            cell_texts = [c.text.strip()[:20] for c in cells2[:9]]
                            sample += f", row2_cells({len(cells2)}): {cell_texts}"
                logger.info(
                    "Poll #%d: %d events extracted, %d table rows, URL=%s%s",
                    poll_count, len(events), len(rows), driver.current_url, sample,
                )

            # Filter to only new events
            new_events = []
            for evt in events:
                key = f"{evt['pin']}-{evt['timestamp']}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    new_events.append(evt)

            if new_events:
                _attach_photos(driver, new_events)
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

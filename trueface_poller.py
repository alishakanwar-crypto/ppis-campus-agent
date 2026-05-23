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


def _deep_diag_vue(driver) -> dict:
    """One-time diagnostic: check Vue data structure (runs once per session)."""
    try:
        return driver.execute_script("""
            var out = {};
            var count = 0;

            // 1. Walk ALL elements looking for __vue__
            var allEls = document.querySelectorAll('*');
            var vueCount = 0;
            for (var i = 0; i < allEls.length && count < 5; i++) {
                var v = allEls[i].__vue__;
                if (!v) continue;
                vueCount++;

                // Check $data
                if (v.$data) {
                    var dkeys = Object.keys(v.$data);
                    for (var j = 0; j < dkeys.length; j++) {
                        var val = v.$data[dkeys[j]];
                        if (Array.isArray(val) && val.length > 0 && typeof val[0] === 'object') {
                            out['__vue_array_' + count + '_name'] = dkeys[j];
                            out['__vue_array_' + count + '_keys'] = Object.keys(val[0]).join(',');
                            out['__vue_array_' + count + '_sample'] = JSON.stringify(val[0]).substring(0, 800);
                            out['__vue_array_' + count + '_len'] = String(val.length);
                            count++;
                        }
                    }
                }

                // Check direct properties (store, $store, etc.)
                var props = ['store', '$store', 'tableData', 'data', 'list', 'records', 'logData', 'accessData'];
                for (var p = 0; p < props.length; p++) {
                    var pval = v[props[p]];
                    if (pval && typeof pval === 'object') {
                        // If it's a store, check state
                        if (pval.state) {
                            var skeys = Object.keys(pval.state);
                            out['__vuex_state_keys'] = skeys.join(',');
                            for (var s = 0; s < skeys.length; s++) {
                                var sv = pval.state[skeys[s]];
                                if (Array.isArray(sv) && sv.length > 0 && typeof sv[0] === 'object') {
                                    out['__vuex_array_' + skeys[s] + '_keys'] = Object.keys(sv[0]).join(',');
                                    out['__vuex_array_' + skeys[s] + '_sample'] = JSON.stringify(sv[0]).substring(0, 800);
                                }
                            }
                        }
                        if (Array.isArray(pval) && pval.length > 0 && typeof pval[0] === 'object') {
                            out['__vue_prop_' + props[p] + '_keys'] = Object.keys(pval[0]).join(',');
                            out['__vue_prop_' + props[p] + '_sample'] = JSON.stringify(pval[0]).substring(0, 800);
                        }
                    }
                }
            }
            out['__vue_instances_found'] = String(vueCount);

            // 2. Check the download icon's click handler
            var dlIcon = document.querySelector('i.el-icon-download, i.ui-pic');
            if (dlIcon) {
                // Try to get the Vue component for this icon's row
                var el = dlIcon.closest('tr') || dlIcon.closest('.el-table__row') || dlIcon.parentElement;
                if (el && el.__vue__) {
                    out['__dl_row_vue_keys'] = Object.keys(el.__vue__.$data || {}).join(',');
                    out['__dl_row_vue_props'] = Object.keys(el.__vue__.$props || {}).join(',');
                }
                // Walk up to find row data
                var walker = dlIcon;
                for (var w = 0; w < 10; w++) {
                    if (!walker) break;
                    if (walker.__vue__ && walker.__vue__.row) {
                        out['__dl_row_data_keys'] = Object.keys(walker.__vue__.row).join(',');
                        out['__dl_row_data_sample'] = JSON.stringify(walker.__vue__.row).substring(0, 800);
                        break;
                    }
                    walker = walker.parentElement;
                }

                // Get the onclick handler source
                var handlers = [];
                var evts = dlIcon._events || {};
                for (var ek in evts) handlers.push(ek);
                out['__dl_icon_events'] = handlers.join(',') || 'none';

                // Check if there's a @click Vue binding
                if (dlIcon.__vue__) {
                    out['__dl_icon_vue'] = JSON.stringify(Object.keys(dlIcon.__vue__)).substring(0, 300);
                }
            }

            // 3. Check el-table's store for data
            var table = document.querySelector('.el-table');
            if (table && table.__vue__) {
                var tv = table.__vue__;
                // el-table internally has store.states.data
                if (tv.store && tv.store.states) {
                    var states = tv.store.states;
                    var stateKeys = Object.keys(states);
                    out['__eltable_state_keys'] = stateKeys.join(',');
                    if (states.data && Array.isArray(states.data) && states.data.length > 0) {
                        out['__eltable_data_keys'] = Object.keys(states.data[0]).join(',');
                        out['__eltable_data_sample'] = JSON.stringify(states.data[0]).substring(0, 800);
                        out['__eltable_data_len'] = String(states.data.length);
                    }
                    // Try _data (Vue 2 reactive)
                    if (states._data && states._data.data && Array.isArray(states._data.data)) {
                        var dd = states._data.data;
                        if (dd.length > 0 && typeof dd[0] === 'object') {
                            out['__eltable_rdata_keys'] = Object.keys(dd[0]).join(',');
                            out['__eltable_rdata_sample'] = JSON.stringify(dd[0]).substring(0, 800);
                        }
                    }
                }
                // Also check $props.data
                if (tv.$props && tv.$props.data && Array.isArray(tv.$props.data) && tv.$props.data.length > 0) {
                    out['__eltable_props_data_keys'] = Object.keys(tv.$props.data[0]).join(',');
                    out['__eltable_props_data_sample'] = JSON.stringify(tv.$props.data[0]).substring(0, 800);
                }
                // Walk parents
                var parent = tv.$parent;
                for (var pp = 0; pp < 5 && parent; pp++) {
                    if (parent.$data) {
                        var pdkeys = Object.keys(parent.$data);
                        for (var pd = 0; pd < pdkeys.length; pd++) {
                            var pdv = parent.$data[pdkeys[pd]];
                            if (Array.isArray(pdv) && pdv.length > 0 && typeof pdv[0] === 'object') {
                                out['__parent' + pp + '_' + pdkeys[pd] + '_keys'] = Object.keys(pdv[0]).join(',');
                                out['__parent' + pp + '_' + pdkeys[pd] + '_sample'] = JSON.stringify(pdv[0]).substring(0, 800);
                                out['__parent' + pp + '_' + pdkeys[pd] + '_len'] = String(pdv.length);
                            }
                        }
                    }
                    parent = parent.$parent;
                }
            }

            return out;
        """)
    except Exception as e:
        return {"__error": str(e)}


def _fetch_snapshot_image(driver, snap_url: str) -> str:
    """Fetch a snapshot image given its full or partial URL.

    Tries multiple methods: async fetch API, async XHR, canvas, and
    direct httpx with digest auth. The path from Vue data is like:
      SnapShot/2026-05-23/07/54/1660_99_100_20260523075434823.jpg
    The device serves it at:
      /RPC2_Loadfile/mnt/appdata1/userpic/SnapShot/...
    """
    import base64

    # Build the relative URL path (for same-origin fetch in browser)
    if snap_url.startswith("http"):
        rel_path = snap_url
    elif snap_url.startswith("/RPC2"):
        rel_path = snap_url
    elif snap_url.startswith("/mnt/"):
        rel_path = f"/RPC2_Loadfile{snap_url}"
    elif snap_url.startswith("SnapShot") or snap_url.startswith("mnt/"):
        rel_path = f"/RPC2_Loadfile/mnt/appdata1/userpic/{snap_url}"
    else:
        rel_path = f"/RPC2_Loadfile/{snap_url}"

    # Method 1: fetch() API with blob — handles auth cookies automatically
    try:
        b64 = driver.execute_async_script("""
            var url = arguments[0];
            var done = arguments[arguments.length - 1];
            fetch(url, {credentials: 'include'})
                .then(function(r) {
                    if (!r.ok) { done('FETCH_ERR_' + r.status); return; }
                    return r.blob();
                })
                .then(function(blob) {
                    if (!blob || blob.size < 500) { done('BLOB_SMALL_' + (blob ? blob.size : 0)); return; }
                    var reader = new FileReader();
                    reader.onloadend = function() {
                        var dataUrl = reader.result || '';
                        var b64part = dataUrl.split(',')[1] || '';
                        done(b64part);
                    };
                    reader.readAsDataURL(blob);
                })
                .catch(function(e) { done('FETCH_EXC_' + e.message); });
        """, rel_path)
        if b64 and len(b64) > 100 and not b64.startswith(("FETCH_", "BLOB_")):
            logger.info("Snapshot fetched via fetch() API: %d bytes", len(b64))
            return b64
        logger.info("fetch() result: %s", str(b64)[:80] if b64 else "empty")
    except Exception as e:
        logger.info("fetch() failed: %s", e)

    # Method 2: async XHR with arraybuffer (sync XHR can't use arraybuffer)
    try:
        b64 = driver.execute_async_script("""
            var url = arguments[0];
            var done = arguments[arguments.length - 1];
            var xhr = new XMLHttpRequest();
            xhr.open('GET', url, true);
            xhr.responseType = 'arraybuffer';
            xhr.withCredentials = true;
            xhr.onload = function() {
                if (xhr.status === 200 && xhr.response && xhr.response.byteLength > 500) {
                    var bytes = new Uint8Array(xhr.response);
                    var binary = '';
                    var chunk = 8192;
                    for (var i = 0; i < bytes.length; i += chunk) {
                        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                    }
                    done(btoa(binary));
                } else {
                    done('XHR_ERR_' + xhr.status + '_' + (xhr.response ? xhr.response.byteLength : 0));
                }
            };
            xhr.onerror = function() { done('XHR_NETWORK_ERR'); };
            xhr.send();
        """, rel_path)
        if b64 and len(b64) > 100 and not b64.startswith("XHR_"):
            logger.info("Snapshot fetched via async XHR: %d bytes", len(b64))
            return b64
        logger.info("async XHR result: %s", str(b64)[:80] if b64 else "empty")
    except Exception as e:
        logger.info("async XHR failed: %s", e)

    # Method 3: Create an <img>, draw to canvas, extract base64
    try:
        b64 = driver.execute_async_script("""
            var url = arguments[0];
            var done = arguments[arguments.length - 1];
            var img = new Image();
            img.crossOrigin = 'use-credentials';
            img.onload = function() {
                try {
                    var c = document.createElement('canvas');
                    c.width = img.naturalWidth;
                    c.height = img.naturalHeight;
                    c.getContext('2d').drawImage(img, 0, 0);
                    var dataUrl = c.toDataURL('image/jpeg', 0.92);
                    done(dataUrl.split(',')[1] || '');
                } catch(e) { done('CANVAS_ERR_' + e.message); }
            };
            img.onerror = function() { done('IMG_LOAD_ERR'); };
            img.src = url;
        """, rel_path)
        if b64 and len(b64) > 100 and not b64.startswith(("CANVAS_", "IMG_")):
            logger.info("Snapshot fetched via canvas: %d bytes", len(b64))
            return b64
        logger.info("canvas result: %s", str(b64)[:80] if b64 else "empty")
    except Exception as e:
        logger.info("canvas failed: %s", e)

    # Method 4: Direct httpx with digest auth
    base = DEVICE_URL.rstrip("/")
    full_url = f"{base}{rel_path}" if rel_path.startswith("/") else f"{base}/{rel_path}"
    try:
        auth = httpx.DigestAuth("admin", "tipl9910")
        resp = httpx.get(full_url, timeout=5, verify=False, auth=auth)
        if resp.status_code == 200 and len(resp.content) > 500:
            logger.info("Snapshot fetched via httpx digest auth: %d bytes", len(resp.content))
            return base64.b64encode(resp.content).decode()
        logger.info("httpx result: status=%d size=%d", resp.status_code, len(resp.content))
    except Exception as e:
        logger.info("httpx failed: %s", e)

    # Method 5: Direct httpx without auth (device might allow after login)
    try:
        resp = httpx.get(full_url, timeout=5, verify=False)
        if resp.status_code == 200 and len(resp.content) > 500:
            logger.info("Snapshot fetched via httpx no-auth: %d bytes", len(resp.content))
            return base64.b64encode(resp.content).decode()
        logger.info("httpx no-auth: status=%d size=%d", resp.status_code, len(resp.content))
    except Exception as e:
        logger.info("httpx no-auth failed: %s", e)

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

    Reads snapshot paths from Vue.js component data, then fetches images.
    Backend falls back to database photos if no snapshot is found.
    """
    global _photo_debug_done

    # Try to extract snapshot URLs from discovered data
    snap_map = _try_extract_snapshots(driver)

    for evt in new_events:
        pin = evt.get("pin", "")
        ts = evt.get("timestamp", "")
        if not pin:
            continue

        snap_path = snap_map.get(f"{pin}-{ts}", "")
        if not snap_path:
            for k, v in snap_map.items():
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
                    str(snap_path)[:120],
                )
            if photo_b64:
                evt["photo"] = photo_b64
                continue

        if not _photo_debug_done:
            _photo_debug_done = True
            logger.info(
                "Live snapshot for %s (PIN=%s): NO URL IN VUE DATA (backend will use DB photo)",
                evt.get("name", "?"), pin,
            )


def _try_extract_snapshots(driver) -> dict[str, str]:
    """Try to read snapshot URLs from el-table internal store or parent data."""
    try:
        return driver.execute_script("""
            var result = {};
            var table = document.querySelector('.el-table');
            if (!table || !table.__vue__) return result;

            var tv = table.__vue__;

            // Strategy 1: el-table store.states.data
            var data = null;
            if (tv.store && tv.store.states && tv.store.states.data)
                data = tv.store.states.data;
            if (!data && tv.$props && tv.$props.data)
                data = tv.$props.data;

            // Strategy 2: Walk parents
            if (!data) {
                var p = tv.$parent;
                for (var i = 0; i < 5 && p; i++) {
                    if (p.$data) {
                        var keys = Object.keys(p.$data);
                        for (var j = 0; j < keys.length; j++) {
                            var v = p.$data[keys[j]];
                            if (Array.isArray(v) && v.length > 0 && typeof v[0] === 'object') {
                                data = v;
                                break;
                            }
                        }
                    }
                    if (data) break;
                    p = p.$parent;
                }
            }

            if (!data || !Array.isArray(data)) return result;

            for (var r = 0; r < data.length; r++) {
                var row = data[r];
                var keys2 = Object.keys(row);
                // Find any value containing 'SnapShot' or 'userpic' or '.jpg'
                var pin = '';
                var ts = '';
                var pic = '';
                for (var k = 0; k < keys2.length; k++) {
                    var val = row[keys2[k]];
                    if (typeof val !== 'string') continue;
                    var vl = val.toLowerCase();
                    if (vl.indexOf('snapshot') > -1 || vl.indexOf('userpic') > -1 || vl.indexOf('.jpg') > -1 || vl.indexOf('.png') > -1) {
                        pic = val;
                    }
                }
                // Find PIN-like field
                for (var k2 = 0; k2 < keys2.length; k2++) {
                    var kl = keys2[k2].toLowerCase();
                    if (kl === 'pin' || kl === 'userid' || kl === 'user_id' || kl === 'employeeid') {
                        pin = String(row[keys2[k2]]);
                    }
                    if (kl === 'time' || kl === 'timestamp' || kl === 'accesstime' || kl === 'checktime') {
                        ts = String(row[keys2[k2]]);
                    }
                }
                if (pin && pic) {
                    result[pin + '-' + ts] = pic;
                }
            }
            return result;
        """) or {}
    except Exception:
        return {}


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

            # Log first poll and then only periodically
            if poll_count == 1:
                logger.info(
                    "Poll #1: %d events found on page", len(events),
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

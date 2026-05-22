/**
 * TrueFace 3000 Attendance Notifier — Browser Poller
 *
 * Paste into Chrome Console (Ctrl+Shift+J) on the TrueFace Search Records page.
 * Polls the table every 10 seconds, sends new face events to the cloud API.
 * The cloud handles arrival/departure logic, WhatsApp, and daily Excel reports.
 *
 * Setup:
 *   1. Open http://192.168.1.112 in Chrome and log in
 *   2. Navigate to Search Records page
 *   3. Click Query once to load initial data
 *   4. Open Console (Ctrl+Shift+J) and paste this script
 */
(function () {
  var CLOUD_API = "https://ppis-whatsapp-bot.fly.dev/api/trueface/event";
  var POLL_MS = 3000;
  var seen = {};
  var nDate = "";

  function ist() {
    return new Date(Date.now() + 5.5 * 3600000);
  }

  function resetDaily() {
    var today = ist().toISOString().slice(0, 10);
    if (nDate !== today) {
      seen = {};
      nDate = today;
    }
  }

  function sendEvent(pin, name, timestamp) {
    fetch(CLOUD_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin: pin, name: name, timestamp: timestamp }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var results = d.results || [];
        for (var i = 0; i < results.length; i++) {
          var r = results[i];
          if (r.status === "arrival") {
            console.log(">>> ARRIVAL: " + r.name + " at " + r.time + " | WhatsApp: " + r.whatsapp);
          } else if (r.status === "departure") {
            console.log(">>> DEPARTURE: " + r.name + " at " + r.time + " | WhatsApp: " + r.whatsapp);
          } else if (r.status === "updated_departure") {
            console.log("Updated departure: " + r.name + " at " + r.time);
          }
        }
      })
      .catch(function (e) { console.error("API error:", e); });
  }

  function scan() {
    resetDaily();
    var d = ist().getUTCDay();
    if (d === 0 || d === 6) return;

    var rows = document.querySelectorAll("table tr");
    var batch = [];

    for (var i = 0; i < rows.length; i++) {
      var c = rows[i].querySelectorAll("td");
      if (c.length < 8) continue;

      var uid = (c[1] ? c[1].textContent : "").trim();
      var name = (c[2] ? c[2].textContent : "").trim();
      var ts = (c[4] ? c[4].textContent : "").trim();
      var st = (c[5] ? c[5].textContent : "").trim();
      var mt = (c[7] ? c[7].textContent : "").trim();

      if (st !== "OK" || !uid) continue;
      if (mt !== "Face" && mt !== "Fingerprint") continue;

      var key = uid + "-" + ts;
      if (seen[key]) continue;
      seen[key] = 1;

      batch.push({ pin: uid, name: name, timestamp: ts });
    }

    if (batch.length > 0) {
      console.log("[TrueFace] Sending " + batch.length + " new events to cloud...");
      fetch(CLOUD_API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(batch),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var results = d.results || [];
          for (var i = 0; i < results.length; i++) {
            var r = results[i];
            if (r.status === "arrival") {
              console.log(">>> ARRIVAL: " + r.name + " at " + r.time + " | WhatsApp: " + r.whatsapp);
            } else if (r.status === "departure") {
              console.log(">>> DEPARTURE: " + r.name + " at " + r.time + " | WhatsApp: " + r.whatsapp);
            } else if (r.status === "updated_departure") {
              console.log("Updated departure: " + r.name + " at " + r.time);
            } else if (r.status === "skipped") {
              console.log("Skipped: PIN " + r.pin + " (" + (r.reason || "") + ")");
            }
          }
        })
        .catch(function (e) { console.error("API error:", e); });
    }
  }

  function poll() {
    var btns = document.querySelectorAll("button");
    var clicked = false;
    for (var i = 0; i < btns.length; i++) {
      var t = btns[i].textContent.trim().toLowerCase();
      if (t === "query" || t === "search" || t === "\u67e5\u8be2") {
        btns[i].click();
        clicked = true;
        break;
      }
    }
    if (clicked) {
      setTimeout(scan, 1500);
    } else {
      scan();
    }
  }

  console.log("=".repeat(50));
  console.log("TrueFace 3000 Attendance Notifier v2");
  console.log("Cloud API: " + CLOUD_API);
  console.log("Poll interval: " + POLL_MS / 1000 + "s");
  console.log("=".repeat(50));

  scan();
  window._tf = setInterval(poll, POLL_MS);
  console.log("Running! To stop: clearInterval(window._tf)");
})();

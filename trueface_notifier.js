/**
 * TrueFace 3000 Attendance Notifier
 *
 * Paste this into Chrome Console (Ctrl+Shift+J) on the TrueFace Search Records page.
 * It polls the records table every 30 seconds, detects new "OK" face recognition events,
 * and sends WhatsApp notifications automatically.
 *
 * Prerequisites:
 *   1. Open http://192.168.1.112 in Chrome and log in
 *   2. Navigate to Search Records page
 *   3. Click Query once to load initial data
 *   4. Open Console (Ctrl+Shift+J) and paste this script
 */
(function () {
  var CLOUD_API = "https://ppis-whatsapp-bot.fly.dev/api/send-whatsapp";
  var POLL_INTERVAL = 30000; // 30 seconds
  var USERS = {
    "1": { name: "Alisha Ahuja", phone: "918076455224" }
  };

  var notifiedToday = {};
  var notifiedDate = "";
  var seenKeys = {};

  function getIST() {
    return new Date(Date.now() + 5.5 * 3600000);
  }

  function resetDaily() {
    var today = getIST().toISOString().slice(0, 10);
    if (notifiedDate !== today) {
      notifiedToday = {};
      seenKeys = {};
      notifiedDate = today;
    }
  }

  function isWeekend() {
    var day = getIST().getUTCDay();
    return day === 0 || day === 6;
  }

  function formatTime(timestamp) {
    var parts = timestamp.split(" ");
    if (parts.length < 2) return "";
    var hms = parts[1].split(":");
    var h = parseInt(hms[0], 10);
    var m = hms[1];
    var ampm = h >= 12 ? "PM" : "AM";
    var h12 = h % 12 || 12;
    return h12 + ":" + m + " " + ampm;
  }

  function sendWhatsApp(name, phone, timeStr) {
    console.log(">>> Sending WhatsApp to " + phone + " for " + name + " at " + timeStr);
    fetch(CLOUD_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        phone: phone,
        template_name: "ppis_teacher_present_text",
        language_code: "en",
        template_params: [name, timeStr],
      }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) { console.log("WhatsApp OK:", JSON.stringify(d)); })
      .catch(function (e) { console.error("WhatsApp ERROR:", e); });
  }

  function processTable() {
    resetDaily();
    if (isWeekend()) {
      console.log("[TrueFace] Weekend — skipping");
      return;
    }

    var rows = document.querySelectorAll("table tr");
    var newEvents = 0;

    for (var i = 0; i < rows.length; i++) {
      var cells = rows[i].querySelectorAll("td");
      if (cells.length < 8) continue;

      var userId = (cells[1] ? cells[1].textContent : "").trim();
      var name = (cells[2] ? cells[2].textContent : "").trim();
      var timestamp = (cells[4] ? cells[4].textContent : "").trim();
      var status = (cells[5] ? cells[5].textContent : "").trim();
      var method = (cells[7] ? cells[7].textContent : "").trim();

      if (status !== "OK" || !userId) continue;
      if (method !== "Face" && method !== "Fingerprint") continue;

      var key = userId + "-" + timestamp;
      if (seenKeys[key]) continue;
      seenKeys[key] = true;
      newEvents++;

      var user = USERS[userId];
      if (!user) {
        console.log("[TrueFace] Unknown user " + userId + " (" + name + ")");
        continue;
      }

      if (notifiedToday[userId]) {
        console.log("[TrueFace] Already notified " + user.name + " today");
        continue;
      }

      var timeStr = formatTime(timestamp);
      if (!timeStr) continue;

      console.log(">>> ATTENDANCE: " + user.name + " at " + timeStr);
      notifiedToday[userId] = true;
      sendWhatsApp(user.name, user.phone, timeStr);
    }

    if (newEvents > 0) {
      console.log("[TrueFace] Found " + newEvents + " new OK events");
    }
  }

  function clickQueryAndProcess() {
    var buttons = document.querySelectorAll("button");
    var clicked = false;
    for (var i = 0; i < buttons.length; i++) {
      var text = buttons[i].textContent.trim().toLowerCase();
      if (text === "query" || text === "search" || text === "查询") {
        buttons[i].click();
        clicked = true;
        break;
      }
    }

    if (clicked) {
      setTimeout(processTable, 3000);
    } else {
      processTable();
    }
  }

  // Start
  console.log("=".repeat(50));
  console.log("TrueFace 3000 Attendance Notifier");
  console.log("=".repeat(50));
  console.log("Users:", JSON.stringify(USERS));
  console.log("Polling every " + POLL_INTERVAL / 1000 + " seconds");
  console.log("Press Ctrl+Shift+J to see logs");
  console.log("=".repeat(50));

  // Process current data immediately
  processTable();

  // Poll every 30 seconds
  window._truefaceInterval = setInterval(clickQueryAndProcess, POLL_INTERVAL);

  console.log("Notifier running! To stop: clearInterval(window._truefaceInterval)");
})();

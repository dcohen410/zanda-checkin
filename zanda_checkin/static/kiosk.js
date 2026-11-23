(() => {
  "use strict";

  // How long each screen waits before quietly going back to the start (ms).
  const IDLE = { dob: 60000, confirm: 45000, done: 8000, reception: 12000 };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  let digits = "";
  let token = null;
  let idleTimer = null;

  function restartIdle(name) {
    clearTimeout(idleTimer);
    if (IDLE[name]) idleTimer = setTimeout(reset, IDLE[name]);
  }

  function show(name) {
    $$(".screen").forEach((s) => s.classList.toggle("is-active", s.dataset.screen === name));
    restartIdle(name);
    if (name === "dob") renderDob();
  }

  function reset() {
    if (token) post("/api/cancel", { token }).catch(() => {});
    token = null;
    digits = "";
    $$("[data-first-name], [data-practitioner], [data-time]").forEach((el) => (el.textContent = ""));
    show("welcome");
  }

  async function post(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  // --- Date of birth keypad -----------------------------------------------
  function renderDob() {
    const parts = [digits.slice(0, 2), digits.slice(2, 4), digits.slice(4, 8)];
    const blanks = ["DD", "MM", "YYYY"];
    $$(".dob-digits").forEach((el, i) => {
      el.textContent = parts[i] ? parts[i].padEnd(blanks[i].length, "·") : blanks[i];
      el.classList.toggle("is-empty", !parts[i]);
      el.classList.toggle("is-current", digits.length >= [0, 2, 4][i] && digits.length < [2, 4, 8][i]);
    });
    $("[data-submit-dob]").disabled = digits.length !== 8;
    $("[data-dob-error]").hidden = true;
  }

  function looksValid(d) {
    const day = +d.slice(0, 2), month = +d.slice(2, 4), year = +d.slice(4, 8);
    const date = new Date(year, month - 1, day);
    return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day
      && year >= 1900 && date <= new Date();
  }

  $$("[data-key]").forEach((key) =>
    key.addEventListener("click", () => {
      const k = key.dataset.key;
      if (k === "clear") digits = "";
      else if (k === "back") digits = digits.slice(0, -1);
      else if (digits.length < 8) digits += k;
      renderDob();
      restartIdle("dob");
    })
  );

  $("[data-submit-dob]").addEventListener("click", async () => {
    if (digits.length !== 8) return;
    if (!looksValid(digits)) {
      $("[data-dob-error]").hidden = false;
      return;
    }
    show("working");
    try {
      const res = await post("/api/lookup", { dob: digits });
      digits = "";
      if (res.status === "match") {
        token = res.token;
        $$("[data-first-name]").forEach((el) => (el.textContent = res.person.firstName));
        $("[data-practitioner]").textContent = res.person.practitioner;
        $("[data-time]").textContent = res.person.time;
        show("confirm");
      } else if (res.status === "invalid") {
        show("dob");
        $("[data-dob-error]").hidden = false;
      } else {
        show("reception");
      }
    } catch (e) {
      digits = "";
      show("reception");
    }
  });

  // --- Confirm --------------------------------------------------------------
  $("[data-confirm]").addEventListener("click", async () => {
    const t = token;
    token = null;
    show("working");
    try {
      const res = await post("/api/confirm", { token: t });
      if (res.status === "done") {
        $$("[data-first-name]").forEach((el) => (el.textContent = res.firstName));
        $("[data-done-message]").textContent = res.seeReception
          ? "Before you take a seat, please pop over to reception. They'd like a quick word."
          : "Take a seat and make yourself comfortable. Your clinician will come and get you.";
        show("done");
      } else {
        show("reception");
      }
    } catch (e) {
      show("reception");
    }
  });

  $("[data-not-me]").addEventListener("click", () => {
    if (token) post("/api/cancel", { token }).catch(() => {});
    token = null;
    show("reception");
  });

  $$("[data-go]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.go === "welcome") reset();
    else show(b.dataset.go);
  }));

  // --- Staff access: tap the top-right corner 5 times within 3 seconds -------
  let taps = [];
  $("[data-staff]").addEventListener("click", () => {
    const now = Date.now();
    taps = taps.filter((t) => now - t < 3000).concat(now);
    if (taps.length >= 5) window.location.href = "/admin/login";
  });

  // --- Keep the screen awake where Safari allows it ---------------------------
  async function wake() {
    try { if ("wakeLock" in navigator) await navigator.wakeLock.request("screen"); } catch (e) { /* not supported */ }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(); });
  wake();

  // Stop pinch-zoom and double-tap zoom on iPad.
  document.addEventListener("gesturestart", (e) => e.preventDefault());
  let lastTouch = 0;
  document.addEventListener("touchend", (e) => {
    const now = Date.now();
    if (now - lastTouch < 350) e.preventDefault();
    lastTouch = now;
  }, { passive: false });

  // Reload once a night so code and settings updates are picked up.
  setInterval(() => {
    const d = new Date();
    if (d.getHours() === 3 && d.getMinutes() === 0 && $(".screen.is-active").dataset.screen === "welcome") {
      window.location.reload();
    }
  }, 60000);
})();

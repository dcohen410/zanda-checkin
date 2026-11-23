"use strict";

// Zanda Checkin desktop notifier: lives in the menu bar (Mac) or system tray (Windows),
// listens to one site's check-ins and shows a native notification for each arrival.

const { app, BrowserWindow, Menu, Notification, Tray, ipcMain, nativeImage, safeStorage } = require("electron");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { SSEParser } = require("./sse");

const DEFAULT_SERVER = process.env.ZANDA_CHECKIN_SERVER || "";
const CONFIG_PATH = () => path.join(app.getPath("userData"), "config.json");

let tray = null;
let win = null;
let state = { server: DEFAULT_SERVER, token: null, siteId: null, siteName: "", orgName: "", email: "",
  showNames: false };
let status = "Not signed in";
let stream = null; // AbortController for the live connection
let retryTimer = null;
let retryDelay = 2000;

// ---------------------------------------------------------------- storage
function loadState() {
  try {
    const raw = JSON.parse(fs.readFileSync(CONFIG_PATH(), "utf8"));
    const token = raw.tokenEnc && safeStorage.isEncryptionAvailable()
      ? safeStorage.decryptString(Buffer.from(raw.tokenEnc, "base64"))
      : null;
    state = { ...state, ...raw, token };
    delete state.tokenEnc;
  } catch (_) { /* first run */ }
}

function saveState() {
  const { token, ...rest } = state;
  const out = { ...rest };
  if (token && safeStorage.isEncryptionAvailable()) {
    out.tokenEnc = safeStorage.encryptString(token).toString("base64");
  }
  fs.mkdirSync(path.dirname(CONFIG_PATH()), { recursive: true });
  fs.writeFileSync(CONFIG_PATH(), JSON.stringify(out, null, 2), { mode: 0o600 });
}

// ---------------------------------------------------------------- server calls
function apiUrl(p) {
  return state.server.replace(/\/+$/, "") + p;
}

async function api(p, { method = "GET", body, token = state.token } = {}) {
  const resp = await fetch(apiUrl(p), {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || data.detail || `Server returned ${resp.status}`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------- live stream
function setStatus(text) {
  status = text;
  refreshTray();
  if (win) win.webContents.send("status", status);
}

function stopStream() {
  clearTimeout(retryTimer);
  if (stream) stream.abort();
  stream = null;
}

async function startStream() {
  stopStream();
  if (!state.token || !state.siteId) return;
  const controller = new AbortController();
  stream = controller;
  setStatus("Connecting…");
  try {
    const resp = await fetch(apiUrl(`/api/v1/sites/${state.siteId}/events`), {
      headers: { Authorization: `Bearer ${state.token}`, Accept: "text/event-stream" },
      signal: controller.signal,
    });
    if (resp.status === 401) return signedOutRemotely();
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
    retryDelay = 2000;
    const parser = new SSEParser(handleEvent);
    const decoder = new TextDecoder();
    for await (const chunk of resp.body) {
      parser.push(decoder.decode(chunk, { stream: true }));
    }
    throw new Error("Connection closed");
  } catch (err) {
    if (controller.signal.aborted) return;
    setStatus("Reconnecting…");
    retryTimer = setTimeout(startStream, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 60000);
  }
}

function handleEvent({ event, data }) {
  if (event === "hello") {
    setStatus(`Listening for ${state.siteName || "check-ins"}`);
  } else if (event === "checkin") {
    let msg;
    try { msg = JSON.parse(data); } catch (_) { return; }
    notify(msg);
  } else if (event === "signed_out") {
    signedOutRemotely();
  }
}

function notify(msg) {
  if (!Notification.isSupported()) return;
  const extra = [];
  if (msg.seeReception) extra.push("Asked to see reception.");
  if (msg.zandaUpdated === false) extra.push("Mark them Arrived in Zanda.");
  const str = (v) => (typeof v === "string" ? v.slice(0, 120) : "");
  // Notifications can show on a locked screen, so names can be hidden (on by default).
  const title = state.showNames
    ? str(msg.message) || `${str(msg.client)} has arrived`
    : `A client has arrived${str(msg.practitioner) ? " for " + str(msg.practitioner) : ""}`;
  const n = new Notification({
    title,
    body: [str(msg.siteName), ...extra].filter(Boolean).join(" · "),
    silent: false,
    timeoutType: "default", // let the OS dismiss it after a few seconds
  });
  n.show();
}

function signedOutRemotely() {
  stopStream();
  state.token = null;
  saveState();
  setStatus("Signed out. Sign in again.");
  showWindow();
}

// ---------------------------------------------------------------- tray + window
function trayIcon() {
  const file = process.platform === "darwin" ? "trayTemplate.png" : "tray.png";
  const img = nativeImage.createFromPath(path.join(__dirname, "..", "assets", file));
  if (process.platform === "darwin") img.setTemplateImage(true);
  return img;
}

function refreshTray() {
  if (!tray) return;
  tray.setToolTip(`Zanda Checkin: ${status}`);
  const signedIn = Boolean(state.token);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: status, enabled: false },
    ...(signedIn ? [{ label: `${state.orgName}${state.siteName ? " · " + state.siteName : ""}`, enabled: false }] : []),
    { type: "separator" },
    { label: signedIn ? "Change site…" : "Sign in…", click: showWindow },
    { label: "Send a test notification", enabled: signedIn,
      click: () => notify({ message: "Test: Alex N. has arrived for Sam", siteName: state.siteName }) },
    ...(signedIn ? [{ label: "Sign out", click: signOut }] : []),
    { type: "separator" },
    { label: "Show client names in notifications", type: "checkbox", checked: Boolean(state.showNames),
      click: (item) => { state.showNames = item.checked; saveState(); } },
    { label: "Open at login", type: "checkbox", checked: app.getLoginItemSettings().openAtLogin,
      click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }) },
    { label: "Quit Zanda Checkin", click: () => app.quit() },
  ]));
}

function showWindow() {
  if (win) { win.show(); win.focus(); return; }
  win = new BrowserWindow({
    width: 440, height: 560, resizable: false, maximizable: false, fullscreenable: false,
    title: "Zanda Checkin", show: false, backgroundColor: "#f6f3ee",
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true },
  });
  win.setMenuBarVisibility(false);
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  win.once("ready-to-show", () => win.show());
  win.on("closed", () => { win = null; });
}

async function signOut() {
  stopStream();
  try { if (state.token) await api("/api/v1/auth/logout", { method: "POST" }); } catch (_) { /* offline is fine */ }
  state = { ...state, token: null, siteId: null, siteName: "", orgName: "", email: "" };
  saveState();
  setStatus("Not signed in");
  showWindow();
}

// ---------------------------------------------------------------- IPC from the window
// Only answer our own bundled window, never another frame or page.
function trusted(event) {
  const url = event.senderFrame && event.senderFrame.url;
  return Boolean(win && event.sender === win.webContents && url && url.startsWith("file://")
    && url.endsWith("/renderer/index.html"));
}

function handle(channel, fn) {
  ipcMain.handle(channel, (event, ...args) => {
    if (!trusted(event)) throw new Error("Not allowed");
    return fn(event, ...args);
  });
}

handle("get-state", () => ({
  server: state.server, signedIn: Boolean(state.token), siteId: state.siteId,
  orgName: state.orgName, email: state.email, status,
}));

handle("sign-in", async (_e, { server, email, password, code }) => {
  let url;
  try {
    url = new URL(String(server || "").trim());
  } catch (_) {
    throw new Error("Enter the server address, starting with https://");
  }
  const local = url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname);
  if (url.protocol !== "https:" && !local) {
    throw new Error("Use the https:// address. Notifications include client names, so they must be encrypted.");
  }
  if (url.username || url.password) throw new Error("Enter just the server address.");
  state.server = url.origin;
  const data = await api("/api/v1/auth/login", {
    method: "POST", token: null,
    body: { email, password, code, deviceName: `${os.hostname()} (desktop app)` },
  });
  state = { ...state, token: data.token, orgName: data.organisation.name, email: data.user.email };
  saveState();
  refreshTray();
  return { orgName: state.orgName };
});

handle("list-sites", async () => {
  try {
    return (await api("/api/v1/sites")).sites;
  } catch (err) {
    if (err.status === 401) signedOutRemotely();
    throw err;
  }
});

handle("choose-site", async (_e, { siteId, siteName }) => {
  if (!Number.isInteger(siteId) || siteId <= 0) throw new Error("Choose a site");
  state.siteId = siteId;
  state.siteName = String(siteName || "").slice(0, 120);
  saveState();
  startStream();
  if (win) win.close();
  return true;
});

handle("sign-out", () => signOut());

// ---------------------------------------------------------------- app lifecycle
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);
  // Lock every window down: no navigation away, no pop-ups, no camera/mic/etc. permissions.
  app.on("web-contents-created", (_e, contents) => {
    contents.on("will-navigate", (ev) => ev.preventDefault());
    contents.on("will-attach-webview", (ev) => ev.preventDefault());
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
  });
  app.whenReady().then(() => {
    const { session } = require("electron");
    session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    if (process.platform === "darwin" && app.dock) app.dock.hide();
    if (process.platform === "win32") app.setAppUserModelId("io.github.dcohen410.zandacheckin");
    loadState();
    tray = new Tray(trayIcon());
    tray.on("click", () => tray.popUpContextMenu());
    refreshTray();
    if (state.token && state.siteId) startStream();
    else showWindow();
  });
  app.on("window-all-closed", (e) => e.preventDefault()); // keep running in the tray
}

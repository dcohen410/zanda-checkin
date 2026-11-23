"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("zc", {
  getState: () => ipcRenderer.invoke("get-state"),
  signIn: (details) => ipcRenderer.invoke("sign-in", details),
  listSites: () => ipcRenderer.invoke("list-sites"),
  chooseSite: (site) => ipcRenderer.invoke("choose-site", site),
  signOut: () => ipcRenderer.invoke("sign-out"),
  onStatus: (fn) => ipcRenderer.on("status", (_e, s) => fn(s)),
});

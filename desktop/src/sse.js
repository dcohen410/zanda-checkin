"use strict";

// Minimal Server-Sent Events parser: feed it text chunks, get {event, data} objects back.
class SSEParser {
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.buffer = "";
    this.reset();
  }

  reset() {
    this.event = "message";
    this.data = [];
  }

  push(chunk) {
    this.buffer += chunk;
    let idx;
    while ((idx = this.buffer.search(/\r?\n/)) !== -1) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + (this.buffer[idx] === "\r" ? 2 : 1));
      this.line(line);
    }
  }

  line(line) {
    if (line === "") {
      if (this.data.length) this.onEvent({ event: this.event, data: this.data.join("\n") });
      this.reset();
      return;
    }
    if (line.startsWith(":")) return; // comment / heartbeat
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
  }
}

module.exports = { SSEParser };

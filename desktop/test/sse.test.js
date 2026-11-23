"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { SSEParser } = require("../src/sse");

test("parses events split across chunks and ignores heartbeats", () => {
  const got = [];
  const p = new SSEParser((e) => got.push(e));
  p.push("retry: 5000\nevent: hello\ndata: {\"siteId\":1}\n\n: ping\n\nevent: check");
  p.push("in\r\ndata: {\"message\":\"Alex N. has arrived\"}\r\n\r\n");
  assert.deepStrictEqual(got, [
    { event: "hello", data: "{\"siteId\":1}" },
    { event: "checkin", data: "{\"message\":\"Alex N. has arrived\"}" },
  ]);
});

test("joins multi-line data", () => {
  const got = [];
  const p = new SSEParser((e) => got.push(e));
  p.push("data: a\ndata: b\n\n");
  assert.deepStrictEqual(got, [{ event: "message", data: "a\nb" }]);
});

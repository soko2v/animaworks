/**
 * Unit tests for server/static/shared/chat-stream.js
 *
 * Run with: node --test tests/unit/frontend/test_chat_stream.mjs
 *
 * Uses Node.js built-in test runner (node:test) — no external dependencies.
 * Mocks fetch and ReadableStream to simulate SSE streaming.
 */

import { describe, it, beforeEach, mock } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../../server/static");

// ── Mock Browser Globals ──────────────────────────────

// Mock performance.now()
globalThis.performance = globalThis.performance || {
  now: () => Date.now(),
};

// ── SSE Helpers ──────────────────────────────

/**
 * Encode an SSE event into a text chunk.
 * @param {string} event - Event name
 * @param {object} data - Event data (will be JSON-serialized)
 * @returns {string} SSE-formatted string
 */
function sseEvent(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * Create a mock ReadableStream reader from an array of text chunks.
 * Each chunk is encoded as Uint8Array.
 * @param {string[]} chunks - Array of SSE text chunks
 * @returns {{ getReader: () => ReadableStreamDefaultReader }}
 */
function createMockBody(chunks) {
  const encoder = new TextEncoder();
  let index = 0;
  return {
    getReader() {
      return {
        async read() {
          if (index < chunks.length) {
            return { done: false, value: encoder.encode(chunks[index++]) };
          }
          return { done: true, value: undefined };
        },
        releaseLock() {},
      };
    },
  };
}

/**
 * Create a mock fetch that returns a successful SSE response.
 * @param {string[]} chunks - SSE text chunks to stream
 * @param {number} [status=200] - HTTP status code
 * @returns {function} Mock fetch function
 */
function createMockFetch(chunks, status = 200) {
  return async (url, options) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    body: createMockBody(chunks),
    text: async () => "Error body text",
  });
}

// ── Import Module Under Test ──────────────────────────

// chat-stream.js (and its deps) use absolute "/shared/..." browser paths.
// Load as separate data: modules with stubs so Node can resolve them.
let streamChat;

{
  const toDataUrl = (body, tag) =>
    "data:text/javascript;base64," +
    Buffer.from(body, "utf8").toString("base64") +
    "#" +
    tag;

  const stripImports = (src) =>
    src.replace(/(?:^|\n)\s*import\b[\s\S]*?;/g, "\n");

  const sseBody =
    `
    const t = (k) => k;
    const createLogger = () => ({ info() {}, warn() {}, error() {}, debug() {} });
  ` + stripImports(readFileSync(resolve(STATIC, "shared/sse-parser.js"), "utf8"));
  const sseUrl = toDataUrl(sseBody, "sse-parser");

  // chat-stream imports parseConvSSE/getErrorMessage from sse and basePath/logger
  const chatBody =
    `
    import { parseConvSSE, getErrorMessage } from "${sseUrl}";
    const createLogger = () => ({ info() {}, warn() {}, error() {}, debug() {} });
    const basePath = "";
  ` + stripImports(readFileSync(resolve(STATIC, "shared/chat-stream.js"), "utf8"));

  const mod = await import(toDataUrl(chatBody, "chat-stream"));
  streamChat = mod.streamChat;
}

// ── Tests ──────────────────────────────

describe("streamChat", () => {
  beforeEach(() => {
    // Suppress console output during tests
    globalThis.console.info = () => {};
    globalThis.console.error = () => {};
  });

  it("should call onTextDelta for text_delta events", async () => {
    const chunks = [
      sseEvent("text_delta", { text: "Hello" }),
      sseEvent("text_delta", { text: " world" }),
      sseEvent("done", { summary: "Hello world", emotion: "neutral" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    const received = [];
    await streamChat("test-anima", '{"message":"hi"}', null, {
      onTextDelta: (text) => received.push(text),
      onDone: () => {},
    });

    assert.deepStrictEqual(received, ["Hello", " world"]);
  });

  it("should call onToolStart and onToolEnd for tool events", async () => {
    const chunks = [
      sseEvent("tool_start", { tool_name: "web_search" }),
      sseEvent("text_delta", { text: "Searching..." }),
      sseEvent("tool_end", {}),
      sseEvent("done", { summary: "Done", emotion: "neutral" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    const toolStarts = [];
    let toolEndCount = 0;

    await streamChat("test-anima", '{"message":"search"}', null, {
      onTextDelta: () => {},
      onToolStart: (name) => toolStarts.push(name),
      onToolEnd: () => { toolEndCount++; },
      onDone: () => {},
    });

    assert.deepStrictEqual(toolStarts, ["web_search"]);
    assert.strictEqual(toolEndCount, 1);
  });

  it("should call onDone with summary and emotion", async () => {
    const chunks = [
      sseEvent("text_delta", { text: "response" }),
      sseEvent("done", { summary: "Summary text", emotion: "happy" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    let doneData = null;
    await streamChat("test-anima", '{"message":"hello"}', null, {
      onTextDelta: () => {},
      onDone: (data) => { doneData = data; },
    });

    assert.strictEqual(doneData.summary, "Summary text");
    assert.strictEqual(doneData.emotion, "happy");
    assert.deepStrictEqual(doneData.images, []);
  });

  it("should default emotion to 'neutral' when not provided", async () => {
    const chunks = [
      sseEvent("done", { summary: "Done" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    let doneData = null;
    await streamChat("test-anima", '{"message":"hi"}', null, {
      onDone: (data) => { doneData = data; },
    });

    assert.strictEqual(doneData.emotion, "neutral");
    assert.deepStrictEqual(doneData.images, []);
  });

  it("should pass done images payload to callback", async () => {
    const chunks = [
      sseEvent("done", {
        summary: "Summary text",
        emotion: "happy",
        images: [{ type: "image", source: "generated", path: "assets/avatar_fullbody.png" }],
      }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    let doneData = null;
    await streamChat("test-anima", '{"message":"hello"}', null, {
      onDone: (data) => { doneData = data; },
    });

    assert.strictEqual(doneData.images.length, 1);
    assert.strictEqual(doneData.images[0].path, "assets/avatar_fullbody.png");
  });

  it("should call onError and reject for terminal error events", async () => {
    const chunks = [
      sseEvent("stream_start", { response_id: "response-that-must-not-reconnect" }),
      sseEvent("error", { code: "IPC_TIMEOUT", message: "timeout" }),
    ];

    let fetchCount = 0;
    globalThis.fetch = async () => {
      fetchCount++;
      return {
        ok: true,
        status: 200,
        body: createMockBody(chunks),
      };
    };

    let errorData = null;
    await assert.rejects(
      () => streamChat("test-anima", '{"message":"hi"}', null, {
        onError: (data) => { errorData = data; },
      }),
      (err) => err.name === "TerminalStreamError" && err.isTerminalStreamFailure,
    );

    // Under Node tests, i18n is stubbed to return the key itself
    assert.strictEqual(errorData.message, "sse.ipc_timeout");
    assert.strictEqual(fetchCount, 1, "a terminal server error must not reconnect or resend the request");
  });

  it("should call onBootstrap for bootstrap events", async () => {
    const chunks = [
      sseEvent("bootstrap", { status: "started" }),
      sseEvent("bootstrap", { status: "completed" }),
      sseEvent("done", { summary: "ok" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    const bootstrapEvents = [];
    await streamChat("test-anima", '{"message":"hi"}', null, {
      onBootstrap: (data) => bootstrapEvents.push(data),
      onDone: () => {},
    });

    assert.strictEqual(bootstrapEvents.length, 2);
    assert.strictEqual(bootstrapEvents[0].status, "started");
    assert.strictEqual(bootstrapEvents[1].status, "completed");
  });

  it("should reject a bootstrap-busy response after reporting it", async () => {
    globalThis.fetch = createMockFetch([
      sseEvent("bootstrap", { status: "busy", message: "still starting" }),
    ]);

    let bootstrapData = null;
    await assert.rejects(
      () => streamChat("test-anima", '{"message":"with document"}', null, {
        onBootstrap: (data) => { bootstrapData = data; },
      }),
      (err) => err.name === "BootstrapBusyError" && err.isTerminalStreamFailure,
    );
    assert.strictEqual(bootstrapData.status, "busy");
  });

  it("should call onChainStart for chain_start events", async () => {
    const chunks = [
      sseEvent("text_delta", { text: "part1" }),
      sseEvent("chain_start", {}),
      sseEvent("text_delta", { text: "part2" }),
      sseEvent("done", { summary: "ok" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    let chainStartCount = 0;
    await streamChat("test-anima", '{"message":"hi"}', null, {
      onTextDelta: () => {},
      onChainStart: () => { chainStartCount++; },
      onDone: () => {},
    });

    assert.strictEqual(chainStartCount, 1);
  });

  it("should throw on non-ok HTTP response", async () => {
    globalThis.fetch = createMockFetch([], 500);

    await assert.rejects(
      () => streamChat("test-anima", '{"message":"hi"}', null, {}),
      (err) => {
        assert.ok(err.message.includes("API 500"));
        return true;
      }
    );
  });

  it("should handle fetch network errors", async () => {
    globalThis.fetch = async () => { throw new Error("Network error"); };

    await assert.rejects(
      () => streamChat("test-anima", '{"message":"hi"}', null, {}),
      (err) => {
        assert.strictEqual(err.message, "Network error");
        return true;
      }
    );
  });

  it("should not set Content-Type header for FormData body", async () => {
    const chunks = [
      sseEvent("done", { summary: "ok" }),
    ];

    let capturedHeaders = null;
    globalThis.fetch = async (url, options) => {
      capturedHeaders = options.headers;
      return {
        ok: true,
        status: 200,
        body: createMockBody(chunks),
      };
    };

    // Create a minimal FormData mock
    class MockFormData {}
    globalThis.FormData = MockFormData;
    const formData = new MockFormData();

    await streamChat("test-anima", formData, null, {
      onDone: () => {},
    });

    assert.deepStrictEqual(capturedHeaders, {});
  });

  it("should set Content-Type to application/json for string body", async () => {
    const chunks = [
      sseEvent("done", { summary: "ok" }),
    ];

    let capturedHeaders = null;
    globalThis.fetch = async (url, options) => {
      capturedHeaders = options.headers;
      return {
        ok: true,
        status: 200,
        body: createMockBody(chunks),
      };
    };

    await streamChat("test-anima", '{"message":"hi"}', null, {
      onDone: () => {},
    });

    assert.strictEqual(capturedHeaders["Content-Type"], "application/json");
  });

  it("should pass AbortSignal to fetch when provided", async () => {
    const chunks = [
      sseEvent("done", { summary: "ok" }),
    ];

    let capturedSignal = null;
    globalThis.fetch = async (url, options) => {
      capturedSignal = options.signal;
      return {
        ok: true,
        status: 200,
        body: createMockBody(chunks),
      };
    };

    const controller = new AbortController();
    await streamChat("test-anima", '{"message":"hi"}', controller.signal, {
      onDone: () => {},
    });

    assert.strictEqual(capturedSignal, controller.signal);
  });

  it("should not include signal in fetch options when signal is null", async () => {
    const chunks = [
      sseEvent("done", { summary: "ok" }),
    ];

    let fetchOptions = null;
    globalThis.fetch = async (url, options) => {
      fetchOptions = options;
      return {
        ok: true,
        status: 200,
        body: createMockBody(chunks),
      };
    };

    await streamChat("test-anima", '{"message":"hi"}', null, {
      onDone: () => {},
    });

    assert.strictEqual(fetchOptions.signal, undefined);
  });

  it("should handle empty text in text_delta gracefully", async () => {
    const chunks = [
      sseEvent("text_delta", {}),
      sseEvent("text_delta", { text: "" }),
      sseEvent("text_delta", { text: "actual" }),
      sseEvent("done", { summary: "ok" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    const received = [];
    await streamChat("test-anima", '{"message":"hi"}', null, {
      onTextDelta: (text) => received.push(text),
      onDone: () => {},
    });

    assert.deepStrictEqual(received, ["", "", "actual"]);
  });

  it("should handle multiple events in a single chunk", async () => {
    // Combine multiple events into one chunk (as SSE servers may do)
    const combinedChunk =
      sseEvent("text_delta", { text: "Hello" }) +
      sseEvent("tool_start", { tool_name: "search" }) +
      sseEvent("tool_end", {}) +
      sseEvent("done", { summary: "ok" });

    globalThis.fetch = createMockFetch([combinedChunk]);

    const events = [];
    await streamChat("test-anima", '{"message":"hi"}', null, {
      onTextDelta: (text) => events.push({ type: "text_delta", text }),
      onToolStart: (name) => events.push({ type: "tool_start", name }),
      onToolEnd: () => events.push({ type: "tool_end" }),
      onDone: (data) => events.push({ type: "done", data }),
    });

    assert.strictEqual(events.length, 4);
    assert.strictEqual(events[0].type, "text_delta");
    assert.strictEqual(events[1].type, "tool_start");
    assert.strictEqual(events[2].type, "tool_end");
    assert.strictEqual(events[3].type, "done");
  });

  it("should handle SSE data split across multiple chunks", async () => {
    // Split a single SSE event across two chunks
    const fullEvent = sseEvent("text_delta", { text: "Hello" });
    const splitPoint = Math.floor(fullEvent.length / 2);
    const chunk1 = fullEvent.slice(0, splitPoint);
    const chunk2 = fullEvent.slice(splitPoint) + sseEvent("done", { summary: "ok" });

    globalThis.fetch = createMockFetch([chunk1, chunk2]);

    const received = [];
    await streamChat("test-anima", '{"message":"hi"}', null, {
      onTextDelta: (text) => received.push(text),
      onDone: () => {},
    });

    assert.deepStrictEqual(received, ["Hello"]);
  });

  it("should work with no optional callbacks", async () => {
    const chunks = [
      sseEvent("text_delta", { text: "Hello" }),
      sseEvent("tool_start", { tool_name: "search" }),
      sseEvent("tool_end", {}),
      sseEvent("bootstrap", { status: "started" }),
      sseEvent("chain_start", {}),
      sseEvent("done", { summary: "ok" }),
    ];

    globalThis.fetch = createMockFetch(chunks);

    // Should not throw even with empty callbacks
    await streamChat("test-anima", '{"message":"hi"}', null, {});
  });

  it("should encode animaName in the URL", async () => {
    const chunks = [sseEvent("done", { summary: "ok" })];

    let capturedUrl = null;
    globalThis.fetch = async (url, options) => {
      capturedUrl = url;
      return {
        ok: true,
        status: 200,
        body: createMockBody(chunks),
      };
    };

    await streamChat("anima with spaces", '{"message":"hi"}', null, {
      onDone: () => {},
    });

    assert.ok(capturedUrl.includes("anima%20with%20spaces"));
  });

  it("should propagate AbortError when signal is aborted mid-stream", async () => {
    const controller = new AbortController();
    const encoder = new TextEncoder();

    // Mock fetch returns a stream that delays between reads, allowing abort mid-stream
    globalThis.fetch = async (url, options) => {
      let readCount = 0;
      return {
        ok: true,
        status: 200,
        body: {
          getReader() {
            return {
              async read() {
                readCount++;
                if (readCount === 1) {
                  return {
                    done: false,
                    value: encoder.encode(sseEvent("text_delta", { text: "Hello" })),
                  };
                }
                // Abort the controller before the second read
                controller.abort();
                // Simulate the AbortError that fetch/reader.read() would throw
                const err = new DOMException("The operation was aborted.", "AbortError");
                throw err;
              },
              releaseLock() {},
            };
          },
        },
      };
    };

    await assert.rejects(
      () => streamChat("test-anima", '{"message":"hi"}', controller.signal, {
        onTextDelta: () => {},
      }),
      (err) => {
        assert.strictEqual(err.name, "AbortError");
        return true;
      }
    );
  });

  it("should call reader.releaseLock() even when a callback throws mid-stream", async () => {
    let releaseLockCalled = false;
    const encoder = new TextEncoder();

    globalThis.fetch = async (url, options) => {
      return {
        ok: true,
        status: 200,
        body: {
          getReader() {
            return {
              async read() {
                return {
                  done: false,
                  value: encoder.encode(sseEvent("text_delta", { text: "boom" })),
                };
              },
              releaseLock() {
                releaseLockCalled = true;
              },
            };
          },
        },
      };
    };

    await assert.rejects(
      () => streamChat("test-anima", '{"message":"hi"}', null, {
        onTextDelta: () => {
          throw new Error("callback exploded");
        },
      }),
      (err) => {
        assert.strictEqual(err.message, "callback exploded");
        return true;
      }
    );

    assert.ok(releaseLockCalled, "reader.releaseLock() should have been called in finally block");
  });
});

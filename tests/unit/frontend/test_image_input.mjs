/**
 * Unit tests for server/static/shared/image-input.js (document drag & drop).
 *
 * Run with: node --test tests/unit/frontend/test_image_input.mjs
 *
 * Uses the Node.js built-in test runner with a minimal DOM/FileReader shim.
 */

import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../../server/static");

// ── DOM shim ──────────────────────────────────────────

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

class MockClassList {
  constructor() { this.values = new Set(); }
  add(...names) { for (const n of names) this.values.add(n); }
  remove(...names) { for (const n of names) this.values.delete(n); }
  contains(name) { return this.values.has(name); }
}

class MockEl {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.classList = new MockClassList();
    this.style = {};
    this.dataset = {};
    this.listeners = new Map();
    this._inner = "";
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  dispatch(type, event) {
    for (const fn of this.listeners.get(type) || []) fn(event);
    return event;
  }
  set innerHTML(v) { this._inner = String(v); }
  get innerHTML() { return this._inner; }
  set textContent(v) { this._inner = escapeHtml(v); }
  get textContent() { return this._inner; }
  querySelectorAll() { return []; }
  contains() { return false; }
  appendChild(child) { return child; }
}

const windowListeners = new Map();
const mockWindow = {
  addEventListener(type, fn) {
    if (!windowListeners.has(type)) windowListeners.set(type, []);
    windowListeners.get(type).push(fn);
  },
  dispatch(type, event) {
    for (const fn of windowListeners.get(type) || []) fn(event);
    return event;
  },
};

class MockFileReader {
  readAsDataURL(file) {
    queueMicrotask(() => {
      if (file._fail) { this.onerror?.(); return; }
      const bytes = file._bytes || Buffer.from("x");
      this.result = `data:application/octet-stream;base64,${bytes.toString("base64")}`;
      this.onload?.();
    });
  }
}

class MockImage {
  set src(_value) { queueMicrotask(() => this.onerror?.()); }
}

globalThis.document = {
  createElement: (tag) => new MockEl(tag),
  addEventListener() {},
  body: new MockEl("body"),
};
globalThis.window = mockWindow;
globalThis.FileReader = MockFileReader;
globalThis.Image = MockImage;
globalThis.URL = globalThis.URL || {};
globalThis.URL.createObjectURL = () => "blob:mock";
globalThis.URL.revokeObjectURL = () => {};

// ── Module loader ─────────────────────────────────────

async function loadImageInput() {
  let source = readFileSync(resolve(STATIC, "shared/image-input.js"), "utf8");
  source = source.replace(/^import\s+.+;?\s*$/gm, "");
  const preamble = `
    const t = (key, params = {}) => Object.keys(params).length ? key + ":" + JSON.stringify(params) : key;
    const basePath = "";
    const createLogger = () => ({ info() {}, warn() {}, error() {}, debug() {} });
  `;
  const url = "data:text/javascript;base64," + Buffer.from(preamble + "\n" + source, "utf8").toString("base64");
  return import(url + "#image-input-" + Math.random());
}

function makeFile(name, { size = 1024, type = "", lastModified = 1000, bytes = "hello", fail = false } = {}) {
  return { name, size, type, lastModified, _bytes: Buffer.from(bytes), _fail: fail };
}

function dropEvent(files, types = ["Files"]) {
  return {
    prevented: false,
    preventDefault() { this.prevented = true; },
    dataTransfer: { files, types, dropEffect: "none" },
    relatedTarget: null,
  };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

// ── Tests ─────────────────────────────────────────────

describe("image-input document drag & drop", () => {
  let mod;
  let container;
  let inputArea;
  let preview;
  let manager;
  let changed;

  beforeEach(async () => {
    mod = await loadImageInput();
    mod._resetWindowDropGuardForTests();
    windowListeners.clear();
    container = new MockEl("div");
    inputArea = new MockEl("textarea");
    preview = new MockEl("div");
    changed = 0;
    manager = mod.createImageInput({
      container,
      inputArea,
      previewContainer: preview,
      onImagesChanged: () => { changed += 1; },
    });
  });

  it("drop adds multiple documents with canonical media types and previews", async () => {
    const ev = container.dispatch("drop", dropEvent([
      makeFile("報告書.docx", { type: "" }),
      makeFile("data.xlsx", { type: "application/octet-stream" }),
      makeFile("notes.md"),
    ]));
    assert.equal(ev.prevented, true, "browser default (navigate to file) must be prevented");
    await flush();
    const files = manager.getPendingFiles();
    assert.equal(files.length, 3);
    assert.deepEqual(files.map((f) => f.media_type), [
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "text/markdown",
    ]);
    assert.equal(manager.getFileCount(), 3);
    assert.ok(changed >= 3);
    assert.match(preview.innerHTML, /DOCX/);
    assert.match(preview.innerHTML, /XLSX/);
    assert.match(preview.innerHTML, /MD/);
    assert.match(preview.innerHTML, /報告書\.docx/);
    assert.equal(preview.style.display, "flex");
    assert.equal(container.classList.contains("image-drag-over"), false);
  });

  it("dragenter/dragover/dragleave manage highlight and prevent default only for file drags", () => {
    const enter = container.dispatch("dragenter", dropEvent([], ["Files"]));
    assert.equal(enter.prevented, true);
    assert.equal(container.classList.contains("image-drag-over"), true);
    const over = container.dispatch("dragover", dropEvent([], ["Files"]));
    assert.equal(over.prevented, true);
    assert.equal(over.dataTransfer.dropEffect, "copy");
    container.dispatch("dragleave", { relatedTarget: null });
    assert.equal(container.classList.contains("image-drag-over"), false);

    const textDrag = container.dispatch("dragover", dropEvent([], ["text/plain"]));
    assert.equal(textDrag.prevented, false, "text drags keep native behaviour");
    assert.equal(container.classList.contains("image-drag-over"), false);
    const textDrop = container.dispatch("drop", dropEvent([], ["text/plain"]));
    assert.equal(textDrop.prevented, false);
  });

  it("window-level guard blocks file drops outside the drop zone but not text drags", () => {
    const outside = mockWindow.dispatch("drop", dropEvent([makeFile("a.pdf")]));
    assert.equal(outside.prevented, true);
    const over = mockWindow.dispatch("dragover", dropEvent([], ["Files"]));
    assert.equal(over.prevented, true);
    const text = mockWindow.dispatch("drop", dropEvent([], ["text/plain"]));
    assert.equal(text.prevented, false);
    assert.equal(mod.installWindowDropGuard(mockWindow), false, "guard installs once");
  });

  it("does not attach the same file twice (drop + file picker share the pipeline)", async () => {
    const file = makeFile("same.pdf", { size: 10, lastModified: 42 });
    container.dispatch("drop", dropEvent([file]));
    await flush();
    manager.addFiles([makeFile("same.pdf", { size: 10, lastModified: 42 })]);
    await flush();
    container.dispatch("drop", dropEvent([file, file]));
    await flush();
    assert.equal(manager.getFileCount(), 1);
    assert.equal(manager.getStatus()?.kind, "info");
    assert.match(manager.getStatus()?.message, /chat\.file_duplicate_client/);

    // A different file with the same name is a different attachment.
    manager.addFiles([makeFile("same.pdf", { size: 11, lastModified: 42 })]);
    await flush();
    assert.equal(manager.getFileCount(), 2);
  });

  it("removing a document allows re-attaching it and updates callbacks", async () => {
    const file = makeFile("a.txt");
    container.dispatch("drop", dropEvent([file, makeFile("b.csv")]));
    await flush();
    assert.equal(manager.getFileCount(), 2);
    const before = changed;
    assert.equal(manager.removeFile(0), true);
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name), ["b.csv"]);
    assert.equal(changed, before + 1);
    assert.equal(manager.removeFile(5), false);

    container.dispatch("drop", dropEvent([file]));
    await flush();
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name), ["b.csv", "a.txt"]);

    manager.clearImages();
    assert.equal(manager.getFileCount(), 0);
    assert.equal(preview.style.display, "none");
    container.dispatch("drop", dropEvent([file]));
    await flush();
    assert.equal(manager.getFileCount(), 1, "clear resets de-duplication");
  });

  it("rejects unsupported formats with a visible error and no navigation", async () => {
    const ev = container.dispatch("drop", dropEvent([
      makeFile("tool.exe", { type: "application/x-msdownload" }),
      makeFile("macro.docm"),
      makeFile("sheet.xlsm"),
    ]));
    assert.equal(ev.prevented, true);
    await flush();
    assert.equal(manager.getFileCount(), 0);
    assert.equal(manager.getImageCount(), 0);
    assert.equal(manager.getStatus()?.kind, "error");
    assert.match(manager.getStatus()?.message, /chat\.file_unsupported_client/);
    assert.match(preview.innerHTML, /image-input-status-error/);
    assert.equal(manager.prepareForSubmit(), false, "text-only send is blocked once after a rejection");
  });

  it("rejects documents above the per-file limit", async () => {
    container.dispatch("drop", dropEvent([makeFile("big.pdf", { size: 11 * 1024 * 1024 })]));
    await flush();
    assert.equal(manager.getFileCount(), 0);
    assert.match(manager.getStatus()?.message, /chat\.file_too_large_client/);
  });

  it("enforces the per-message document count limit", async () => {
    const files = Array.from({ length: 11 }, (_, i) => makeFile(`f${i}.txt`, { lastModified: i }));
    container.dispatch("drop", dropEvent(files));
    await flush();
    assert.equal(manager.getFileCount(), 10);
    assert.equal(manager.getStatus()?.kind, "error");
    assert.match(manager.getStatus()?.message, /chat\.file_count_limit_client/);
  });

  it("reports read failures and frees the slot for a retry", async () => {
    const broken = makeFile("broken.docx", { fail: true });
    container.dispatch("drop", dropEvent([broken]));
    await flush();
    assert.equal(manager.getFileCount(), 0);
    assert.match(manager.getStatus()?.message, /chat\.file_read_failed/);
    assert.equal(manager.isProcessing(), false);
    container.dispatch("drop", dropEvent([makeFile("broken.docx")]));
    await flush();
    assert.equal(manager.getFileCount(), 1, "a failed read must not poison de-duplication");
  });

  it("blocks submit while a document is still being read", () => {
    container.dispatch("drop", dropEvent([makeFile("slow.pdf")]));
    assert.equal(manager.isProcessing(), true);
    assert.equal(manager.prepareForSubmit(), false);
  });

  it("documentLabelFor maps extensions to short labels", () => {
    assert.equal(mod.documentLabelFor("a.docx"), "DOCX");
    assert.equal(mod.documentLabelFor("A.XLS"), "XLS");
    assert.equal(mod.documentLabelFor("readme.md"), "MD");
    assert.equal(mod.documentLabelFor("weird.bin"), "FILE");
  });
});

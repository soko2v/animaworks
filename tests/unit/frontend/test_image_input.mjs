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

  it("keeps documents with colliding metadata when their bytes differ", async () => {
    manager.addFiles([
      makeFile("same.txt", { size: 4, lastModified: 42, bytes: "aaaa" }),
      makeFile("same.txt", { size: 4, lastModified: 42, bytes: "bbbb" }),
    ]);
    await flush();
    assert.deepEqual(manager.getPendingFiles().map((file) => file.data), ["YWFhYQ==", "YmJiYg=="]);
    assert.notEqual(manager.getDisplayFiles()[0].key, manager.getDisplayFiles()[1].key);

    // If the first attachment is removed, the remaining suffixed key must
    // still make an exact re-selection a duplicate rather than add it again.
    manager.removeFile(0);
    manager.addFiles([makeFile("same.txt", { size: 4, lastModified: 42, bytes: "bbbb" })]);
    await flush();
    assert.deepEqual(manager.getPendingFiles().map((file) => file.data), ["YmJiYg=="]);
  });

  it("keeps images with colliding metadata when their converted payloads differ", async () => {
    const OriginalImage = globalThis.Image;
    const originalCreateElement = globalThis.document.createElement;
    const payloads = ["YWFhYQ==", "YmJiYg=="];
    globalThis.Image = class {
      constructor() { this.width = 1; this.height = 1; }
      set src(_value) { queueMicrotask(() => this.onload?.()); }
    };
    globalThis.document.createElement = (tag) => tag === "canvas"
      ? {
          getContext: () => ({ drawImage() {} }),
          toDataURL: (type) => `data:${type};base64,${payloads.shift()}`,
        }
      : originalCreateElement(tag);
    try {
      manager.addFiles([
        makeFile("same.png", { type: "image/png", size: 4, lastModified: 42 }),
        makeFile("same.png", { type: "image/png", size: 4, lastModified: 42 }),
      ]);
      await flush();
      await flush();
    } finally {
      globalThis.Image = OriginalImage;
      globalThis.document.createElement = originalCreateElement;
    }
    assert.deepEqual(manager.getPendingImages().map((image) => image.data), ["YWFhYQ==", "YmJiYg=="]);
    assert.notEqual(manager.getDisplayImages()[0].key, manager.getDisplayImages()[1].key);
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

  it("escapes crafted file names in status messages (no DOM XSS)", async () => {
    const name = "<img src=x onerror=alert(1)>.exe";
    container.dispatch("drop", dropEvent([makeFile(name, { type: "application/x-msdownload" })]));
    await flush();
    assert.equal(manager.getFileCount(), 0);
    assert.equal(manager.getStatus()?.kind, "error");
    assert.ok(manager.getStatus()?.message.includes(name), "status message carries the raw file name");
    assert.doesNotMatch(preview.innerHTML, /<img src=x/, "file name must not be inserted as markup");
    assert.match(preview.innerHTML, /&lt;img src=x onerror=alert\(1\)&gt;\.exe/);
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

  it("rejects documents that would exceed the server's encoded aggregate limit", async () => {
    const eightMiB = 8 * 1024 * 1024;
    manager.addFiles([
      makeFile("first.txt", { size: eightMiB, lastModified: 1 }),
      makeFile("second.txt", { size: eightMiB, lastModified: 2 }),
    ]);
    await flush();
    assert.equal(manager.getFileCount(), 1);
    assert.equal(manager.getStatus()?.kind, "error");
    assert.match(manager.getStatus()?.message, /chat\.file_payload_too_large_client/);
  });

  it("keeps a queued restore all-or-none when its document payload would exceed the aggregate limit", () => {
    const elevenMiB = "Y".repeat(11 * 1024 * 1024);
    const entry = {
      files: [
        { name: "first.txt", media_type: "text/plain", data: elevenMiB },
        { name: "second.txt", media_type: "text/plain", data: elevenMiB },
      ],
      displayFiles: [
        { key: "first.txt|11m|1" },
        { key: "second.txt|11m|2" },
      ],
    };
    assert.equal(manager.canRestoreAttachments(entry), false);
    assert.equal(manager.restoreAttachments(entry), 0);
    assert.equal(manager.getFileCount(), 0);
    assert.match(manager.getStatus()?.message, /chat\.file_payload_too_large_client/);
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

  it("keeps an empty document read sticky so a bare submit is blocked", async () => {
    // A zero-byte file yields a data URL without a payload; the read fails inside onload.
    container.dispatch("drop", dropEvent([makeFile("empty.txt", { bytes: "" })]));
    await flush();
    assert.equal(manager.getFileCount(), 0);
    assert.equal(manager.isProcessing(), false);
    assert.equal(manager.getStatus()?.kind, "error");
    assert.match(manager.getStatus()?.message, /chat\.file_read_failed/);
    assert.equal(manager.prepareForSubmit(), false, "a failed document read must not degrade into a text-only send");
  });

  it("keeps an image conversion failure sticky so a bare submit is blocked", async () => {
    // The mock <canvas> has no getContext(), so decoding succeeds but conversion throws.
    const OriginalImage = globalThis.Image;
    globalThis.Image = class { set src(_value) { queueMicrotask(() => this.onload?.()); } };
    try {
      container.dispatch("drop", dropEvent([makeFile("photo.png", { type: "image/png" })]));
      await flush();
    } finally {
      globalThis.Image = OriginalImage;
    }
    assert.equal(manager.getImageCount(), 0);
    assert.equal(manager.isProcessing(), false);
    assert.equal(manager.getStatus()?.kind, "error");
    assert.equal(manager.prepareForSubmit(), false, "a rejected image must not degrade into a text-only send");
    // A later successful document in the same batch must not hide the conversion error.
    container.dispatch("drop", dropEvent([makeFile("ok.pdf")]));
    await flush();
    assert.equal(manager.getFileCount(), 1);
  });

  it("blocks submit while a document is still being read", () => {
    container.dispatch("drop", dropEvent([makeFile("slow.pdf")]));
    assert.equal(manager.isProcessing(), true);
    assert.equal(manager.prepareForSubmit(), false);
  });

  it("restoreAttachments puts a queued entry's documents and images back and keeps de-duplication", async () => {
    const file = makeFile("spec.docx", { size: 10, lastModified: 42 });
    container.dispatch("drop", dropEvent([file, makeFile("data.csv")]));
    await flush();
    assert.equal(manager.getFileCount(), 2);
    // Snapshot exactly what addToQueue() captures, then clear like enqueue does.
    const entry = {
      text: "later",
      images: manager.getPendingImages(),
      displayImages: manager.getDisplayImages(),
      files: manager.getPendingFiles(),
      displayFiles: manager.getDisplayFiles(),
    };
    manager.clearImages();
    assert.equal(manager.getFileCount(), 0);

    const before = changed;
    assert.equal(manager.restoreAttachments(entry), 2);
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name), ["spec.docx", "data.csv"]);
    assert.equal(manager.getPendingFiles()[0].data, entry.files[0].data, "payload survives the round trip");
    assert.equal(changed, before + 1);
    assert.match(preview.innerHTML, /spec\.docx/);

    // The same entry restored twice, or the original file dropped again, is not duplicated.
    assert.equal(manager.restoreAttachments(entry), 0);
    container.dispatch("drop", dropEvent([file]));
    await flush();
    assert.equal(manager.getFileCount(), 2);
    assert.match(manager.getStatus()?.message, /chat\.file_duplicate_client/);

    // Images restore with their preview dataUrl; malformed/empty entries are ignored.
    const img = { data: "aGVsbG8=", media_type: "image/png" };
    assert.equal(manager.restoreAttachments({ images: [img], displayImages: [{ ...img, dataUrl: "data:x" }] }), 1);
    assert.equal(manager.getImageCount(), 1);
    assert.equal(manager.getDisplayImages()[0].dataUrl, "data:x");
    assert.equal(manager.restoreAttachments(null), 0);
    assert.equal(manager.restoreAttachments({ files: [{ name: "" }, { data: "" }] }), 0);
  });

  it("restoreAttachments enforces the per-message document count limit", async () => {
    const files = Array.from({ length: 10 }, (_, i) => makeFile("f" + i + ".txt"));
    container.dispatch("drop", dropEvent(files));
    await flush();
    assert.equal(manager.getFileCount(), 10);
    const restored = manager.restoreAttachments({
      files: [{ name: "extra.txt", media_type: "text/plain", data: "eA==" }],
      displayFiles: [{ name: "extra.txt", media_type: "text/plain", key: "extra" }],
    });
    assert.equal(restored, 0);
    assert.equal(manager.getFileCount(), 10);
    assert.equal(manager.getStatus()?.kind, "error");
    assert.match(manager.getStatus()?.message, /chat\.file_count_limit_client/);
  });

  it("restoreAttachments is all-or-none and canRestoreAttachments preflights the count limit", async () => {
    const files = Array.from({ length: 9 }, (_, i) => makeFile("f" + i + ".txt"));
    container.dispatch("drop", dropEvent(files));
    await flush();
    assert.equal(manager.getFileCount(), 9);
    const entry = {
      files: [
        { name: "a.txt", media_type: "text/plain", data: "YQ==" },
        { name: "b.txt", media_type: "text/plain", data: "Yg==" },
      ],
      displayFiles: [
        { name: "a.txt", media_type: "text/plain", key: "a" },
        { name: "b.txt", media_type: "text/plain", key: "b" },
      ],
    };
    const before = changed;
    assert.equal(manager.canRestoreAttachments(entry), false, "9 + 2 exceeds the limit of 10");
    assert.equal(manager.getStatus()?.kind, "error");
    assert.match(manager.getStatus()?.message, /chat\.file_count_limit_client/);
    assert.match(manager.getStatus()?.message, /b\.txt/, "names the document that does not fit");
    assert.equal(manager.restoreAttachments(entry), 0, "nothing is restored when the entry does not fit");
    assert.equal(manager.getFileCount(), 9, "the first document must not be restored without the second");
    assert.equal(changed, before);
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name).includes("a.txt"), false);

    // Removing one composer document makes room for both; the preflight agrees with the restore.
    manager.removeFile(0);
    assert.equal(manager.canRestoreAttachments(entry), true);
    assert.equal(manager.restoreAttachments(entry), 2);
    assert.equal(manager.getFileCount(), 10);

    // Documents already in the composer do not count against the entry again.
    assert.equal(manager.canRestoreAttachments(entry), true);
    assert.equal(manager.restoreAttachments(entry), 0);
    assert.equal(manager.getFileCount(), 10);
  });

  it("restoreAttachments tells apart distinct payloads of equal length when the snapshot has no key", () => {
    const one = { data: "YWFhYQ==", media_type: "image/png" }; // "aaaa"
    const two = { data: "YmJiYg==", media_type: "image/png" }; // "bbbb" — same media type, same encoded length
    assert.equal(one.data.length, two.data.length);
    assert.equal(manager.restoreAttachments({ images: [one, two], displayImages: [] }), 2);
    assert.equal(manager.getImageCount(), 2);
    const keys = manager.getDisplayImages().map((i) => i.key);
    assert.notEqual(keys[0], keys[1]);
    // Restoring the same payloads again is still de-duplicated.
    assert.equal(manager.restoreAttachments({ images: [one, two] }), 0);
    assert.equal(manager.getImageCount(), 2);

    const docA = { name: "same.txt", media_type: "text/plain", data: "YWFhYQ==" };
    const docB = { name: "same.txt", media_type: "text/plain", data: "YmJiYg==" };
    assert.equal(manager.restoreAttachments({ files: [docA, docB] }), 2);
    assert.equal(manager.getFileCount(), 2);
    assert.equal(manager.restoreAttachments({ files: [docA, docB] }), 0);
    // The same payload listed twice within one entry is only restored once.
    assert.equal(manager.restoreAttachments({ files: [{ ...docA, name: "twice.txt" }, { ...docA, name: "twice.txt" }] }), 1);
  });

  it("restoreAttachments confirms fallback identity by the whole payload, not only its fingerprint", () => {
    // Force every payload to the same fingerprint: a collision must not drop a
    // distinct payload, while identical payloads stay de-duplicated.
    const colliding = mod.createImageInput({
      container: new MockEl("div"),
      inputArea: new MockEl("textarea"),
      previewContainer: new MockEl("div"),
      fingerprint: () => "collision",
    });
    const one = { data: "YWFhYQ==", media_type: "image/png" };
    const two = { data: "YmJiYg==", media_type: "image/png" };
    assert.equal(colliding.restoreAttachments({ images: [one, two] }), 2, "colliding fingerprints restore both payloads");
    const keys = colliding.getDisplayImages().map((i) => i.key);
    assert.notEqual(keys[0], keys[1]);
    assert.equal(colliding.restoreAttachments({ images: [one, two] }), 0, "identical payloads are still de-duplicated");
    assert.equal(colliding.restoreAttachments({ images: [two, one] }), 0, "order does not matter");
    assert.equal(colliding.getImageCount(), 2);

    const docA = { name: "same.txt", media_type: "text/plain", data: "YWFhYQ==" };
    const docB = { name: "same.txt", media_type: "text/plain", data: "YmJiYg==" };
    const docC = { name: "same.txt", media_type: "text/plain", data: "Y2NjYw==" };
    assert.equal(colliding.restoreAttachments({ files: [docA, docB, docA] }), 2, "third distinct-looking entry is the first payload again");
    assert.equal(colliding.restoreAttachments({ files: [docB, docA] }), 0);
    assert.equal(colliding.restoreAttachments({ files: [docC] }), 1, "a third colliding payload still gets its own key");
    assert.equal(colliding.getFileCount(), 3);
    assert.equal(new Set(colliding.getDisplayFiles().map((f) => f.key)).size, 3);
  });

  it("restoreAttachments keeps distinct payloads when an explicit snapshot key collides", () => {
    const imageKey = "same.png|4|42";
    const composerImage = { data: "YmJiYg==", media_type: "image/png" };
    const queuedImage = { data: "YWFhYQ==", media_type: "image/png" };
    assert.equal(manager.restoreAttachments({
      images: [composerImage],
      displayImages: [{ ...composerImage, key: imageKey }],
    }), 1);
    const imageEntry = {
      images: [queuedImage],
      displayImages: [{ ...queuedImage, key: imageKey }],
    };
    assert.equal(manager.canRestoreAttachments(imageEntry), true);
    assert.equal(manager.restoreAttachments(imageEntry), 1);
    assert.deepEqual(manager.getPendingImages().map((image) => image.data), [composerImage.data, queuedImage.data]);
    assert.notEqual(manager.getDisplayImages()[0].key, manager.getDisplayImages()[1].key);
    assert.equal(manager.restoreAttachments(imageEntry), 0, "the restored image remains de-duplicated");

    const fileKey = "same.txt|4|42";
    const composerFile = { name: "same.txt", media_type: "text/plain", data: "YmJiYg==" };
    const queuedFile = { name: "same.txt", media_type: "text/plain", data: "YWFhYQ==" };
    assert.equal(manager.restoreAttachments({
      files: [composerFile],
      displayFiles: [{ ...composerFile, key: fileKey }],
    }), 1);
    const fileEntry = {
      files: [queuedFile],
      displayFiles: [{ ...queuedFile, key: fileKey }],
    };
    assert.equal(manager.canRestoreAttachments(fileEntry), true);
    assert.equal(manager.restoreAttachments(fileEntry), 1);
    assert.deepEqual(manager.getPendingFiles().map((file) => file.data), [composerFile.data, queuedFile.data]);
    assert.notEqual(manager.getDisplayFiles()[0].key, manager.getDisplayFiles()[1].key);
    assert.equal(manager.restoreAttachments(fileEntry), 0, "the restored document remains de-duplicated");
  });

  it("keeps a queued document intact while a colliding composer document is still loading", () => {
    const OriginalFileReader = globalThis.FileReader;
    const readers = [];
    globalThis.FileReader = class {
      readAsDataURL(file) { this.file = file; readers.push(this); }
      finish() {
        this.result = `data:application/octet-stream;base64,${this.file._bytes.toString("base64")}`;
        this.onload?.();
      }
    };
    try {
      const key = "same.txt|4|42";
      const queued = {
        files: [{ name: "same.txt", media_type: "text/plain", data: "YWFhYQ==" }],
        displayFiles: [{ key }],
      };
      manager.addFiles([makeFile("same.txt", { size: 4, lastModified: 42, bytes: "bbbb" })]);
      assert.equal(manager.isProcessing(), true);
      assert.equal(manager.canRestoreAttachments(queued), false);
      assert.equal(manager.restoreAttachments(queued), 0);
      assert.equal(manager.getFileCount(), 0);

      readers[0].finish();
      assert.equal(manager.canRestoreAttachments(queued), true);
      assert.equal(manager.restoreAttachments(queued), 1);
      assert.deepEqual(manager.getPendingFiles().map((file) => file.data), ["YmJiYg==", "YWFhYQ=="]);
    } finally {
      globalThis.FileReader = OriginalFileReader;
    }
  });

  it("keeps a queued image intact while a colliding composer image is still decoding", () => {
    const OriginalImage = globalThis.Image;
    const originalCreateElement = globalThis.document.createElement;
    const images = [];
    globalThis.Image = class {
      constructor() { this.width = 1; this.height = 1; images.push(this); }
      set src(value) { this._src = value; }
      finish() { this.onload?.(); }
    };
    globalThis.document.createElement = (tag) => tag === "canvas"
      ? {
          getContext: () => ({ drawImage() {} }),
          toDataURL: (type) => `data:${type};base64,YmJiYg==`,
        }
      : originalCreateElement(tag);
    try {
      const key = "same.png|4|42";
      const queued = {
        images: [{ media_type: "image/png", data: "YWFhYQ==" }],
        displayImages: [{ key }],
      };
      manager.addFiles([makeFile("same.png", { type: "image/png", size: 4, lastModified: 42 })]);
      assert.equal(manager.isProcessing(), true);
      assert.equal(manager.canRestoreAttachments(queued), false);
      assert.equal(manager.restoreAttachments(queued), 0);
      assert.equal(manager.getImageCount(), 0);

      images[0].finish();
      assert.equal(manager.canRestoreAttachments(queued), true);
      assert.equal(manager.restoreAttachments(queued), 1);
      assert.deepEqual(manager.getPendingImages().map((image) => image.data), ["YmJiYg==", "YWFhYQ=="]);
    } finally {
      globalThis.Image = OriginalImage;
      globalThis.document.createElement = originalCreateElement;
    }
  });

  it("reserves in-flight document reads during queue restore preflight", () => {
    const OriginalFileReader = globalThis.FileReader;
    const readers = [];
    globalThis.FileReader = class {
      readAsDataURL(file) { this.file = file; readers.push(this); }
      finish() {
        this.result = `data:application/octet-stream;base64,${this.file._bytes.toString("base64")}`;
        this.onload?.();
      }
    };
    try {
      manager.addFiles(Array.from({ length: 10 }, (_, i) => makeFile(`f${i}.txt`, {
        size: 1,
        lastModified: i,
        bytes: String(i),
      })));
      readers.slice(0, 9).forEach((reader) => reader.finish());
      assert.equal(manager.getFileCount(), 9);
      assert.equal(manager.isProcessing(), true);
      const queued = {
        files: [{ name: "queued.txt", media_type: "text/plain", data: "cQ==" }],
        displayFiles: [{ key: "queued.txt|1|99" }],
      };
      assert.equal(manager.canRestoreAttachments(queued), false);
      assert.equal(manager.restoreAttachments(queued), 0);
      readers[9].finish();
      assert.equal(manager.getFileCount(), 10);
    } finally {
      globalThis.FileReader = OriginalFileReader;
    }
  });

  it("pasted images receive distinct identities that survive a queue edit round trip", async () => {
    // Pasted images have no name/mtime; two of the same type and size must not
    // collapse into one after being queued and restored into the composer.
    const OriginalImage = globalThis.Image;
    const originalCreateElement = globalThis.document.createElement;
    const payloads = ["YWFhYQ==", "YmJiYg=="]; // equal length, different bytes
    globalThis.Image = class { set src(_value) { queueMicrotask(() => this.onload?.()); } };
    globalThis.document.createElement = (tag) => {
      if (tag !== "canvas") return originalCreateElement(tag);
      return {
        getContext: () => ({ drawImage() {} }),
        toDataURL: (type) => `data:${type || "image/png"};base64,${payloads.shift() || "eA=="}`,
      };
    };
    try {
      const pasteEvent = (file) => ({
        prevented: false,
        preventDefault() { this.prevented = true; },
        clipboardData: { items: [{ type: "image/png", getAsFile: () => file }] },
      });
      const first = inputArea.dispatch("paste", pasteEvent(makeFile("image.png", { type: "image/png", size: 4 })));
      const second = inputArea.dispatch("paste", pasteEvent(makeFile("image.png", { type: "image/png", size: 4 })));
      assert.equal(first.prevented, true);
      assert.equal(second.prevented, true);
      await flush();
      await flush();
    } finally {
      globalThis.Image = OriginalImage;
      globalThis.document.createElement = originalCreateElement;
    }
    assert.equal(manager.getImageCount(), 2, "two pastes attach two images");
    const keys = manager.getDisplayImages().map((i) => i.key);
    assert.match(keys[0], /^paste\|/);
    assert.match(keys[1], /^paste\|/);
    assert.notEqual(keys[0], keys[1]);

    const entry = { images: manager.getPendingImages(), displayImages: manager.getDisplayImages(), files: [], displayFiles: [] };
    manager.clearImages();
    assert.equal(manager.restoreAttachments(entry), 2, "both pasted images come back after a queue edit");
    assert.deepEqual(manager.getDisplayImages().map((i) => i.key), keys);
    assert.equal(manager.restoreAttachments(entry), 0);
  });

  it("documentLabelFor maps extensions to short labels", () => {
    assert.equal(mod.documentLabelFor("a.docx"), "DOCX");
    assert.equal(mod.documentLabelFor("A.XLS"), "XLS");
    assert.equal(mod.documentLabelFor("readme.md"), "MD");
    assert.equal(mod.documentLabelFor("weird.bin"), "FILE");
  });
});

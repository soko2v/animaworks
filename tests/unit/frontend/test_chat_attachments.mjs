/**
 * Unit tests for chat attachments (images + PDF files):
 *   - server/static/shared/image-input.js   (picker / drop / paste / preview)
 *   - server/static/shared/chat/session-manager.js (files in the request body)
 *   - server/static/shared/chat/render-utils.js    (file chips in bubbles)
 *   - server/static/shared/chat-stream.js          (readable HTTP errors)
 *
 * Run with: node --test tests/unit/frontend/test_chat_attachments.mjs
 */

import { describe, it, beforeEach, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(__dirname, "../../../server/static");

const toDataUrl = (body, tag) =>
  "data:text/javascript;base64," + Buffer.from(body, "utf8").toString("base64") + "#" + tag;
const stripImports = (src) => src.replace(/(?:^|\n)\s*import\b[\s\S]*?;/g, "\n");
// t() stub: key plus raw parameter values, so tests can check escaping.
const T_STUB = `const t = (key, params = {}) => [key, ...Object.values(params)].join("|");`;

const imageInput = await import(toDataUrl(
  `${T_STUB}\nconst basePath = "/base";\n` + stripImports(readFileSync(resolve(STATIC, "shared/image-input.js"), "utf8")),
  "image-input",
));
const renderUtils = await import(toDataUrl(
  `${T_STUB}\n` + stripImports(readFileSync(resolve(STATIC, "shared/chat/render-utils.js"), "utf8")),
  "render-utils",
));
const historyUrl = toDataUrl(readFileSync(resolve(STATIC, "shared/chat/history-loader.js"), "utf8"), "history-loader");
const { ChatSessionManager } = await import(toDataUrl(
  `import { createHistoryState, applyHistoryData, mergePolledHistory } from "${historyUrl}";\nconst basePath = "";\n`
    + stripImports(readFileSync(resolve(STATIC, "shared/chat/session-manager.js"), "utf8")),
  "session-manager",
));
const sseUrl = toDataUrl(
  `const t = (k) => k;\nconst createLogger = () => ({ debug() {}, info() {}, warn() {}, error() {} });\n`
    + stripImports(readFileSync(resolve(STATIC, "shared/sse-parser.js"), "utf8")),
  "sse-parser",
);
const { streamChat } = await import(toDataUrl(
  `import { parseConvSSE, getErrorMessage } from "${sseUrl}";\nconst basePath = "";\n`
    + `const createLogger = () => ({ debug() {}, info() {}, warn() {}, error() {} });\n`
    + stripImports(readFileSync(resolve(STATIC, "shared/chat-stream.js"), "utf8")),
  "chat-stream",
));

// ── Fake browser environment ──────────────────────────

class FakeClassList {
  constructor() { this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
  contains(c) { return this.set.has(c); }
}

class FakeElement {
  constructor() {
    this.listeners = {};
    this.classList = new FakeClassList();
    this.dataset = {};
    this.style = {};
    this.innerHTML = "";
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event) { for (const fn of this.listeners[type] || []) fn(event); return event; }
  contains(node) { return node === this; }
  querySelectorAll() { return []; }
  focus() {}
}

function fakeFile(name, { type = "application/pdf", size = null, content = "%PDF-1.4 test", lastModified = 1, fail = false } = {}) {
  return { name, type, size: size ?? content.length, lastModified, _content: content, _fail: fail };
}

class FakeFileReader {
  readAsDataURL(file) {
    queueMicrotask(() => {
      if (file._fail) { this.onerror?.(); return; }
      this.result = `data:${file.type || "application/octet-stream"};base64,` + Buffer.from(file._content).toString("base64");
      this.onload?.();
    });
  }
}

class FakeImage {
  set src(value) {
    this._src = value;
    this.width = 10;
    this.height = 10;
    queueMicrotask(() => (value.includes("broken") ? this.onerror?.() : this.onload?.()));
  }
  get src() { return this._src; }
}

function dragEvent(types, files = []) {
  return {
    dataTransfer: { types, files, dropEffect: "" },
    defaultPrevented: false,
    relatedTarget: null,
    preventDefault() { this.defaultPrevented = true; },
  };
}

const flush = () => new Promise((r) => setImmediate(r));

const saved = {};
beforeEach(() => {
  mock.timers.enable({ apis: ["setTimeout"] });
  saved.createObjectURL = URL.createObjectURL;
  saved.revokeObjectURL = URL.revokeObjectURL;
  URL.createObjectURL = (file) => `blob:fake/${file.name}`;
  URL.revokeObjectURL = () => {};
  globalThis.FileReader = FakeFileReader;
  globalThis.Image = FakeImage;
  globalThis.document = {
    createElement: () => ({ getContext: () => ({ drawImage() {} }), toDataURL: (type) => `data:${type};base64,AAAA` }),
  };
});
afterEach(() => {
  mock.timers.reset();
  URL.createObjectURL = saved.createObjectURL;
  URL.revokeObjectURL = saved.revokeObjectURL;
});

function makeManager(extra = {}) {
  const container = new FakeElement();
  const inputArea = new FakeElement();
  const previewContainer = new FakeElement();
  let changes = 0;
  const manager = imageInput.createImageInput({
    container, inputArea, previewContainer, onImagesChanged: () => { changes += 1; }, ...extra,
  });
  return { manager, container, inputArea, previewContainer, changes: () => changes };
}

// ── helpers ───────────────────────────────────────────

describe("attachment helpers", () => {
  it("detects PDFs by type or by name for generic types", () => {
    assert.equal(imageInput.isPdfFile({ name: "a.bin", type: "application/pdf" }), true);
    assert.equal(imageInput.isPdfFile({ name: "A.PDF", type: "" }), true);
    assert.equal(imageInput.isPdfFile({ name: "a.pdf", type: "application/octet-stream" }), true);
    assert.equal(imageInput.isPdfFile({ name: "a.pdf", type: "image/png" }), false);
    assert.equal(imageInput.isPdfFile({ name: "a.txt", type: "" }), false);
    assert.equal(imageInput.isPdfFile(null), false);
  });

  it("formats sizes", () => {
    assert.equal(imageInput.formatFileSize(820), "820 B");
    assert.equal(imageInput.formatFileSize(12 * 1024), "12 KB");
    assert.equal(imageInput.formatFileSize(3.4 * 1024 * 1024), "3.4 MB");
  });

  it("recognises OS file drags only", () => {
    assert.equal(imageInput.hasFilePayload({ types: ["Files"] }), true);
    assert.equal(imageInput.hasFilePayload({ types: { contains: (t) => t === "Files" } }), true);
    assert.equal(imageInput.hasFilePayload({ types: ["text/plain"] }), false);
    assert.equal(imageInput.hasFilePayload(null), false);
  });

  it("accept list covers images and PDF", () => {
    assert.ok(imageInput.ATTACHMENT_ACCEPT.includes("application/pdf"));
    assert.ok(imageInput.ATTACHMENT_ACCEPT.includes(".pdf"));
    assert.ok(imageInput.ATTACHMENT_ACCEPT.includes("image/png"));
  });
});

// ── manager ───────────────────────────────────────────

describe("createImageInput: PDF files", () => {
  it("adds a PDF and exposes send/display payloads", async () => {
    const { manager, previewContainer, changes } = makeManager();
    manager.addFiles([fakeFile("見積書.pdf", { content: "%PDF-1.7 body" })]);
    assert.equal(manager.isBusy(), true);
    assert.equal(manager.getAttachmentCount(), 1);
    assert.deepEqual(manager.getPendingFiles(), []); // not loaded yet
    assert.match(previewContainer.innerHTML, /chat\.attach_loading/);
    await flush();
    assert.equal(manager.isBusy(), false);
    assert.deepEqual(manager.getPendingFiles(), [{
      data: Buffer.from("%PDF-1.7 body").toString("base64"), media_type: "application/pdf", name: "見積書.pdf",
    }]);
    assert.deepEqual(manager.getDisplayFiles(), [{
      name: "見積書.pdf", size: 13, media_type: "application/pdf", url: "blob:fake/見積書.pdf",
    }]);
    assert.equal(previewContainer.style.display, "flex");
    assert.match(previewContainer.innerHTML, /file-preview-item/);
    assert.match(previewContainer.innerHTML, /見積書\.pdf/);
    assert.match(previewContainer.innerHTML, /13 B/);
    assert.match(previewContainer.innerHTML, /file-preview-remove/);
    assert.ok(changes() >= 2);
  });

  it("rejects oversized, too many, over-total, duplicate, empty and unsupported files", async () => {
    const { manager, previewContainer } = makeManager();
    manager.addFiles([fakeFile("big.pdf", { size: imageInput.MAX_PDF_SIZE + 1 })]);
    assert.match(previewContainer.innerHTML, /attachment-status--error/);
    assert.match(previewContainer.innerHTML, /chat\.attach_pdf_too_large\|big\.pdf/);
    assert.equal(manager.getAttachmentCount(), 0);

    manager.addFiles([fakeFile("notes.txt", { type: "text/plain" })]);
    assert.match(previewContainer.innerHTML, /chat\.attach_unsupported\|notes\.txt/);

    manager.addFiles([fakeFile("empty.pdf", { content: "", size: 0 })]);
    assert.match(previewContainer.innerHTML, /chat\.attach_read_failed\|empty\.pdf/);

    for (let i = 0; i < imageInput.MAX_PDF_FILES; i += 1) manager.addFiles([fakeFile(`${i}.pdf`)]);
    manager.addFiles([fakeFile("one-too-many.pdf")]);
    assert.match(previewContainer.innerHTML, /chat\.attach_too_many_pdfs/);
    assert.equal(manager.getFileCount(), imageInput.MAX_PDF_FILES);

    const fresh = makeManager();
    const part = 15 * 1024 * 1024; // 3 x 15 MB > 40 MB total, each under 20 MB
    assert.ok(part <= imageInput.MAX_PDF_SIZE && 3 * part > imageInput.MAX_PDF_TOTAL_SIZE);
    fresh.manager.addFiles([
      fakeFile("a.pdf", { size: part }), fakeFile("b.pdf", { size: part }), fakeFile("c.pdf", { size: part }),
    ]);
    assert.equal(fresh.manager.getFileCount(), 2);
    assert.match(fresh.previewContainer.innerHTML, /chat\.attach_pdf_total_too_large/);

    fresh.manager.addFiles([fakeFile("a.pdf", { size: part })]);
    assert.match(fresh.previewContainer.innerHTML, /attachment-status--info/);
    assert.match(fresh.previewContainer.innerHTML, /chat\.attach_duplicate\|a\.pdf/);
  });

  it("drops a file whose read fails", async () => {
    const { manager, previewContainer } = makeManager();
    manager.addFiles([fakeFile("broken.pdf", { fail: true })]);
    await flush();
    assert.equal(manager.getAttachmentCount(), 0);
    assert.equal(manager.isBusy(), false);
    assert.match(previewContainer.innerHTML, /chat\.attach_read_failed\|broken\.pdf/);
  });

  it("escapes file names in preview and status", async () => {
    const evil = "<img src=x onerror=alert(1)>.pdf";
    const { manager, previewContainer } = makeManager();
    manager.addFiles([fakeFile(evil)]);
    await flush();
    manager.addFiles([fakeFile(evil)]); // duplicate -> status message with the name
    assert.ok(!previewContainer.innerHTML.includes("<img src=x"));
    assert.ok(previewContainer.innerHTML.includes("&lt;img src=x onerror=alert(1)&gt;.pdf"));
  });

  it("clears and restores attachments (queue edit)", async () => {
    const { manager, previewContainer } = makeManager();
    manager.addFiles([fakeFile("r.pdf")]);
    await flush();
    const entry = {
      files: manager.getPendingFiles(), displayFiles: manager.getDisplayFiles(),
      images: [{ data: "QUJD", media_type: "image/png" }], displayImages: [{ dataUrl: "data:image/png;base64,QUJD" }],
    };
    manager.clearImages();
    assert.equal(manager.getAttachmentCount(), 0);
    assert.equal(previewContainer.style.display, "none");
    manager.restoreAttachments(entry);
    assert.deepEqual(manager.getPendingFiles(), entry.files);
    assert.deepEqual(manager.getDisplayFiles(), entry.displayFiles);
    assert.deepEqual(manager.getPendingImages(), entry.images);
  });

  it("releases blob URLs of sent files later, but not ones restored for editing", async () => {
    const revoked = [];
    URL.revokeObjectURL = (url) => revoked.push(url);
    const { manager } = makeManager();
    manager.addFiles([fakeFile("sent.pdf"), fakeFile("edited.pdf")]);
    await flush();
    const sent = manager.getDisplayFiles();
    const pending = manager.getPendingFiles();
    manager.clearImages(); // both were sent/queued
    manager.restoreAttachments({ files: [pending[1]], displayFiles: [sent[1]] }); // edited.pdf back from the queue
    mock.timers.tick(31 * 60 * 1000);
    assert.deepEqual(revoked, ["blob:fake/sent.pdf"]);
  });

  it("status disappears after a while", async () => {
    const { manager, previewContainer } = makeManager();
    manager.showStatus("info", "wait please");
    assert.match(previewContainer.innerHTML, /wait please/);
    mock.timers.tick(9000);
    assert.equal(previewContainer.style.display, "none");
  });
});

describe("createImageInput: images keep working", () => {
  it("adds a supported image via the same pipeline", async () => {
    const { manager } = makeManager();
    manager.addFiles([fakeFile("shot.png", { type: "image/png", content: "png" })]);
    assert.equal(manager.isBusy(), true);
    await flush();
    assert.equal(manager.isBusy(), false);
    assert.deepEqual(manager.getPendingImages(), [{ data: "AAAA", media_type: "image/png" }]);
    assert.equal(manager.getImageCount(), 1);
    assert.equal(manager.getFileCount(), 0);
  });

  it("explains unsupported or oversized images instead of ignoring them", () => {
    const { manager, previewContainer } = makeManager();
    manager.addFiles([fakeFile("photo.heic", { type: "image/heic" })]);
    assert.match(previewContainer.innerHTML, /chat\.attach_unsupported_image\|photo\.heic/);
    manager.addFiles([fakeFile("huge.jpg", { type: "image/jpeg", size: 6 * 1024 * 1024 })]);
    assert.match(previewContainer.innerHTML, /chat\.attach_image_too_large\|huge\.jpg/);
    assert.equal(manager.getAttachmentCount(), 0);
  });
});

describe("createImageInput: attachments belong to one conversation", () => {
  it("a PDF dropped for Anima A is never sent to Anima B, and comes back with A", async () => {
    const { manager, previewContainer } = makeManager();
    manager.switchConversation("alice|default");
    manager.addFiles([fakeFile("for-alice.pdf")]);
    await flush();
    manager.switchConversation("bob|default");
    assert.equal(manager.getAttachmentCount(), 0);
    assert.deepEqual(manager.getPendingFiles(), []);
    assert.equal(previewContainer.style.display, "none");
    manager.switchConversation("alice|default");
    assert.deepEqual(manager.getDisplayFiles().map((f) => f.name), ["for-alice.pdf"]);
  });

  it("reads finishing after a switch land in the conversation they were added to", async () => {
    const { manager, previewContainer } = makeManager();
    manager.switchConversation("alice|t1");
    manager.addFiles([fakeFile("slow.pdf"), fakeFile("pic.png", { type: "image/png", content: "png" })]);
    manager.switchConversation("bob|t1"); // before the reads complete
    await flush();
    assert.equal(manager.getAttachmentCount(), 0);
    assert.deepEqual(manager.getPendingImages(), []);
    assert.ok(!previewContainer.innerHTML.includes("slow.pdf"));
    manager.switchConversation("alice|t1");
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name), ["slow.pdf"]);
    assert.equal(manager.getPendingImages().length, 1);
  });

  it("an image still being read when switching is not lost", async () => {
    const { manager } = makeManager();
    manager.switchConversation("alice|default");
    manager.addFiles([fakeFile("only.png", { type: "image/png", content: "png" })]);
    manager.switchConversation("bob|default");
    await flush();
    manager.switchConversation("alice|default");
    assert.equal(manager.getImageCount(), 1);
  });

  it("retarget (new/closed thread, text stays) keeps the attachments and files them under the new key", async () => {
    const { manager } = makeManager();
    manager.switchConversation("alice|t1");
    manager.addFiles([fakeFile("draft.pdf")]);
    await flush();
    manager.retargetConversation("alice|default"); // thread t1 closed
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name), ["draft.pdf"]);
    manager.switchConversation("bob|default");
    assert.equal(manager.getAttachmentCount(), 0);
    manager.switchConversation("alice|t1"); // nothing left under the closed thread
    assert.equal(manager.getAttachmentCount(), 0);
    manager.switchConversation("alice|default");
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name), ["draft.pdf"]);
    manager.clearImages(); // sent
    manager.switchConversation("bob|default");
    manager.switchConversation("alice|default");
    assert.equal(manager.getAttachmentCount(), 0);
  });

  it("retarget onto a conversation with stashed attachments keeps both", async () => {
    const { manager, previewContainer } = makeManager();
    manager.switchConversation("alice|default");
    manager.addFiles([fakeFile("old.pdf")]);
    await flush();
    manager.switchConversation("alice|t2");
    manager.addFiles([fakeFile("new.pdf")]);
    await flush();
    manager.retargetConversation("alice|default"); // t2 closed while "default" had a stash
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name).sort(), ["new.pdf", "old.pdf"]);
    assert.match(previewContainer.innerHTML, /old\.pdf/);
    assert.match(previewContainer.innerHTML, /new\.pdf/);
    manager.switchConversation("alice|t2"); // the closed thread has nothing left
    assert.equal(manager.getAttachmentCount(), 0);
  });

  it("an image still being read follows a retarget (thread closed before the read finished)", async () => {
    const { manager, previewContainer } = makeManager();
    manager.switchConversation("alice|default");
    manager.addFiles([fakeFile("late.png", { type: "image/png", content: "png" })]);
    manager.switchConversation("alice|t3"); // before the read completes
    manager.retargetConversation("alice|default"); // t3 closed: the composer is back on "default"
    await flush();
    assert.equal(manager.getImageCount(), 1);
    assert.match(previewContainer.innerHTML, /image-preview-item/);
    manager.switchConversation("alice|t3"); // the closed thread has nothing
    assert.equal(manager.getImageCount(), 0);
    manager.switchConversation("alice|default");
    assert.equal(manager.getImageCount(), 1);
  });

  it("removing a file of another conversation's stash only touches that stash", async () => {
    const { manager } = makeManager();
    manager.switchConversation("alice|default");
    manager.addFiles([fakeFile("keep.pdf")]);
    await flush();
    manager.switchConversation("bob|default");
    manager.addFiles([fakeFile("bob.pdf")]);
    await flush();
    manager.clearImages();
    assert.equal(manager.getAttachmentCount(), 0);
    manager.switchConversation("alice|default");
    assert.deepEqual(manager.getPendingFiles().map((f) => f.name), ["keep.pdf"]);
  });
});

describe("createImageInput: drag & drop and paste", () => {
  it("shows the drop overlay for file drags and accepts dropped PDFs", async () => {
    const { manager, container } = makeManager();
    assert.equal(container.dataset.dropLabel, "chat.attach_drop_here");
    const over = container.dispatch("dragover", dragEvent(["Files"]));
    assert.equal(over.defaultPrevented, true);
    assert.equal(over.dataTransfer.dropEffect, "copy");
    assert.equal(container.classList.contains("image-drag-over"), true);
    mock.timers.tick(1000); // dragover stopped (e.g. Safari without relatedTarget)
    assert.equal(container.classList.contains("image-drag-over"), false);

    container.dispatch("dragenter", dragEvent(["Files"]));
    const drop = container.dispatch("drop", dragEvent(["Files"], [fakeFile("dropped.pdf")]));
    assert.equal(drop.defaultPrevented, true);
    assert.equal(container.classList.contains("image-drag-over"), false);
    await flush();
    assert.equal(manager.getPendingFiles()[0].name, "dropped.pdf");
  });

  it("leaves text drags alone", () => {
    const { manager, container } = makeManager();
    const over = container.dispatch("dragover", dragEvent(["text/plain"]));
    const drop = container.dispatch("drop", dragEvent(["text/plain"]));
    assert.equal(over.defaultPrevented, false);
    assert.equal(drop.defaultPrevented, false);
    assert.equal(container.classList.contains("image-drag-over"), false);
    assert.equal(manager.getAttachmentCount(), 0);
  });

  it("pastes a PDF file", async () => {
    const { manager, inputArea } = makeManager();
    const file = fakeFile("pasted.pdf");
    const event = {
      clipboardData: { items: [{ kind: "string", type: "text/plain" }, { kind: "file", type: "application/pdf", getAsFile: () => file }] },
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
    };
    inputArea.dispatch("paste", event);
    assert.equal(event.defaultPrevented, true);
    await flush();
    assert.equal(manager.getPendingFiles()[0].name, "pasted.pdf");
  });

  it("window guard stops navigation only for unhandled file drops", () => {
    const win = new FakeElement();
    imageInput.installWindowDropGuard(win);
    const over = win.dispatch("dragover", dragEvent(["Files"]));
    assert.equal(over.defaultPrevented, true);
    assert.equal(over.dataTransfer.dropEffect, "none");
    const drop = win.dispatch("drop", dragEvent(["Files"]));
    assert.equal(drop.defaultPrevented, true);
    const handled = dragEvent(["Files"]);
    handled.defaultPrevented = true;
    handled.dataTransfer.dropEffect = "copy";
    win.dispatch("dragover", handled);
    assert.equal(handled.dataTransfer.dropEffect, "copy");
    const text = win.dispatch("drop", dragEvent(["text/plain"]));
    assert.equal(text.defaultPrevented, false);
  });
});

// ── rendering ─────────────────────────────────────────

describe("renderChatFiles", () => {
  it("links stored PDFs through the attachments API", () => {
    const html = imageInput.renderChatFiles(
      [{ path: "attachments/20261009_1_契約書.pdf", name: "契約書.pdf", size: 2048, pages: 3 }],
      { animaName: "sofia" },
    );
    assert.match(html, /class="chat-file-chip"/);
    assert.ok(html.includes(`href="/base/api/animas/sofia/attachments/${encodeURIComponent("20261009_1_契約書.pdf")}"`));
    assert.match(html, /target="_blank" rel="noopener noreferrer"/);
    assert.match(html, /契約書\.pdf/);
    assert.match(html, /chat\.attach_pages\|3 · 2 KB/);
  });

  it("uses blob URLs for just-sent files and refuses other URLs", () => {
    assert.match(imageInput.renderChatFiles([{ name: "a.pdf", url: "blob:fake/a.pdf" }]), /href="blob:fake\/a\.pdf"/);
    const evil = imageInput.renderChatFiles([{ name: "x.pdf", url: "javascript:alert(1)" }], { animaName: "s" });
    assert.ok(!evil.includes("javascript:"));
    assert.ok(!evil.includes("<a "));
    const nested = imageInput.renderChatFiles([{ name: "y.pdf", path: "attachments/../y.pdf" }], { animaName: "s" });
    assert.ok(!nested.includes("<a ") && nested.includes("y.pdf"));
  });

  it("escapes names", () => {
    const html = imageInput.renderChatFiles([{ name: "<script>x</script>.pdf", url: "blob:fake/1" }]);
    assert.ok(!html.includes("<script>"));
    assert.ok(html.includes("&lt;script&gt;"));
  });

  it("renders nothing for empty input", () => {
    assert.equal(imageInput.renderChatFiles([]), "");
    assert.equal(imageInput.renderChatFiles(undefined), "");
  });
});

describe("render-utils bubbles", () => {
  const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const opts = {
    escapeHtml,
    renderMarkdown: (s) => escapeHtml(s),
    smartTimestamp: () => "",
    renderChatImages: imageInput.renderChatImages,
    renderChatFiles: imageInput.renderChatFiles,
    animaName: "sofia",
  };

  it("history human message shows PDF chips and image thumbnails", () => {
    const html = renderUtils.renderHistoryMessage({
      role: "human",
      content: "",
      ts: "",
      attachments: [
        { path: "attachments/20261009_0.jpeg", media_type: "image/jpeg" },
        { path: "attachments/x_report.pdf", name: "report.pdf", media_type: "application/pdf", size: 10 },
        { path: "attachments/ignored.bin", media_type: "application/zip" },
      ],
    }, opts);
    assert.match(html, /chat-bubble user/);
    assert.match(html, /class="chat-attached-image"/);
    assert.match(html, /\/base\/api\/animas\/sofia\/attachments\/20261009_0\.jpeg/);
    assert.match(html, /class="chat-file-chip"/);
    assert.match(html, /report\.pdf/);
    assert.ok(!html.includes("ignored.bin"));
  });

  it("history message without attachments is unchanged", () => {
    const html = renderUtils.renderHistoryMessage({ role: "human", content: "hi", ts: "" }, opts);
    assert.ok(!html.includes("chat-file-chip") && !html.includes("chat-images"));
  });

  it("live user bubble shows files", () => {
    const html = renderUtils.renderLiveBubble({
      role: "user", text: "読んで", files: [{ name: "a.pdf", size: 1024, url: "blob:fake/a.pdf" }], images: [], timestamp: "",
    }, opts);
    assert.match(html, /chat-file-chip/);
    assert.match(html, /a\.pdf/);
    assert.match(html, /読んで/);
  });

  it("works without renderChatFiles (older callers)", () => {
    const { renderChatFiles, ...legacy } = opts;
    const html = renderUtils.renderLiveBubble({ role: "user", text: "hi", files: [{ name: "a.pdf" }], timestamp: "" }, legacy);
    assert.match(html, /hi/);
  });
});

// ── session manager ───────────────────────────────────

describe("ChatSessionManager.sendChat with files", () => {
  function configured(streamChat) {
    const mgr = new ChatSessionManager();
    mgr.configure({
      streamChat,
      fetchActiveStream: async () => null,
      fetchStreamProgress: async () => null,
      getUser: () => "human",
      fetchHistory: async () => ({ sessions: [] }),
    });
    return mgr;
  }

  it("sends files in the body and shows them in the user message", async () => {
    let body = null;
    const mgr = configured(async (anima, raw) => { body = JSON.parse(raw); });
    const files = [{ data: "JVBERi0=", media_type: "application/pdf", name: "a.pdf" }];
    const displayFiles = [{ name: "a.pdf", size: 5, media_type: "application/pdf", url: "blob:fake/a.pdf" }];
    await mgr.sendChat("sofia", "default", "", { files, displayFiles });
    assert.deepEqual(body.files, files);
    assert.equal(body.images, undefined);
    const user = mgr.getMessages("sofia", "default").find((m) => m.role === "user");
    assert.deepEqual(user.files, displayFiles);
  });

  it("omits files when there are none (text and image messages unchanged)", async () => {
    let body = null;
    const mgr = configured(async (anima, raw) => { body = JSON.parse(raw); });
    await mgr.sendChat("sofia", "default", "hi", { images: [{ data: "QQ==", media_type: "image/png" }] });
    assert.equal(body.files, undefined);
    assert.equal(body.images.length, 1);
  });

  it("queues files while a stream is running", async () => {
    let release;
    const mgr = configured(() => new Promise((r) => { release = r; }));
    const first = mgr.sendChat("sofia", "default", "first");
    const files = [{ data: "JVBERi0=", media_type: "application/pdf", name: "q.pdf" }];
    const res = await mgr.sendChat("sofia", "default", "second", { files, displayFiles: [{ name: "q.pdf" }] });
    assert.equal(res.queued, true);
    assert.deepEqual(mgr.getPendingQueue("sofia", "default")[0].files, files);
    release();
    await first;
  });
});

// ── dashboard streaming controller: queued items vs meeting mode ──

const { createStreamingController } = await import(toDataUrl(
  `const saveDraft = () => {}; const clearDraft = () => {}; const chatInputMaxHeight = () => 200;
   const scheduleSaveChatUiState = () => {}; const CONSTANTS = {};
   class TextAnimator { constructor() {} start() {} push() {} flush() {} stop() {} }
   const stripThinkTags = (text) => ({ thinking: "", response: text });
   const streamMeetingChat = async (...args) => { globalThis.__meetingCalls.push(args); };\n`
    + stripImports(readFileSync(resolve(STATIC, "pages/chat/streaming-controller.js"), "utf8")),
  "streaming-controller",
));

describe("dashboard sendChat routing", () => {
  function controller({ meetingActive }) {
    globalThis.__meetingCalls = [];
    globalThis.localStorage = { getItem: () => "tester", setItem() {} };
    const chatCalls = [];
    const mgr = {
      addEventListener() {},
      isStreamingFor: () => false,
      getStreamingContext: () => null,
      getPendingQueue: () => [],
      addMessage() {},
      sendChat: async (...args) => { chatCalls.push(args); return { success: true }; },
    };
    const ctx = {
      $: () => null,
      state: {
        manager: mgr, selectedAnima: "luna", selectedThreadId: "default", animas: [{ name: "luna" }],
        threads: {}, modelByThread: {}, imageInputManager: null, container: null,
      },
      deps: {
        t: (k) => k, escapeHtml: (s) => s,
        logger: { debug() {}, info() {}, warn() {}, error() {} },
        fetchActiveStream: async () => null, fetchStreamProgress: async () => null,
      },
      controllers: {
        meeting: { isActive: () => meetingActive, getRoom: () => ({ room_id: "room1" }) },
        anima: { renderAnimaTabs() {} },
        thread: { renderThreadTabs() {} },
        renderer: { reattach() {}, renderChat() {}, isUserDetached: () => false, renderStreamingBubble() {}, markResponseComplete() {} },
        activity: { addLocalActivity() {} },
      },
    };
    return { ctrl: createStreamingController(ctx), chatCalls };
  }

  const queued = {
    images: [], displayImages: [],
    files: [{ data: "JVBERi0=", media_type: "application/pdf", name: "q.pdf" }],
    displayFiles: [{ name: "q.pdf", size: 5 }],
    targetAnima: "luna", targetThread: "t1",
  };

  it("delivers a queued PDF to its Anima even when meeting mode is on", async () => {
    const { ctrl, chatCalls } = controller({ meetingActive: true });
    await ctrl.sendChat("", queued);
    assert.equal(globalThis.__meetingCalls.length, 0);
    assert.equal(chatCalls.length, 1);
    const [anima, thread, text, options] = chatCalls[0];
    assert.equal(anima, "luna");
    assert.equal(thread, "t1");
    assert.equal(text, "");
    assert.deepEqual(options.files, queued.files);
    assert.deepEqual(options.displayFiles, queued.displayFiles);
  });

  it("untargeted sends still go to the active meeting", async () => {
    const { ctrl, chatCalls } = controller({ meetingActive: true });
    await ctrl.sendChat("hello meeting");
    assert.equal(chatCalls.length, 0);
    assert.equal(globalThis.__meetingCalls.length, 1);
  });

  it("normal chat send passes files through", async () => {
    const { ctrl, chatCalls } = controller({ meetingActive: false });
    await ctrl.sendChat("read this", queued);
    assert.deepEqual(chatCalls[0][3].files, queued.files);
  });
});

// ── workspace: a queue draining in the background ─────

describe("workspace queue drain while another conversation is on screen", () => {
  it("sends to the queued Anima with its model and leaves the visible draft alone", async () => {
    mock.timers.reset(); // this flow relies on the real 150 ms drain timer
    const wsState = { conversationAnima: "alice", activeThreadId: "default", threads: {} };
    const sessions = new Map();
    const session = (a, th) => {
      const key = `${a}:${th}`;
      if (!sessions.has(key)) sessions.set(key, { queue: [], streaming: false });
      return sessions.get(key);
    };
    const sendCalls = [];
    let releaseFirst;
    const fakeMgr = {
      getPendingQueue: (a, th) => session(a, th).queue,
      enqueue: (a, th, item) => session(a, th).queue.push(item),
      dequeue: (a, th) => session(a, th).queue.shift(),
      removeFromQueue() {},
      clearQueue() {},
      isStreamingFor: (a, th) => session(a, th).streaming,
      async sendChat(anima, thread, text, options) {
        sendCalls.push({ anima, thread, text, options });
        const s = session(anima, thread);
        s.streaming = true;
        try {
          if (sendCalls.length === 1) await new Promise((r) => { releaseFirst = r; });
          return { success: true };
        } finally {
          s.streaming = false;
          options.onFinally?.();
        }
      },
    };
    globalThis.localStorage = {
      getItem: (k) => (k === "animaworks_ws_model_by_thread" ? JSON.stringify({ "alice|default": "alice-model" }) : null),
      setItem() {},
    };
    globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
    const ws = await import(toDataUrl(
      `const getState = () => globalThis.__wsState; const setState = (p) => Object.assign(globalThis.__wsState, p);
       const t = (k) => k; const getCurrentUser = () => "tester"; const escapeHtml = (s) => String(s);
       const setExpression = () => {}; const setTalking = () => {};
       const createLogger = () => ({ debug() {}, info() {}, warn() {}, error() {} });
       const renderConvMessages = () => {}; const renderOpts = () => ({});
       const renderStreamingBubbleInner = () => ""; const updateStreamingZone = () => {};
       class TextAnimator { constructor() {} start() {} push() {} flush() {} stop() {} }
       const stripThinkTags = (text) => ({ thinking: "", response: text });
       const renderWsThreadTabs = () => {}; const wsSaveDraft = () => {}; const wsClearDraft = () => {};
       const isMobileView = () => false;
       const ChatSessionManager = { getInstance: () => globalThis.__wsMgr };
       const fetchModelCatalog = async () => []; const populateModelSelect = () => {};\n`
        + stripImports(readFileSync(resolve(STATIC, "workspace/modules/chat-streaming.js"), "utf8")),
      "ws-chat-streaming",
    ));
    globalThis.__wsState = wsState;
    globalThis.__wsMgr = fakeMgr;
    const el = () => ({ value: "", disabled: false, style: {}, innerHTML: "", classList: { add() {}, remove() {} }, focus() {} });
    const dom = { convInput: el(), convSend: el(), convModel: { value: "alice-model" }, convQueueBtn: el() };
    ws.initStreaming({ getDom: () => dom, getImageManager: () => null });

    dom.convInput.value = "first";
    ws.submitConversation(); // starts streaming to alice (held open)
    dom.convInput.value = "second for alice";
    ws.addToQueue(); // queued for alice while she is busy
    assert.equal(fakeMgr.getPendingQueue("alice", "default").length, 1);

    // The user switches to Bob, picks another model and starts typing.
    wsState.conversationAnima = "bob";
    dom.convInput.value = "draft for bob";
    dom.convModel.value = "bob-model";

    releaseFirst();
    await new Promise((r) => setTimeout(r, 400)); // drain fires after 150 ms

    assert.equal(sendCalls.length, 2);
    assert.equal(sendCalls[1].anima, "alice");
    assert.equal(sendCalls[1].text, "second for alice");
    assert.equal(sendCalls[1].options.model, "alice-model");
    assert.equal(dom.convInput.value, "draft for bob");
    assert.equal(dom.convInput.disabled, false);
  });
});

// ── chat-stream errors ────────────────────────────────

describe("streamChat HTTP errors", () => {
  it("shows the server's detail message instead of raw JSON", async () => {
    globalThis.fetch = async () => ({
      ok: false, status: 400, statusText: "Bad Request",
      text: async () => JSON.stringify({ detail: "「a.pdf」はパスワードで保護されているため読み込めません" }),
    });
    await assert.rejects(
      () => streamChat("sofia", "{}", null, {}),
      (err) => err.message === "API 400: 「a.pdf」はパスワードで保護されているため読み込めません",
    );
  });

  it("keeps non-JSON bodies", async () => {
    globalThis.fetch = async () => ({ ok: false, status: 502, statusText: "Bad Gateway", text: async () => "upstream down" });
    await assert.rejects(() => streamChat("sofia", "{}", null, {}), (err) => err.message === "API 502: upstream down");
  });
});

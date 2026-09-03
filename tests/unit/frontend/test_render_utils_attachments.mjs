/**
 * Unit tests for document attachment chips in
 * server/static/shared/chat/render-utils.js (persisted history rendering).
 *
 * Run with: node --test tests/unit/frontend/test_render_utils_attachments.mjs
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RENDER_UTILS = resolve(__dirname, "../../../server/static/shared/chat/render-utils.js");

const source = readFileSync(RENDER_UTILS, "utf8").replace(
  /^import\s+\{\s*t\s*\}\s+from\s+["'][^"']+["'];?\s*$/m,
  "const t = (k) => k;",
);
const moduleUrl = "data:text/javascript;base64," + Buffer.from(source, "utf8").toString("base64");
const { renderHistoryMessage, isDocumentAttachmentPath } = await import(moduleUrl);

const escapeHtml = (s) => String(s ?? "")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

const opts = {
  escapeHtml,
  renderMarkdown: (s) => s,
  smartTimestamp: () => "",
  renderChatImages: () => "",
};

function chips(html) {
  return [...html.matchAll(/<span class="chat-attached-file"><b>([A-Z]+)<\/b> ([^<]+)<\/span>/g)]
    .map((m) => [m[1], m[2]]);
}

describe("render-utils document attachment chips", () => {
  it("isDocumentAttachmentPath accepts every allowlisted document suffix and rejects images", () => {
    for (const p of ["a.pdf", "a.csv", "a.txt", "a.md", "a.doc", "a.DOCX", "a.xls", "a.xlsx"]) {
      assert.equal(isDocumentAttachmentPath(`attachments/20260904_${p}`), true, p);
    }
    for (const p of ["a.png", "a.jpeg", "a.gif", "a.webp", "a.docm", "a.xlsm", "a.exe", ""]) {
      assert.equal(isDocumentAttachmentPath(`attachments/${p}`), false, p || "(empty)");
    }
  });

  it("reloaded human turns keep Word/Excel/text attachments as chips", () => {
    const html = renderHistoryMessage(
      {
        role: "human",
        content: "資料です",
        attachments: [
          "attachments/20260904_1_議事録.docx",
          "attachments/20260904_2_売上.xlsx",
          "attachments/20260904_3_memo.md",
          "attachments/20260904_4_report.pdf",
          "attachments/20260904_5_photo.png",
        ],
      },
      opts,
    );
    assert.deepEqual(chips(html), [
      ["DOCX", "20260904_1_議事録.docx"],
      ["XLSX", "20260904_2_売上.xlsx"],
      ["MD", "20260904_3_memo.md"],
      ["PDF", "20260904_4_report.pdf"],
    ]);
    assert.doesNotMatch(html, /photo\.png/, "images are rendered by renderChatImages, not as file chips");
  });

  it("live turns with explicit files use the file names and escape them", () => {
    const html = renderHistoryMessage(
      { role: "human", content: "x", files: [{ name: "<b>evil</b>.xls", media_type: "application/vnd.ms-excel" }] },
      opts,
    );
    assert.deepEqual(chips(html), [["XLS", "&lt;b&gt;evil&lt;/b&gt;.xls"]]);
  });

  it("turns without attachments render no chip container (backward compatible)", () => {
    const html = renderHistoryMessage({ role: "human", content: "plain" }, opts);
    assert.doesNotMatch(html, /chat-attached-files/);
    const legacy = renderHistoryMessage({ role: "human", content: "old", attachments: [] }, opts);
    assert.doesNotMatch(legacy, /chat-attached-files/);
  });
});

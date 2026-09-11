# AnimaWorks - Digital Anima Framework
# Copyright (C) 2026 AnimaWorks Authors
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for thread lifecycle logic in shared/chat/thread-logic.js.

Covers: new-thread ordering (top of list, creation timestamp),
auto-archive of stale threads (1 week), and restore touching lastTs.

Runs the pure JS logic in node with the browser-only i18n import
replaced by a stub (thread-logic.js is otherwise DOM-free).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
THREAD_LOGIC_JS = REPO_ROOT / "server" / "static" / "shared" / "chat" / "thread-logic.js"

NODE_SCRIPT = """
import assert from "node:assert/strict";
import {
  createThread, autoArchiveStaleThreads, restoreThread, THREAD_AUTO_ARCHIVE_MS,
  renameThread, normalizeThreadLabel, THREAD_LABEL_MAX_LENGTH,
} from "./thread-logic.js";

const DAY = 24 * 60 * 60 * 1000;
const iso = (agoMs) => new Date(Date.now() - agoMs).toISOString();

// ── createThread: inserted right after default, with creation lastTs ──
{
  const list = [
    { id: "default", label: "main", unread: false },
    { id: "old1", label: "old", unread: false, lastTs: iso(8 * DAY) },
  ];
  const { updatedList, newThreadId } = createThread(list, "mei");
  assert.equal(updatedList[1].id, newThreadId);
  const created = updatedList.find(t => t.id === newThreadId);
  assert.ok(created.lastTs, "new thread must carry a creation timestamp");
  assert.ok(Date.parse(created.lastTs) > Date.now() - 5000);
}

// ── autoArchiveStaleThreads: >1 week idle → archived ──
{
  const list = [
    { id: "default", label: "main" },
    { id: "stale", lastTs: iso(8 * DAY) },
    { id: "fresh", lastTs: iso(1 * DAY) },
    { id: "unknown" },
  ];
  const { list: out, changed } = autoArchiveStaleThreads(list, {});
  assert.equal(changed, true);
  assert.equal(out.find(t => t.id === "stale").archived, true);
  assert.ok(!out.find(t => t.id === "fresh").archived);
  assert.ok(!out.find(t => t.id === "default").archived);
  assert.ok(!out.find(t => t.id === "unknown").archived, "no lastTs → untouched");
}

// ── active thread is never auto-archived ──
{
  const list = [{ id: "default" }, { id: "x", lastTs: iso(10 * DAY) }];
  const { changed } = autoArchiveStaleThreads(list, { activeThreadId: "x" });
  assert.equal(changed, false);
}

// ── unchanged input returns same reference (no spurious saves) ──
{
  const list = [{ id: "default" }, { id: "fresh", lastTs: iso(1 * DAY) }];
  const { list: out, changed } = autoArchiveStaleThreads(list, {});
  assert.equal(changed, false);
  assert.equal(out, list);
}

// ── restoreThread: un-archives and touches lastTs (prevents instant re-archive) ──
{
  const list = [
    { id: "default" },
    { id: "stale", archived: true, lastTs: iso(8 * DAY) },
  ];
  const out = restoreThread(list, "stale");
  const restored = out.find(t => t.id === "stale");
  assert.equal(restored.archived, false);
  assert.ok(Date.parse(restored.lastTs) > Date.now() - 5000);
  const { changed } = autoArchiveStaleThreads(out, {});
  assert.equal(changed, false, "restored thread must not be re-archived");
}

// ── sanity: threshold is 7 days ──
assert.equal(THREAD_AUTO_ARCHIVE_MS, 7 * DAY);

// ── renameThread: applies normalized label, keeps other fields ──
{
  const list = [
    { id: "default", label: "main" },
    { id: "abc", label: "Thread 03:34", unread: true, lastTs: iso(1 * DAY) },
  ];
  const out = renameThread(list, "abc", "  LEAP   設計  ");
  assert.notEqual(out, list);
  const renamed = out.find(t => t.id === "abc");
  assert.equal(renamed.label, "LEAP 設計");
  assert.equal(renamed.unread, true, "other fields preserved");
  assert.equal(renamed.lastTs, list[1].lastTs);
  assert.equal(out.find(t => t.id === "default").label, "main");
  assert.equal(list[1].label, "Thread 03:34", "input not mutated");
}

// ── renameThread: default thread is renamable too ──
{
  const list = [{ id: "default", label: "main" }];
  assert.equal(renameThread(list, "default", "Home")[0].label, "Home");
}

// ── renameThread: no-op cases return the same reference (no spurious saves) ──
{
  const list = [{ id: "default", label: "main" }, { id: "abc", label: "x" }];
  assert.equal(renameThread(list, "abc", ""), list, "empty");
  assert.equal(renameThread(list, "abc", "   \\n\\t "), list, "whitespace only");
  assert.equal(renameThread(list, "abc", "x"), list, "identical");
  assert.equal(renameThread(list, "missing", "y"), list, "unknown id");
  assert.equal(renameThread(list, "abc", null), list, "non-string");
}

// ── normalizeThreadLabel: truncation ──
{
  const long = "a".repeat(THREAD_LABEL_MAX_LENGTH + 20);
  assert.equal(normalizeThreadLabel(long).length, THREAD_LABEL_MAX_LENGTH);
  assert.equal(THREAD_LABEL_MAX_LENGTH, 60);
}

// ── normalizeThreadLabel: truncation never splits surrogate pairs (emoji) ──
{
  const input = "a".repeat(THREAD_LABEL_MAX_LENGTH - 1) + "\\u{1F600}\\u{1F600}";
  const out = normalizeThreadLabel(input);
  assert.equal(Array.from(out).length, THREAD_LABEL_MAX_LENGTH, "counted by code points");
  assert.ok(out.endsWith("\\u{1F600}"), "last emoji kept whole");
  assert.ok(!/[\\uD800-\\uDBFF]$/.test(out), "no lone high surrogate");
  assert.equal(normalizeThreadLabel("a".repeat(THREAD_LABEL_MAX_LENGTH) + "  x"), "a".repeat(THREAD_LABEL_MAX_LENGTH));
}

console.log("thread-logic lifecycle tests passed");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_thread_logic_lifecycle(tmp_path: Path) -> None:
    src = THREAD_LOGIC_JS.read_text(encoding="utf-8")
    # Stub the browser-only i18n dependency for node execution
    patched = src.replace('from "../i18n.js"', 'from "./i18n-stub.js"')
    assert patched != src, "expected thread-logic.js to import ../i18n.js"
    (tmp_path / "thread-logic.js").write_text(patched, encoding="utf-8")
    (tmp_path / "i18n-stub.js").write_text(
        "export function t(key) { return key; }\n", encoding="utf-8"
    )
    (tmp_path / "run_test.mjs").write_text(NODE_SCRIPT, encoding="utf-8")

    result = subprocess.run(
        ["node", "run_test.mjs"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node test failed:\n{result.stdout}\n{result.stderr}"

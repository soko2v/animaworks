// ── Image / File Attachment Input Module ────────────────
// Shared attachment handling for chat UIs: images and PDF files.
// Supports: Ctrl+V paste, drag & drop onto the chat area (with a drop
// overlay), and the file picker button.

import { t } from "/shared/i18n.js";
import { basePath } from "/shared/base-path.js";

const MAX_IMAGE_SIZE = 5 * 1024 * 1024; // 5MB per image
const MAX_DIMENSION = 1568; // Max pixel dimension (Anthropic recommendation)
const SUPPORTED_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);

// PDF limits — keep in sync with core/chat_attachments.py
export const MAX_PDF_SIZE = 20 * 1024 * 1024; // per file
export const MAX_PDF_FILES = 5; // per message
export const MAX_PDF_TOTAL_SIZE = 40 * 1024 * 1024; // per message
export const ATTACHMENT_ACCEPT = "image/jpeg,image/png,image/gif,image/webp,application/pdf,.pdf";

const PDF_MEDIA_TYPE = "application/pdf";
const PDF_TYPES = new Set(["application/pdf", "application/x-pdf"]);
const GENERIC_TYPES = new Set(["", "application/octet-stream", "binary/octet-stream"]);
const STATUS_TIMEOUT_MS = 8000;
const DRAG_HIDE_DELAY_MS = 700;
const BLOB_URL_TTL_MS = 30 * 60 * 1000;

const _revokeTimers = new Map(); // blob URL -> pending revoke timer

function _revokeLater(url) {
  if (typeof url !== "string" || !url.startsWith("blob:")) return;
  clearTimeout(_revokeTimers.get(url));
  _revokeTimers.set(url, setTimeout(() => {
    _revokeTimers.delete(url);
    try { URL.revokeObjectURL(url); } catch { /* ignore */ }
  }, BLOB_URL_TTL_MS));
}

/** A blob URL is back in use (queue edit): cancel its pending revocation. */
function _keepUrl(url) {
  const timer = _revokeTimers.get(url);
  if (timer === undefined) return;
  clearTimeout(timer);
  _revokeTimers.delete(url);
}

function _esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** True when *file* is a PDF (by MIME type, or by name when the type is generic). */
export function isPdfFile(file) {
  if (!file) return false;
  const type = String(file.type || "").toLowerCase();
  if (PDF_TYPES.has(type)) return true;
  return GENERIC_TYPES.has(type) && String(file.name || "").toLowerCase().endsWith(".pdf");
}

/** Human readable size: "820 B", "12 KB", "3.4 MB". */
export function formatFileSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** True when a drag carries OS files (not text or in-page elements). */
export function hasFilePayload(dataTransfer) {
  const types = dataTransfer?.types;
  if (!types) return false;
  if (typeof types.includes === "function") return types.includes("Files");
  if (typeof types.contains === "function") return types.contains("Files");
  return Array.from(types).includes("Files");
}

// ── Window drop guard ───────────────────────────────
// A file dropped outside a chat drop zone must not make the browser
// navigate away to the PDF (losing the chat).  Drop zones call
// preventDefault() first, so handled events are left alone.

let _windowGuardInstalled = false;

export function installWindowDropGuard(target = typeof window !== "undefined" ? window : null) {
  if (_windowGuardInstalled || !target) return;
  _windowGuardInstalled = true;
  target.addEventListener("dragover", (e) => {
    if (e.defaultPrevented || !hasFilePayload(e.dataTransfer)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "none";
  });
  target.addEventListener("drop", (e) => {
    if (e.defaultPrevented || !hasFilePayload(e.dataTransfer)) return;
    e.preventDefault();
  });
}

/**
 * Create the attachment input manager for a chat container.
 *
 * @param {object} options
 * @param {HTMLElement} options.container - Chat area that accepts dropped files
 * @param {HTMLElement} options.inputArea - Chat input element (for paste events)
 * @param {HTMLElement} options.previewContainer - Element that shows pending attachments
 * @param {function(): void} [options.onImagesChanged] - Called when attachments change
 * @returns {object} Manager (see the public API at the end)
 */
export function createImageInput({ container, inputArea, previewContainer, onImagesChanged }) {
  // Pending attachments of the conversation (Anima + thread) on screen.
  // Other conversations' attachments are stashed, like their text drafts,
  // so a PDF dropped for Anima A can never be sent to Anima B.
  let pendingImages = []; // { data, media_type, dataUrl }
  let pendingFiles = []; // { id, name, size, lastModified, media_type, data, url, loading, owner }
  let conversationKey = null;
  const stashes = new Map(); // conversation key -> { images, files }
  // Images still being read: { owner } = the list each lands in.  Like PDF
  // entries, the owner follows the conversation when it is retargeted.
  const imageJobs = new Set();
  let processing = 0;
  let status = null; // { kind: "error" | "info", message }
  let statusTimer = null;
  let dragTimer = null;
  let nextFileId = 1;

  function changed() {
    renderPreviews();
    onImagesChanged?.();
  }

  function setStatus(kind, message) {
    status = { kind, message };
    clearTimeout(statusTimer);
    statusTimer = setTimeout(() => {
      status = null;
      renderPreviews();
    }, STATUS_TIMEOUT_MS);
    renderPreviews();
  }

  // ── File Processing Pipeline ──────────────────────

  function processImageFile(file) {
    const name = file.name || "image";
    if (!SUPPORTED_TYPES.has(file.type)) {
      setStatus("error", t("chat.attach_unsupported_image", { name }));
      return;
    }
    if (file.size > MAX_IMAGE_SIZE) {
      setStatus("error", t("chat.attach_image_too_large", {
        name, size: formatFileSize(file.size), max: formatFileSize(MAX_IMAGE_SIZE),
      }));
      return;
    }

    processing += 1;
    const job = { owner: pendingImages }; // the conversation this image was added to
    imageJobs.add(job);
    const img = new Image();
    const objectUrl = URL.createObjectURL(file);
    img.onload = () => {
      processing -= 1;
      imageJobs.delete(job);
      const canvas = document.createElement("canvas");
      let { width, height } = img;

      // Resize if needed (preserve aspect ratio)
      if (width > MAX_DIMENSION || height > MAX_DIMENSION) {
        if (width > height) {
          height = Math.round(height * (MAX_DIMENSION / width));
          width = MAX_DIMENSION;
        } else {
          width = Math.round(width * (MAX_DIMENSION / height));
          height = MAX_DIMENSION;
        }
      }

      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, width, height);

      // Convert to base64
      const outputType = file.type === "image/png" ? "image/png" : "image/jpeg";
      const quality = outputType === "image/jpeg" ? 0.85 : undefined;
      const dataUrl = canvas.toDataURL(outputType, quality);
      const base64Data = dataUrl.split(",")[1];

      job.owner.push({
        data: base64Data,
        media_type: outputType,
        dataUrl, // Keep for preview display
      });

      URL.revokeObjectURL(objectUrl);
      if (job.owner === pendingImages) changed();
    };
    img.onerror = () => {
      processing -= 1;
      imageJobs.delete(job);
      URL.revokeObjectURL(objectUrl);
      if (job.owner === pendingImages) setStatus("error", t("chat.attach_read_failed", { name }));
    };
    img.src = objectUrl;
  }

  function pdfTotalSize() {
    return pendingFiles.reduce((sum, f) => sum + (f.size || 0), 0);
  }

  function removeFileEntry(entry) {
    entry.removed = true;
    const list = entry.owner || pendingFiles;
    const idx = list.indexOf(entry);
    if (idx >= 0) list.splice(idx, 1);
    if (entry.url && entry.url.startsWith("blob:")) {
      try { URL.revokeObjectURL(entry.url); } catch { /* ignore */ }
    }
  }

  function processPdfFile(file) {
    const name = file.name || "document.pdf";
    if (pendingFiles.some(f => f.name === name && f.size === file.size && f.lastModified === file.lastModified)) {
      setStatus("info", t("chat.attach_duplicate", { name }));
      return;
    }
    if (pendingFiles.length >= MAX_PDF_FILES) {
      setStatus("error", t("chat.attach_too_many_pdfs", { max: MAX_PDF_FILES }));
      return;
    }
    if (file.size > MAX_PDF_SIZE) {
      setStatus("error", t("chat.attach_pdf_too_large", {
        name, size: formatFileSize(file.size), max: formatFileSize(MAX_PDF_SIZE),
      }));
      return;
    }
    if (!file.size) {
      setStatus("error", t("chat.attach_read_failed", { name }));
      return;
    }
    if (pdfTotalSize() + file.size > MAX_PDF_TOTAL_SIZE) {
      setStatus("error", t("chat.attach_pdf_total_too_large", { max: formatFileSize(MAX_PDF_TOTAL_SIZE) }));
      return;
    }

    const entry = {
      id: nextFileId++,
      name,
      size: file.size,
      lastModified: file.lastModified || 0,
      media_type: PDF_MEDIA_TYPE,
      data: null,
      url: "",
      loading: true,
    };
    try { entry.url = URL.createObjectURL(file); } catch { entry.url = ""; }
    entry.owner = pendingFiles;
    pendingFiles.push(entry);
    processing += 1;
    changed();

    const reader = new FileReader();
    reader.onload = () => {
      processing -= 1;
      if (entry.removed) return; // removed while loading
      const onScreen = entry.owner === pendingFiles;
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      entry.data = comma >= 0 ? result.slice(comma + 1) : "";
      entry.loading = false;
      if (!entry.data) {
        removeFileEntry(entry);
        if (onScreen) setStatus("error", t("chat.attach_read_failed", { name }));
        return;
      }
      if (onScreen) changed();
    };
    reader.onerror = () => {
      processing -= 1;
      const onScreen = entry.owner === pendingFiles;
      removeFileEntry(entry);
      if (onScreen) setStatus("error", t("chat.attach_read_failed", { name }));
    };
    reader.readAsDataURL(file);
  }

  function addFiles(files) {
    for (const file of Array.from(files || [])) {
      if (!file) continue;
      if (isPdfFile(file)) {
        processPdfFile(file);
      } else if (String(file.type || "").startsWith("image/")) {
        processImageFile(file);
      } else {
        setStatus("error", t("chat.attach_unsupported", { name: file.name || "?" }));
      }
    }
  }

  // ── Preview Rendering ─────────────────────────────

  function renderPreviews() {
    if (!previewContainer) return;

    if (pendingImages.length === 0 && pendingFiles.length === 0 && !status) {
      previewContainer.style.display = "none";
      previewContainer.innerHTML = "";
      previewContainer.classList.remove("has-status");
      return;
    }

    previewContainer.style.display = "flex";
    previewContainer.classList.toggle("has-status", Boolean(status));
    const removeLabel = _esc(t("chat.attach_remove"));
    let html = pendingImages.map((img, i) => `
      <div class="image-preview-item" data-index="${i}">
        <img src="${img.dataUrl}" alt="Preview ${i + 1}" />
        <button type="button" class="image-preview-remove" data-kind="image" data-index="${i}" title="${removeLabel}" aria-label="${removeLabel}">&times;</button>
      </div>
    `).join("");
    html += pendingFiles.map((f, i) => `
      <div class="file-preview-item${f.loading ? " is-loading" : ""}" data-index="${i}" title="${_esc(f.name)}">
        <span class="file-preview-icon" aria-hidden="true">PDF</span>
        <span class="file-preview-text">
          <span class="file-preview-name">${_esc(f.name)}</span>
          <span class="file-preview-size">${_esc(f.loading ? t("chat.attach_loading") : formatFileSize(f.size))}</span>
        </span>
        <button type="button" class="file-preview-remove" data-kind="file" data-index="${i}" title="${removeLabel}" aria-label="${removeLabel}: ${_esc(f.name)}">&times;</button>
      </div>
    `).join("");
    if (status) {
      const role = status.kind === "error" ? "alert" : "status";
      html += `<div class="attachment-status attachment-status--${_esc(status.kind)}" role="${role}">${_esc(status.message)}</div>`;
    }
    previewContainer.innerHTML = html;

    // Bind remove buttons
    previewContainer.querySelectorAll(".image-preview-remove, .file-preview-remove").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const idx = parseInt(btn.dataset.index, 10);
        if (btn.dataset.kind === "file") {
          const entry = pendingFiles[idx];
          if (entry) removeFileEntry(entry);
        } else {
          pendingImages.splice(idx, 1);
        }
        changed();
      });
    });
  }

  // ── Event Listeners ───────────────────────────────

  // Ctrl+V paste (images, or PDF files copied in the OS file manager)
  inputArea?.addEventListener("paste", (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind && item.kind !== "file") continue;
      if (!String(item.type || "").startsWith("image/") && !PDF_TYPES.has(item.type)) continue;
      const file = item.getAsFile();
      if (file) files.push(file);
    }
    if (files.length > 0) {
      e.preventDefault();
      addFiles(files);
    }
  });

  // Drag & drop on the chat area
  function showDropOverlay() {
    container.classList.add("image-drag-over");
    clearTimeout(dragTimer);
    // dragover repeats while hovering; hide shortly after it stops
    // (Safari reports no relatedTarget on dragleave).
    dragTimer = setTimeout(hideDropOverlay, DRAG_HIDE_DELAY_MS);
  }

  function hideDropOverlay() {
    clearTimeout(dragTimer);
    container.classList.remove("image-drag-over");
  }

  if (container) {
    container.dataset.dropLabel = t("chat.attach_drop_here");

    const onDragOver = (e) => {
      if (!hasFilePayload(e.dataTransfer)) return; // text drags keep native behaviour
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      showDropOverlay();
    };
    container.addEventListener("dragenter", onDragOver);
    container.addEventListener("dragover", onDragOver);

    container.addEventListener("dragleave", (e) => {
      if (e.relatedTarget && !container.contains(e.relatedTarget)) hideDropOverlay();
    });

    container.addEventListener("drop", (e) => {
      if (!hasFilePayload(e.dataTransfer)) return;
      e.preventDefault();
      hideDropOverlay();
      addFiles(e.dataTransfer.files);
    });
  }

  installWindowDropGuard();

  // ── Public API ────────────────────────────────────

  return {
    /** Get pending images for sending (without dataUrl preview field). */
    getPendingImages() {
      return pendingImages.map(({ data, media_type }) => ({ data, media_type }));
    },

    /** Get pending images with dataUrl for display in chat history. */
    getDisplayImages() {
      return pendingImages.map(({ data, media_type, dataUrl }) => ({ data, media_type, dataUrl }));
    },

    /** Get loaded PDF files for sending: [{ data, media_type, name }]. */
    getPendingFiles() {
      return pendingFiles.filter(f => f.data).map(({ data, media_type, name }) => ({ data, media_type, name }));
    },

    /** Get loaded PDF files for the chat bubble: [{ name, size, media_type, url }]. */
    getDisplayFiles() {
      return pendingFiles.filter(f => f.data).map(({ name, size, media_type, url }) => ({ name, size, media_type, url }));
    },

    /** Clear all pending attachments (after they were queued or sent). */
    clearImages() {
      // The sent/queued bubble keeps linking to the local copy for a while;
      // by then history shows the server link, so release the blob later.
      for (const f of pendingFiles) _revokeLater(f.url);
      pendingImages.length = 0;
      pendingFiles.length = 0;
      status = null;
      clearTimeout(statusTimer);
      renderPreviews();
    },

    /** Get current image count. */
    getImageCount() {
      return pendingImages.length;
    },

    /** Get current PDF file count. */
    getFileCount() {
      return pendingFiles.length;
    },

    /** Images + files waiting to be sent. */
    getAttachmentCount() {
      return pendingImages.length + pendingFiles.length;
    },

    /** True while a dropped/selected file is still being read. */
    isBusy() {
      return processing > 0;
    },

    /** Programmatically add files (file picker button / drop). */
    addFiles(files) {
      addFiles(files);
    },

    /** Put attachments of a queued message back into the input (queue edit). */
    restoreAttachments({ images = [], displayImages = [], files = [], displayFiles = [] } = {}) {
      images.forEach((img, i) => {
        if (!img?.data) return;
        const display = displayImages[i] || {};
        pendingImages.push({
          data: img.data,
          media_type: img.media_type,
          dataUrl: display.dataUrl || `data:${img.media_type};base64,${img.data}`,
        });
      });
      files.forEach((file, i) => {
        if (!file?.data) return;
        const display = displayFiles[i] || {};
        _keepUrl(display.url);
        pendingFiles.push({
          id: nextFileId++,
          name: file.name || display.name || "document.pdf",
          size: display.size || Math.floor((file.data.length * 3) / 4),
          lastModified: 0,
          media_type: file.media_type || PDF_MEDIA_TYPE,
          data: file.data,
          url: display.url || "",
          loading: false,
          owner: pendingFiles,
        });
      });
      changed();
    },

    /** Show a message in the attachment preview area. */
    showStatus(kind, message) {
      setStatus(kind, message);
    },

    /**
     * The conversation on screen changed (Anima and/or thread): stash the
     * current attachments under the previous key and bring back the ones
     * of *key* (if any), like the per-conversation text drafts.
     */
    switchConversation(key) {
      if (key === conversationKey) return;
      // Also keep (still empty) lists that a picture being read will land in.
      if (pendingImages.length || pendingFiles.length || processing > 0) {
        stashes.set(conversationKey, { images: pendingImages, files: pendingFiles });
      }
      const next = stashes.get(key);
      stashes.delete(key);
      pendingImages = next ? next.images : [];
      pendingFiles = next ? next.files : [];
      conversationKey = key;
      status = null;
      clearTimeout(statusTimer);
      changed();
    },

    /**
     * The composer moved to another conversation of the same Anima while
     * its text stays in the input (new thread, closed thread): the pending
     * attachments stay too and now belong to *key*.  Anything stashed for
     * *key* earlier is added, so nothing is lost.
     */
    retargetConversation(key) {
      if (key === conversationKey) return;
      const stashed = stashes.get(key);
      stashes.delete(key);
      if (stashed) {
        for (const job of imageJobs) if (job.owner === stashed.images) job.owner = pendingImages;
        pendingImages.push(...stashed.images);
        for (const entry of stashed.files) entry.owner = pendingFiles;
        pendingFiles.push(...stashed.files);
      }
      conversationKey = key;
      if (stashed) changed();
    },
  };
}

// ── Lightbox ────────────────────────────────────────

/** Open a lightbox when clicking a chat image. Attach once to document. */
let _lightboxInitialized = false;

export function initLightbox() {
  if (_lightboxInitialized) return;
  _lightboxInitialized = true;

  document.addEventListener("click", (e) => {
    const img = e.target.closest(".chat-attached-image");
    if (!img) return;

    const overlay = document.createElement("div");
    overlay.className = "image-lightbox";
    overlay.innerHTML = `<img src="${img.src}" />`;
    overlay.addEventListener("click", () => overlay.remove());
    document.body.appendChild(overlay);
  });
}

// ── Helper: Render images HTML for a chat bubble ────

function _resolveArtifactSrc(img, animaName) {
  if (!img) return "";
  if (img.dataUrl) return img.dataUrl;
  if (img.data && img.media_type) {
    return `data:${img.media_type};base64,${img.data}`;
  }
  if (img.path && animaName) {
    if (img.path.startsWith("assets/")) {
      const filename = img.path.slice("assets/".length);
      return `${basePath}/api/animas/${encodeURIComponent(animaName)}/assets/${encodeURIComponent(filename)}`;
    }
    if (img.path.startsWith("attachments/")) {
      const filename = img.path.slice("attachments/".length);
      return `${basePath}/api/animas/${encodeURIComponent(animaName)}/attachments/${encodeURIComponent(filename)}`;
    }
  }
  if (img.url) {
    return `${basePath}/api/media/proxy?url=${encodeURIComponent(img.url)}`;
  }
  return "";
}

/**
 * Build HTML string for images inside a chat bubble.
 * @param {Array} images - Array of image-like objects
 * @param {Object} [options]
 * @param {string} [options.animaName] - Required for assets/attachments paths
 * @returns {string} HTML string (empty if no images)
 */
export function renderChatImages(images, options = {}) {
  if (!images || images.length === 0) return "";
  const animaName = options.animaName || "";
  let html = '<div class="chat-images">';
  let count = 0;
  for (const img of images) {
    const src = _resolveArtifactSrc(img, animaName);
    if (!src) continue;
    count += 1;
    html += `<img src="${src}" class="chat-attached-image" loading="lazy" alt="Attached image" onerror="this.onerror=null;this.classList.add('chat-attached-image-error');this.alt='Image unavailable';" />`;
  }
  html += '</div>';
  return count > 0 ? html : "";
}

// ── Helper: Render file (PDF) chips for a chat bubble ──

function _resolveFileHref(file, animaName) {
  if (!file) return "";
  if (typeof file.url === "string" && file.url.startsWith("blob:")) return file.url;
  if (typeof file.path === "string" && animaName && file.path.startsWith("attachments/")) {
    const filename = file.path.slice("attachments/".length);
    if (!filename || filename.includes("/")) return "";
    return `${basePath}/api/animas/${encodeURIComponent(animaName)}/attachments/${encodeURIComponent(filename)}`;
  }
  return "";
}

/**
 * Build HTML for attached files (PDF chips that open the file in a new tab).
 * @param {Array} files - [{ name, size, pages, path | url }]
 * @param {Object} [options]
 * @param {string} [options.animaName] - Required for attachments/ paths
 * @returns {string} HTML string (empty if nothing to show)
 */
export function renderChatFiles(files, options = {}) {
  if (!Array.isArray(files) || files.length === 0) return "";
  const animaName = options.animaName || "";
  let html = "";
  for (const file of files) {
    if (!file) continue;
    const name = file.name || (typeof file.path === "string" ? file.path.split("/").pop() : "") || "document.pdf";
    const href = _resolveFileHref(file, animaName);
    const parts = [];
    if (Number(file.pages) > 0) parts.push(t("chat.attach_pages", { pages: Number(file.pages) }));
    if (Number(file.size) > 0) parts.push(formatFileSize(file.size));
    const meta = parts.length ? `<span class="chat-file-meta">${_esc(parts.join(" · "))}</span>` : "";
    const inner = `<span class="chat-file-icon" aria-hidden="true">PDF</span><span class="chat-file-text"><span class="chat-file-name">${_esc(name)}</span>${meta}</span>`;
    html += href
      ? `<a class="chat-file-chip" href="${_esc(href)}" target="_blank" rel="noopener noreferrer" title="${_esc(name)}">${inner}</a>`
      : `<span class="chat-file-chip" title="${_esc(name)}">${inner}</span>`;
  }
  return html ? `<div class="chat-files">${html}</div>` : "";
}

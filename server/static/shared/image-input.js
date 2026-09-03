// ── Image Input Module ──────────────────────────────────
// Shared image input handling for chat UIs.
// Supports: Ctrl+V paste, drag & drop, file picker button.

import { t } from "/shared/i18n.js";
import { basePath } from "/shared/base-path.js";
import { createLogger } from "/shared/logger.js";

const logger = createLogger("image-input");

const MAX_IMAGE_SIZE = 5 * 1024 * 1024; // 5MB per image
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB per document
const MAX_FILE_COUNT = 10; // documents per message (server enforces the same limit)
const MAX_DIMENSION = 1568; // Max pixel dimension (Anthropic recommendation)
const SUPPORTED_TYPES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
const HEIC_TYPES = new Set(["image/heic", "image/heif"]);
// Extension -> canonical media type sent to the server. The server re-validates
// bytes, so the browser-declared type is never trusted; we always send this one.
const DOCUMENT_TYPES = new Map([
  ["pdf", "application/pdf"],
  ["csv", "text/csv"],
  ["txt", "text/plain"],
  ["md", "text/markdown"],
  ["docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
  ["doc", "application/msword"],
  ["xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
  ["xls", "application/vnd.ms-excel"],
]);
const DOCUMENT_LABELS = new Map([
  ["pdf", "PDF"], ["csv", "CSV"], ["txt", "TXT"], ["md", "MD"],
  ["docx", "DOCX"], ["doc", "DOC"], ["xlsx", "XLSX"], ["xls", "XLS"],
]);
const TYPE_BY_EXTENSION = new Map([
  ["jpg", "image/jpeg"], ["jpeg", "image/jpeg"], ["png", "image/png"],
  ["gif", "image/gif"], ["webp", "image/webp"], ["heic", "image/heic"],
  ["heif", "image/heif"],
]);

function fileExtension(name) {
  const value = String(name || "");
  const dot = value.lastIndexOf(".");
  return dot === -1 ? "" : value.slice(dot + 1).toLowerCase();
}

function resolvedFileType(file) {
  const declared = (file?.type || "").toLowerCase();
  if (declared) return declared;
  return TYPE_BY_EXTENSION.get(fileExtension(file?.name)) || "";
}

/** Stable identity used to prevent the same file from being attached twice. */
function fileIdentity(file) {
  return `${file?.name || ""}|${file?.size ?? ""}|${file?.lastModified ?? ""}`;
}

/** Return the short label ("PDF", "DOCX", ...) for a document file name. */
export function documentLabelFor(name) {
  return DOCUMENT_LABELS.get(fileExtension(name)) || "FILE";
}

/** True when a drag/drop event carries OS files (not text/links). */
export function hasFilePayload(event) {
  const dt = event?.dataTransfer;
  if (!dt) return false;
  if (dt.files && dt.files.length > 0) return true;
  return Array.from(dt.types || []).includes("Files");
}

// The browser default for a file dropped outside a drop zone is to navigate
// away and display the file, losing the chat. Block that once per page; the
// managers below still receive drops inside their container.
let _windowDropGuardInstalled = false;

export function installWindowDropGuard(target = globalThis.window) {
  if (_windowDropGuardInstalled || typeof target?.addEventListener !== "function") return false;
  _windowDropGuardInstalled = true;
  const block = (event) => {
    if (hasFilePayload(event)) event.preventDefault();
  };
  target.addEventListener("dragover", block);
  target.addEventListener("drop", block);
  return true;
}

/** Test hook: allow re-installing the guard on a fresh window object. */
export function _resetWindowDropGuardForTests() {
  _windowDropGuardInstalled = false;
}

/**
 * Create image input manager for a chat container.
 *
 * @param {object} options
 * @param {HTMLElement} options.container - Parent container for drop events
 * @param {HTMLElement} options.inputArea - Chat input area element (for paste events)
 * @param {HTMLElement} options.previewContainer - Element to render thumbnails in
 * @param {function(): void} [options.onImagesChanged] - Callback when images array changes
 * @returns {object} Manager with getPendingImages(), clearImages(), getImageCount(), addFiles()
 */
export function createImageInput({ container, inputArea, previewContainer, onImagesChanged }) {
  const pendingImages = []; // Array of { data: base64String, media_type: string, dataUrl: string }
  const pendingFiles = []; // Array of { data: base64String, media_type: string, name: string, key: string }
  const queuedIdentities = new Set(); // fileIdentity() of every attached or in-flight file
  let pendingDocumentReads = 0;
  let processingCount = 0;
  let status = null;
  let rejectedSinceLastSubmit = false;
  // A rejection inside a batch must stay visible even after the accepted files
  // in the same batch finish reading; it is cleared when the next batch starts.
  let stickyError = null;

  function setStatus(kind, message) {
    status = message ? { kind, message } : null;
    if (kind === "error") {
      rejectedSinceLastSubmit = true;
      stickyError = message || null;
    }
    renderPreviews();
  }

  function successStatus(message) {
    return stickyError ? { kind: "error", message: stickyError } : { kind: "success", message };
  }

  function renderedPreviewCount() {
    return previewContainer?.querySelectorAll?.(".image-preview-item")?.length || 0;
  }

  // ── File Processing Pipeline ──────────────────────

  function releaseIdentity(identity) {
    if (identity) queuedIdentities.delete(identity);
  }

  function processImageFile(file, identity = "") {
    if (!file) return;
    const inputType = resolvedFileType(file);
    const isHeic = HEIC_TYPES.has(inputType);
    if (!SUPPORTED_TYPES.has(inputType) && !isHeic) {
      setStatus("error", t("chat.image_unsupported_client"));
      return;
    }
    if (file.size > MAX_IMAGE_SIZE) {
      setStatus("error", t("chat.image_too_large_client", {
        size: (file.size / 1024 / 1024).toFixed(1),
      }));
      return;
    }

    if (identity) queuedIdentities.add(identity);
    processingCount += 1;
    setStatus("info", isHeic ? t("chat.image_converting_heic") : t("chat.image_processing"));
    const img = new Image();
    img.onload = () => {
      try {
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
        if (!ctx) throw new Error("Canvas 2D context is unavailable");
        ctx.drawImage(img, 0, 0, width, height);

        // HEIC/HEIF and animated formats are normalized to JPEG. PNG remains lossless.
        const outputType = inputType === "image/png" ? "image/png" : "image/jpeg";
        const quality = outputType === "image/jpeg" ? 0.85 : undefined;
        const dataUrl = canvas.toDataURL(outputType, quality);
        const base64Data = dataUrl.split(",")[1];
        const outputSize = Math.floor(base64Data.length * 3 / 4);
        if (outputSize > MAX_IMAGE_SIZE) {
          throw new Error(t("chat.image_converted_too_large"));
        }

        pendingImages.push({
          data: base64Data,
          media_type: outputType,
          dataUrl, // Keep for preview display
          key: identity,
        });

        logger.info("[IMAGE-SEND] image ready", {
          image_count: pendingImages.length,
          media_type: outputType,
          base64_chars: base64Data.length,
        });

        rejectedSinceLastSubmit = false;
        status = successStatus(t("chat.image_ready", { count: pendingImages.length }));
        onImagesChanged?.();
      } catch (error) {
        releaseIdentity(identity);
        setStatus("error", error?.message || t("chat.image_decode_failed"));
      } finally {
        processingCount -= 1;
        URL.revokeObjectURL(img.src);
        renderPreviews();
      }
    };
    img.onerror = () => {
      releaseIdentity(identity);
      processingCount -= 1;
      URL.revokeObjectURL(img.src);
      setStatus("error", isHeic ? t("chat.image_heic_conversion_failed") : t("chat.image_decode_failed"));
    };
    img.src = URL.createObjectURL(file);
  }

  // Shared entry point for the file picker button and drag & drop, so both
  // paths get identical validation, de-duplication and limits.
  function processImageFiles(files) {
    stickyError = null;
    for (const file of Array.from(files || [])) {
      if (!file) continue;
      const identity = fileIdentity(file);
      if (queuedIdentities.has(identity)) {
        setStatus("info", t("chat.file_duplicate_client", { name: file.name || "" }));
        continue;
      }
      const extension = fileExtension(file.name);
      if (DOCUMENT_TYPES.has(extension)) {
        processDocumentFile(file, extension, identity);
      } else if (resolvedFileType(file).startsWith("image/")) {
        processImageFile(file, identity);
      } else {
        setStatus("error", t("chat.file_unsupported_client", { name: file.name || "" }));
      }
    }
  }

  function processDocumentFile(file, extension, identity = "") {
    if (file.size > MAX_FILE_SIZE) {
      setStatus("error", t("chat.file_too_large_client", {
        size: (file.size / 1024 / 1024).toFixed(1),
      }));
      return;
    }
    if (pendingFiles.length + pendingDocumentReads >= MAX_FILE_COUNT) {
      setStatus("error", t("chat.file_count_limit_client", { max: MAX_FILE_COUNT, name: file.name || "" }));
      return;
    }
    const mediaType = DOCUMENT_TYPES.get(extension);
    if (identity) queuedIdentities.add(identity);
    pendingDocumentReads += 1;
    processingCount += 1;
    setStatus("info", t("chat.file_processing"));
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const result = String(reader.result || "");
        const base64Data = result.split(",")[1];
        if (!base64Data) throw new Error(t("chat.file_read_failed"));
        pendingFiles.push({ data: base64Data, media_type: mediaType, name: file.name, key: identity });
        rejectedSinceLastSubmit = false;
        status = successStatus(t("chat.file_ready", { count: pendingFiles.length }));
        onImagesChanged?.();
      } catch (error) {
        releaseIdentity(identity);
        setStatus("error", error?.message || t("chat.file_read_failed"));
      } finally {
        pendingDocumentReads -= 1;
        processingCount -= 1;
        renderPreviews();
      }
    };
    reader.onerror = () => {
      releaseIdentity(identity);
      pendingDocumentReads -= 1;
      processingCount -= 1;
      setStatus("error", t("chat.file_read_failed"));
    };
    reader.readAsDataURL(file);
  }

  function removeImageAt(index) {
    const [removed] = pendingImages.splice(index, 1);
    if (!removed) return false;
    releaseIdentity(removed.key);
    renderPreviews();
    onImagesChanged?.();
    return true;
  }

  function removeFileAt(index) {
    const [removed] = pendingFiles.splice(index, 1);
    if (!removed) return false;
    releaseIdentity(removed.key);
    renderPreviews();
    onImagesChanged?.();
    return true;
  }

  // ── Preview Rendering ─────────────────────────────

  function renderPreviews() {
    if (!previewContainer) return;

    if (pendingImages.length === 0 && pendingFiles.length === 0 && !status && processingCount === 0) {
      previewContainer.style.display = "none";
      previewContainer.innerHTML = "";
      return;
    }

    previewContainer.style.display = "flex";
    const previews = pendingImages.map((img, i) => `
      <div class="image-preview-item" data-index="${i}">
        <img src="${img.dataUrl}" alt="Preview ${i + 1}" />
        <button class="image-preview-remove" data-index="${i}" title="${t("assets.delete")}">&times;</button>
      </div>
    `).join("");
    const filePreviews = pendingFiles.map((file, i) => `
      <div class="file-preview-item">
        <span class="file-preview-icon" aria-hidden="true">${documentLabelFor(file.name)}</span>
        <span class="file-preview-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
        <button class="file-preview-remove" data-index="${i}" title="${t("assets.delete")}">&times;</button>
      </div>
    `).join("");
    const statusHtml = status
      ? `<div class="image-input-status image-input-status-${status.kind}" role="${status.kind === "error" ? "alert" : "status"}">${escapeHtml(status.message)}</div>`
      : "";
    previewContainer.innerHTML = previews + filePreviews + statusHtml;

    // Bind remove buttons
    previewContainer.querySelectorAll(".image-preview-remove").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        removeImageAt(parseInt(btn.dataset.index, 10));
      });
    });
    previewContainer.querySelectorAll(".file-preview-remove").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        removeFileAt(parseInt(btn.dataset.index, 10));
      });
    });
  }

  // ── Event Listeners ───────────────────────────────

  // Ctrl+V paste
  inputArea.addEventListener("paste", (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith("image/")) {
        e.preventDefault();
        processImageFile(item.getAsFile());
      }
    }
  });

  // Drag & drop on container. Only OS file drags are intercepted so plain
  // text drags into the textarea keep their native behaviour.
  installWindowDropGuard();

  container.addEventListener("dragenter", (e) => {
    if (!hasFilePayload(e)) return;
    e.preventDefault();
    container.classList.add("image-drag-over");
  });

  container.addEventListener("dragover", (e) => {
    if (!hasFilePayload(e)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    container.classList.add("image-drag-over");
  });

  container.addEventListener("dragleave", (e) => {
    // Only remove class when leaving the container entirely
    if (!container.contains(e.relatedTarget)) {
      container.classList.remove("image-drag-over");
    }
  });

  container.addEventListener("drop", (e) => {
    if (!hasFilePayload(e)) return;
    e.preventDefault();
    container.classList.remove("image-drag-over");
    processImageFiles(e.dataTransfer?.files || []);
  });

  // ── Public API ────────────────────────────────────

  return {
    /** Get pending images for sending (without dataUrl preview field). */
    getPendingImages() {
      const images = pendingImages.map(({ data, media_type }) => ({ data, media_type }));
      logger.info("[IMAGE-SEND] manager snapshot", {
        image_count: images.length,
        rendered_preview_count: renderedPreviewCount(),
        processing_count: processingCount,
      });
      return images;
    },

    /** Get pending images with dataUrl for display in chat history. */
    getDisplayImages() {
      return pendingImages.map(({ data, media_type, dataUrl, key }) => ({ data, media_type, dataUrl, key }));
    },

    getPendingFiles() {
      return pendingFiles.map(({ data, media_type, name }) => ({ data, media_type, name }));
    },

    getDisplayFiles() {
      return pendingFiles.map(({ media_type, name, key }) => ({ media_type, name, key }));
    },

    /**
     * Re-attach a queued entry ({images, displayImages, files, displayFiles})
     * so editing a queued message restores its attachments, not only its text.
     * Identities are taken from the display snapshots so the original file
     * cannot be attached a second time; entries already present are skipped.
     * Returns the number of attachments restored.
     */
    restoreAttachments(entry) {
      const images = Array.isArray(entry?.images) ? entry.images : [];
      const displayImages = Array.isArray(entry?.displayImages) ? entry.displayImages : [];
      const files = Array.isArray(entry?.files) ? entry.files : [];
      const displayFiles = Array.isArray(entry?.displayFiles) ? entry.displayFiles : [];
      let restored = 0;
      images.forEach((img, index) => {
        if (!img?.data || !img?.media_type) return;
        const shown = displayImages[index] || {};
        const key = shown.key || `restored|image|${img.media_type}|${img.data.length}`;
        if (queuedIdentities.has(key)) return;
        queuedIdentities.add(key);
        pendingImages.push({
          data: img.data,
          media_type: img.media_type,
          dataUrl: shown.dataUrl || `data:${img.media_type};base64,${img.data}`,
          key,
        });
        restored += 1;
      });
      files.forEach((file, index) => {
        if (!file?.data || !file?.name) return;
        if (pendingFiles.length >= MAX_FILE_COUNT) {
          setStatus("error", t("chat.file_count_limit_client", { max: MAX_FILE_COUNT, name: file.name }));
          return;
        }
        const shown = displayFiles[index] || {};
        const key = shown.key || `restored|file|${file.name}|${file.data.length}`;
        if (queuedIdentities.has(key)) return;
        queuedIdentities.add(key);
        pendingFiles.push({ data: file.data, media_type: file.media_type || "", name: file.name, key });
        restored += 1;
      });
      if (restored > 0) {
        rejectedSinceLastSubmit = false;
        onImagesChanged?.();
      }
      renderPreviews();
      return restored;
    },

    /** Clear all pending images and documents. */
    clearImages() {
      pendingImages.length = 0;
      pendingFiles.length = 0;
      queuedIdentities.clear();
      status = null;
      stickyError = null;
      rejectedSinceLastSubmit = false;
      renderPreviews();
    },

    /** Remove one pending image by index (same path as the preview "x" button). */
    removeImage(index) {
      return removeImageAt(index);
    },

    /** Remove one pending document by index (same path as the preview "x" button). */
    removeFile(index) {
      return removeFileAt(index);
    },

    /** Current status line shown under the previews ({kind, message} or null). */
    getStatus() {
      return status ? { ...status } : null;
    },

    /** Get current image count. */
    getImageCount() {
      return pendingImages.length;
    },

    getFileCount() {
      return pendingFiles.length;
    },

    showError(message) {
      setStatus("error", message);
    },

    /** True while selected files are still being decoded or converted. */
    isProcessing() {
      return processingCount > 0;
    },

    /** Block sending while processing, or once after every selected image was rejected. */
    prepareForSubmit() {
      logger.info("[IMAGE-SEND] prepare submit", {
        image_count: pendingImages.length,
        rendered_preview_count: renderedPreviewCount(),
        processing_count: processingCount,
      });
      if (processingCount > 0) {
        setStatus("info", t("chat.image_wait_for_processing"));
        return false;
      }
      if (rejectedSinceLastSubmit && pendingImages.length === 0 && pendingFiles.length === 0) {
        rejectedSinceLastSubmit = false;
        setStatus("error", t("chat.image_send_without_attachment"));
        return false;
      }
      // Never allow a visible thumbnail to degrade silently into a text-only
      // message. This also catches stale/duplicate manager wiring in the UI.
      if (renderedPreviewCount() > pendingImages.length) {
        setStatus("error", t("chat.image_send_without_attachment"));
        return false;
      }
      return true;
    },

    /** Programmatically add files (for file picker button). */
    addFiles(files) {
      processImageFiles(files);
    },
  };
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value || "";
  return div.innerHTML;
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

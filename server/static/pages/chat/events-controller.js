// ── Event Binding Controller ──────────────────
import { saveDraft, chatInputMaxHeight } from "./ctx.js";

function _positionDropdown(menu, trigger, { align = "right" } = {}) {
  const rect = trigger.getBoundingClientRect();
  const gap = 5;
  menu.style.position = "fixed";
  menu.style.top = `${rect.bottom + gap}px`;
  menu.style.bottom = "auto";

  if (align === "right") {
    menu.style.left = "auto";
    menu.style.right = `${window.innerWidth - rect.right}px`;
  } else {
    menu.style.right = "auto";
    menu.style.left = `${rect.left}px`;
  }

  requestAnimationFrame(() => {
    const mRect = menu.getBoundingClientRect();
    if (mRect.right > window.innerWidth - 8) {
      menu.style.left = "auto";
      menu.style.right = "8px";
    }
    if (mRect.left < 8) {
      menu.style.right = "auto";
      menu.style.left = "8px";
    }
    if (mRect.bottom > window.innerHeight - 8) {
      menu.style.top = "auto";
      menu.style.bottom = `${window.innerHeight - rect.top + gap}px`;
    }
  });
}

function _resetDropdownPosition(menu) {
  menu.style.position = "";
  menu.style.top = "";
  menu.style.bottom = "";
  menu.style.left = "";
  menu.style.right = "";
}

export function createEventsController(ctx) {
  const $ = ctx.$;
  const { state, deps } = ctx;
  const { t } = deps;

  function addListener(id, event, handler) {
    const el = $(id);
    if (el) {
      el.addEventListener(event, handler);
      state.boundListeners.push({ el, event, handler });
    }
  }

  function bindPaneEvents() {
    // Add conversation picker
    addListener("chatAddConversationBtn", "click", e => {
      e.stopPropagation();
      const area = $("chatAddConversationArea");
      if (!area) return;
      const nextOpen = !area.classList.contains("open");
      area.classList.toggle("open", nextOpen);
      if (nextOpen) {
        ctx.controllers.anima.renderAddConversationMenu();
        const menu = $("chatAddConversationMenu");
        const btn = $("chatAddConversationBtn");
        if (menu && btn) _positionDropdown(menu, btn);
      }
    });
    const closeMenu = e => {
      const area = $("chatAddConversationArea");
      if (!area || !area.classList.contains("open")) return;
      if (e.target instanceof Element && area.contains(e.target)) return;
      area.classList.remove("open");
      const menu = $("chatAddConversationMenu");
      if (menu) _resetDropdownPosition(menu);
    };
    document.addEventListener("pointerdown", closeMenu);
    state.boundListeners.push({ el: document, event: "pointerdown", handler: closeMenu });

    // Chat form submit
    addListener("chatPageForm", "submit", e => { e.preventDefault(); ctx.controllers.streaming.submitChat(); });

    // Focus textarea on wrap click
    const inputWrap = state.container.querySelector(".chat-input-wrap");
    const focusInput = e => {
      if (e.target instanceof Element && e.target.closest("button, input, select, textarea, a")) return;
      $("chatPageInput")?.focus();
    };
    if (inputWrap) {
      inputWrap.addEventListener("pointerdown", focusInput);
      state.boundListeners.push({ el: inputWrap, event: "pointerdown", handler: focusInput });
      inputWrap.addEventListener("click", focusInput);
      state.boundListeners.push({ el: inputWrap, event: "click", handler: focusInput });
    }

    // Textarea shortcuts
    addListener("chatPageInput", "keydown", e => {
      if (e.key === "Enter" && e.altKey) { e.preventDefault(); ctx.controllers.streaming.addToQueue(); }
      else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ctx.controllers.streaming.submitChat(); }
      else if (e.key === "Enter" && !e.shiftKey && localStorage.getItem("aw-enter-to-send") === "true") {
        e.preventDefault(); ctx.controllers.streaming.submitChat();
      }
    });

    // Queue / pending (queue button removed — auto-queue via Enter during streaming)
    addListener("chatPagePendingCancel", "click", () => {
      if (state.selectedAnima) state.manager.clearQueue(state.selectedAnima, state.selectedThreadId);
      ctx.controllers.streaming.hidePendingIndicator();
      ctx.controllers.streaming.updateSendButton();
    });

    // Auto-resize + draft save
    addListener("chatPageInput", "input", () => {
      const el = $("chatPageInput");
      if (el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, chatInputMaxHeight()) + "px";
        saveDraft(state.selectedAnima, el.value || "", state.selectedThreadId);
      }
      ctx.controllers.streaming.updateSendButton();
    });

    // Attach / file input
    addListener("chatPageAttachBtn", "click", () => { $("chatPageFileInput")?.click(); });
    addListener("chatPageFileInput", "change", () => {
      const fi = $("chatPageFileInput");
      if (fi?.files.length > 0) { state.imageInputManager?.addFiles(fi.files); fi.value = ""; }
    });

    // Split / close pane
    addListener("chatSplitPaneBtn", "click", () => { state.paneHost?.splitPane(); });
    addListener("chatClosePaneBtn", "click", () => {
      if (state.paneId != null) state.paneHost?.removePane(state.paneId);
    });

    // Image input + voice init
    ctx.controllers.imageVoice.initImageInput();

    // Infinite scroll observer + scroll-to-bottom tracking
    ctx.controllers.renderer.setupChatObserver();
    ctx.controllers.renderer.initScrollTracking();

    // Demo suggest card click (event delegation on messages container)
    addListener("chatPageMessages", "click", e => {
      const card = e.target.closest(".demo-suggest-card");
      if (!card) return;
      const prompt = card.dataset.demoPrompt;
      if (!prompt) return;
      const input = $("chatPageInput");
      if (input) {
        input.value = prompt;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      ctx.controllers.streaming.submitChat();
    });

    // Demo prompt chips click (event delegation)
    addListener("chatPromptChips", "click", e => {
      const chip = e.target.closest(".chat-prompt-chip");
      if (!chip) return;
      const prompt = chip.dataset.demoPrompt;
      if (!prompt) return;
      const input = $("chatPageInput");
      if (input) {
        input.value = prompt;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      const chipsEl = $("chatPromptChips");
      if (chipsEl) chipsEl.style.display = "none";
    });

    // ── Mobile unified header ──
    _bindUnifiedHeader(addListener);
  }

  function _bindUnifiedHeader(addListener) {
    addListener("chatUnifiedHamburger", "click", () => {
      document.body.classList.toggle("mobile-nav-open");
    });

    addListener("chatUnifiedUserBtn", "click", e => {
      e.stopPropagation();
      const menu = $("chatUnifiedUserMenu");
      const btn = $("chatUnifiedUserBtn");
      if (!menu) return;
      const nextOpen = !menu.classList.contains("open");
      menu.classList.toggle("open", nextOpen);
      if (nextOpen && btn) _positionDropdown(menu, btn);
      else _resetDropdownPosition(menu);
    });
    const closeUserMenu = e => {
      const menu = $("chatUnifiedUserMenu");
      if (!menu || !menu.classList.contains("open")) return;
      const btn = $("chatUnifiedUserBtn");
      if (btn && btn.contains(e.target)) return;
      if (menu.contains(e.target)) return;
      menu.classList.remove("open");
      _resetDropdownPosition(menu);
    };
    document.addEventListener("pointerdown", closeUserMenu);
    state.boundListeners.push({ el: document, event: "pointerdown", handler: closeUserMenu });

    addListener("chatUnifiedUserLogout", "click", () => {
      const mainLogout = document.getElementById("logoutBtn");
      if (mainLogout) mainLogout.click();
    });

    addListener("chatUnifiedInfoBtn", "click", () => ctx.controllers.sidebar.toggleRightPane());

    _populateUnifiedUser();

    addListener("chatThreadDropdownBtn", "click", e => {
      e.stopPropagation();
      const dd = $("chatThreadDropdown");
      if (dd) dd.classList.toggle("open");
      if (dd?.classList.contains("open")) {
        ctx.controllers.thread.renderThreadDropdownMenu?.();
        const menu = $("chatThreadDropdownMenu");
        const btn = $("chatThreadDropdownBtn");
        if (menu && btn) _positionDropdown(menu, btn);
      }
    });
    const closeThreadDropdown = e => {
      const dd = $("chatThreadDropdown");
      if (!dd || !dd.classList.contains("open")) return;
      if (dd.contains(e.target)) return;
      dd.classList.remove("open");
      const menu = $("chatThreadDropdownMenu");
      if (menu) _resetDropdownPosition(menu);
    };
    document.addEventListener("pointerdown", closeThreadDropdown);
    state.boundListeners.push({ el: document, event: "pointerdown", handler: closeThreadDropdown });
  }

  function _populateUnifiedUser() {
    const nameEl = $("chatUnifiedUserName");
    const statusEl = $("chatUnifiedUserStatus");
    const initialEl = $("chatUnifiedUserInitial");
    const userName = document.getElementById("currentUserLabel")?.textContent || "?";
    const statusText = document.getElementById("systemStatusText")?.textContent || "";
    if (nameEl) nameEl.textContent = userName;
    if (statusEl) statusEl.textContent = statusText;
    if (initialEl) initialEl.textContent = (userName.charAt(0) || "?").toUpperCase();
  }

  function updateDemoChips() {
    const chipsEl = $("chatPromptChips");
    if (!chipsEl) return;

    if (!state.demoMode || !state.selectedAnima) {
      chipsEl.style.display = "none";
      return;
    }

    const name = state.selectedAnima.toLowerCase();
    const prompts = [];
    for (let i = 1; i <= 4; i++) {
      const key = `demo.prompts.${name}.${i}`;
      const val = t(key);
      if (val && val !== key) prompts.push(val);
    }

    if (prompts.length === 0) {
      chipsEl.style.display = "none";
      return;
    }

    chipsEl.innerHTML = prompts.map(p =>
      `<button type="button" class="chat-prompt-chip" data-demo-prompt="${p.replace(/"/g, '&quot;')}">${p.length > 40 ? p.slice(0, 37) + "..." : p}</button>`
    ).join("");
    chipsEl.style.display = "flex";
  }

  return { bindPaneEvents, updateDemoChips };
}

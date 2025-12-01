// Lightweight keyboard shortcut listener that reuses existing background handlers.
(() => {
  let enabled = false;
  let downloadCombo = "ctrl+shift+e";
  let queueCombo = "ctrl+shift+q";

  const editableTags = new Set(["input", "textarea", "select", "option"]);

  function normalizeComboString(str) {
    if (!str || typeof str !== "string") return "";
    return str
      .toLowerCase()
      .split("+")
      .map(s => s.trim())
      .filter(Boolean)
      .join("+");
  }

  function comboFromEvent(evt) {
    const parts = [];
    if (evt.ctrlKey) parts.push("ctrl");
    if (evt.metaKey) parts.push("meta");
    if (evt.shiftKey) parts.push("shift");
    if (evt.altKey) parts.push("alt");
    const key = (evt.key || "").toLowerCase();
    if (key && !["control", "shift", "alt", "meta"].includes(key)) {
      parts.push(key);
    }
    return parts.join("+");
  }

  function isTypingTarget(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    if (editableTags.has(tag)) return true;
    return target.isContentEditable === true;
  }

  function showToast(text, bg = "#4CAF50") {
    try {
      const existing = document.getElementById("epub-shortcut-toast");
      if (existing) existing.remove();
      const el = document.createElement("div");
      el.id = "epub-shortcut-toast";
      el.textContent = text;
      el.style.cssText = `
        position: fixed;
        top: 16px;
        right: 16px;
        background: ${bg};
        color: white;
        padding: 10px 14px;
        border-radius: 4px;
        z-index: 2147483647;
        font-size: 13px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.25);
      `;
      document.body.appendChild(el);
      setTimeout(() => { el.remove(); }, 2500);
    } catch (_) {
      /* ignore toast errors */
    }
  }

  function handleKeydown(evt) {
    if (!enabled) return;
    if (isTypingTarget(evt.target)) return;
    const combo = comboFromEvent(evt);
    if (!combo) return;

    if (combo === downloadCombo) {
      evt.preventDefault();
      evt.stopPropagation();
      browser.runtime.sendMessage({
        action: "shortcut-download",
        url: window.location.href
      }).then(() => {
        showToast("Starting EPUB download…");
      }).catch((e) => {
        showToast("Download shortcut failed", "#d9534f");
        console.error("Shortcut download failed", e);
      });
    } else if (combo === queueCombo) {
      evt.preventDefault();
      evt.stopPropagation();
      browser.runtime.sendMessage({
        action: "shortcut-queue",
        url: window.location.href
      }).then(() => {
        showToast("Added to EPUB queue");
      }).catch((e) => {
        showToast("Queue shortcut failed", "#d9534f");
        console.error("Shortcut queue failed", e);
      });
    }
  }

  async function loadSettings() {
    try {
      const res = await browser.storage.local.get([
        "keyboardShortcutsEnabled",
        "keyboardShortcutDownload",
        "keyboardShortcutQueue"
      ]);
      enabled = Boolean(res.keyboardShortcutsEnabled);
      downloadCombo = normalizeComboString(res.keyboardShortcutDownload) || downloadCombo;
      queueCombo = normalizeComboString(res.keyboardShortcutQueue) || queueCombo;
    } catch (e) {
      // ignore, keep defaults
    }
  }

  function setupStorageListener() {
    browser.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      if (changes.keyboardShortcutsEnabled) {
        enabled = Boolean(changes.keyboardShortcutsEnabled.newValue);
      }
      if (changes.keyboardShortcutDownload) {
        downloadCombo =
          normalizeComboString(changes.keyboardShortcutDownload.newValue) || downloadCombo;
      }
      if (changes.keyboardShortcutQueue) {
        queueCombo = normalizeComboString(changes.keyboardShortcutQueue.newValue) || queueCombo;
      }
    });
  }

  function setupToastListener() {
    browser.runtime.onMessage.addListener((message) => {
      if (message && message.action === "shortcut-toast" && message.message) {
        showToast(message.message);
      }
    });
  }

  loadSettings();
  setupStorageListener();
  setupToastListener();
  document.addEventListener("keydown", handleKeydown, true);
})();

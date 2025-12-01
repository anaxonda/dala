const enableBox = document.getElementById("enable-shortcuts");
const downloadInput = document.getElementById("download-shortcut");
const queueInput = document.getElementById("queue-shortcut");
const subfolderInput = document.getElementById("download-subfolder");
const termuxInput = document.getElementById("download-termux");

const DEFAULT_DOWNLOAD = "ctrl+shift+e";
const DEFAULT_QUEUE = "ctrl+shift+q";

function normalizeCombo(str) {
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

function attachCapture(inputEl) {
  inputEl.addEventListener("keydown", (evt) => {
    evt.preventDefault();
    const combo = comboFromEvent(evt);
    if (combo) {
      inputEl.value = combo;
      saveSettings();
    }
  });
}

async function loadSettings() {
  const res = await browser.storage.local.get([
    "keyboardShortcutsEnabled",
    "keyboardShortcutDownload",
    "keyboardShortcutQueue",
    "savedOptions"
  ]);
  enableBox.checked = Boolean(res.keyboardShortcutsEnabled);
  downloadInput.value = normalizeCombo(res.keyboardShortcutDownload) || DEFAULT_DOWNLOAD;
  queueInput.value = normalizeCombo(res.keyboardShortcutQueue) || DEFAULT_QUEUE;
  const savedOpts = res.savedOptions || {};
  subfolderInput.value = (savedOpts.subfolder || "").trim();
  termuxInput.value = (savedOpts.termux_copy_dir || "").trim();
}

async function saveSettings() {
  const enabled = enableBox.checked;
  const dl = normalizeCombo(downloadInput.value) || DEFAULT_DOWNLOAD;
  const q = normalizeCombo(queueInput.value) || DEFAULT_QUEUE;
  const subfolder = (subfolderInput.value || "").trim();
  const termux = (termuxInput.value || "").trim();
  const res = await browser.storage.local.get("savedOptions");
  const existing = res.savedOptions || {};
  await browser.storage.local.set({
    keyboardShortcutsEnabled: enabled,
    keyboardShortcutDownload: dl,
    keyboardShortcutQueue: q,
    savedOptions: {
      ...existing,
      subfolder,
      termux_copy_dir: termux
    }
  });
}

enableBox.addEventListener("change", saveSettings);
downloadInput.addEventListener("change", saveSettings);
queueInput.addEventListener("change", saveSettings);
subfolderInput.addEventListener("change", saveSettings);
termuxInput.addEventListener("change", saveSettings);

attachCapture(downloadInput);
attachCapture(queueInput);
loadSettings();

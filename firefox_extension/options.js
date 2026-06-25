const enableBox = document.getElementById("enable-shortcuts");
const downloadInput = document.getElementById("download-shortcut");
const queueInput = document.getElementById("queue-shortcut");
const serverUrlInput = document.getElementById("opt-server-url");
const saveFolderInput = document.getElementById("opt-save-folder");
const archiveServerBox = document.getElementById("opt-archive-server");
const browserFallbackBox = document.getElementById("opt-browser-fallback");
const openChallengeUserBrowserBox = document.getElementById("opt-open-challenge-user-browser");
const browserExtensionPathInput = document.getElementById("opt-browser-extension-path");
const browserProfileDirInput = document.getElementById("opt-browser-profile-dir");
const browserExecutableInput = document.getElementById("opt-browser-executable");
const browserFallbackStatus = document.getElementById("browser-fallback-status");
const checkBrowserFallbackBtn = document.getElementById("check-browser-fallback");
const initBrowserProfileBtn = document.getElementById("init-browser-profile");
const serverDiagnostics = document.getElementById("server-diagnostics");
const llmFormatBox = document.getElementById("llm-format");
const llmProviderSelect = document.getElementById("llm-provider");
const llmModelPresetSelect = document.getElementById("llm-model-preset");
const llmModelInput = document.getElementById("llm-model");
const llmCustomModelRow = document.getElementById("llm-custom-model-row");
const llmModelStatus = document.getElementById("llm-model-status");
const llmApiKeyInput = document.getElementById("llm-api-key");
const ytLangInput = document.getElementById("yt-lang");
const ytAutoBox = document.getElementById("yt-auto");
const ytMaxCommentsInput = document.getElementById("yt-max-comments");
const ytCommentSortSelect = document.getElementById("yt-comment-sort");

const DEFAULT_DOWNLOAD = "ctrl+shift+e";
const DEFAULT_QUEUE = "ctrl+shift+q";
const DEFAULT_SERVER_URL = "http://127.0.0.1:8000";
const FALLBACK_LLM_MODELS = {
  gemini: [
    {id: "gemini-3.1-flash-lite", label: "Gemini 3.1 Flash Lite (fast)"},
    {id: "gemini-2.5-flash-lite", label: "Gemini 2.5 Flash Lite"},
    {id: "gemini-2.5-flash", label: "Gemini 2.5 Flash"}
  ],
  openrouter: [
    {id: "deepseek/deepseek-v4-flash", label: "DeepSeek V4 Flash (cheap)"},
    {id: "deepseek/deepseek-v4-pro", label: "DeepSeek V4 Pro"},
    {id: "minimax/minimax-m3", label: "MiniMax M3"},
    {id: "x-ai/grok-4.3", label: "Grok 4.3"}
  ],
  openai: [
    {id: "gpt-4o-mini", label: "GPT-4o mini"}
  ]
};
let translationStatus = null;

function normalizeServerUrl(value) {
  let raw = (value || "").trim();
  if (!raw) return DEFAULT_SERVER_URL;
  if (!/^https?:\/\//i.test(raw)) raw = `http://${raw}`;
  try {
    const url = new URL(raw);
    url.pathname = url.pathname.replace(/\/+$/, "");
    url.search = "";
    url.hash = "";
    return url.toString().replace(/\/$/, "");
  } catch (_) {
    return DEFAULT_SERVER_URL;
  }
}

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
  
  // Migration
  const legacySub = (savedOpts.subfolder || "").trim();
  const legacyDir = (savedOpts.server_save_dir || savedOpts.termux_copy_dir || "").trim();
  serverUrlInput.value = normalizeServerUrl(savedOpts.server_url);
  saveFolderInput.value = (savedOpts.save_folder || legacyDir || legacySub || "").trim();
  
  archiveServerBox.checked = Boolean(savedOpts.archive_server);
  browserFallbackBox.checked = savedOpts.browser_fallback !== false;
  openChallengeUserBrowserBox.checked = savedOpts.browser_challenge_action === "user_browser";
  browserExtensionPathInput.value = (savedOpts.browser_extension_path || "").trim();
  browserProfileDirInput.value = (savedOpts.browser_profile_dir || "").trim();
  browserExecutableInput.value = (savedOpts.browser_executable || "").trim();
  llmFormatBox.checked = Boolean(savedOpts.llm_format);
  llmProviderSelect.value = savedOpts.llm_provider || "auto";
  llmModelInput.value = (savedOpts.llm_model || "").trim();
  llmApiKeyInput.value = (savedOpts.llm_api_key || "").trim();
  ytLangInput.value = (savedOpts.youtube_lang || "").trim();
  ytAutoBox.checked = Boolean(savedOpts.youtube_prefer_auto);
  ytMaxCommentsInput.value = savedOpts.youtube_max_comments !== undefined ? savedOpts.youtube_max_comments : "";
  ytCommentSortSelect.value = savedOpts.youtube_comment_sort || "top";
  syncLlmModelOptions();
}

async function saveSettings() {
  const enabled = enableBox.checked;
  const dl = normalizeCombo(downloadInput.value) || DEFAULT_DOWNLOAD;
  const q = normalizeCombo(queueInput.value) || DEFAULT_QUEUE;
  const serverUrl = normalizeServerUrl(serverUrlInput.value);
  const saveFolder = (saveFolderInput.value || "").trim();
  const archiveServer = archiveServerBox.checked;
  const browserFallback = browserFallbackBox.checked;
  const browserChallengeAction = openChallengeUserBrowserBox.checked ? "user_browser" : "archive";
  const browserExtensionPath = (browserExtensionPathInput.value || "").trim();
  const browserProfileDir = (browserProfileDirInput.value || "").trim();
  const browserExecutable = (browserExecutableInput.value || "").trim();
  const llmFormat = llmFormatBox.checked;
  const llmProvider = llmProviderSelect.value || "auto";
  const llmModel = llmModelPresetSelect.value === "__custom__"
    ? (llmModelInput.value || "").trim()
    : (llmModelPresetSelect.value || "").trim();
  const llmApiKey = (llmApiKeyInput.value || "").trim();
  const ytLang = (ytLangInput.value || "").trim();
  const ytAuto = ytAutoBox.checked;
  const ytMaxComments = parseInt(ytMaxCommentsInput.value, 10);
  const ytCommentSort = ytCommentSortSelect.value;
  
  const res = await browser.storage.local.get("savedOptions");
  const existing = res.savedOptions || {};
  await browser.storage.local.set({
    keyboardShortcutsEnabled: enabled,
    keyboardShortcutDownload: dl,
    keyboardShortcutQueue: q,
    savedOptions: {
      ...existing,
      save_folder: saveFolder,
      server_url: serverUrl,
      server_save_dir: saveFolder,
      subfolder: saveFolder,
      archive_server: archiveServer,
      browser_fallback: browserFallback,
      browser_challenge_action: browserChallengeAction,
      browser_extension_path: browserExtensionPath,
      browser_profile_dir: browserProfileDir,
      browser_executable: browserExecutable,
      llm_format: llmFormat,
      llm_provider: llmProvider,
      llm_model: llmModel,
      llm_api_key: llmApiKey,
      youtube_lang: ytLang,
      youtube_prefer_auto: ytAuto,
      youtube_max_comments: isNaN(ytMaxComments) ? 25 : ytMaxComments,
      youtube_comment_sort: ytCommentSort
    }
  });
}

function modelListForProvider(provider) {
  const effectiveProvider = provider === "auto"
    ? (translationStatus?.recommended_provider || "gemini")
    : provider;
  return (translationStatus?.models && translationStatus.models[effectiveProvider])
    || FALLBACK_LLM_MODELS[effectiveProvider]
    || [];
}

function syncLlmModelOptions() {
  if (!llmModelPresetSelect) return;
  const savedModel = (llmModelInput.value || "").trim();
  const provider = llmProviderSelect.value || "auto";
  const models = modelListForProvider(provider);
  const recommended = provider === "auto" ? (translationStatus?.recommended_model || "") : "";
  llmModelPresetSelect.textContent = "";
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = recommended ? `Recommended (${recommended})` : "Server default";
  llmModelPresetSelect.appendChild(defaultOption);
  for (const model of models) {
    const opt = document.createElement("option");
    opt.value = model.id;
    opt.textContent = model.label || model.id;
    llmModelPresetSelect.appendChild(opt);
  }
  const customOption = document.createElement("option");
  customOption.value = "__custom__";
  customOption.textContent = "Custom...";
  llmModelPresetSelect.appendChild(customOption);
  if (savedModel && models.some(model => model.id === savedModel)) {
    llmModelPresetSelect.value = savedModel;
  } else if (savedModel) {
    llmModelPresetSelect.value = "__custom__";
  } else {
    llmModelPresetSelect.value = "";
  }
  const custom = llmModelPresetSelect.value === "__custom__";
  if (llmCustomModelRow) llmCustomModelRow.style.display = custom ? "" : "none";
  if (llmModelStatus) {
    const keys = translationStatus?.keys || {};
    const available = Object.entries(keys).filter(([, value]) => value).map(([key]) => key).join(", ");
    llmModelStatus.textContent = available ? `Server keys available: ${available}` : "No server LLM keys detected.";
  }
}

async function refreshTranslationStatus() {
  try {
    const serverUrl = normalizeServerUrl(serverUrlInput.value);
    const response = await fetch(`${serverUrl}/helper/translation/status`, {signal: AbortSignal.timeout(1500)});
    if (response.ok) translationStatus = await response.json();
  } catch (_) {
    translationStatus = null;
  }
  syncLlmModelOptions();
}

async function checkBrowserFallback() {
  if (!browserFallbackStatus) return;
  browserFallbackStatus.textContent = "Checking...";
  if (serverDiagnostics) serverDiagnostics.textContent = "Checking...";
  try {
    const serverUrl = normalizeServerUrl(serverUrlInput.value);
    const response = await fetch(`${serverUrl}/ping`, {signal: AbortSignal.timeout(1500)});
    if (!response.ok) throw new Error(`Server ${response.status}`);
    const status = await response.json();
    const browser = status.browser_fallback_available ? "Browser fallback ready" : "Browser fallback unavailable";
    const bpc = status.bpc_extension_valid ? "BPC valid" : "BPC not configured or invalid";
    const pdf = status.pdf_available ? "PDF ready" : "PDF unavailable";
    browserFallbackStatus.textContent = `${browser}; ${pdf}; ${bpc}`;
    await renderDiagnostics(serverUrl, status);
  } catch (err) {
    browserFallbackStatus.textContent = "Server unavailable.";
    renderDiagnosticsError(err);
  }
}

async function initializeBrowserProfile() {
  if (!browserFallbackStatus) return;
  browserFallbackStatus.textContent = "Opening Dala profile...";
  try {
    const serverUrl = normalizeServerUrl(serverUrlInput.value);
    const payload = {
      url: "https://example.com/",
      browser_extension_path: (browserExtensionPathInput.value || "").trim() || null,
      browser_profile_dir: (browserProfileDirInput.value || "").trim() || null,
      browser_executable: (browserExecutableInput.value || "").trim() || null
    };
    const response = await fetch(`${serverUrl}/browser/warm/start`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!response.ok) throw new Error(`Server ${response.status}`);
    const body = await response.json();
    const warmUrl = new URL(body.warm_url, `${serverUrl}/`).toString();
    await browser.tabs.create({url: warmUrl});
    browserFallbackStatus.textContent = "Dala profile opened.";
  } catch (err) {
    browserFallbackStatus.textContent = err && err.message ? err.message : "Could not open Dala profile.";
  }
}

function setDiagnosticsRows(rows) {
  if (!serverDiagnostics) return;
  serverDiagnostics.textContent = "";
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    row.textContent = `${label}: ${value}`;
    serverDiagnostics.appendChild(row);
  }
}

async function renderDiagnostics(serverUrl, pingStatus) {
  let last = null;
  try {
    const response = await fetch(`${serverUrl}/helper/last-conversion`, {signal: AbortSignal.timeout(1500)});
    if (response.ok) last = await response.json();
  } catch (_) {}
  const rows = [
    ["Server", serverUrl],
    ["Version", pingStatus.server_version || "unknown"],
    ["Playwright", pingStatus.playwright_available ? "installed" : "missing"],
    ["Browser executable", pingStatus.browser_executable_found ? (pingStatus.browser_executable || "found") : "missing"],
    ["Browser fallback", pingStatus.browser_fallback_available ? "ready" : "unavailable"],
    ["PDF", pingStatus.pdf_available ? "ready" : "unavailable"],
    ["Profile", pingStatus.browser_profile_dir || "server default"],
    ["BPC", pingStatus.bpc_extension_valid ? (pingStatus.bpc_extension_path || "valid") : "invalid or unavailable"],
    ["Jobs", `${pingStatus.job_count || 0} retained; cleanup after ${Math.round((pingStatus.job_retention_seconds || 0) / 60)} min`],
  ];
  if (last) {
    rows.push(["Last conversion", `${last.status || "unknown"}${last.output_filename ? ` (${last.output_filename})` : ""}`]);
    if (last.error) rows.push(["Last error", last.error]);
    if (Array.isArray(last.failed_source_details) && last.failed_source_details.length) {
      rows.push(["Failed sources", String(last.failed_source_details.length)]);
    }
  } else {
    rows.push(["Last conversion", "none"]);
  }
  setDiagnosticsRows(rows);
}

function renderDiagnosticsError(err) {
  setDiagnosticsRows([["Error", err && err.message ? err.message : "Server unavailable"]]);
}

enableBox.addEventListener("change", saveSettings);
downloadInput.addEventListener("change", saveSettings);
queueInput.addEventListener("change", saveSettings);
serverUrlInput.addEventListener("change", saveSettings);
saveFolderInput.addEventListener("change", saveSettings);
archiveServerBox.addEventListener("change", saveSettings);
browserFallbackBox.addEventListener("change", saveSettings);
openChallengeUserBrowserBox.addEventListener("change", saveSettings);
browserExtensionPathInput.addEventListener("change", saveSettings);
browserProfileDirInput.addEventListener("change", saveSettings);
browserExecutableInput.addEventListener("change", saveSettings);
checkBrowserFallbackBtn.addEventListener("click", checkBrowserFallback);
initBrowserProfileBtn.addEventListener("click", initializeBrowserProfile);
llmFormatBox.addEventListener("change", saveSettings);
llmProviderSelect.addEventListener("change", () => {
  llmModelInput.value = "";
  syncLlmModelOptions();
  saveSettings();
});
llmModelPresetSelect.addEventListener("change", () => {
  syncLlmModelOptions();
  saveSettings();
});
llmModelInput.addEventListener("change", saveSettings);
llmApiKeyInput.addEventListener("change", saveSettings);
ytLangInput.addEventListener("change", saveSettings);
ytAutoBox.addEventListener("change", saveSettings);
ytMaxCommentsInput.addEventListener("change", saveSettings);
ytCommentSortSelect.addEventListener("change", saveSettings);

attachCapture(downloadInput);
attachCapture(queueInput);
loadSettings().then(async () => {
  await refreshTranslationStatus();
  await checkBrowserFallback();
});

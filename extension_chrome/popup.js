let currentTab = null;
const DEFAULT_SERVER_URL = "http://127.0.0.1:8000";
const TRANSLATION_LANGUAGE_SUGGESTIONS = [
    ["auto", "Auto detect"],
    ["af", "Afrikaans"],
    ["sq", "Albanian"],
    ["am", "Amharic"],
    ["ar", "Arabic"],
    ["hy", "Armenian"],
    ["az", "Azerbaijani"],
    ["eu", "Basque"],
    ["be", "Belarusian"],
    ["bn", "Bengali"],
    ["bs", "Bosnian"],
    ["bg", "Bulgarian"],
    ["ca", "Catalan"],
    ["ceb", "Cebuano"],
    ["zh-CN", "Chinese Simplified"],
    ["zh-TW", "Chinese Traditional"],
    ["co", "Corsican"],
    ["hr", "Croatian"],
    ["cs", "Czech"],
    ["da", "Danish"],
    ["nl", "Dutch"],
    ["en", "English"],
    ["eo", "Esperanto"],
    ["et", "Estonian"],
    ["fi", "Finnish"],
    ["fr", "French"],
    ["fy", "Frisian"],
    ["gl", "Galician"],
    ["ka", "Georgian"],
    ["de", "German"],
    ["el", "Greek"],
    ["gu", "Gujarati"],
    ["ht", "Haitian Creole"],
    ["ha", "Hausa"],
    ["haw", "Hawaiian"],
    ["he", "Hebrew"],
    ["hi", "Hindi"],
    ["hmn", "Hmong"],
    ["hu", "Hungarian"],
    ["is", "Icelandic"],
    ["ig", "Igbo"],
    ["id", "Indonesian"],
    ["ga", "Irish"],
    ["it", "Italian"],
    ["ja", "Japanese"],
    ["jv", "Javanese"],
    ["kn", "Kannada"],
    ["kk", "Kazakh"],
    ["km", "Khmer"],
    ["ko", "Korean"],
    ["ku", "Kurdish"],
    ["ky", "Kyrgyz"],
    ["lo", "Lao"],
    ["la", "Latin"],
    ["lv", "Latvian"],
    ["lt", "Lithuanian"],
    ["lb", "Luxembourgish"],
    ["mk", "Macedonian"],
    ["mg", "Malagasy"],
    ["ms", "Malay"],
    ["ml", "Malayalam"],
    ["mt", "Maltese"],
    ["mi", "Maori"],
    ["mr", "Marathi"],
    ["mn", "Mongolian"],
    ["my", "Myanmar Burmese"],
    ["ne", "Nepali"],
    ["no", "Norwegian"],
    ["ny", "Nyanja Chichewa"],
    ["or", "Odia"],
    ["ps", "Pashto"],
    ["fa", "Persian"],
    ["pl", "Polish"],
    ["pt", "Portuguese"],
    ["pt-BR", "Portuguese Brazil"],
    ["pa", "Punjabi"],
    ["ro", "Romanian"],
    ["ru", "Russian"],
    ["sm", "Samoan"],
    ["gd", "Scots Gaelic"],
    ["sr", "Serbian"],
    ["st", "Sesotho"],
    ["sn", "Shona"],
    ["sd", "Sindhi"],
    ["si", "Sinhala"],
    ["sk", "Slovak"],
    ["sl", "Slovenian"],
    ["so", "Somali"],
    ["es", "Spanish"],
    ["su", "Sundanese"],
    ["sw", "Swahili"],
    ["sv", "Swedish"],
    ["tl", "Tagalog Filipino"],
    ["tg", "Tajik"],
    ["ta", "Tamil"],
    ["tt", "Tatar"],
    ["te", "Telugu"],
    ["th", "Thai"],
    ["tr", "Turkish"],
    ["tk", "Turkmen"],
    ["uk", "Ukrainian"],
    ["ur", "Urdu"],
    ["ug", "Uyghur"],
    ["uz", "Uzbek"],
    ["vi", "Vietnamese"],
    ["cy", "Welsh"],
    ["xh", "Xhosa"],
    ["yi", "Yiddish"],
    ["yo", "Yoruba"],
    ["zu", "Zulu"],
];

function isValidUrl(url) {
    return url && url.startsWith("http") && !url.includes("localhost");
}

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

function isLocalServerUrl(value) {
    try {
        const url = new URL(normalizeServerUrl(value));
        const host = url.hostname.toLowerCase();
        return host === "localhost" || host === "127.0.0.1" || host === "::1" || host.endsWith(".localhost");
    } catch (_) {
        return true;
    }
}

function defaultCookiesForSavedOptions(savedOptions) {
    const opts = savedOptions || {};
    if (opts.forum) return true;
    if (opts.include_cookies_user_set === true) {
        return !!opts.include_cookies;
    }
    if (opts.include_cookies_user_set !== false && Object.prototype.hasOwnProperty.call(opts, "include_cookies")) {
        return !!opts.include_cookies;
    }
    return isLocalServerUrl(opts.server_url);
}

async function getServerBaseUrl() {
    const res = await browser.storage.local.get("savedOptions");
    return normalizeServerUrl(res.savedOptions && res.savedOptions.server_url);
}

function populateTranslationLanguageList() {
    const datalist = document.getElementById("translation-language-list");
    if (!datalist || datalist.children.length) return;
    const fragment = document.createDocumentFragment();
    TRANSLATION_LANGUAGE_SUGGESTIONS.forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.label = label;
        option.textContent = label;
        fragment.appendChild(option);
    });
    datalist.appendChild(fragment);
}

function setupDateInput(textId, pickerId, buttonId) {
    const text = document.getElementById(textId);
    const picker = document.getElementById(pickerId);
    const button = document.getElementById(buttonId);
    if (!text || !picker || !button) return;

    text.oninput = () => {
        const formatted = formatDateInput(text.value);
        if (formatted !== text.value) {
            text.value = formatted;
        }
        reconcileDateRange(textId);
        saveOptions();
    };
    text.onchange = () => {
        text.value = formatDateInput(text.value);
        reconcileDateRange(textId);
        saveOptions();
    };
    picker.onchange = () => {
        if (picker.value) {
            text.value = picker.value;
            reconcileDateRange(textId);
            saveOptions();
        }
    };
    button.addEventListener("click", (evt) => {
        evt.preventDefault();
        showDatePopover(textId, button);
    });
    button.addEventListener("keydown", (evt) => {
        if (!["Enter", " "].includes(evt.key)) return;
        evt.preventDefault();
        showDatePopover(textId, button);
    });
}

function closeDatePopover() {
    document.querySelectorAll('.date-popover').forEach(el => el.remove());
}

function showDatePopover(textId, button) {
    const text = document.getElementById(textId);
    if (!text || !button) return;
    const existing = document.querySelector(`.date-popover[data-target="${textId}"]`);
    closeDatePopover();
    if (existing) return;
    const parsed = parseDateBound(text.value, false) || new Date();
    renderDatePopover(textId, button, parsed.getUTCFullYear(), parsed.getUTCMonth());
}

function renderDatePopover(textId, button, year, month) {
    closeDatePopover();
    const text = document.getElementById(textId);
    if (!text) return;

    const popover = document.createElement('div');
    popover.className = 'date-popover';
    popover.dataset.target = textId;

    const header = document.createElement('div');
    header.className = 'date-popover-header';
    const prevYear = document.createElement('button');
    prevYear.type = 'button';
    prevYear.className = 'date-popover-year-btn';
    prevYear.textContent = '<<';
    const prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'date-popover-month-btn';
    prev.textContent = '<';
    const title = document.createElement('span');
    title.className = 'date-popover-title';
    title.textContent = new Date(Date.UTC(year, month, 1)).toLocaleString(undefined, {month: 'short', year: 'numeric', timeZone: 'UTC'});
    const next = document.createElement('button');
    next.type = 'button';
    next.className = 'date-popover-month-btn';
    next.textContent = '>';
    const nextYear = document.createElement('button');
    nextYear.type = 'button';
    nextYear.className = 'date-popover-year-btn';
    nextYear.textContent = '>>';
    header.append(prevYear, prev, title, next, nextYear);
    popover.appendChild(header);

    const weekdays = document.createElement('div');
    weekdays.className = 'date-popover-weekdays';
    ['S', 'M', 'T', 'W', 'T', 'F', 'S'].forEach(day => {
        const cell = document.createElement('span');
        cell.textContent = day;
        weekdays.appendChild(cell);
    });
    popover.appendChild(weekdays);

    const grid = document.createElement('div');
    grid.className = 'date-popover-days';
    const firstWeekday = new Date(Date.UTC(year, month, 1)).getUTCDay();
    const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    const today = new Date();
    const todayValue = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
    for (let i = 0; i < firstWeekday; i++) {
        grid.appendChild(document.createElement('span'));
    }
    for (let day = 1; day <= daysInMonth; day++) {
        const dateValue = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const dayButton = document.createElement('button');
        dayButton.type = 'button';
        dayButton.className = 'date-popover-day';
        dayButton.dataset.date = dateValue;
        dayButton.textContent = String(day);
        if (text.value === dateValue) dayButton.classList.add('selected');
        if (dateValue === todayValue) {
            dayButton.classList.add('today');
            dayButton.title = 'Today';
        }
        dayButton.addEventListener('click', () => {
            text.value = dateValue;
            reconcileDateRange(textId);
            saveOptions();
            closeDatePopover();
        });
        grid.appendChild(dayButton);
    }
    popover.appendChild(grid);

    const footer = document.createElement('button');
    footer.type = 'button';
    footer.className = 'date-popover-today';
    footer.textContent = `Today: ${todayValue}`;
    footer.addEventListener('click', () => {
        text.value = todayValue;
        reconcileDateRange(textId);
        saveOptions();
        closeDatePopover();
    });
    popover.appendChild(footer);

    prevYear.addEventListener('click', () => {
        renderDatePopover(textId, button, year - 1, month);
    });
    prev.addEventListener('click', () => {
        const previous = new Date(Date.UTC(year, month - 1, 1));
        renderDatePopover(textId, button, previous.getUTCFullYear(), previous.getUTCMonth());
    });
    next.addEventListener('click', () => {
        const following = new Date(Date.UTC(year, month + 1, 1));
        renderDatePopover(textId, button, following.getUTCFullYear(), following.getUTCMonth());
    });
    nextYear.addEventListener('click', () => {
        renderDatePopover(textId, button, year + 1, month);
    });

    document.body.appendChild(popover);
    const rect = button.getBoundingClientRect();
    const left = Math.max(8, Math.min(rect.left - 188, document.body.clientWidth - 238));
    popover.style.left = `${left}px`;
    popover.style.top = `${rect.bottom + 4}px`;
}

document.addEventListener('click', (evt) => {
    if (evt.target.closest('.date-popover') || evt.target.closest('.date-picker-btn')) return;
    closeDatePopover();
});

function formatDateInput(value) {
    const raw = (value || "").trim();
    if (!raw) return "";
    const digits = raw.replace(/\D/g, "").slice(0, 8);
    if (digits.length === 0) return "";
    if (digits.length <= 4) return digits;
    if (digits.length <= 6) return `${digits.slice(0, 4)}-${digits.slice(4)}`;
    return `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6)}`;
}

function parseDateBound(value, isEnd) {
    const raw = (value || "").trim();
    let match = raw.match(/^(\d{4})$/);
    if (match) {
        const year = Number(match[1]);
        return new Date(Date.UTC(year, isEnd ? 11 : 0, isEnd ? 31 : 1));
    }
    match = raw.match(/^(\d{4})-(\d{1,2})$/);
    if (match) {
        const year = Number(match[1]);
        const month = Number(match[2]);
        if (month < 1 || month > 12) return null;
        const day = isEnd ? new Date(Date.UTC(year, month, 0)).getUTCDate() : 1;
        return new Date(Date.UTC(year, month - 1, day));
    }
    match = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
    if (match) {
        const year = Number(match[1]);
        const month = Number(match[2]);
        const day = Number(match[3]);
        const parsed = new Date(Date.UTC(year, month - 1, day));
        if (
            parsed.getUTCFullYear() !== year ||
            parsed.getUTCMonth() !== month - 1 ||
            parsed.getUTCDate() !== day
        ) return null;
        return parsed;
    }
    return null;
}

function reconcileDateRange(changedId) {
    const start = document.getElementById('opt-start-date');
    const end = document.getElementById('opt-end-date');
    const endPicker = document.getElementById('opt-end-date-picker');
    if (!start || !end || !start.value || !end.value) return;
    const startBound = parseDateBound(start.value, false);
    const endBound = parseDateBound(end.value, true);
    if (!startBound || !endBound || startBound <= endBound) return;
    if (changedId === 'opt-start-date') {
        end.value = "";
        if (endPicker) endPicker.value = "";
    } else {
        start.value = "";
        const startPicker = document.getElementById('opt-start-date-picker');
        if (startPicker) startPicker.value = "";
    }
}

async function clearDateOptions() {
    const start = document.getElementById('opt-start-date');
    const end = document.getElementById('opt-end-date');
    const startPicker = document.getElementById('opt-start-date-picker');
    const endPicker = document.getElementById('opt-end-date-picker');
    if (start) start.value = "";
    if (end) end.value = "";
    if (startPicker) startPicker.value = "";
    if (endPicker) endPicker.value = "";
    const existing = (await browser.storage.local.get("savedOptions")).savedOptions || {};
    await browser.storage.local.set({
        savedOptions: {
            ...existing,
            start_date: "",
            end_date: "",
            date_fallback: "auto",
            include_undated: false
        }
    });
}

document.addEventListener('DOMContentLoaded', async () => {
    // --- 1. Attach Event Listeners Immediately ---
    try {
        populateTranslationLanguageList();
        // Tab Switching
        document.getElementById('tab-single').onclick = () => switchTab('single');
        document.getElementById('tab-queue').onclick = () => switchTab('queue');

        // Download Actions
        document.getElementById('btn-download-single').onclick = safeDownloadSingle;
        document.getElementById('btn-download-bundle').onclick = safeDownloadBundle;

        // Queue Actions
        document.getElementById('btn-add-tab').onclick = async () => {
            if (!currentTab) currentTab = (await browser.tabs.query({active: true, currentWindow: true}))[0];
            if (currentTab && isValidUrl(currentTab.url)) addToQueue(currentTab.url);
        };
        document.getElementById('btn-clear').onclick = clearQueue;
        const queueEditor = document.getElementById('queue-editor');
        if (queueEditor) {
            queueEditor.addEventListener('input', debounce(saveQueueFromEditor, 300));
        }

        // Bulk Actions
        document.getElementById('btn-add-selected').onclick = importSelectedTabs;
        document.getElementById('btn-add-all').onclick = importAllTabs;
        const retryFailedBtn = document.getElementById('btn-retry-failed');
        if (retryFailedBtn) retryFailedBtn.onclick = retryFailedSources;

        // Options
        document.querySelectorAll('input[type="checkbox"]').forEach(box => {
            box.onchange = saveOptions;
        });
        const cookieBox = document.getElementById('opt-cookies');
        if (cookieBox) {
            cookieBox.onchange = handleCookieToggle;
        }
        const forumBox = document.getElementById('opt-forum');
        if (forumBox) {
            forumBox.onchange = handleForumToggle;
        }
        const noImagesBox = document.getElementById('opt-noimages');
        if (noImagesBox) {
            noImagesBox.onchange = () => {
                syncImageOptionsState();
                saveOptions();
            };
        }
        const pagesInput = document.getElementById('opt-pages');
        const maxPagesInput = document.getElementById('opt-maxpages');
        if (pagesInput) pagesInput.onchange = saveOptions;
        if (maxPagesInput) maxPagesInput.onchange = saveOptions;
        const outputFormat = document.getElementById('opt-output-format');
        if (outputFormat) outputFormat.onchange = () => {
            syncPdfOptionsVisibility();
            syncTranslationDisplayOptions();
            saveOptions();
        };
        const pdfPageSize = document.getElementById('opt-pdf-page-size');
        if (pdfPageSize) pdfPageSize.onchange = saveOptions;
        const imagePreset = document.getElementById('opt-image-preset');
        if (imagePreset) imagePreset.onchange = saveOptions;
        ['opt-translation-target', 'opt-translation-source', 'opt-translation-glossary'].forEach(id => {
            const input = document.getElementById(id);
            if (input) input.onchange = saveOptions;
        });
        ['opt-translation-provider', 'opt-translation-display', 'opt-translation-scope'].forEach(id => {
            const select = document.getElementById(id);
            if (select) select.onchange = saveOptions;
        });
        const testTranslationBtn = document.getElementById('btn-test-translation');
        if (testTranslationBtn) testTranslationBtn.onclick = testTranslationProvider;
        const clearTranslationCacheBtn = document.getElementById('btn-clear-translation-cache');
        if (clearTranslationCacheBtn) clearTranslationCacheBtn.onclick = clearTranslationCache;
        setupDateInput('opt-start-date', 'opt-start-date-picker', 'opt-start-date-btn');
        setupDateInput('opt-end-date', 'opt-end-date-picker', 'opt-end-date-btn');
        const clearDatesBtn = document.getElementById('opt-clear-dates');
        if (clearDatesBtn) clearDatesBtn.onclick = clearDateOptions;
        const cancelBtn = document.getElementById('btn-cancel');
        if (cancelBtn) cancelBtn.onclick = () => {
            browser.runtime.sendMessage({ action: "cancel-download" });
            showStatus("Cancel requested");
        };
        const optsBtn = document.getElementById('btn-options');
        if (optsBtn) {
            optsBtn.onclick = () => {
                if (browser.runtime.openOptionsPage) {
                    browser.runtime.openOptionsPage();
                } else {
                    const url = browser.runtime.getURL("options.html");
                    browser.tabs.create({ url });
                }
            };
        }
    } catch (e) {
        console.error("Error attaching listeners:", e);
    }

    // --- 2. Initialize Data ---
    try {
        checkServer();
        restoreOptions();
        refreshQueue();
        syncPdfOptionsVisibility();
        syncImageOptionsState();
    } catch (e) {
        console.error("Error restoring state:", e);
    }

    // --- 3. Get Active Tab ---
    try {
        const tabs = await browser.tabs.query({active: true, currentWindow: true});
        if (tabs && tabs.length > 0) {
            currentTab = tabs[0];
            const titleInput = document.getElementById('single-title');
            if (titleInput) titleInput.value = currentTab.title;
            await autoDetectForumForActiveTab(currentTab);
        }
    } catch (e) {
        console.warn("Tab Init Warning:", e);
    }
});

// --- SERVER CHECK ---
async function checkServer() {
    const dot = document.getElementById('server-status');
    try {
        const serverUrl = await getServerBaseUrl();
        const res = await fetch(`${serverUrl}/ping`, {signal: AbortSignal.timeout(1000)});
        if (res.ok) {
            let title = `Server Online: ${serverUrl}`;
            try {
                const body = await res.json();
                setPdfAvailability(body.pdf_available !== false);
                if (body && body.pdf_available === false) {
                    title = `${title}; PDF unavailable`;
                }
                if (body && body.bpc_extension_configured) {
                    title = body.bpc_extension_valid ? `${title}; BPC valid` : `${title}; BPC invalid`;
                }
            } catch (_) {}
            dot.className = "status-dot online";
            dot.title = title;
        } else { throw new Error(); }
    } catch {
        dot.className = "status-dot offline";
        dot.title = "Server Offline";
        showStatus("Server Offline");
    }
}

function setPdfAvailability(isAvailable) {
    const outputFormat = document.getElementById('opt-output-format');
    if (!outputFormat) return;
    const pdfOption = outputFormat.querySelector('option[value="pdf"]');
    if (!pdfOption) return;
    pdfOption.disabled = !isAvailable;
    pdfOption.title = isAvailable ? "" : "PDF requires Playwright and a Chromium-compatible browser on the Dala server.";
    if (!isAvailable && outputFormat.value === "pdf") {
        outputFormat.value = "epub";
        syncPdfOptionsVisibility();
        saveOptions().catch(() => {});
        showStatus("PDF unavailable on server; switched to EPUB.");
    }
}

// --- OPTIONS ---
async function saveOptions(meta = {}) {
    const existing = (await browser.storage.local.get("savedOptions")).savedOptions || {};
    const pdfPageSize = document.getElementById('opt-pdf-page-size')?.value || "letter";
    const outputFormat = document.getElementById('opt-output-format')?.value || "epub";
    const forumEnabled = document.getElementById('opt-forum').checked;
    const cookiesEnabled = document.getElementById('opt-cookies').checked || forumEnabled;
    const cookiesUserSet = !!(meta && meta.cookiesUserSet);
    const includeCookiesUserSet = cookiesUserSet ? true : existing.include_cookies_user_set === true;
    if (forumEnabled) {
        document.getElementById('opt-cookies').checked = true;
    }
    const options = {
        no_comments: document.getElementById('opt-nocomments').checked,
        no_article: document.getElementById('opt-noarticle').checked,
        no_images: document.getElementById('opt-noimages').checked,
        archive: document.getElementById('opt-archive').checked,
        summary: document.getElementById('opt-summary').checked,
        thumbnails: document.getElementById('opt-thumbnails').checked,
        include_cookies: cookiesEnabled,
        include_cookies_user_set: includeCookiesUserSet,
        forum: forumEnabled,
        output_format: outputFormat,
        pdf_preset: pdfPresetForPageSize(pdfPageSize),
        pdf_page_size: pdfPageSize,
        image_preset: normalizeImagePreset(document.getElementById('opt-image-preset')?.value),
        image_color: document.getElementById('opt-grayscale')?.checked ? "grayscale" : "color",
        translation_enabled: !!document.getElementById('opt-translate')?.checked,
        translation_provider: document.getElementById('opt-translation-provider')?.value || "llm",
        translation_target_lang: (document.getElementById('opt-translation-target')?.value || "").trim(),
        translation_source_lang: (document.getElementById('opt-translation-source')?.value || "").trim() || "auto",
        translation_display: translationDisplayForOutput(document.getElementById('opt-translation-display')?.value, outputFormat),
        translation_scope: document.getElementById('opt-translation-scope')?.value || "article-captions",
        translation_glossary: (document.getElementById('opt-translation-glossary')?.value || "").trim(),
        translation_cache: document.getElementById('opt-translation-cache')?.checked !== false,
        browser_challenge_action: document.getElementById('opt-open-challenge-user-browser')?.checked ? "user_browser" : "archive",
        start_date: (document.getElementById('opt-start-date')?.value || "").trim(),
        end_date: (document.getElementById('opt-end-date')?.value || "").trim(),
        date_fallback: "auto",
        include_undated: false,
        pages: (document.getElementById('opt-pages')?.value || "").trim(),
        max_pages: document.getElementById('opt-maxpages')?.value || ""
    };
    await browser.storage.local.set({ savedOptions: { ...existing, ...options } });
}

async function restoreOptions() {
    const res = await browser.storage.local.get("savedOptions");
    const savedOptions = res.savedOptions || {};
    const cookieBox = document.getElementById('opt-cookies');
    if (cookieBox) {
        cookieBox.checked = defaultCookiesForSavedOptions(savedOptions);
    }
    if (res.savedOptions) {
        document.getElementById('opt-nocomments').checked = res.savedOptions.no_comments;
        document.getElementById('opt-noarticle').checked = res.savedOptions.no_article;
        document.getElementById('opt-noimages').checked = res.savedOptions.no_images;
        document.getElementById('opt-archive').checked = res.savedOptions.archive;
        document.getElementById('opt-summary').checked = !!res.savedOptions.summary;
        document.getElementById('opt-thumbnails').checked = !!res.savedOptions.thumbnails;
        document.getElementById('opt-open-challenge-user-browser').checked = res.savedOptions.browser_challenge_action === "user_browser";
        document.getElementById('opt-forum').checked = !!res.savedOptions.forum;
        if (res.savedOptions.image_preset !== undefined) {
            document.getElementById('opt-image-preset').value = normalizeImagePreset(res.savedOptions.image_preset);
        }
        document.getElementById('opt-grayscale').checked = res.savedOptions.image_color === "grayscale";
        if (res.savedOptions.output_format !== undefined) {
            document.getElementById('opt-output-format').value = res.savedOptions.output_format || "epub";
        }
        if (res.savedOptions.pdf_page_size !== undefined) {
            document.getElementById('opt-pdf-page-size').value = res.savedOptions.pdf_page_size || "letter";
        }
        document.getElementById('opt-translate').checked = !!res.savedOptions.translation_enabled;
        document.getElementById('opt-translation-provider').value = res.savedOptions.translation_provider || "llm";
        document.getElementById('opt-translation-target').value = res.savedOptions.translation_target_lang || "";
        document.getElementById('opt-translation-source').value = res.savedOptions.translation_source_lang || "auto";
        document.getElementById('opt-translation-display').value = translationDisplayForOutput(
            res.savedOptions.translation_display || "underneath",
            document.getElementById('opt-output-format')?.value || "epub"
        );
        document.getElementById('opt-translation-scope').value = res.savedOptions.translation_scope || "article-captions";
        document.getElementById('opt-translation-glossary').value = res.savedOptions.translation_glossary || "";
        document.getElementById('opt-translation-cache').checked = res.savedOptions.translation_cache !== false;
        if (res.savedOptions.start_date !== undefined) {
            document.getElementById('opt-start-date').value = res.savedOptions.start_date || "";
        }
        if (res.savedOptions.end_date !== undefined) {
            document.getElementById('opt-end-date').value = res.savedOptions.end_date || "";
        }
        if (res.savedOptions.pages !== undefined) {
            document.getElementById('opt-pages').value = res.savedOptions.pages;
        }
        if (res.savedOptions.max_pages !== undefined) {
            document.getElementById('opt-maxpages').value = res.savedOptions.max_pages;
        }
    }
    syncPdfOptionsVisibility();
    syncImageOptionsState();
    syncAdvancedOptionSections();
}

function getOptions() {
    const pdfPageSize = document.getElementById('opt-pdf-page-size')?.value || "letter";
    const outputFormat = document.getElementById('opt-output-format')?.value || "epub";
    const forumEnabled = document.getElementById('opt-forum').checked;
    const cookiesEnabled = document.getElementById('opt-cookies').checked || forumEnabled;
    return {
        no_comments: document.getElementById('opt-nocomments').checked,
        no_article: document.getElementById('opt-noarticle').checked,
        no_images: document.getElementById('opt-noimages').checked,
        archive: document.getElementById('opt-archive').checked,
        summary: document.getElementById('opt-summary').checked,
        thumbnails: document.getElementById('opt-thumbnails').checked,
        include_cookies: cookiesEnabled,
        forum: forumEnabled,
        output_format: outputFormat,
        pdf_preset: pdfPresetForPageSize(pdfPageSize),
        pdf_page_size: pdfPageSize,
        image_preset: normalizeImagePreset(document.getElementById('opt-image-preset')?.value),
        image_color: document.getElementById('opt-grayscale')?.checked ? "grayscale" : "color",
        translation_enabled: !!document.getElementById('opt-translate')?.checked,
        translation_provider: document.getElementById('opt-translation-provider')?.value || "llm",
        translation_target_lang: (document.getElementById('opt-translation-target')?.value || "").trim(),
        translation_source_lang: (document.getElementById('opt-translation-source')?.value || "").trim() || "auto",
        translation_display: translationDisplayForOutput(document.getElementById('opt-translation-display')?.value, outputFormat),
        translation_scope: document.getElementById('opt-translation-scope')?.value || "article-captions",
        translation_glossary: (document.getElementById('opt-translation-glossary')?.value || "").trim(),
        translation_cache: document.getElementById('opt-translation-cache')?.checked !== false,
        browser_challenge_action: document.getElementById('opt-open-challenge-user-browser')?.checked ? "user_browser" : "archive",
        start_date: (document.getElementById('opt-start-date')?.value || "").trim(),
        end_date: (document.getElementById('opt-end-date')?.value || "").trim(),
        date_fallback: "auto",
        include_undated: false,
        pages: (document.getElementById('opt-pages')?.value || "").trim(),
        max_pages: (document.getElementById('opt-maxpages')?.value || "").trim()
    };
}

function pdfPresetForPageSize(pageSize) {
    return pageSize === "kobo_clara" ? "ereader" : "document";
}

function normalizeImagePreset(value) {
    const preset = String(value || "balanced").trim().toLowerCase().replace(/_/g, "-");
    if (preset === "baseline") return "balanced";
    if (preset === "optimized") return "compact";
    if (["compact", "balanced", "full"].includes(preset)) return preset;
    return "balanced";
}

function translationDisplayForOutput(value, outputFormat) {
    const display = value || "underneath";
    return outputFormat === "pdf" && display === "popup_footnote" ? "underneath" : display;
}

function syncImageOptionsState() {
    const noImages = !!document.getElementById('opt-noimages')?.checked;
    const group = document.getElementById('image-settings');
    const imagePreset = document.getElementById('opt-image-preset');
    const grayscale = document.getElementById('opt-grayscale');
    if (group) {
        group.classList.toggle("disabled", noImages);
        group.setAttribute("aria-disabled", noImages ? "true" : "false");
    }
    if (imagePreset) imagePreset.disabled = noImages;
    if (grayscale) grayscale.disabled = noImages;
}

function syncTranslationDisplayOptions() {
    const outputFormat = document.getElementById('opt-output-format')?.value || "epub";
    const display = document.getElementById('opt-translation-display');
    if (!display) return;
    const footnoteOption = display.querySelector('option[value="popup_footnote"]');
    const isPdf = outputFormat === "pdf";
    if (footnoteOption) {
        footnoteOption.disabled = isPdf;
        footnoteOption.hidden = isPdf;
    }
    if (isPdf && display.value === "popup_footnote") {
        display.value = "underneath";
    }
}

function syncPdfOptionsVisibility() {
    const outputFormat = document.getElementById('opt-output-format')?.value || "epub";
    const pdfOptions = document.getElementById('pdf-options');
    const pdfPageSize = document.getElementById('opt-pdf-page-size');
    const isPdf = outputFormat === "pdf";
    if (pdfOptions) pdfOptions.classList.toggle("hidden", !isPdf);
    if (pdfPageSize) pdfPageSize.disabled = !isPdf;
    syncTranslationDisplayOptions();
}

function syncAdvancedOptionSections() {
    const dateSection = document.getElementById('date-options');
    const forumSection = document.getElementById('forum-options');
    const translationSection = document.getElementById('translation-options');
    const hasDates = !!(
        (document.getElementById('opt-start-date')?.value || "").trim()
        || (document.getElementById('opt-end-date')?.value || "").trim()
    );
    const hasForumOptions = !!(
        document.getElementById('opt-forum')?.checked
        || (document.getElementById('opt-pages')?.value || "").trim()
        || (document.getElementById('opt-maxpages')?.value || "").trim()
    );
    const hasTranslationOptions = !!(
        document.getElementById('opt-translate')?.checked
        || (document.getElementById('opt-translation-target')?.value || "").trim()
        || (document.getElementById('opt-translation-glossary')?.value || "").trim()
    );
    if (dateSection) dateSection.open = hasDates;
    if (forumSection) forumSection.open = hasForumOptions;
    if (translationSection) translationSection.open = hasTranslationOptions;
}

function parsePageSpecInput(spec) {
    if (!spec) return null;
    const parts = spec.split(',').map(p => p.trim()).filter(Boolean);
    const pages = new Set();
    for (const part of parts) {
        if (part.includes('-')) {
            const [a, b] = part.split('-').map(x => parseInt(x, 10));
            if (!isNaN(a) && !isNaN(b)) {
                const start = Math.min(a, b);
                const end = Math.max(a, b);
                for (let i = start; i <= end; i++) pages.add(i);
            }
        } else {
            const n = parseInt(part, 10);
            if (!isNaN(n) && n > 0) pages.add(n);
        }
    }
    const arr = Array.from(pages).sort((a, b) => a - b);
    return arr.length ? arr : null;
}



async function handleCookieToggle() {
    const box = document.getElementById('opt-cookies');
    if (!box) return;
    const forumBox = document.getElementById('opt-forum');
    if (forumBox && forumBox.checked && !box.checked) {
        box.checked = true;
        await saveOptions({cookiesUserSet: true});
        showStatus("Cookies are required for forum mode");
        return;
    }
    if (!box.checked) {
        await saveOptions({cookiesUserSet: true});
        showStatus("Cookies disabled");
        return;
    }
    try {
        const tabs = await browser.tabs.query({active: true, currentWindow: true});
        const tab = tabs && tabs[0];
        if (!tab || !isValidUrl(tab.url)) {
            showStatus("No active page to request cookies");
            box.checked = false;
            await saveOptions({cookiesUserSet: true});
            return;
        }
        const granted = await ensureCookiePermissionForUrl(tab.url);
        if (!granted) {
            showStatus("Cookie permission denied");
            box.checked = false;
        } else {
            showStatus("Cookies enabled for this site");
        }
    } catch (e) {
        console.warn("Cookie toggle error", e);
        box.checked = false;
        showStatus("Cookie access failed");
    } finally {
        await saveOptions({cookiesUserSet: true});
    }
}

async function handleForumToggle() {
    const forumBox = document.getElementById('opt-forum');
    const cookieBox = document.getElementById('opt-cookies');
    if (forumBox && cookieBox && forumBox.checked) {
        cookieBox.checked = true;
        const forumSection = document.getElementById('forum-options');
        if (forumSection) forumSection.open = true;
        showStatus("Forum mode uses site cookies");
    }
    await saveOptions();
}


async function autoDetectForumForActiveTab(tab) {
    const forumBox = document.getElementById('opt-forum');
    const cookieBox = document.getElementById('opt-cookies');
    if (!forumBox || !cookieBox || forumBox.checked || !tab || !isValidUrl(tab.url)) return;
    let detected = isLikelyForumUrl(tab.url);
    if (!detected) {
        try {
            const results = await new Promise((resolve) => {
                chrome.scripting.executeScript({
                    target: { tabId: tab.id },
                    func: () => document.documentElement.outerHTML
                }, (res) => {
                    if (chrome.runtime.lastError) resolve(null);
                    else resolve(res);
                });
            });
            detected = !!(results && results[0] && isLikelyForumHtml(results[0].result));
        } catch (e) {
            console.warn("Forum auto-detect failed", e);
        }
    }
    if (!detected) return;
    forumBox.checked = true;
    cookieBox.checked = true;
    syncAdvancedOptionSections();
    await saveOptions();
    showStatus("Forum detected; cookies enabled");
}

async function ensureCookiePermissionForUrl(url) {
    try {
        const u = new URL(url);
        const originPattern = `${u.origin}/*`;
        // cookies permission is already declared in manifest; request only host origin
        const perms = {origins: [originPattern]};
        const hasPerm = await browser.permissions.contains(perms);
        if (hasPerm) return true;
        return await browser.permissions.request(perms);
    } catch (e) {
        console.warn("Permission request failed", e);
        return false;
    }
}

async function getCookiesForUrl(url) {
    try {
        const list = await browser.cookies.getAll({url});
        if (!list || list.length === 0) return null;
        const jar = {};
        list.forEach(c => { jar[c.name] = c.value; });
        return jar;
    } catch (e) {
        console.warn("Cookie fetch failed for", url, e);
        return null;
    }
}

// --- TAB IMPORT LOGIC ---
async function importSelectedTabs() {
    try {
        const tabs = await browser.tabs.query({currentWindow: true});
        let count = 0;
        for (let tab of tabs) {
            if (tab.highlighted && isValidUrl(tab.url)) {
                await addToQueue(tab.url);
                count++;
            }
        }
        showStatus(`Added ${count} selected tabs`);
    } catch (e) {
        showStatus("Error: " + e.message);
    }
}

async function importAllTabs() {
    try {
        const tabs = await browser.tabs.query({currentWindow: true});
        let count = 0;
        for (let tab of tabs) {
            if (isValidUrl(tab.url)) {
                await addToQueue(tab.url);
                count++;
            }
        }
        showStatus(`Added ${count} tabs`);
    } catch (e) {
        showStatus("Error: " + e.message);
    }
}

// --- QUEUE MANAGEMENT ---
async function refreshQueue() {
    const res = await browser.storage.local.get("urlQueue");
    const queue = res.urlQueue || [];
    const editor = document.getElementById('queue-editor');
    if(document.getElementById('queue-count')) {
        document.getElementById('queue-count').textContent = `(${queue.length})`;
    }
    if (editor) {
        editor.value = queue.join("\n");
    }
    updateBadgeCount(queue.length);
}

async function addToQueue(url) {
    const res = await browser.storage.local.get("urlQueue");
    const queue = res.urlQueue || [];
    if (!queue.includes(url)) {
        queue.push(url);
        await browser.storage.local.set({ urlQueue: queue });
        refreshQueue();
    }
}

async function clearQueue() {
    await browser.storage.local.set({ urlQueue: [] });
    refreshQueue();
}

async function saveQueueFromEditor() {
    const editor = document.getElementById('queue-editor');
    if (!editor) return;
    const lines = editor.value.split('\n').map(l => l.trim()).filter(l => l);
    const uniq = Array.from(new Set(lines.filter(isValidUrl)));
    await browser.storage.local.set({ urlQueue: uniq });
    if(document.getElementById('queue-count')) {
        document.getElementById('queue-count').textContent = `(${uniq.length})`;
    }
    updateBadgeCount(uniq.length);
}

async function retryFailedSources() {
    const res = await browser.storage.local.get("lastFailedSources");
    const failed = Array.isArray(res.lastFailedSources) ? res.lastFailedSources : [];
    const urls = Array.from(new Set(
        failed
            .map(item => typeof item === "string" ? item : item && item.url)
            .filter(isValidUrl)
    ));
    if (!urls.length) {
        showStatus("No failed sources saved");
        return;
    }
    await browser.storage.local.set({ urlQueue: urls });
    refreshQueue();
    switchTab("queue");
    showStatus(`Queued ${urls.length} failed source${urls.length === 1 ? "" : "s"}`);
}

function updateBadgeCount(count) {
    browser.browserAction.setBadgeText({ text: count > 0 ? count.toString() : "" });
    browser.browserAction.setBadgeBackgroundColor({ color: "#e85a4f" });
}

function debounce(fn, delay) {
    let t = null;
    return (...args) => {
        clearTimeout(t);
        t = setTimeout(() => fn(...args), delay);
    };
}

// --- UI HELPERS ---
function switchTab(tab) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.getElementById(`view-${tab}`).classList.add('active');
    document.getElementById(`tab-${tab}`).classList.add('active');
}

function showStatus(msg) {
    const el = document.getElementById('status-bar');
    el.textContent = msg;
    // Optional: flash effect
    el.style.backgroundColor = "#ffffcc";
    setTimeout(() => el.style.backgroundColor = "#eee", 500);
}

async function testTranslationProvider() {
    try {
        await saveOptions();
        const opts = getOptions();
        if (!opts.translation_target_lang) {
            showStatus("Set a target language first");
            return;
        }
        const saved = await browser.storage.local.get("savedOptions");
        const savedOptions = saved.savedOptions || {};
        const serverUrl = await getServerBaseUrl();
        showStatus("Testing translation...");
        const response = await fetch(`${serverUrl}/helper/translation/test`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                text: "Hello world.",
                translation_provider: opts.translation_provider,
                translation_target_lang: opts.translation_target_lang,
                translation_source_lang: opts.translation_source_lang,
                translation_glossary: opts.translation_glossary || null,
                translation_cache: false,
                llm_provider: savedOptions.llm_provider || "auto",
                llm_model: (savedOptions.llm_model || "").trim() || null,
                llm_api_key: (savedOptions.llm_api_key || "").trim() || null
            }),
            signal: AbortSignal.timeout(20000)
        });
        if (!response.ok) {
            const text = await response.text();
            throw new Error(text || `Server ${response.status}`);
        }
        const body = await response.json();
        showStatus(`Test: ${body.translated_text || "OK"}`);
    } catch (e) {
        showStatus("Test failed: " + (e.message || e));
    }
}

async function clearTranslationCache() {
    try {
        const serverUrl = await getServerBaseUrl();
        const response = await fetch(`${serverUrl}/helper/translation/cache/clear`, {
            method: "POST",
            signal: AbortSignal.timeout(5000)
        });
        if (!response.ok) {
            const text = await response.text();
            throw new Error(text || `Server ${response.status}`);
        }
        const body = await response.json();
        showStatus(body.cleared ? "Translation cache cleared" : "Translation cache already empty");
    } catch (e) {
        showStatus("Cache clear failed: " + (e.message || e));
    }
}

// --- DOWNLOAD TRIGGERS ---
async function safeDownloadSingle() {
    try {
        if (!currentTab) {
            const tabs = await browser.tabs.query({active: true, currentWindow: true});
            currentTab = tabs[0];
        }

        if (!currentTab || !isValidUrl(currentTab.url)) {
            showStatus("Error: Invalid or empty tab.");
            return;
        }

        await saveOptions();
        showStatus("Initiating download...");
        const title = document.getElementById('single-title').value;
        
        browser.runtime.sendMessage({ 
            action: "init_download", 
            urls: [currentTab.url],
            title: title,
            isBundle: false 
        });
        
        showStatus("Started in background...");
        setTimeout(() => window.close(), 1000);
    } catch (e) {
        showStatus("Error: " + e.message);
        console.error(e);
    }
}

async function safeDownloadBundle() {
    try {
        console.log("Starting bundle download...");
        await saveQueueFromEditor();
        await saveOptions();
        
        const res = await browser.storage.local.get("urlQueue");
        const queue = res.urlQueue || [];

        if (queue.length === 0) {
            showStatus("Queue is empty!");
            return;
        }

        showStatus("Initiating bundle...");
        const title = document.getElementById('bundle-title').value;
        
        browser.runtime.sendMessage({ 
            action: "init_download", 
            urls: queue,
            title: title,
            isBundle: true 
        });
        
        console.log("Message sent.");
        showStatus("Bundle started...");
        setTimeout(() => window.close(), 1000);
    } catch (e) {
        showStatus("Error: " + e.message);
        console.error(e);
    }
}

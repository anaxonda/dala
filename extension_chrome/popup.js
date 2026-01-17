let currentTab = null;

function isValidUrl(url) {
    return url && url.startsWith("http") && !url.includes("localhost");
}

document.addEventListener('DOMContentLoaded', async () => {
    // --- 1. Attach Event Listeners Immediately ---
    try {
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

        // Options
        document.querySelectorAll('input[type="checkbox"]').forEach(box => {
            box.onchange = saveOptions;
        });
        const cookieBox = document.getElementById('opt-cookies');
        if (cookieBox) {
            cookieBox.onchange = handleCookieToggle;
        }
        const pagesInput = document.getElementById('opt-pages');
        const maxPagesInput = document.getElementById('opt-maxpages');
        if (pagesInput) pagesInput.onchange = saveOptions;
        if (maxPagesInput) maxPagesInput.onchange = saveOptions;
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
        }
    } catch (e) {
        console.warn("Tab Init Warning:", e);
    }
});

// --- SERVER CHECK ---
async function checkServer() {
    const dot = document.getElementById('server-status');
    try {
        const res = await fetch("http://127.0.0.1:8000/ping", {signal: AbortSignal.timeout(1000)});
        if (res.ok) {
            dot.className = "status-dot online";
            dot.title = "Server Online";
        } else { throw new Error(); }
    } catch {
        dot.className = "status-dot offline";
        dot.title = "Server Offline (Run server.py!)";
        showStatus("Server Offline");
    }
}

// --- OPTIONS ---
async function saveOptions() {
    const existing = (await browser.storage.local.get("savedOptions")).savedOptions || {};
    const options = {
        no_comments: document.getElementById('opt-nocomments').checked,
        no_article: document.getElementById('opt-noarticle').checked,
        no_images: document.getElementById('opt-noimages').checked,
        archive: document.getElementById('opt-archive').checked,
        summary: document.getElementById('opt-summary').checked,
        include_cookies: document.getElementById('opt-cookies').checked,
        forum: document.getElementById('opt-forum').checked,
        pages: (document.getElementById('opt-pages')?.value || "").trim(),
        max_pages: document.getElementById('opt-maxpages')?.value || ""
    };
    await browser.storage.local.set({ savedOptions: { ...existing, ...options } });
}

async function restoreOptions() {
    const res = await browser.storage.local.get("savedOptions");
    if (res.savedOptions) {
        document.getElementById('opt-nocomments').checked = res.savedOptions.no_comments;
        document.getElementById('opt-noarticle').checked = res.savedOptions.no_article;
        document.getElementById('opt-noimages').checked = res.savedOptions.no_images;
        document.getElementById('opt-archive').checked = res.savedOptions.archive;
        document.getElementById('opt-summary').checked = !!res.savedOptions.summary;
        document.getElementById('opt-cookies').checked = !!res.savedOptions.include_cookies;
        document.getElementById('opt-forum').checked = !!res.savedOptions.forum;
        if (res.savedOptions.pages !== undefined) {
            document.getElementById('opt-pages').value = res.savedOptions.pages;
        }
        if (res.savedOptions.max_pages !== undefined) {
            document.getElementById('opt-maxpages').value = res.savedOptions.max_pages;
        }
    }
}

function getOptions() {
    return {
        no_comments: document.getElementById('opt-nocomments').checked,
        no_article: document.getElementById('opt-noarticle').checked,
        no_images: document.getElementById('opt-noimages').checked,
        archive: document.getElementById('opt-archive').checked,
        summary: document.getElementById('opt-summary').checked,
        include_cookies: document.getElementById('opt-cookies').checked,
        forum: document.getElementById('opt-forum').checked,
        pages: (document.getElementById('opt-pages')?.value || "").trim(),
        max_pages: (document.getElementById('opt-maxpages')?.value || "").trim()
    };
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
    if (!box.checked) {
        await saveOptions();
        showStatus("Cookies disabled");
        return;
    }
    try {
        const tabs = await browser.tabs.query({active: true, currentWindow: true});
        const tab = tabs && tabs[0];
        if (!tab || !isValidUrl(tab.url)) {
            showStatus("No active page to request cookies");
            box.checked = false;
            await saveOptions();
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
        await saveOptions();
    }
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
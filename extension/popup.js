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
    const options = {
        no_comments: document.getElementById('opt-nocomments').checked,
        no_article: document.getElementById('opt-noarticle').checked,
        no_images: document.getElementById('opt-noimages').checked,
        archive: document.getElementById('opt-archive').checked,
        include_cookies: document.getElementById('opt-cookies').checked,
        pages: (document.getElementById('opt-pages')?.value || "").trim(),
        max_pages: document.getElementById('opt-maxpages')?.value || ""
    };
    await browser.storage.local.set({ savedOptions: options });
}

async function restoreOptions() {
    const res = await browser.storage.local.get("savedOptions");
    if (res.savedOptions) {
        document.getElementById('opt-nocomments').checked = res.savedOptions.no_comments;
        document.getElementById('opt-noarticle').checked = res.savedOptions.no_article;
        document.getElementById('opt-noimages').checked = res.savedOptions.no_images;
        document.getElementById('opt-archive').checked = res.savedOptions.archive;
        document.getElementById('opt-cookies').checked = !!res.savedOptions.include_cookies;
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
        include_cookies: document.getElementById('opt-cookies').checked,
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

async function fetchAssetsFromPage(tabId, refererUrl) {
    try {
        const results = await browser.tabs.executeScript(tabId, {
            code: `(() => {
                const imgs = Array.from(document.querySelectorAll('img'));
                return imgs.map(img => {
                    const srcset = img.getAttribute('data-srcset') || img.getAttribute('srcset');
                    const dataUrl = img.getAttribute('data-url');
                    const src = img.getAttribute('data-src') || img.getAttribute('src');
                    const a = img.closest('a');
                    const viewer = a && a.href ? a.href : null;
                    return {src, srcset, dataUrl, viewer};
                });
            })();`
        });
        const assets = [];
        if (results && results[0]) {
            for (const rec of results[0]) {
                const best = pickBestImageCandidate(rec, refererUrl);
                if (!best) continue;
                const data = await fetchBinaryWithCookies(best.url, refererUrl);
                if (data) {
                    assets.push({
                        original_url: best.url,
                        viewer_url: rec.viewer ? new URL(rec.viewer, refererUrl).href : null,
                        filename_hint: best.url.split('/').pop(),
                        content_type: data.type,
                        content: data.base64
                    });
                }
            }
        }
        return assets;
    } catch (e) {
        console.warn('fetchAssetsFromPage failed', e);
        return [];
    }
}

function pickBestImageCandidate(rec, baseUrl) {
    const candidates = [];
    if (rec.dataUrl) candidates.push({u: rec.dataUrl, w: 99999});
    if (rec.srcset) {
        const parts = rec.srcset.split(',').map(p => p.trim()).filter(Boolean);
        for (const p of parts) {
            const [u, w] = p.split(/\\s+/);
            let width = parseInt((w || '').replace('w',''), 10);
            if (isNaN(width)) width = 0;
            candidates.push({u, w: width});
        }
    }
    if (rec.src) candidates.push({u: rec.src, w: 0});
    let best = null;
    let maxw = -1;
    for (const c of candidates) {
        let abs = null;
        try { abs = new URL(c.u, baseUrl).href; } catch(e) { abs = null; }
        if (!abs) continue;
        if (c.w > maxw) { maxw = c.w; best = abs; }
    }
    if (!best) return null;
    return {url: best};
}

async function fetchPageAssets(url, cookies, page_spec, max_pages) {
    const assets = [];
    try {
        const resp = await browser.runtime.sendMessage({
            action: "fetch-assets",
            url,
            page_spec,
            max_pages
        });
        if (resp && resp.assets) return resp.assets;
    } catch (e) {
        console.warn("fetchPageAssets failed", e);
    }
    return assets;
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
    const list = document.getElementById('queue-list');

    if(document.getElementById('queue-count')) {
        document.getElementById('queue-count').textContent = `(${queue.length})`;
    }

    list.innerHTML = '';
    if (queue.length === 0) {
        list.innerHTML = '<div class="empty-state">Queue is empty</div>';
        return;
    }

    queue.forEach((url, index) => {
        const div = document.createElement('div');
        div.className = 'queue-item';
        // &times; is the HTML entity for the multiplication X
        div.innerHTML = `<span>${url}</span><button class="btn-remove" data-idx="${index}">&times;</button>`;
        list.appendChild(div);
    });

    document.querySelectorAll('.btn-remove').forEach(btn => {
        btn.onclick = async (e) => {
            const idx = parseInt(e.target.dataset.idx);
            queue.splice(idx, 1);
            await browser.storage.local.set({ urlQueue: queue });
            refreshQueue();
        };
    });
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

// --- PAYLOAD BUILDER (THE HYBRID ENGINE) ---
// Combines URLs with raw HTML from open tabs to bypass paywalls
async function preparePayload(urls, bundleTitle) {
    const options = getOptions();
    const sources = [];
    const page_spec = parsePageSpecInput(options.pages);
    const max_pages = options.max_pages ? parseInt(options.max_pages, 10) || null : null;
    const include_assets = options.include_cookies;

    // 1. Get all tabs to check for matches
    let allTabs = [];
    try {
        allTabs = await browser.tabs.query({});
    } catch(e) {
        console.warn("Cannot query tabs for HTML injection");
    }

    // 2. Iterate requested URLs
    for (const url of urls) {
        let html = null;
        // Find a tab that matches this URL and is fully loaded
        const match = allTabs.find(t => t.url === url && t.status === "complete");

        if (match) {
            try {
                // Inject script to steal DOM
                const results = await browser.tabs.executeScript(match.id, {
                    code: "document.documentElement.outerHTML;"
                });
                if (results && results[0]) {
                    html = results[0];
                    console.log("Injecting HTML for:", url);
                }
            } catch (e) {
                // Fails on restricted domains (addons.mozilla.org) or discarded tabs
                console.log("Could not grab HTML (using fallback):", url);
            }
        }
        let cookies = null;
        if (options.include_cookies) {
            cookies = await getCookiesForUrl(url);
        }
        let assets = [];
        if (include_assets && match) {
            assets = await fetchAssetsFromPage(match.id, url) || [];
        }
        if (include_assets && assets.length === 0) {
            assets = await fetchPageAssets(url, cookies, page_spec, max_pages);
        }
        sources.push({ url: url, html: html, cookies: cookies, assets: assets });
    }

    return {
        sources: sources,
        bundle_title: bundleTitle,
        no_comments: options.no_comments,
        no_article: options.no_article,
        no_images: options.no_images,
        archive: options.archive,
        max_pages: max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        fetch_assets: include_assets
    };
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

        showStatus("Grabbing content...");
        const title = document.getElementById('single-title').value;
        const payload = await preparePayload([currentTab.url], title);

        browser.runtime.sendMessage({ action: "download", payload: payload, isBundle: false });
        showStatus("Started in background...");
        setTimeout(() => window.close(), 1500);
    } catch (e) {
        showStatus("Error: " + e.message);
        console.error(e);
    }
}

async function safeDownloadBundle() {
    try {
        const res = await browser.storage.local.get("urlQueue");
        const queue = res.urlQueue || [];

        if (queue.length === 0) {
            showStatus("Queue is empty!");
            return;
        }

        showStatus("Preparing bundle...");
        const title = document.getElementById('bundle-title').value;
        const payload = await preparePayload(queue, title);

        browser.runtime.sendMessage({ action: "download", payload: payload, isBundle: true });
        showStatus("Bundle started...");
        setTimeout(() => window.close(), 1500);
    } catch (e) {
        showStatus("Error: " + e.message);
        console.error(e);
    }
}

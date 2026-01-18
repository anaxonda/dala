try {
    importScripts('chrome-shim.js');
} catch (e) {
    console.log("Shim load skipped (not in SW?)");
}

// Initialize
browser.runtime.onInstalled.addListener(() => {
    browser.storage.local.get([
        "urlQueue", 
        "keyboardShortcutsEnabled",
        "keyboardShortcutDownload",
        "keyboardShortcutQueue"
    ]).then((res) => {
        if (!res.urlQueue) browser.storage.local.set({ urlQueue: [] });
        
        // Initialize defaults if not set
        const updates = {};
        if (res.keyboardShortcutsEnabled === undefined) updates.keyboardShortcutsEnabled = true;
        if (res.keyboardShortcutDownload === undefined) updates.keyboardShortcutDownload = "ctrl+shift+e";
        if (res.keyboardShortcutQueue === undefined) updates.keyboardShortcutQueue = "ctrl+shift+q";
        
        if (Object.keys(updates).length > 0) {
            browser.storage.local.set(updates);
        }
        
        updateBadge();
    });

    const menus = browser.menus;
    if (menus) {
        try {
            chrome.contextMenus.removeAll(() => {
                menus.create({
                    id: "add-to-queue",
                    title: "Add to EPUB Queue",
                    contexts: ["page", "link"]
                });
                menus.create({
                    id: "add-selected-to-queue",
                    title: "Add selected tabs to EPUB Queue",
                    contexts: ["page"]
                });
                menus.create({
                    id: "download-page",
                    title: "Download Page to EPUB",
                    contexts: ["page", "link"]
                });
            });
        } catch(e) {
            menus.create({
                id: "add-to-queue",
                title: "Add to EPUB Queue",
                contexts: ["page", "link"]
            });
            menus.create({
                id: "add-selected-to-queue",
                title: "Add selected tabs to EPUB Queue",
                contexts: ["page"]
            });
            menus.create({
                id: "download-page",
                title: "Download Page to EPUB",
                contexts: ["page", "link"]
            });
        }
    }
});

// Context Menu Action
const menusApi = browser.menus;
if (menusApi && menusApi.onClicked) {
    menusApi.onClicked.addListener(async (info, tab) => {
        if (info.menuItemId === "add-to-queue") {
            const url = info.linkUrl || tab.url;
            await addToQueue(url);
        } else if (info.menuItemId === "add-selected-to-queue") {
            try {
                const tabs = await browser.tabs.query({currentWindow: true, highlighted: true});
                let count = 0;
                for (let t of tabs) {
                    if (t.url && t.url.startsWith("http")) {
                        await addToQueue(t.url);
                        count++;
                    }
                }
                console.log(`Added ${count} selected tabs to queue via context menu`);
            } catch (e) {
                console.error("Failed to add selected tabs to queue", e);
            }
        } else if (info.menuItemId === "download-page") {
            const url = info.linkUrl || tab.url;
            await downloadSingleFromContext(url);
        }
    });
}

// Command Listener (Native Shortcuts)
if (browser.commands && browser.commands.onCommand) {
    browser.commands.onCommand.addListener(async (command) => {
        const tabs = await browser.tabs.query({ active: true, currentWindow: true });
        const tab = tabs[0];
        if (!tab || !tab.url || !tab.url.startsWith("http")) return;

        if (command === "download-page") {
            lastShortcutTabId = tab.id;
            let html = null;
            try {
                const results = await chrome.scripting.executeScript({
                    target: { tabId: tab.id },
                    func: () => document.documentElement.outerHTML
                });
                if (results && results[0] && results[0].result) html = results[0].result;
            } catch (e) { /* fallback */ }
            downloadFromShortcut(tab.url, html);
        } else if (command === "add-to-queue") {
            addToQueue(tab.url);
        }
    });
}

let currentController = null;
let lastShortcutTabId = null;

// Message Listener
browser.runtime.onMessage.addListener((message, sender) => {
    if (message.action === "download") {
        processDownloadWithAssets(message.payload, message.isBundle);
        return true; 
    } else if (message.action === "init_download") {
        preparePayloadFromBackground(message.urls, message.title, message.isBundle)
            .then(payload => processDownloadWithAssets(payload, message.isBundle))
            .catch(e => {
                console.error("Preparation failed", e);
                browser.notifications.create({
                    type: "basic",
                    iconUrl: "icon.png",
                    title: "Preparation Failed",
                    message: e.message
                });
                browser.browserAction.setBadgeText({ text: "ERR" });
                browser.browserAction.setBadgeBackgroundColor({ color: "red" });
            });
        return true;
    } else if (message.action === "cancel-download") {
        cancelDownload();
    } else if (message.action === "fetch-assets") {
        return fetchAssetsForPage(message.url, message.page_spec, message.max_pages);
    } else if (message.action === "shortcut-download") {
        if (sender && sender.tab && sender.tab.id) {
            lastShortcutTabId = sender.tab.id;
        }
        const target = message.url;
        if (target && target.startsWith("http")) {
            downloadFromShortcut(target, message.html || null);
        }
        return true;
    } else if (message.action === "shortcut-queue") {
        if (sender && sender.tab && sender.tab.id) {
            lastShortcutTabId = sender.tab.id;
        }
        const target = message.url;
        if (target && target.startsWith("http")) {
            addToQueue(target);
        }
        return true;
    }
});

function getPageHTML() { 
    return document.documentElement.outerHTML; 
}

async function preparePayloadFromBackground(urls, bundleTitle, isBundle) {
    browser.browserAction.setBadgeText({ text: "PREP" });
    browser.browserAction.setBadgeBackgroundColor({ color: "#FFA500" });

    const savedOpts = (await browser.storage.local.get("savedOptions")).savedOptions || {};
    
    const options = {
        no_comments: !!savedOpts.no_comments,
        no_article: !!savedOpts.no_article,
        no_images: !!savedOpts.no_images,
        archive: !!savedOpts.archive,
        summary: !!savedOpts.summary,
        include_cookies: !!savedOpts.include_cookies,
        forum: !!savedOpts.forum,
        pages: (savedOpts.pages || "").trim(),
        max_pages: savedOpts.max_pages ? parseInt(savedOpts.max_pages, 10) || null : null
    };

    const sources = [];
    const page_spec = parsePageSpecInput(options.pages);
    const max_pages = options.max_pages;
    const include_assets = options.include_cookies;
    const is_forum = options.forum;

    let allTabs = [];
    try {
        allTabs = await browser.tabs.query({});
    } catch(e) {
        console.warn("Cannot query tabs for HTML injection");
    }

    for (const url of urls) {
        console.log(`Processing payload for: ${url}`);
        let html = null;
        const match = allTabs.find(t => t.url === url);

        if (match) {
            try {
                if (match.status !== "complete") {
                   await new Promise(r => setTimeout(r, 1000));
                }
                
                console.log(`Injecting script into tab ${match.id}...`);
                const results = await new Promise((resolve, reject) => {
                    chrome.scripting.executeScript({
                        target: { tabId: match.id },
                        func: getPageHTML
                    }, (res) => {
                        if (chrome.runtime.lastError) resolve(null); // resolve null on error
                        else resolve(res);
                    });
                });

                if (results && results[0] && results[0].result) {
                    html = results[0].result;
                    console.log("Injecting HTML for:", url);
                }
            } catch (e) {
                console.log("Could not grab HTML (using fallback):", url, e);
            }
        }
        
        let cookies = null;
        let assets = [];
        if (include_assets) {
            cookies = await getCookiesForUrl(url);
            if (is_forum && match) {
                try {
                    assets = await scrapeAssetsFromTab(match.id, url);
                    console.log(`Scraped ${assets.length} assets from DOM for ${url}`);
                } catch (e) {
                    console.warn("DOM asset scrape failed", e);
                }
            }
        }
        sources.push({ url: url, html: html, cookies: cookies, assets: assets, is_forum: is_forum });
    }

    const shouldFetchAssets = include_assets && is_forum;
    
    return {
        sources: sources,
        bundle_title: bundleTitle,
        no_comments: options.no_comments,
        no_article: options.no_article,
        no_images: options.no_images,
        archive: options.archive,
        summary: options.summary,
        max_pages: max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        fetch_assets: shouldFetchAssets,
        termux_copy_dir: (savedOpts.termux_copy_dir || "").trim() || null,
        llm_format: !!savedOpts.llm_format,
        llm_model: (savedOpts.llm_model || "").trim() || null,
        llm_api_key: (savedOpts.llm_api_key || "").trim() || null
    };
}

function scrapeImagesFunc() {
    const imgs = Array.from(document.querySelectorAll('.message-body img'));
    return imgs.map(img => {
        const srcset = img.getAttribute('data-srcset') || img.getAttribute('srcset');
        const dataUrl = img.getAttribute('data-url');
        const src = img.getAttribute('data-src') || img.getAttribute('src');
        const a = img.closest('a');
        const viewer = a && a.href ? a.href : null;
        return {src, srcset, dataUrl, viewer};
    });
}

async function scrapeAssetsFromTab(tabId, refererUrl) {
    try {
        const results = await new Promise((resolve, reject) => {
            chrome.scripting.executeScript({
                target: { tabId: tabId },
                func: scrapeImagesFunc
            }, (res) => {
                if (chrome.runtime.lastError) resolve(null);
                else resolve(res);
            });
        });

        const assets = [];
        if (results && results[0] && results[0].result) {
            for (const rec of results[0].result) {
                const best = pickBestImageCandidate(rec, refererUrl);
                if (best && best.url) assets.push(best);
            }
        }
        return assets;
    } catch (e) {
        console.warn('scrapeAssetsFromTab failed', e);
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
    return {original_url: best, url: best};
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

async function downloadFromShortcut(url, html) {
    if (!url || !url.startsWith("http")) return;
    const optsRes = await browser.storage.local.get("savedOptions");
    const opts = optsRes.savedOptions || {};
    const page_spec = parsePageSpecInput(opts.pages);
    const max_pages = opts.max_pages ? parseInt(opts.max_pages, 10) || null : null;
    const is_forum = !!opts.forum;
    const termux_copy_dir = (opts.termux_copy_dir || "").trim() || null;
    
    let cookies = null;
    if (opts.include_cookies) {
        cookies = await getCookiesForUrl(url);
    }

    const payload = {
        sources: [{ url, html: html || null, cookies: cookies, assets: [], is_forum }],
        bundle_title: null,
        no_comments: !!opts.no_comments,
        no_article: !!opts.no_article,
        no_images: !!opts.no_images,
        archive: !!opts.archive,
        summary: !!opts.summary,
        max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        fetch_assets: false,
        termux_copy_dir,
        llm_format: !!opts.llm_format,
        llm_model: (opts.llm_model || "").trim() || null,
        llm_api_key: (opts.llm_api_key || "").trim() || null
    };
    await processDownloadWithAssets(payload, false);
}

async function addToQueue(url) {
    const res = await browser.storage.local.get("urlQueue");
    const queue = res.urlQueue || [];
    if (!queue.includes(url)) {
        queue.push(url);
        await browser.storage.local.set({ urlQueue: queue });
        updateBadge();
    }
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

async function downloadSingleFromContext(url) {
    if (!url || !url.startsWith("http")) return;
    const optsRes = await browser.storage.local.get("savedOptions");
    const opts = optsRes.savedOptions || {};
    const page_spec = parsePageSpecInput(opts.pages);
    const max_pages = opts.max_pages ? parseInt(opts.max_pages, 10) || null : null;
    const is_forum = !!opts.forum;
    const termux_copy_dir = (opts.termux_copy_dir || "").trim() || null;
    
    let cookies = null;
    if (opts.include_cookies) {
        cookies = await getCookiesForUrl(url);
    }

    const payload = {
        sources: [{ url, html: null, cookies: cookies, assets: [], is_forum }],
        bundle_title: null,
        no_comments: !!opts.no_comments,
        no_article: !!opts.no_article,
        no_images: !!opts.no_images,
        archive: !!opts.archive,
        summary: !!opts.summary,
        max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        fetch_assets: false,
        termux_copy_dir,
        llm_format: !!opts.llm_format,
        llm_model: (opts.llm_model || "").trim() || null,
        llm_api_key: (opts.llm_api_key || "").trim() || null
    };
    await processDownloadWithAssets(payload, false);
}

function updateBadge() {
    browser.storage.local.get("urlQueue").then((res) => {
        const count = res.urlQueue ? res.urlQueue.length : 0;
        browser.browserAction.setBadgeText({ text: count > 0 ? count.toString() : "" });
        browser.browserAction.setBadgeBackgroundColor({ color: "#e85a4f" });
    });
}

function getFilenameFromHeader(header) {
    if (!header) return "download.epub";
    let filename = "download.epub";
    let matches = /filename=\"([^\"]*)\"/i.exec(header);
    if (matches && matches[1]) {
        filename = matches[1];
    } else {
        matches = /filename=([^;]*)/i.exec(header);
        if (matches && matches[1]) {
            filename = matches[1].trim();
        }
    }
    let starMatches = /filename\*=UTF-8''([^;]*)/i.exec(header);
    if (starMatches && starMatches[1]) {
        try {
            filename = decodeURIComponent(starMatches[1]);
        } catch (e) {
            console.warn("Could not decode filename", e);
        }
    }
    return filename;
}

async function openEpubInTab(blob, filename) {
    try {
        const buffer = await blob.arrayBuffer();
        const bytes = new Uint8Array(buffer);
        const chunkSize = 8192;
        let binary = "";
        for (let i = 0; i < bytes.length; i += chunkSize) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
        }
        const base64 = btoa(binary);
        const dataUrl = `data:application/epub+zip;base64,${base64}`;
        await browser.tabs.create({ url: dataUrl });
        console.warn(`Opened EPUB in tab for manual save: ${filename}`);
    } catch (e) {
        console.error("Failed to open EPUB blob in tab", e);
    }
}

async function processDownloadWithAssets(payload, isBundle) {
    console.log("🔧 Background: Processing download with asset enrichment");
    if (payload && payload.sources) {
        for (const src of payload.sources) {
            if (!src.is_forum) continue;
            const existing = Array.isArray(src.assets) ? src.assets : [];
            console.log(`🔍 Fetching assets for ${src.url}`);
            try {
                const res = await fetchAssetsForPage(src.url, payload.page_spec, payload.max_pages);
                if (res && res.assets) {
                    console.log(`✓ Fetched ${res.assets.length} assets in background`);
                    const byUrl = new Map();
                    existing.forEach(a => { if (a && a.original_url) byUrl.set(a.original_url, a); });
                    res.assets.forEach(a => { if (a && a.original_url && !byUrl.has(a.original_url)) existing.push(a); });
                    src.assets = existing;
                }
            } catch (err) {
                console.error("Background asset fetch failed:", err);
            }
        }
    }
    await processDownloadCore(payload, isBundle);
}

function blobToDataURL(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
    });
}

async function processDownloadCore(payload, isBundle) {
    if (currentController) {
        currentController.abort();
        currentController = null;
    }
    currentController = new AbortController();
    browser.browserAction.setBadgeText({ text: "..." });
    browser.browserAction.setBadgeBackgroundColor({ color: "#FFA500" });

    try {
        if (!payload.termux_copy_dir) {
            const res = await browser.storage.local.get("savedOptions");
            const termuxDir = (res.savedOptions && typeof res.savedOptions.termux_copy_dir === "string") ? res.savedOptions.termux_copy_dir.trim() : "";
            if (termuxDir) {
                payload.termux_copy_dir = termuxDir;
            }
        }
    } catch (_) { } 

    try {
        console.log("Preparing JSON payload...");
        const bodyStr = JSON.stringify(payload);
        console.log(`Payload size: ${bodyStr.length} chars. Sending request to server...`);

        const response = await fetch("http://127.0.0.1:8000/convert", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: bodyStr,
            signal: currentController.signal
        });

        console.log("Server response received:", response.status);

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`Server ${response.status}: ${errText}`);
        }

        const blob = await response.blob();
        let downloadUrl = null;
        let isBlobUrl = false;

        // MV3 Service Worker strategy: standard URL.createObjectURL is often disabled.
        // Prefer Data URI if we can't create a Blob URL.
        if (typeof URL.createObjectURL === 'function') {
            try {
                downloadUrl = URL.createObjectURL(blob);
                isBlobUrl = true;
            } catch (e) {
                console.log("createObjectURL threw error, falling back to Data URI");
            }
        }

        if (!downloadUrl) {
            console.log("Generating Data URI for download...");
            downloadUrl = await blobToDataURL(blob);
        }

        const filename = getFilenameFromHeader(response.headers.get('Content-Disposition'));
        const res = await browser.storage.local.get("savedOptions");
        const rawSub = (res.savedOptions && typeof res.savedOptions.subfolder === "string") ? res.savedOptions.subfolder.trim() : "";
        const cleanSub = rawSub.replace(/[/\\]+/g, '');
        const targetPath = cleanSub ? `${cleanSub}/${filename}` : filename;

        const canDownload = browser.downloads && typeof browser.downloads.download === "function";
        const isAndroid = /Android/i.test((navigator && navigator.userAgent) || "");
        let downloaded = false;

        if (canDownload) {
            try {
                console.log(`Attempting download 1: URL length=${downloadUrl.length}, Filename=${targetPath}`);
                await browser.downloads.download({
                    url: downloadUrl,
                    filename: targetPath,
                    saveAs: false,
                    conflictAction: 'uniquify'
                });
                downloaded = true;
            } catch (e) {
                console.warn("Attempt 1 failed. Retrying with generic filename...", e);
                try {
                     await browser.downloads.download({
                        url: downloadUrl,
                        filename: "web_to_epub_export.epub",
                        saveAs: false,
                        conflictAction: 'uniquify'
                    });
                    downloaded = true;
                } catch (e2) {
                    console.warn("Attempt 2 failed. Trying last resort (Data URI + Default Name)...", e2);
                    try {
                        if (isBlobUrl) {
                             downloadUrl = await blobToDataURL(blob);
                             isBlobUrl = false;
                        }
                        await browser.downloads.download({ 
                            url: downloadUrl,
                            filename: "fallback.epub",
                            conflictAction: 'uniquify' 
                        });
                        downloaded = true;
                    } catch (e3) {
                        const msg = e3.message || JSON.stringify(e3);
                        console.error("All download attempts failed", e3);
                        browser.notifications.create({
                            type: "basic",
                            iconUrl: "icon.png",
                            title: "Download Save Failed",
                            message: `Final Error: ${msg}`
                        });
                    }
                }
            }
        } else {
            console.warn("downloads API unavailable; will open blob in new tab");
        }

        if (!downloaded) {
            await openEpubInTab(blob, filename);
        } else if (isAndroid) {
            await openEpubInTab(blob, filename);
        }

        if (isBlobUrl && downloadUrl) {
            setTimeout(() => URL.revokeObjectURL(downloadUrl), 30000);
        }

        browser.browserAction.setBadgeText({ text: "OK" });
        browser.browserAction.setBadgeBackgroundColor({ color: "green" });

        try {
            const targetTabId = lastShortcutTabId;
            lastShortcutTabId = null;
            const sendToastToTab = async (tabId) => {
                if (!tabId) return;
                try {
                    await browser.tabs.sendMessage(tabId, { action: "shortcut-toast", message: "Downloaded" });
                } catch (e) {
                    const code = `
                      (() => {
                        try {
                          const existing = document.getElementById("epub-shortcut-toast");
                          if (existing) existing.remove();
                          const el = document.createElement("div");
                          el.id = "epub-shortcut-toast";
                          el.textContent = "Downloaded";
                          el.style.cssText = "position:fixed;top:16px;right:16px;background:#4CAF50;color:white;padding:10px 14px;border-radius:4px;z-index:2147483647;font-size:13px;box-shadow:0 2px 6px rgba(0,0,0,0.25);";
                          document.body.appendChild(el);
                          setTimeout(() => { el.remove(); }, 2500);
                        } catch(_) {}
                      })();`;
                    try { await chrome.scripting.executeScript({ target: {tabId}, func: () => {
                        // inline func for toast
                        const existing = document.getElementById("epub-shortcut-toast");
                        if (existing) existing.remove();
                        const el = document.createElement("div");
                        el.id = "epub-shortcut-toast";
                        el.textContent = "Downloaded";
                        el.style.cssText = "position:fixed;top:16px;right:16px;background:#4CAF50;color:white;padding:10px 14px;border-radius:4px;z-index:2147483647;font-size:13px;box-shadow:0 2px 6px rgba(0,0,0,0.25);";
                        document.body.appendChild(el);
                        setTimeout(() => { el.remove(); }, 2500);
                    } }); } catch (_) {}
                }
            };
            if (targetTabId) {
                await sendToastToTab(targetTabId);
            } else {
                const tabs = await browser.tabs.query({active: true, currentWindow: true});
                if (tabs && tabs[0]) {
                    await sendToastToTab(tabs[0].id);
                }
            }
            browser.runtime.sendMessage({ action: "shortcut-toast", message: "Downloaded" }).catch(() => {});
        } catch (_) { } 

        if (isBundle) {
            await browser.storage.local.set({ urlQueue: [] });
            updateBadge();
        } else {
            setTimeout(updateBadge, 3000);
        }
        currentController = null;

    } catch (error) {
        if (error.name === 'AbortError') {
            browser.browserAction.setBadgeText({ text: "" });
            browser.browserAction.setBadgeBackgroundColor({ color: "#e85a4f" });
        } else {
            console.error("Download Failed:", error);
            browser.browserAction.setBadgeText({ text: "ERR" });
            browser.browserAction.setBadgeBackgroundColor({ color: "red" });

            browser.notifications.create({
                type: "basic",
                iconUrl: "icon.png",
                title: "Download Failed",
                message: error.message || "Check server.py console"
            });
        }
        currentController = null;
    }
}

function cancelDownload() {
    if (currentController) {
        currentController.abort();
        currentController = null;
        browser.notifications.create({
            type: "basic",
            iconUrl: "icon.png",
            title: "Download Cancelled",
            message: "Current download was cancelled."
        });
        updateBadge();
    }
}

async function parseHtmlOnServer(html, url) {
    try {
        const resp = await fetch("http://127.0.0.1:8000/helper/extract-links", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ html, url })
        });
        if (resp.ok) {
            return await resp.json();
        }
    } catch(e) {
        console.warn("Server parse failed", e);
    }
    return { assets: [], externals: [], next_page_num: null };
}

async function fetchAssetsForPage(threadUrl, page_spec, max_pages) {
    const assets = [];
    try {
        const normBase = threadUrl.replace(/#.*$/, "").replace(/\/page-\d+/i, "").replace(/([?&])page=\d+/i, "$1").replace(/[?&]$/, "");
        const currentPageMatch = threadUrl.match(/page-(\d+)/i) || threadUrl.match(/[?&]page=(\d+)/i);
        const currentPage = currentPageMatch ? parseInt(currentPageMatch[1], 10) : 1;
        const hasExplicitPages = page_spec && page_spec.length;
        const pages = hasExplicitPages ? page_spec : [1];
        const uniquePages = Array.from(new Set(pages.filter(p => p && p > 0))).sort((a, b) => a - b);
        const limiter = (arr, n) => arr.slice(0, n || arr.length);
        const pagesToFetch = limiter(uniquePages, max_pages || uniquePages.length);
        const seenPages = new Set();
        const queue = [...pagesToFetch];

        while (queue.length) {
            const page = queue.shift();
            if (seenPages.has(page)) continue;
            seenPages.add(page);
            const url = buildForumPageUrl(normBase, page);
            const html = await fetchWithCookies(url, threadUrl);
            if (!html) continue;
            
            const parseResult = await parseHtmlOnServer(html, url);
            const foundAssets = parseResult.assets || [];
            const externals = parseResult.externals || [];
            const nextPageNum = parseResult.next_page_num;

            for (const att of foundAssets) {
                let fullData = null;
                if (att.viewer_url) {
                    const viewerResp = await fetchBinaryMaybeHtml(att.viewer_url, url);
                    if (viewerResp && viewerResp.type && !viewerResp.isHtml && viewerResp.base64) {
                        fullData = viewerResp;
                    } 
                }

                if (!fullData) {
                    fullData = await fetchBinaryMaybeHtml(att.url, url);
                }

                if (fullData && fullData.base64 && !fullData.isHtml) {
                    assets.push({
                        original_url: att.url,
                        viewer_url: att.viewer_url,
                        canonical_url: att.url.split("?")[0],
                        filename_hint: att.filename_hint,
                        content_type: fullData.type,
                        content: fullData.base64
                    });
                }
            }

            for (const ext of externals) {
                const data = await fetchBinaryMaybeHtml(ext, url);
                if (data && data.base64 && !data.isHtml) {
                    assets.push({
                        original_url: ext,
                        viewer_url: null,
                        filename_hint: ext.split('/').pop(),
                        content_type: data.type,
                        content: data.base64
                    });
                }
            }

            if (!hasExplicitPages) {
                if (nextPageNum && (!max_pages || nextPageNum <= max_pages) && !seenPages.has(nextPageNum)) {
                    queue.push(nextPageNum);
                }
            }
        }
    } catch (e) {
        console.warn("fetchAssetsForPage error", e);
    }
    return { assets };
}

function buildForumPageUrl(base, page) {
    if (page <= 1) return base;
    if (base.includes("page-")) {
        return base.replace(/page-\d+/, `page-${page}`);
    }
    if (base.includes("?")) {
        return `${base}&page=${page}`;
    }
    return `${base}page-${page}`;
}

async function fetchWithCookies(url, referer) {
    try {
        const resp = await fetch(url, {credentials: "include", headers: {"Referer": referer || url}});
        if (!resp.ok) return null;
        return await resp.text();
    } catch (e) {
        console.warn("fetchWithCookies failed", e);
        return null;
    }
}

async function fetchBinaryMaybeHtml(url, referer) {
    try {
        const attemptFetch = async (targetUrl) => {
            return await fetch(targetUrl, {
                credentials: "include",
                headers: {
                    "Referer": referer || targetUrl,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                },
                cache: "reload"
            });
        };

        let target = url;
        let resp = await attemptFetch(target);
        if (!resp.ok && resp.status === 409 && target.includes("?")) {
            target = target.split("?")[0];
            resp = await attemptFetch(target);
        }
        if (!resp.ok && resp.type === "opaqueredirect" && target.includes("?")) {
            target = target.split("?")[0];
            resp = await attemptFetch(target);
        }
        const ct = resp.headers.get("Content-Type") || "application/octet-stream";
        const allowOnError = ct.startsWith("image/");
        if (!resp.ok && !allowOnError) return null;
        if (ct.startsWith("text/") || ct.includes("html")) {
            const text = await resp.text();
            return {type: ct, base64: null, text, isHtml: true};
        }
        const buf = await resp.arrayBuffer();
        const bytes = new Uint8Array(buf);
        const chunk = 8192;
        let binary = "";
        for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        const base64 = btoa(binary);
        return {type: ct, base64, text: null, isHtml: false};
    } catch (e) {
        console.warn("fetchBinaryWithCookies failed", e);
        return null;
    }
}

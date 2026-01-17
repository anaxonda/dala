try {
    importScripts('chrome-shim.js');
} catch (e) {
    console.log("Shim load skipped (not in SW?)");
}

// Initialize
browser.runtime.onInstalled.addListener(() => {
    browser.storage.local.get("urlQueue").then((res) => {
        if (!res.urlQueue) browser.storage.local.set({ urlQueue: [] });
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
        } else if (info.menuItemId === "download-page") {
            const url = info.linkUrl || tab.url;
            await downloadSingleFromContext(url);
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
    } else if (message.action === "shortcut-queue") {
        if (sender && sender.tab && sender.tab.id) {
            lastShortcutTabId = sender.tab.id;
        }
        const target = message.url;
        if (target && target.startsWith("http")) {
            addToQueue(target);
        }
    }
});

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
        const url = URL.createObjectURL(blob);
        let dataUrl = null;

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
                console.log(`Attempting download: URL=${url}, Filename=${targetPath}`);
                await browser.downloads.download({
                    url: url,
                    filename: targetPath,
                    saveAs: false,
                    conflictAction: 'uniquify'
                });
                downloaded = true;
            } catch (e) {
                console.warn("downloads API failed with specific filename; retrying with generic name", e);
                try {
                     await browser.downloads.download({
                        url: url,
                        filename: "web_to_epub_export.epub",
                        saveAs: false,
                        conflictAction: 'uniquify'
                    });
                    downloaded = true;
                } catch (e2) {
                    console.warn("Generic filename failed; trying last resort (Data URI)", e2);
                    try {
                        // Last resort: Data URL (bypasses blob permission issues)
                        if (!dataUrl) dataUrl = await blobToDataURL(blob);
                        await browser.downloads.download({ 
                            url: dataUrl,
                            filename: "fallback.epub",
                            conflictAction: 'uniquify' 
                        });
                        downloaded = true;
                    } catch (e3) {
                        const msg = e3.message || JSON.stringify(e3);
                        console.error("All download attempts failed. Final error:", msg);
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

        // Delay revocation to allow download to start
        setTimeout(() => URL.revokeObjectURL(url), 10000);

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
        } catch (_) { } // ignore toast failures

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

// -- REPLACED: parseHtmlInOffscreen with Server-Side Parsing --
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
            
            // USE SERVER FOR PARSING
            const parseResult = await parseHtmlOnServer(html, url);
            const foundAssets = parseResult.assets || [];
            const externals = parseResult.externals || [];
            const nextPageNum = parseResult.next_page_num;

            console.log(`📎 Page ${page}: Found ${foundAssets.length} attachments, ${externals.length} externals via Server`);

            for (const att of foundAssets) {
                let fullData = null;
                // If viewer URL exists, fetch it and parse via server again? 
                // Wait, background.js logic did fetchBinaryMaybeHtml on viewer.
                if (att.viewer_url) {
                    const viewerResp = await fetchBinaryMaybeHtml(att.viewer_url, url);
                    if (viewerResp && viewerResp.type && !viewerResp.isHtml && viewerResp.base64) {
                        fullData = viewerResp;
                    } else if (viewerResp && viewerResp.text) {
                        // Viewer page HTML -> Parse via Server again? 
                        // Simplified: just try standard fetch on url if viewer fails
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
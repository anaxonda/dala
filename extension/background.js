// Initialize
browser.runtime.onInstalled.addListener(() => {
    browser.storage.local.get("urlQueue").then((res) => {
        if (!res.urlQueue) browser.storage.local.set({ urlQueue: [] });
        updateBadge();
    });

    // Some Firefox Android builds omit menus; guard to keep background alive
    const menus = browser.menus || browser.contextMenus;
    if (menus) {
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
    } else {
        console.warn("Context menus unavailable on this platform");
    }
});

// Context Menu Action
const menusApi = browser.menus || browser.contextMenus;
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

// Message Listener (From Popup)
browser.runtime.onMessage.addListener((message) => {
    if (message.action === "download") {
        processDownloadWithAssets(message.payload, message.isBundle);
        return true; // keep channel open for async work
    } else if (message.action === "cancel-download") {
        cancelDownload();
    } else if (message.action === "fetch-assets") {
        return fetchAssetsForPage(message.url, message.page_spec, message.max_pages);
    } else if (message.action === "shortcut-download") {
        const target = message.url;
        if (target && target.startsWith("http")) {
            downloadSingleFromContext(target);
        }
    } else if (message.action === "shortcut-queue") {
        const target = message.url;
        if (target && target.startsWith("http")) {
            addToQueue(target);
        }
    }
});

let currentController = null;

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
    const payload = {
        sources: [{ url, html: null, cookies: null, assets: [], is_forum }],
        bundle_title: null,
        no_comments: !!opts.no_comments,
        no_article: !!opts.no_article,
        no_images: !!opts.no_images,
        archive: !!opts.archive,
        max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        fetch_assets: false
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

// Helper to parse Content-Disposition header
function getFilenameFromHeader(header) {
    if (!header) return "download.epub";

    let filename = "download.epub";

    // Try standard filename="file.epub"
    let matches = /filename="([^"]*)"/.exec(header);
    if (matches && matches[1]) {
        filename = matches[1];
    } else {
        // Try filename=file.epub
        matches = /filename=([^;]*)/.exec(header);
        if (matches && matches[1]) {
            filename = matches[1].trim();
        }
    }

    // Try UTF-8 encoded filename*=utf-8''file.epub (Takes precedence if present)
    // RFC 5987
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

// The Main Download Logic
async function processDownloadCore(payload, isBundle) {
    if (currentController) {
        currentController.abort();
        currentController = null;
    }
    currentController = new AbortController();
    browser.browserAction.setBadgeText({ text: "..." });
    browser.browserAction.setBadgeBackgroundColor({ color: "#FFA500" }); // Orange

    try {
        const response = await fetch("http://127.0.0.1:8000/convert", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            signal: currentController.signal
        });

        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`Server ${response.status}: ${errText}`);
        }

        const blob = await response.blob();
        const url = URL.createObjectURL(blob);

        // Use Robust Header Parsing
        const filename = getFilenameFromHeader(response.headers.get('Content-Disposition'));
        const res = await browser.storage.local.get("savedOptions");
        const rawSub = (res.savedOptions && typeof res.savedOptions.subfolder === "string") ? res.savedOptions.subfolder.trim() : "";
        const cleanSub = rawSub.replace(/[/\\\\]+/g, '');
        const targetPath = cleanSub ? `${cleanSub}/${filename}` : filename;

        const canDownload = browser.downloads && typeof browser.downloads.download === "function";
        const isAndroid = /Android/i.test((navigator && navigator.userAgent) || "");
        let downloaded = false;

        if (canDownload) {
            try {
                await browser.downloads.download({
                    url: url,
                    filename: targetPath,
                    saveAs: false,
                    conflictAction: 'uniquify'
                });
                downloaded = true;
            } catch (e) {
                console.warn("downloads API failed; will open blob in new tab", e);
            }
        } else {
            console.warn("downloads API unavailable; will open blob in new tab");
        }

        if (!downloaded) {
            await openEpubInTab(blob, filename);
        } else if (isAndroid) {
            // Some Android builds acknowledge downloads but fail silently; also open tab as backup
            await openEpubInTab(blob, filename);
        }

        URL.revokeObjectURL(url);

        browser.browserAction.setBadgeText({ text: "OK" });
        browser.browserAction.setBadgeBackgroundColor({ color: "green" });

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

async function fetchAssetsForPage(threadUrl, page_spec, max_pages) {
    console.log("🔍 fetchAssetsForPage called with:", threadUrl);
    const assets = [];
    try {
        const normBase = threadUrl.replace(/#.*$/, "").replace(/\/page-\d+/i, "").replace(/([?&])page=\d+/i, "$1").replace(/[?&]$/, "");
        console.log("📍 Normalized base URL:", normBase);
        const currentPageMatch = threadUrl.match(/page-(\d+)/i) || threadUrl.match(/[?&]page=(\d+)/i);
        const currentPage = currentPageMatch ? parseInt(currentPageMatch[1], 10) : 1;
        const hasExplicitPages = page_spec && page_spec.length;
        const pages = hasExplicitPages ? page_spec : [1];
        const uniquePages = Array.from(new Set(pages.filter(p => p && p > 0))).sort((a, b) => a - b);
        const limiter = (arr, n) => arr.slice(0, n || arr.length);
        const pagesToFetch = limiter(uniquePages, max_pages || uniquePages.length);
        let debugViewerLogged = false;
        const seenPages = new Set();
        const queue = [...pagesToFetch];

        while (queue.length) {
            const page = queue.shift();
            if (seenPages.has(page)) continue;
            seenPages.add(page);
            const url = buildForumPageUrl(normBase, page);
            const html = await fetchWithCookies(url, threadUrl);
            if (!html) continue;
            const found = parseAttachmentsFromHtml(html, url);
            console.log(`📎 Page ${page}: Found ${found.length} attachments`);
            for (const att of found) {
                let fullData = null;

                if (att.viewer_url) {
                    const viewerResp = await fetchBinaryMaybeHtml(att.viewer_url, url);
                    if (!debugViewerLogged && viewerResp && viewerResp.text) {
                        console.log("DEBUG viewer HTML snippet", att.viewer_url, viewerResp.text.slice(0, 500));
                        debugViewerLogged = true;
                    }
                    if (viewerResp && viewerResp.type && !viewerResp.isHtml && viewerResp.base64) {
                        fullData = viewerResp;
                    } else if (viewerResp && viewerResp.text) {
                        const fullUrl = parseViewerForFullImage(viewerResp.text, att.viewer_url) || att.url;
                        fullData = await fetchBinaryMaybeHtml(fullUrl, att.viewer_url);
                    }
                }

                if (!fullData) {
                    fullData = await fetchBinaryMaybeHtml(att.url, url);
                }

                if (fullData && fullData.base64 && !fullData.isHtml) {
                    const canonical = att.url && att.url.includes("?") ? att.url.split("?")[0] : att.url;
                    assets.push({
                        original_url: att.url,
                        viewer_url: att.viewer_url || canonical,
                        canonical_url: canonical,
                        filename_hint: att.filename,
                        content_type: fullData.type,
                        content: fullData.base64
                    });
                }
            }

            // External images (non-attachment)
            const externals = parseExternalImages(html, url);
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
                const nextPage = findNextPage(html, page);
                if (nextPage && (!max_pages || nextPage <= max_pages) && !seenPages.has(nextPage)) {
                    queue.push(nextPage);
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

function parseAttachmentsFromHtml(html, baseUrl) {
    console.log("🔎 parseAttachmentsFromHtml called, HTML length:", html ? html.length : 0);
    const list = [];
    const seen = new Set();
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        // ONLY process images that are actual forum post content
        const messageImages = doc.querySelectorAll(".message-body img, .messageContent img, .bbWrapper img, .bbImage img");
        const containers = doc.querySelectorAll("article.message, .message--post, [data-lb-id]");
        console.log(`🖼️ Found ${messageImages.length} images in message bodies`);
        console.log(`📦 Found ${containers.length} message containers`);

        const processImg = (img) => {
            try {
                const src = img.getAttribute("data-src") || img.getAttribute("src");
                const srcset = img.getAttribute("data-srcset") || img.getAttribute("srcset");
                const dataUrl = img.getAttribute("data-url");

                // Skip avatars, reactions, smilies
                if (!src) return;
                const srcLower = src.toLowerCase();
                if (srcLower.includes("/avatar") ||
                    srcLower.includes("/reaction") ||
                    srcLower.includes("/smilies") ||
                    srcLower.includes("/emoji") ||
                    srcLower.startsWith("data:image/gif") ||
                    srcLower.includes("/d3/avatars/")) {
                    return;
                }

                let parentLink = null;
                if (typeof img.closest === "function") {
                    parentLink = img.closest('a');
                }
                const viewer = parentLink && parentLink.href ? new URL(parentLink.href, baseUrl).href : null;

                // Collect URL variants
                const urls = new Set();
                try { if (src) urls.add(new URL(src, baseUrl).href); } catch (e) {}
                try { if (dataUrl) urls.add(new URL(dataUrl, baseUrl).href); } catch (e) {}
                if (viewer) urls.add(viewer);
                if (img.dataset && img.dataset.lbPlaceholder) {
                    try { urls.add(new URL(img.dataset.lbPlaceholder, baseUrl).href); } catch (e) {}
                }

                // Parse srcset
                if (srcset) {
                    srcset.split(',').forEach(part => {
                        const url = part.trim().split(/\s+/)[0];
                        try {
                            urls.add(new URL(url, baseUrl).href);
                        } catch(e) {}
                    });
                }

                const canonical = viewer ? viewer.split("?")[0] : src.split("?")[0];
                const primary = urls.size ? Array.from(urls)[0] : (src ? new URL(src, baseUrl).href : null);
                if (!primary) return;
                // Only keep attachment-like URLs
                const hasAttachment = Array.from(urls).some(u => typeof u === "string" && u.includes("/attachments/"));
                if (!hasAttachment) return;
                const key = primary.split("#")[0].split("?")[0];
                if (seen.has(key)) return;
                seen.add(key);
                list.push({
                    url: primary,
                    viewer_url: viewer,
                    canonical_url: canonical,
                    all_urls: Array.from(urls),
                    filename: src.split('/').pop()
                });
            } catch (e) {
                console.warn("processImg failed", e);
            }
        };

        messageImages.forEach(img => processImg(img));
        // Fallbacks: lightbox/attachment wrappers
        const lbNodes = doc.querySelectorAll("[data-lb-trigger-target], [data-lb-id], [data-attachment-id], a.attachment, .bbImage, a[data-lb-src], a[data-lb-placeholder]");
        lbNodes.forEach(node => {
            const candSrc = node.getAttribute("data-src") || node.getAttribute("href") || node.getAttribute("data-lb-src") || node.getAttribute("data-lb-placeholder");
            if (candSrc) {
                const fakeImg = { getAttribute: (k) => k === "src" ? candSrc : null, closest: () => null, dataset: node.dataset || {} };
                processImg(fakeImg);
            }
        });
        containers.forEach(container => {
            const imgs = container.querySelectorAll("img");
            console.log(`  Container has ${imgs.length} images`);
            imgs.forEach(img => {
                console.log("    Image src:", img.getAttribute("src") || img.getAttribute("data-src") || "");
                processImg(img);
            });
        });

        console.log(`Found ${list.length} forum post images`);
    } catch (e) {
        console.warn("parseAttachmentsFromHtml failed", e);
    }
    return list;
}

function parseViewerForFullImage(html, baseUrl) {
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const og = doc.querySelector("meta[property='og:image']");
        if (og && og.content) return new URL(og.content, baseUrl).href;
        const img = doc.querySelector("img");
        if (img) {
            const dataUrl = img.getAttribute("data-url");
            if (dataUrl) return new URL(dataUrl, baseUrl).href;
            const srcset = img.getAttribute("data-srcset") || img.getAttribute("srcset");
            if (srcset) {
                const best = pickLargestFromSrcset(srcset, baseUrl);
                if (best) return best;
            }
            if (img.src) return new URL(img.src, baseUrl).href;
        }
    } catch (e) {
        console.warn("parseViewerForFullImage failed", e);
    }
    return null;
}

function parseExternalImages(html, baseUrl) {
    const urls = new Set();
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const imgs = doc.querySelectorAll(".message-body img");
        imgs.forEach(img => {
            const src = img.getAttribute("data-src") || img.getAttribute("src");
            if (!src) return;
            if (src.startsWith('data:')) return;
            try { urls.add(new URL(src, baseUrl).href); } catch(e) {}
        });
    } catch (e) {
        console.warn("parseExternalImages failed", e);
    }
    return Array.from(urls);
}

function pickLargestFromSrcset(srcset, baseUrl) {
    const parts = srcset.split(',').map(p => p.trim()).filter(Boolean);
    let best = null;
    let maxw = -1;
    for (const p of parts) {
        const [u, w] = p.split(/\s+/);
        let width = parseInt((w || '').replace('w',''), 10);
        if (isNaN(width)) width = 0;
        if (width > maxw) {
            maxw = width;
            try { best = new URL(u, baseUrl).href; } catch(e) { best = null; }
        }
    }
    return best;
}

function findNextPage(html, currentPage) {
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const link = doc.querySelector("link[rel='next']");
        if (link && link.href) {
            const m = link.href.match(/page-(\d+)/i) || link.href.match(/[?&]page=(\d+)/i);
            if (m) return parseInt(m[1], 10);
        }
        const anchors = Array.from(doc.querySelectorAll("a"));
        for (const a of anchors) {
            const txt = (a.textContent || "").trim().toLowerCase();
            if (txt === "next" || txt === "next >" || txt === "next>") {
                const m = a.href && (a.href.match(/page-(\d+)/i) || a.href.match(/[?&]page=(\d+)/i));
                if (m) return parseInt(m[1], 10);
            }
            if (txt === String(currentPage + 1)) {
                const m = a.href && (a.href.match(/page-(\d+)/i) || a.href.match(/[?&]page=(\d+)/i));
                if (m) return parseInt(m[1], 10);
                return currentPage + 1;
            }
        }
    } catch (e) {
        console.warn("findNextPage failed", e);
    }
    return null;
}

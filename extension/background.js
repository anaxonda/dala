// Initialize
browser.runtime.onInstalled.addListener(() => {
    browser.storage.local.get("urlQueue").then((res) => {
        if (!res.urlQueue) browser.storage.local.set({ urlQueue: [] });
        updateBadge();
    });

    browser.menus.create({
        id: "add-to-queue",
        title: "Add to EPUB Queue",
        contexts: ["page", "link"]
    });
});

// Context Menu Action
browser.menus.onClicked.addListener(async (info, tab) => {
    if (info.menuItemId === "add-to-queue") {
        const url = info.linkUrl || tab.url;
        await addToQueue(url);
    }
});

// Message Listener (From Popup)
browser.runtime.onMessage.addListener((message) => {
    if (message.action === "download") {
        processDownload(message.payload, message.isBundle);
    } else if (message.action === "cancel-download") {
        cancelDownload();
    } else if (message.action === "fetch-assets") {
        return fetchAssetsForPage(message.url, message.page_spec, message.max_pages);
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

// The Main Download Logic
async function processDownload(payload, isBundle) {
    if (currentController) {
        currentController.abort();
        currentController = null;
    }
    currentController = new AbortController();
    browser.browserAction.setBadgeText({ text: "..." });
    browser.browserAction.setBadgeBackgroundColor({ color: "#FFA500" }); // Orange

    try {
        // Background asset fetch if requested
        if (payload.fetch_assets && payload.sources) {
            for (const src of payload.sources) {
                if (!src.is_forum) continue;
                if (src.assets && src.assets.length) continue;
                const res = await fetchAssetsForPage(src.url, payload.page_spec, payload.max_pages);
                if (res && res.assets) {
                    src.assets = res.assets;
                }
            }
        }

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

        await browser.downloads.download({
            url: url,
            filename: "WebToEpub/" + filename,
            saveAs: false,
            conflictAction: 'uniquify'
        });

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
    const assets = [];
    try {
        const normBase = threadUrl.replace(/\/page-\d+/i, "").replace(/([?&])page=\d+/i, "$1").replace(/[?&]$/, "");
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
            console.debug("attachments found", found.slice(0, 3).map(x => x.url));
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
        let target = url;
        let resp = await fetch(target, {
            credentials: "include",
            headers: {
                "Referer": referer || target,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
            },
            cache: "no-store"
        });
        if (!resp.ok && resp.status === 409 && target.includes("?")) {
            target = target.split("?")[0];
            resp = await fetch(target, {
                credentials: "include",
                headers: {
                    "Referer": referer || target,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                },
                cache: "no-store"
            });
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
    const list = [];
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const anchors = doc.querySelectorAll("a[href*='/attachments/']");
        anchors.forEach(a => {
            const viewer = a.href ? new URL(a.href, baseUrl).href : null;
            const img = a.querySelector('img');
            const src = img ? (img.getAttribute("data-src") || img.getAttribute("src")) : null;
            const srcset = img ? (img.getAttribute("data-srcset") || img.getAttribute("srcset")) : null;
            let candidate = src;
            if (srcset) {
                const best = pickLargestFromSrcset(srcset, baseUrl);
                if (best) candidate = best;
            }
            if (!viewer && !src) return;
            const absSrc = candidate ? new URL(candidate, baseUrl).href : viewer;
            if (!absSrc.startsWith("http")) return;
            const viewerClean = viewer ? viewer.split("?")[0] : null;
            list.push({url: absSrc, viewer_url: viewerClean || viewer, filename: (absSrc || '').split('/').pop()});
        });

        // Fallback: images with attachment src/data-src even without anchors
        const imgs = doc.querySelectorAll("img");
        imgs.forEach(img => {
            const src = img.getAttribute("data-src") || img.getAttribute("src");
            const srcset = img.getAttribute("data-srcset") || img.getAttribute("srcset");
            let candidate = src;
            if (srcset) {
                const best = pickLargestFromSrcset(srcset, baseUrl);
                if (best) candidate = best;
            }
            if (!candidate || !candidate.includes("/attachments/")) return;
            try {
                const absSrc = new URL(candidate, baseUrl).href;
                if (!absSrc.startsWith("http")) return;
                const viewerClean = absSrc.includes("?") ? absSrc.split("?")[0] : null;
                list.push({url: absSrc, viewer_url: viewerClean, filename: (absSrc || '').split('/').pop()});
            } catch (_) {}
        });

        // Fallback: elements with data-src pointing at attachments (e.g., lightbox containers)
        const dataSrcNodes = doc.querySelectorAll("[data-src*='/attachments/']");
        dataSrcNodes.forEach(node => {
            const ds = node.getAttribute("data-src");
            if (!ds) return;
            try {
                const absSrc = new URL(ds, baseUrl).href;
                if (!absSrc.startsWith("http")) return;
                const viewerClean = absSrc.includes("?") ? absSrc.split("?")[0] : null;
                list.push({url: absSrc, viewer_url: viewerClean, filename: (absSrc || '').split('/').pop()});
            } catch (_) {}
        });
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

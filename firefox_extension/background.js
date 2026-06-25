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

    // Some Firefox Android builds omit menus; guard to keep background alive
    const menus = browser.menus || browser.contextMenus;
    if (menus) {
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
            const tabIdForAssets = tab && tab.url === url ? tab.id : null;
            await downloadSingleFromContext(url, tabIdForAssets);
        }
    });
}

// Command Listener (Native Shortcuts)
if (browser.commands && browser.commands.onCommand) {
    browser.commands.onCommand.addListener(async (command) => {
        const tabs = await browser.tabs.query({ active: true, currentWindow: true });
        const tab = tabs[0];
        if (!tab || !tab.url || !tab.url.startsWith("http")) return;

        const showNativeToast = async (tabId, text) => {
            try {
                // Try sending message first (cleaner)
                await browser.tabs.sendMessage(tabId, { action: "shortcut-toast", message: text });
            } catch (e) {
                // Fallback to injection
                browser.tabs.executeScript(tabId, {
                    code: `
                    (() => {
                        const existing = document.getElementById("epub-shortcut-toast");
                        if (existing) existing.remove();
                        const el = document.createElement("div");
                        el.id = "epub-shortcut-toast";
                        el.textContent = "${text}";
                        el.style.cssText = "position:fixed;top:16px;right:16px;background:#4CAF50;color:white;padding:10px 14px;border-radius:4px;z-index:2147483647;font-size:13px;box-shadow:0 2px 6px rgba(0,0,0,0.25);";
                        document.body.appendChild(el);
                        setTimeout(() => { el.remove(); }, 2500);
                    })();`
                }).catch(() => {});
            }
        };

        if (command === "download-page") {
            lastShortcutTabId = tab.id;
            showNativeToast(tab.id, "Starting EPUB download…");
            // Native command doesn't have HTML, so we grab it if possible or fallback
            let html = null;
            try {
                const results = await browser.tabs.executeScript(tab.id, { code: "document.documentElement.outerHTML;" });
                if (results && results[0]) html = results[0];
            } catch (e) { /* fallback to no-html fetch */ }
            downloadFromShortcut(tab.url, html, tab.id);
        } else if (command === "add-to-queue") {
            await addToQueue(tab.url);
            showNativeToast(tab.id, "Added to EPUB queue");
        }
    });
}

let currentController = null;
let currentJobId = null;
let currentRunToken = null;
let lastShortcutTabId = null;
const DEFAULT_SERVER_URL = "http://127.0.0.1:8000";
let lastArticleAssetDebug = null;


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

function includeCookiesForSavedOptions(opts) {
    const saved = opts || {};
    if (saved.forum) return true;
    if (saved.include_cookies_user_set === true) {
        return !!saved.include_cookies;
    }
    if (saved.include_cookies_user_set !== false && Object.prototype.hasOwnProperty.call(saved, "include_cookies")) {
        return !!saved.include_cookies;
    }
    return isLocalServerUrl(saved.server_url);
}

async function getServerBaseUrl() {
    const res = await browser.storage.local.get("savedOptions");
    return normalizeServerUrl(res.savedOptions && res.savedOptions.server_url);
}
const pendingBlobUrls = new Map();

function pdfPresetForPageSize(pageSize) {
    return pageSize === "kobo_clara" ? "ereader" : "document";
}

function dateOptionsFromSaved(opts) {
    const startDate = (opts.start_date || "").trim();
    const endDate = (opts.end_date || "").trim();
    const startBound = parseDateBound(startDate, false);
    const endBound = parseDateBound(endDate, true);
    return {
        start_date: startBound ? startDate : null,
        end_date: endBound && (!startBound || startBound <= endBound) ? endDate : null,
        date_fallback: opts.date_fallback || "auto",
        include_undated: false
    };
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

function responseFormatFrom(filename, contentType) {
    const lowerName = (filename || "").toLowerCase();
    const lowerType = (contentType || "").toLowerCase();
    if (lowerName.endsWith(".pdf") || lowerType.includes("application/pdf")) return "pdf";
    if (lowerName.endsWith(".epub") || lowerType.includes("application/epub+zip")) return "epub";
    return null;
}

function validateResponseFormat(payload, filename, contentType) {
    const expected = (payload && payload.output_format) || "epub";
    const actual = responseFormatFrom(filename, contentType);
    if (!actual || actual === expected) return;
    throw new Error(`Requested ${expected.toUpperCase()} but server returned ${actual.toUpperCase()}. Restart or update the Dala server and try again.`);
}

function trackBlobUrlForDownload(downloadId, blobUrl) {
    if (typeof downloadId !== "number" || !blobUrl) return;
    const existing = pendingBlobUrls.get(downloadId);
    if (existing && existing.cleanupTimer) {
        clearTimeout(existing.cleanupTimer);
    }
    const cleanupTimer = setTimeout(() => {
        const tracked = pendingBlobUrls.get(downloadId);
        if (tracked && tracked.url) {
            try { URL.revokeObjectURL(tracked.url); } catch (_) {}
            pendingBlobUrls.delete(downloadId);
        }
    }, 10 * 60 * 1000); // Hard cleanup if browser never emits completion event.
    pendingBlobUrls.set(downloadId, { url: blobUrl, cleanupTimer });
}

function releaseBlobUrlForDownload(downloadId) {
    const tracked = pendingBlobUrls.get(downloadId);
    if (!tracked) return;
    if (tracked.cleanupTimer) clearTimeout(tracked.cleanupTimer);
    try { URL.revokeObjectURL(tracked.url); } catch (_) {}
    pendingBlobUrls.delete(downloadId);
}

if (browser.downloads && browser.downloads.onChanged) {
    browser.downloads.onChanged.addListener((delta) => {
        if (!delta || typeof delta.id !== "number" || !delta.state || !delta.state.current) return;
        if (delta.state.current === "complete" || delta.state.current === "interrupted") {
            releaseBlobUrlForDownload(delta.id);
        }
    });
}

// Message Listener (From Popup/Content)
browser.runtime.onMessage.addListener((message, sender) => {
    if (message.action === "download") {
        // Legacy/Direct download calls
        processDownloadWithAssets(message.payload, message.isBundle);
        return true; 
    } else if (message.action === "init_download") {
        preparePayloadFromBackground(message.urls, message.title, message.isBundle)
            .then(async payload => {
                await clearSavedDateOptions();
                return processDownloadWithAssets(payload, message.isBundle);
            })
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
        let tabId = null;
        if (sender && sender.tab && sender.tab.id) {
            lastShortcutTabId = sender.tab.id;
            tabId = sender.tab.id;
        }
        const target = message.url;
        if (target && target.startsWith("http")) {
            // Trigger download asynchronously; resolve immediately to show "Starting..." toast
            downloadFromShortcut(target, message.html || null, tabId || lastShortcutTabId);
        }
        return Promise.resolve(); 
    } else if (message.action === "shortcut-queue") {
        if (sender && sender.tab && sender.tab.id) {
            lastShortcutTabId = sender.tab.id;
        }
        const target = message.url;
        if (target && target.startsWith("http")) {
            // Return the promise so we wait for storage update before ack
            return addToQueue(target);
        }
        return Promise.resolve();
    }
});

function browserFallbackOptionsFromSaved(savedOpts) {
    return {
        browser_fallback: savedOpts.browser_fallback !== false,
        browser_challenge_action: savedOpts.browser_challenge_action === "user_browser" ? "user_browser" : "archive",
        browser_extension_path: (savedOpts.browser_extension_path || "").trim() || null,
        browser_profile_dir: (savedOpts.browser_profile_dir || "").trim() || null,
        browser_executable: (savedOpts.browser_executable || "").trim() || null
    };
}

function translationOptionsFromSaved(savedOpts) {
    const target = (savedOpts.translation_target_lang || "").trim();
    return {
        translation_enabled: !!target && !!savedOpts.translation_enabled,
        translation_provider: savedOpts.translation_provider || "llm",
        translation_target_lang: target || null,
        translation_source_lang: (savedOpts.translation_source_lang || "").trim() || "auto",
        translation_display: savedOpts.translation_display || "underneath",
        translation_scope: savedOpts.translation_scope || "article-captions",
        translation_glossary: (savedOpts.translation_glossary || "").trim() || null,
        translation_cache: savedOpts.translation_cache !== false,
        llm_provider: savedOpts.llm_provider || "auto",
        llm_model: (savedOpts.llm_model || "").trim() || null,
        llm_api_key: (savedOpts.llm_api_key || "").trim() || null
    };
}

async function clearSavedDateOptions() {
    const res = await browser.storage.local.get("savedOptions");
    const existing = res.savedOptions || {};
    if (!existing.start_date && !existing.end_date) return;
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

async function preparePayloadFromBackground(urls, bundleTitle, isBundle) {
    browser.browserAction.setBadgeText({ text: "PREP" });
    browser.browserAction.setBadgeBackgroundColor({ color: "#FFA500" });

    const savedOpts = (await browser.storage.local.get("savedOptions")).savedOptions || {};
    
    // Default options if not saved
    const options = {
        no_comments: !!savedOpts.no_comments,
        no_article: !!savedOpts.no_article,
        no_images: !!savedOpts.no_images,
        archive: !!savedOpts.archive,
        summary: !!savedOpts.summary,
        thumbnails: !!savedOpts.thumbnails,
        output_format: savedOpts.output_format || "epub",
        pdf_preset: pdfPresetForPageSize(savedOpts.pdf_page_size || "letter"),
        pdf_page_size: savedOpts.pdf_page_size || "letter",
        image_preset: savedOpts.image_preset || "balanced",
        image_color: savedOpts.image_color || "color",
        ...translationOptionsFromSaved(savedOpts),
        ...dateOptionsFromSaved(savedOpts),
        youtube_lang: (savedOpts.youtube_lang || "").trim() || "en",
        youtube_prefer_auto: !!savedOpts.youtube_prefer_auto,
        youtube_max_comments: savedOpts.youtube_max_comments || 25,
        youtube_comment_sort: savedOpts.youtube_comment_sort || "top",
        include_cookies: includeCookiesForSavedOptions(savedOpts),
        forum: !!savedOpts.forum,
        pages: (savedOpts.pages || "").trim(),
        max_pages: savedOpts.max_pages ? parseInt(savedOpts.max_pages, 10) || null : null
    };

    const sources = [];
    const page_spec = parsePageSpecInput(options.pages);
    const max_pages = options.max_pages;
    const forceForum = options.forum;
    const shouldFetchAssets = forceForum || urls.some(url => isLikelyForumUrl(url));
    if (forceForum && !options.include_cookies) {
        console.log("Forum mode enabled: using browser cookies automatically.");
    }

    // 1. Get all tabs to check for matches
    let allTabs = [];
    try {
        allTabs = await browser.tabs.query({});
    } catch(e) {
        console.warn("Cannot query tabs for HTML injection");
    }

    // 2. Iterate requested URLs
    for (const url of urls) {
        console.log(`Processing payload for: ${url}`);
        let html = null;
        // Find a tab that matches this URL and is fully loaded
        const match = allTabs.find(t => t.url === url);

        if (match) {
            try {
                // If tab is loading, wait a bit (simplified wait)
                if (match.status !== "complete") {
                   await new Promise(r => setTimeout(r, 1000));
                }
                
                // Inject script to steal DOM
                console.log(`Injecting script into tab ${match.id}...`);
                const results = await browser.tabs.executeScript(match.id, {
                    code: "document.documentElement.outerHTML;"
                });

                if (results && results[0]) {
                    html = results[0];
                    console.log("Injecting HTML for:", url);
                }
            } catch (e) {
                console.log("Could not grab HTML (using fallback):", url, e);
            }
        }
        
        const is_forum = forceForum || isLikelyForumUrl(url) || isLikelyForumHtml(html);
        const include_cookies = options.include_cookies || is_forum;
        if (is_forum && !forceForum) {
            console.log(`Forum auto-detected for ${url}`);
        }
        if (is_forum && !options.include_cookies) {
            console.log(`Forum download using browser cookies automatically for ${url}`);
        }
        let cookies = null;
        let assets = [];
        if (include_cookies) {
            cookies = await getCookiesForUrl(url);
            if (is_forum && match) {
                try {
                    assets = await scrapeAssetsFromTab(match.id, url);
                    console.log(`Scraped ${assets.length} assets from DOM for ${url}`);
                } catch (e) {
                    console.warn("DOM asset scrape failed", e);
                }
            } else if (match) {
                try {
                    assets = await scrapeArticleAssetsFromTab(match.id, url, html);
                    console.log(`Scraped ${assets.length} article image assets from browser for ${url}`);
                } catch (e) {
                    console.warn("Article asset scrape failed", e);
                }
            }
        }
        sources.push({ url: url, html: html, cookies: cookies, assets: assets, asset_debug: { entry: "popup", asset_count: assets.length, ...(lastArticleAssetDebug || {}) }, is_forum: is_forum });
    }
    
    return {
        sources: sources,
        bundle_title: bundleTitle,
        no_comments: options.no_comments,
        no_article: options.no_article,
        no_images: options.no_images,
        archive: options.archive,
        summary: options.summary,
        thumbnails: options.thumbnails,
        output_format: options.output_format || "epub",
        pdf_preset: pdfPresetForPageSize(options.pdf_page_size || "letter"),
        pdf_page_size: options.pdf_page_size || "letter",
        image_preset: options.image_preset || "balanced",
        image_color: options.image_color || "color",
        ...translationOptionsFromSaved(savedOpts),
        start_date: options.start_date,
        end_date: options.end_date,
        date_fallback: options.date_fallback,
        include_undated: options.include_undated,
        youtube_lang: options.youtube_lang,
        youtube_prefer_auto: options.youtube_prefer_auto,
        youtube_max_comments: options.youtube_max_comments,
        youtube_comment_sort: options.youtube_comment_sort,
        max_pages: max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        ...browserFallbackOptionsFromSaved(savedOpts),
        fetch_assets: shouldFetchAssets,
        server_save_dir: (savedOpts.save_folder || savedOpts.server_save_dir || savedOpts.termux_copy_dir || "").trim() || null,
        archive_server: !!savedOpts.archive_server,
        termux_copy_dir: (savedOpts.termux_copy_dir || "").trim() || null,
        llm_format: !!savedOpts.llm_format,
        llm_model: (savedOpts.llm_model || "").trim() || null,
        llm_api_key: (savedOpts.llm_api_key || "").trim() || null
    };
}

async function scrapeAssetsFromTab(tabId, refererUrl) {
    try {
        const results = await browser.tabs.executeScript(tabId, {
            code: `(() => {
                const imgs = Array.from(document.querySelectorAll('.message-body img'));
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
                if (best && best.url) assets.push(best);
            }
        }
        return assets;
    } catch (e) {
        console.warn('scrapeAssetsFromTab failed', e);
        return [];
    }
}

async function scrapeArticleAssetsFromTab(tabId, refererUrl, html) {
    lastArticleAssetDebug = null;
    const debug = { dom_records: 0, parsed_assets: 0, parsed_externals: 0, candidates: 0, fetched: 0 };
    try {
        let results = null;
        try {
            results = await browser.tabs.executeScript(tabId, {
                code: `(() => {
                    const root = document.querySelector('article, main, [role="main"], .article-body, .article-content, .entry-content, #content') || document.body;
                    const records = [];
                    const readImg = (img) => {
                        const srcset = img.getAttribute('data-srcset') || img.getAttribute('srcset');
                        const dataUrl = img.getAttribute('data-url') || img.getAttribute('data-original');
                        const src = img.currentSrc || img.getAttribute('data-src') || img.getAttribute('src');
                        const alt = img.getAttribute('alt') || "";
                        const a = img.closest('a');
                        const viewer = a && a.href ? a.href : null;
                        const width = img.naturalWidth || img.width || 0;
                        const height = img.naturalHeight || img.height || 0;
                        return {src, srcset, dataUrl, viewer, alt, width, height};
                    };
                    const readSvg = (svg) => {
                        const src = svg.getAttribute('data-inject-url') || svg.getAttribute('data-src') || svg.getAttribute('src');
                        const fig = svg.closest('figure');
                        const alt = svg.getAttribute('aria-label') || svg.getAttribute('data-name') || (fig && fig.innerText ? fig.innerText.slice(0, 240) : "Article graphic");
                        const a = svg.closest('a');
                        const viewer = a && a.href ? a.href : null;
                        const box = svg.getBoundingClientRect ? svg.getBoundingClientRect() : {width: 0, height: 0};
                        const width = Math.round(box.width || Number(svg.getAttribute('width')) || 9999);
                        const height = Math.round(box.height || Number(svg.getAttribute('height')) || 9999);
                        return {src, srcset: null, dataUrl: null, viewer, alt, width, height};
                    };
                    for (const img of Array.from(root.querySelectorAll('picture img, figure img, img')).slice(0, 100)) {
                        records.push(readImg(img));
                    }
                    for (const svg of Array.from(root.querySelectorAll('figure svg[data-inject-url], figure svg[data-src], figure svg[src], svg[data-inject-url], svg[data-src], svg[src]')).slice(0, 40)) {
                        records.push(readSvg(svg));
                    }
                    for (const meta of document.querySelectorAll("meta[property='og:image'], meta[property='og:image:url'], meta[name='twitter:image'], meta[name='twitter:image:src']")) {
                        const src = meta.getAttribute("content");
                        if (src) records.push({src, srcset: null, dataUrl: null, viewer: null, alt: "Article image", width: 9999, height: 9999});
                    }
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_COMMENT);
                    let node = null;
                    while ((node = walker.nextNode())) {
                        const text = node.nodeValue || "";
                        if (!text.toLowerCase().includes("<img")) continue;
                        const box = document.createElement("div");
                        box.innerHTML = text;
                        for (const img of Array.from(box.querySelectorAll("img"))) {
                            records.push(readImg(img));
                        }
                    }
                    return records.slice(0, 140);
                })();`
            });
        } catch (e) {
            console.warn("Article DOM image scrape failed; falling back to captured HTML", e);
        }
        const records = results && results[0] ? results[0] : [];
        debug.dom_records = records.length;
        const picked = [];
        const seen = new Set();
        const addPicked = (item) => {
            const url = item && (item.url || item.original_url);
            if (!url || seen.has(url)) return;
            seen.add(url);
            picked.push({
                url,
                original_url: url,
                viewer_url: item.viewer_url || null,
                filename_hint: item.filename_hint || url.split("/").pop()
            });
        };
        for (const rec of records) {
            if (isLowValueArticleImage(rec)) continue;
            const best = pickBestImageCandidate(rec, refererUrl);
            if (best) addPicked(best);
        }
        if (html) {
            const parsed = await parseHtmlOnServer(html, refererUrl);
            debug.parsed_assets = (parsed.assets || []).length;
            debug.parsed_externals = (parsed.externals || []).length;
            for (const asset of (parsed.assets || [])) {
                addPicked({url: asset.url, viewer_url: asset.viewer_url, filename_hint: asset.filename_hint});
            }
            for (const url of (parsed.externals || [])) {
                addPicked({url});
            }
        }
        debug.candidates = picked.length;
        const fetched = await mapWithConcurrency(
            picked.slice(0, 120).map(item => async () => {
                const data = await fetchBinaryMaybeHtml(item.url, refererUrl);
                if (!data || !data.base64 || data.isHtml) return null;
                return {
                    original_url: item.url,
                    viewer_url: item.viewer_url || null,
                    canonical_url: item.url.split("?")[0],
                    filename_hint: item.url.split("/").pop(),
                    content_type: data.type,
                    content: data.base64
                };
            }),
            4
        );
        const assets = fetched.filter(Boolean);
        debug.fetched = assets.length;
        lastArticleAssetDebug = debug;
        return assets;
    } catch (e) {
        console.warn("scrapeArticleAssetsFromTab failed", e);
        lastArticleAssetDebug = debug;
        return [];
    }
}

function isLowValueArticleImage(rec) {
    const url = String((rec && (rec.src || rec.dataUrl)) || "").toLowerCase();
    const alt = String((rec && rec.alt) || "").toLowerCase();
    const width = Number((rec && rec.width) || 0);
    const height = Number((rec && rec.height) || 0);
    if (!url || url.startsWith("data:")) return true;
    if (url.includes("/avatar") || url.includes("author") || url.includes("logo") || url.includes("sprite")) return true;
    if (alt === "logo" || alt === "avatar" || (alt.length <= 24 && (alt.includes("logo") || alt.includes("avatar")))) return true;
    if (width && height && Math.max(width, height) < 120) return true;
    return false;
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
    return {original_url: best, url: best, viewer_url: rec.viewer || null}; // Match expected format
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

async function collectBrowserContextForDownload(url, tabId, html, is_forum, include_cookies, label) {
    let cookies = null;
    let assets = [];
    const shouldScrapeArticleAssets = !!tabId && !is_forum;
    if (!include_cookies && !shouldScrapeArticleAssets) {
        return { cookies, assets };
    }
    if (is_forum && !label.includes("explicit-cookies")) {
        console.log(`${label}: using browser cookies automatically.`);
    }
    if (include_cookies) {
        cookies = await getCookiesForUrl(url);
    }
    if (tabId) {
        try {
            assets = is_forum
                ? await scrapeAssetsFromTab(tabId, url)
                : await scrapeArticleAssetsFromTab(tabId, url, html);
            console.log(`${label}: scraped ${assets.length} browser assets for ${url}`);
        } catch (e) {
            console.warn(`${label}: browser asset scrape failed`, e);
        }
    }
    return { cookies, assets };
}

async function downloadFromShortcut(url, html, tabId = null) {
    if (!url || !url.startsWith("http")) return;
    const optsRes = await browser.storage.local.get("savedOptions");
    const opts = optsRes.savedOptions || {};
    const page_spec = parsePageSpecInput(opts.pages);
    const max_pages = opts.max_pages ? parseInt(opts.max_pages, 10) || null : null;
    const is_forum = !!opts.forum || isLikelyForumUrl(url) || isLikelyForumHtml(html);
    const server_save_dir = (opts.save_folder || opts.server_save_dir || opts.termux_copy_dir || "").trim() || null;
    
    const include_cookies = includeCookiesForSavedOptions(opts) || is_forum;
    const { cookies, assets } = await collectBrowserContextForDownload(
        url,
        tabId,
        html,
        is_forum,
        include_cookies,
        "Shortcut download"
    );

    const payload = {
        sources: [{ url, html: html || null, cookies: cookies, assets: assets, asset_debug: { entry: "shortcut", asset_count: assets.length, ...(lastArticleAssetDebug || {}) }, is_forum }],
        bundle_title: null,
        no_comments: !!opts.no_comments,
        no_article: !!opts.no_article,
        no_images: !!opts.no_images,
        archive: !!opts.archive,
        summary: !!opts.summary,
        thumbnails: !!opts.thumbnails,
        output_format: opts.output_format || "epub",
        pdf_preset: pdfPresetForPageSize(opts.pdf_page_size || "letter"),
        pdf_page_size: opts.pdf_page_size || "letter",
        image_preset: opts.image_preset || "balanced",
        image_color: opts.image_color || "color",
        ...translationOptionsFromSaved(opts),
        ...dateOptionsFromSaved(opts),
        youtube_lang: (opts.youtube_lang || "").trim() || "en",
        youtube_prefer_auto: !!opts.youtube_prefer_auto,
        youtube_max_comments: opts.youtube_max_comments || 25,
        youtube_comment_sort: opts.youtube_comment_sort || "top",
        max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        ...browserFallbackOptionsFromSaved(opts),
        fetch_assets: is_forum,
        server_save_dir,
        archive_server: !!opts.archive_server,
        termux_copy_dir: server_save_dir, // Legacy
        llm_format: !!opts.llm_format,
        llm_model: (opts.llm_model || "").trim() || null,
        llm_api_key: (opts.llm_api_key || "").trim() || null
    };
    await clearSavedDateOptions();
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

async function downloadSingleFromContext(url, tabId = null) {
    if (!url || !url.startsWith("http")) return;
    const optsRes = await browser.storage.local.get("savedOptions");
    const opts = optsRes.savedOptions || {};
    const page_spec = parsePageSpecInput(opts.pages);
    const max_pages = opts.max_pages ? parseInt(opts.max_pages, 10) || null : null;
    const is_forum = !!opts.forum || isLikelyForumUrl(url);
    const server_save_dir = (opts.save_folder || opts.server_save_dir || opts.termux_copy_dir || "").trim() || null;
    
    const include_cookies = includeCookiesForSavedOptions(opts) || is_forum;
    const { cookies, assets } = await collectBrowserContextForDownload(
        url,
        tabId,
        null,
        is_forum,
        include_cookies,
        "Context download"
    );

    const payload = {
        sources: [{ url, html: null, cookies: cookies, assets: assets, asset_debug: { entry: "context", asset_count: assets.length, ...(lastArticleAssetDebug || {}) }, is_forum }],
        bundle_title: null,
        no_comments: !!opts.no_comments,
        no_article: !!opts.no_article,
        no_images: !!opts.no_images,
        archive: !!opts.archive,
        summary: !!opts.summary,
        thumbnails: !!opts.thumbnails,
        output_format: opts.output_format || "epub",
        pdf_preset: pdfPresetForPageSize(opts.pdf_page_size || "letter"),
        pdf_page_size: opts.pdf_page_size || "letter",
        image_preset: opts.image_preset || "balanced",
        image_color: opts.image_color || "color",
        ...translationOptionsFromSaved(opts),
        ...dateOptionsFromSaved(opts),
        youtube_lang: (opts.youtube_lang || "").trim() || "en",
        youtube_prefer_auto: !!opts.youtube_prefer_auto,
        youtube_max_comments: opts.youtube_max_comments || 25,
        youtube_comment_sort: opts.youtube_comment_sort || "top",
        max_pages,
        page_spec: page_spec && page_spec.length ? page_spec : null,
        ...browserFallbackOptionsFromSaved(opts),
        fetch_assets: is_forum,
        server_save_dir,
        archive_server: !!opts.archive_server,
        termux_copy_dir: server_save_dir, // Legacy
        llm_format: !!opts.llm_format,
        llm_model: (opts.llm_model || "").trim() || null,
        llm_api_key: (opts.llm_api_key || "").trim() || null
    };
    await clearSavedDateOptions();
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

async function openOutputInTab(blob, filename) {
    // Prefer blob URL for large files; data URIs can exceed browser URL limits.
    try {
        const blobUrl = URL.createObjectURL(blob);
        await browser.tabs.create({ url: blobUrl });
        setTimeout(() => {
            try { URL.revokeObjectURL(blobUrl); } catch (_) {}
        }, 10 * 60 * 1000);
        console.warn(`Opened output blob URL in tab for manual save: ${filename}`);
        return;
    } catch (e) {
        console.warn("Blob URL tab open failed; trying data URI fallback", e);
    }

    try {
        const buffer = await blob.arrayBuffer();
        const bytes = new Uint8Array(buffer);
        const chunkSize = 8192;
        let binary = "";
        for (let i = 0; i < bytes.length; i += chunkSize) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
        }
        const base64 = btoa(binary);
        const mediaType = filename.toLowerCase().endsWith(".pdf") ? "application/pdf" : "application/epub+zip";
        const dataUrl = `data:${mediaType};base64,${base64}`;
        await browser.tabs.create({ url: dataUrl });
        console.warn(`Opened output in tab for manual save: ${filename}`);
    } catch (e) {
        console.error("Failed to open output blob in tab", e);
    }
}

function createRequestToken() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function recoverServerSavedSuccess(payload, isBundle) {
    if (!payload || !payload.request_token) return false;
    try {
        const timeoutSignal = (typeof AbortSignal !== "undefined" && AbortSignal.timeout)
            ? AbortSignal.timeout(2000)
            : undefined;
        const serverUrl = await getServerBaseUrl();
        const response = await fetch(`${serverUrl}/helper/last-conversion`, {
            signal: timeoutSignal
        });
        if (!response.ok) return false;

        const state = await response.json();
        if (!state || state.status !== "completed") return false;
        if (state.request_token !== payload.request_token) return false;
        if (!state.server_saved) return false;

        const expectedSources = Array.isArray(payload.sources) ? payload.sources.length : 0;
        if (expectedSources && state.total_sources && state.total_sources !== expectedSources) return false;

        const finishedMs = Date.parse(state.finished_at || "");
        if (!Number.isFinite(finishedMs)) return false;
        if ((Date.now() - finishedMs) > 5 * 60 * 1000) return false;

        browser.browserAction.setBadgeText({ text: "OK" });
        browser.browserAction.setBadgeBackgroundColor({ color: "green" });
        browser.notifications.create({
            type: "basic",
            iconUrl: "icon.png",
            title: "Saved on Server",
            message: "Server completed and saved the EPUB locally."
        });

        if (isBundle) {
            await browser.storage.local.set({ urlQueue: [] });
            updateBadge();
        } else {
            setTimeout(updateBadge, 3000);
        }

        return true;
    } catch (e) {
        console.warn("Server-save recovery check failed", e);
        return false;
    }
}

function abortableDelay(ms, signal) {
    return new Promise((resolve, reject) => {
        if (signal && signal.aborted) {
            reject(new DOMException("Aborted", "AbortError"));
            return;
        }
        const timer = setTimeout(resolve, ms);
        if (signal) {
            signal.addEventListener("abort", () => {
                clearTimeout(timer);
                reject(new DOMException("Aborted", "AbortError"));
            }, { once: true });
        }
    });
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
        const errText = await response.text();
        throw new Error(`Server ${response.status}: ${errText}`);
    }
    return await response.json();
}

async function rememberFailedSourcesFromJob(job) {
    const failed = job && Array.isArray(job.failed_source_details) ? job.failed_source_details : [];
    if (failed.length) {
        await browser.storage.local.set({ lastFailedSources: failed });
    } else if (job && job.status === "completed") {
        await browser.storage.local.remove("lastFailedSources");
    }
}

async function runServerJob(payload, signal) {
    if (!payload.request_token) {
        payload.request_token = createRequestToken();
    }
    const bodyStr = JSON.stringify(payload);
    console.log(`Payload size: ${bodyStr.length} chars. Submitting job...`);
    const serverUrl = await getServerBaseUrl();

    const submitted = await fetchJson(`${serverUrl}/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: bodyStr,
        signal
    });
    currentJobId = submitted.job_id;
    let openedVerificationUrl = null;

    while (true) {
        if (signal && signal.aborted) {
            throw new DOMException("Aborted", "AbortError");
        }
        const job = await fetchJson(`${serverUrl}/jobs/${currentJobId}`, { signal });
        await rememberFailedSourcesFromJob(job);
        if (job.total_sources) {
            browser.browserAction.setBadgeText({ text: `${job.processed_sources || 0}/${job.total_sources}` });
        }
        if (job.status === "verification_starting") {
            browser.browserAction.setBadgeText({ text: "OPEN" });
        }
        if (job.status === "verification_required") {
            browser.browserAction.setBadgeText({ text: "WARM" });
            if (job.verification_url && job.verification_url !== openedVerificationUrl) {
                openedVerificationUrl = job.verification_url;
                const warmUrl = new URL(job.verification_url, `${serverUrl}/`).toString();
                await browser.tabs.create({ url: warmUrl });
                browser.notifications.create({
                    type: "basic",
                    iconUrl: "icon.png",
                    title: "Verification Needed",
                    message: "Complete verification in the server browser tab, then the download will resume."
                });
            }
            await abortableDelay(1000, signal);
            continue;
        }
        if (job.status === "user_browser_required") {
            browser.browserAction.setBadgeText({ text: "TAB" });
            const openUrl = job.user_browser_url || job.verification_source_url || job.current_url;
            if (openUrl) {
                await browser.tabs.create({ url: openUrl });
                browser.notifications.create({
                    type: "basic",
                    iconUrl: "icon.png",
                    title: "Open Article in Browser",
                    message: "After the article loads, run Dala Download Page from that tab."
                });
            }
            throw new Error("Opened the article in your browser. Run Dala again from that readable tab.");
        }
        if (job.status === "completed") {
            const response = await fetch(`${serverUrl}/jobs/${currentJobId}/download`, { signal });
            if (!response.ok) {
                const errText = await response.text();
                throw new Error(`Server ${response.status}: ${errText}`);
            }
            return response;
        }
        if (job.status === "failed") {
            const err = new Error(job.error || "Conversion failed.");
            err.failedSourceDetails = job.failed_source_details || [];
            throw err;
        }
        if (job.status === "cancelled") {
            throw new DOMException(job.error || "Job cancelled.", "AbortError");
        }
        await abortableDelay(1000, signal);
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
                if (res && Array.isArray(res.page_htmls) && res.page_htmls.length) {
                    src.page_htmls = res.page_htmls;
                    if (!src.html) {
                        const firstPage = res.page_htmls.find(p => p && p.page === 1);
                        if (firstPage && firstPage.html) src.html = firstPage.html;
                    }
                    console.log(`✓ Fetched ${res.page_htmls.length} forum page HTML snapshots in background`);
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
    const runToken = Symbol("download-run");
    const controller = new AbortController();
    currentRunToken = runToken;
    currentController = controller;
    browser.browserAction.setBadgeText({ text: "..." });
    browser.browserAction.setBadgeBackgroundColor({ color: "#FFA500" }); // Orange

    // Ensure termux copy dir is included if configured
    try {
        if (!payload.termux_copy_dir) {
            const res = await browser.storage.local.get("savedOptions");
            const termuxDir = (res.savedOptions && typeof res.savedOptions.termux_copy_dir === "string") ? res.savedOptions.termux_copy_dir.trim() : "";
            if (termuxDir) {
                payload.termux_copy_dir = termuxDir;
            }
        }
    } catch (_) {
        // ignore; not critical
    }

    try {
        if (!payload.request_token) {
            payload.request_token = createRequestToken();
        }
        const response = await runServerJob(payload, controller.signal);
        console.log("Server job download response received:", response.status);
        const serverSaved = response.headers.get("X-Dala-Server-Saved") === "1";
        const filename = getFilenameFromHeader(response.headers.get('Content-Disposition'));
        validateResponseFormat(payload, filename, response.headers.get("Content-Type"));

        const blob = await response.blob();
        const url = URL.createObjectURL(blob);

        // Use Robust Header Parsing
        const res = await browser.storage.local.get("savedOptions");
        const saveFolder = (res.savedOptions && typeof res.savedOptions.save_folder === "string") ? res.savedOptions.save_folder.trim() : "";
        
        // Determine Browser Target Path
        // Browsers can only save relative to Downloads.
        const isAbsolute = saveFolder.startsWith("/") || /^[a-zA-Z]:\\/.test(saveFolder);
        const cleanSub = isAbsolute ? "" : saveFolder.replace(/[/\\\\]+/g, '/').replace(/^\/+|\/+$/g, '');
        const targetPath = cleanSub ? `${cleanSub}/${filename}` : filename;

        const canDownload = browser.downloads && typeof browser.downloads.download === "function";
        const isAndroid = /Android/i.test((navigator && navigator.userAgent) || "");
        let downloaded = false;

        if (serverSaved) {
            console.log("Server already saved the output locally; skipping browser download to avoid duplicates.");
            try { URL.revokeObjectURL(url); } catch (_) {}
            downloaded = true;
        } else if (canDownload) {
            try {
                console.log(`Attempting download: URL=${url}, Filename=${targetPath}`);
                const downloadId = await browser.downloads.download({
                    url: url,
                    filename: targetPath,
                    saveAs: false,
                    conflictAction: 'uniquify'
                });
                if (typeof downloadId === "number") {
                    trackBlobUrlForDownload(downloadId, url);
                } else {
                    // Keep blob URL alive long enough for browser to consume it.
                    setTimeout(() => {
                        try { URL.revokeObjectURL(url); } catch (_) {}
                    }, 2 * 60 * 1000);
                }
                downloaded = true;
            } catch (e) {
                console.error("downloads API failed for", targetPath, e);
                if (!serverSaved) {
                    browser.notifications.create({
                        type: "basic",
                        iconUrl: "icon.png",
                        title: "Download Save Failed",
                        message: `Browser refused to save '${targetPath}'. Error: ${e.message || e}`
                    });
                } else {
                    browser.notifications.create({
                        type: "basic",
                        iconUrl: "icon.png",
                        title: "Saved on Server",
                        message: "Browser save failed, but the output was saved by the server."
                    });
                }
            }
        } else {
            console.warn("downloads API unavailable; will open blob in new tab");
        }

        if (!downloaded) {
            if (serverSaved) {
                console.warn("Treating as success because server saved output locally.");
                try { URL.revokeObjectURL(url); } catch (_) {}
                downloaded = true;
            } else {
                try { URL.revokeObjectURL(url); } catch (_) {}
                await openOutputInTab(blob, filename);
            }
        } else if (isAndroid) {
            // Some Android builds acknowledge downloads but fail silently; also open tab as backup
            await openOutputInTab(blob, filename);
        }

        browser.browserAction.setBadgeText({ text: "OK" });
        browser.browserAction.setBadgeBackgroundColor({ color: "green" });

        // Notify page (if shortcut initiated) for inline toast feedback
        try {
            const targetTabId = lastShortcutTabId;
            lastShortcutTabId = null;
            const sendToastToTab = async (tabId) => {
                if (!tabId) return;
                try {
                    await browser.tabs.sendMessage(tabId, { action: "shortcut-toast", message: "Downloaded" });
                } catch (e) {
                    // Fallback: inject a minimal toast directly
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
                    try { await browser.tabs.executeScript(tabId, { code }); } catch (_) {}
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
            // Broadcast fallback for any listeners that are alive
            browser.runtime.sendMessage({ action: "shortcut-toast", message: "Downloaded" }).catch(() => {});
        } catch (_) {
            // ignore toast failures
        }

        if (isBundle) {
            await browser.storage.local.set({ urlQueue: [] });
            updateBadge();
        } else {
            setTimeout(updateBadge, 3000);
        }
        if (currentRunToken === runToken) {
            currentController = null;
            currentJobId = null;
            currentRunToken = null;
        }

    } catch (error) {
        if (error.name === 'AbortError') {
            browser.browserAction.setBadgeText({ text: "" });
            browser.browserAction.setBadgeBackgroundColor({ color: "#e85a4f" });
        } else {
            const recovered = await recoverServerSavedSuccess(payload, isBundle);
            if (recovered) {
                if (currentRunToken === runToken) {
                    currentController = null;
                    currentJobId = null;
                    currentRunToken = null;
                }
                return;
            }
            console.error("Download Failed:", error);
            browser.browserAction.setBadgeText({ text: "ERR" });
            browser.browserAction.setBadgeBackgroundColor({ color: "red" });

            browser.notifications.create({
                type: "basic",
                iconUrl: "icon.png",
                title: "Download Failed",
                message: error.failedSourceDetails && error.failedSourceDetails.length
                    ? `${error.failedSourceDetails.length} source(s) failed. Open Queue > Retry Failed.`
                    : (error.message || "Check server.py console")
            });
        }
        if (currentRunToken === runToken) {
            currentController = null;
            currentJobId = null;
            currentRunToken = null;
        }
    }
}

async function cancelDownload() {
    if (currentController) {
        currentController.abort();
        if (currentJobId) {
            try {
                const serverUrl = await getServerBaseUrl();
                await fetch(`${serverUrl}/jobs/${currentJobId}/cancel`, { method: "POST" });
            } catch (e) {
                console.warn("Server job cancellation failed", e);
            }
            currentJobId = null;
        }
        currentController = null;
        currentRunToken = null;
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
        const serverUrl = await getServerBaseUrl();
        const resp = await fetch(`${serverUrl}/helper/extract-links`, {
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
    return { assets: [], externals: [], next_page_url: null, next_page_num: null };
}

async function fetchAssetsForPage(threadUrl, page_spec, max_pages) {
    console.log("🔍 fetchAssetsForPage called with:", threadUrl);
    const assets = [];
    const page_htmls = [];
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
        const seenPageKeys = new Set();
        const queue = pagesToFetch.map(page => ({ page, url: buildForumPageUrl(normBase, page) }));

        while (queue.length) {
            const item = queue.shift();
            const url = item.url || buildForumPageUrl(normBase, item.page);
            const page = item.page || forumPageNumberFromUrl(url) || 1;
            const key = url.replace(/#.*$/, "");
            if (seenPageKeys.has(key)) continue;
            seenPageKeys.add(key);
            const html = await fetchWithCookies(url, threadUrl);
            if (!html) continue;
            page_htmls.push({ page, url, html });
            const found = parseAttachmentsFromHtml(html, url);
            console.log(`📎 Page ${page}: Found ${found.length} attachments`);
            const fetchAttachment = async (att) => {
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
                    return {
                        original_url: att.url,
                        viewer_url: att.viewer_url || canonical,
                        canonical_url: canonical,
                        filename_hint: att.filename,
                        content_type: fullData.type,
                        content: fullData.base64
                    };
                }
                return null;
            };

            // External images (non-attachment)
            const externals = parseExternalImages(html, url);
            const fetchExternal = async (ext) => {
                const data = await fetchBinaryMaybeHtml(ext, url);
                if (data && data.base64 && !data.isHtml) {
                    return {
                        original_url: ext,
                        viewer_url: null,
                        filename_hint: ext.split('/').pop(),
                        content_type: data.type,
                        content: data.base64
                    };
                }
                return null;
            };

            const fetched = await mapWithConcurrency([
                ...found.map(att => () => fetchAttachment(att)),
                ...externals.map(ext => () => fetchExternal(ext))
            ], 4);
            for (const item of fetched) {
                if (item) assets.push(item);
            }

            if (!hasExplicitPages) {
                const nextPage = findNextPage(html, page, url);
                if (nextPage && nextPage.url && (!max_pages || !nextPage.page || nextPage.page <= max_pages) && !seenPageKeys.has(nextPage.url.replace(/#.*$/, ""))) {
                    queue.push(nextPage);
                } else if (nextPage && nextPage.page && (!max_pages || nextPage.page <= max_pages)) {
                    const fallbackUrl = buildForumPageUrl(normBase, nextPage.page);
                    if (!seenPageKeys.has(fallbackUrl.replace(/#.*$/, ""))) {
                        queue.push({ page: nextPage.page, url: fallbackUrl });
                    }
                }
            }
        }
    } catch (e) {
        console.warn("fetchAssetsForPage error", e);
    }
    return { assets, page_htmls };
}

function forumPageNumberFromUrl(url) {
    if (!url) return null;
    const match = url.match(/page-(\d+)/i) || url.match(/[?&]page=(\d+)/i) || url.match(/\/page\/(\d+)/i);
    return match ? parseInt(match[1], 10) : null;
}

async function mapWithConcurrency(tasks, limit) {
    const results = new Array(tasks.length);
    let next = 0;
    const workers = Array.from({ length: Math.min(limit, tasks.length) }, async () => {
        while (next < tasks.length) {
            const idx = next++;
            results[idx] = await tasks[idx]();
        }
    });
    await Promise.all(workers);
    return results;
}

function buildForumPageUrl(base, page) {
    if (page <= 1) return base;
    if (base.includes("page-")) {
        return base.replace(/page-\d+/, `page-${page}`);
    }
    if (base.includes("?")) {
        return `${base}&page=${page}`;
    }
    return `${base.replace(/\/?$/, "/")}page-${page}`;
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
                }
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

function findNextPage(html, currentPage, baseUrl) {
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const resolve = (href) => {
            if (!href) return null;
            const url = new URL(href, baseUrl).href;
            return { page: forumPageNumberFromUrl(url), url };
        };
        const link = doc.querySelector("link[rel='next']");
        const linked = resolve(link && link.getAttribute("href"));
        if (linked) return linked;
        const jump = doc.querySelector("a.pageNav-jump--next[href], a.pageNavSimple-el--next[href], a[rel='next'][href]");
        const jumped = resolve(jump && jump.getAttribute("href"));
        if (jumped) return jumped;
        const nextNumeric = doc.querySelector(`a.pageNav-page[href]`);
        if (nextNumeric) {
            const numeric = Array.from(doc.querySelectorAll("a.pageNav-page[href]"))
                .find(a => (a.textContent || "").trim() === String(currentPage + 1));
            const resolvedNumeric = resolve(numeric && numeric.getAttribute("href"));
            if (resolvedNumeric) return resolvedNumeric;
        }
        const anchors = Array.from(doc.querySelectorAll("a"));
        for (const a of anchors) {
            const txt = (a.textContent || "").trim().toLowerCase();
            if (txt === "next" || txt === "next >" || txt === "next>") {
                const resolved = resolve(a.getAttribute("href"));
                if (resolved) return resolved;
            }
            if (txt === String(currentPage + 1)) {
                const resolved = resolve(a.getAttribute("href"));
                if (resolved) return resolved;
                return { page: currentPage + 1, url: null };
            }
        }
    } catch (e) {
        console.warn("findNextPage failed", e);
    }
    return null;
}

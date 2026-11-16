// Self-contained because this function executes in the selected page.
function collectDebugPage() {
    const describe = el => ({tag: el.tagName, id: el.id, class: el.getAttribute("class"),
        article_id: el.closest("article[data-content-id]")?.getAttribute("data-content-id")});
    const roots = [document];
    const shadows = [];
    const images = [];
    const clone = document.documentElement.cloneNode(true);
    // Preserve what the browser actually selected, including lazy image URLs.
    const originalImages = document.querySelectorAll("img");
    clone.querySelectorAll("img").forEach((img, index) => {
        if (originalImages[index].currentSrc) img.setAttribute("src", originalImages[index].currentSrc);
    });
    for (let index = 0; index < roots.length && index < 200; index++) {
        const root = roots[index];
        for (const el of root.querySelectorAll("*")) {
            if (el.shadowRoot && roots.length < 200) {
                roots.push(el.shadowRoot);
                shadows.push({host: describe(el), html: el.shadowRoot.innerHTML});
            }
        }
        for (const img of root.querySelectorAll("img")) {
            if (images.length >= 500) break;
            const style = getComputedStyle(img);
            const box = img.getBoundingClientRect();
            images.push({...describe(img), html: img.outerHTML, current_src: img.currentSrc,
                natural_width: img.naturalWidth, natural_height: img.naturalHeight,
                complete: img.complete, display: style.display, visibility: style.visibility,
                box: {x: box.x, y: box.y, width: box.width, height: box.height},
                ancestors: Array.from((function* () { let el = img.parentElement;
                    for (let n = 0; el && n < 8; n++, el = el.parentElement) yield el;
                })()).map(describe)});
        }
    }
    return {schema_version: 1, captured_at: new Date().toISOString(), url: location.href,
        title: document.title, base_uri: document.baseURI, html: document.documentElement.outerHTML,
        rendered_html: clone.outerHTML, visible_text: (document.body?.innerText || "").slice(0, 300000),
        viewport: {width: innerWidth, height: innerHeight, scroll_x: scrollX, scroll_y: scrollY},
        articles: Array.from(document.querySelectorAll("article")).slice(0, 100).map(el => ({
            ...describe(el), content_id: el.getAttribute("data-content-id"),
            heading: el.querySelector("h1,h2")?.textContent, text_chars: el.textContent.length,
            image_count: el.querySelectorAll("img").length})),
        images, shadow_roots: shadows,
        frames: Array.from(document.querySelectorAll("iframe")).slice(0, 100).map(el => ({src: el.src, title: el.title}))};
}

async function sendDebugCapture(tabId) {
    const stored = await browser.storage.local.get("savedOptions");
    const serverUrl = normalizeServerUrl((stored.savedOptions || {}).server_url);
    if (!["127.0.0.1", "localhost", "[::1]"].includes(new URL(serverUrl).hostname)) {
        throw new Error("Debug capture requires a localhost Dala server. Check Settings.");
    }
    const tab = await browser.tabs.get(tabId);
    if (!/^https?:\/\//.test(tab.url || "")) throw new Error("Open an article tab first.");
    let capture;
    if (typeof chrome !== "undefined" && chrome.scripting) {
        const results = await chrome.scripting.executeScript({target: {tabId}, func: collectDebugPage});
        capture = results[0]?.result;
    } else {
        const results = await browser.tabs.executeScript(tabId, {code: `(${collectDebugPage.toString()})()`});
        capture = results[0];
    }
    if (!capture?.html) throw new Error("The page did not provide a debug capture.");
    capture.extension_version = browser.runtime.getManifest().version;
    const headers = {"Content-Type": "application/json"};
    let body = new Blob([JSON.stringify(capture)], {type: "application/json"});
    if (typeof CompressionStream !== "undefined") {
        body = await new Response(body.stream().pipeThrough(new CompressionStream("gzip"))).blob();
        headers["Content-Encoding"] = "gzip";
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch(`${serverUrl}/helper/debug-capture`, {
            method: "POST", headers, body, signal: controller.signal
        });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || `Debug capture failed: HTTP ${response.status}`);
        }
        return await response.json();
    } finally {
        clearTimeout(timer);
    }
}

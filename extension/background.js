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

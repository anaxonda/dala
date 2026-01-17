if (!globalThis.browser) {
    globalThis.browser = {};

    const wrapAsync = (fn, thisArg) => (...args) => {
        return new Promise((resolve, reject) => {
            fn.call(thisArg, ...args, (result) => {
                if (chrome.runtime.lastError) {
                    reject(chrome.runtime.lastError);
                } else {
                    resolve(result);
                }
            });
        });
    };

    // runtime
    browser.runtime = {
        sendMessage: wrapAsync(chrome.runtime.sendMessage, chrome.runtime),
        getURL: (path) => chrome.runtime.getURL(path),
        openOptionsPage: wrapAsync(chrome.runtime.openOptionsPage, chrome.runtime),
        onMessage: chrome.runtime.onMessage,
        onInstalled: chrome.runtime.onInstalled
    };

    // tabs
    browser.tabs = {
        query: wrapAsync(chrome.tabs.query, chrome.tabs),
        create: wrapAsync(chrome.tabs.create, chrome.tabs),
        sendMessage: wrapAsync(chrome.tabs.sendMessage, chrome.tabs),
        onUpdated: chrome.tabs.onUpdated,
        // executeScript is NOT fully polyfilled here because of the code->func shift.
        // We will manually fix callsites to use chrome.scripting
    };

    // storage
    browser.storage = {
        local: {
            get: wrapAsync(chrome.storage.local.get, chrome.storage.local),
            set: wrapAsync(chrome.storage.local.set, chrome.storage.local)
        },
        onChanged: chrome.storage.onChanged
    };

    // cookies
    browser.cookies = {
        getAll: wrapAsync(chrome.cookies.getAll, chrome.cookies)
    };

    // downloads
    browser.downloads = {
        download: wrapAsync(chrome.downloads.download, chrome.downloads)
    };

    // action (was browserAction)
    browser.browserAction = {
        setBadgeText: (details) => chrome.action.setBadgeText(details),
        setBadgeBackgroundColor: (details) => chrome.action.setBadgeBackgroundColor(details)
    };

    // menus (was contextMenus or menus)
    browser.menus = {
        create: (props) => chrome.contextMenus.create(props),
        onClicked: chrome.contextMenus.onClicked,
        removeAll: wrapAsync(chrome.contextMenus.removeAll, chrome.contextMenus)
    };
    
    // permissions
    browser.permissions = {
        contains: wrapAsync(chrome.permissions.contains, chrome.permissions),
        request: wrapAsync(chrome.permissions.request, chrome.permissions)
    };
    
    // notifications
    browser.notifications = {
        create: wrapAsync(chrome.notifications.create, chrome.notifications)
    };
}

if (!globalThis.browser) {
    globalThis.browser = {};

    const wrapAsync = (fn, thisArg) => (...args) => {
        return new Promise((resolve, reject) => {
            try {
                fn.call(thisArg, ...args, (result) => {
                    if (chrome.runtime.lastError) {
                        reject(chrome.runtime.lastError);
                    } else {
                        resolve(result);
                    }
                });
            } catch (e) {
                reject(e);
            }
        });
    };

    // runtime
    if (chrome.runtime) {
        browser.runtime = {
            sendMessage: wrapAsync(chrome.runtime.sendMessage, chrome.runtime),
            getURL: (path) => chrome.runtime.getURL(path),
            getManifest: () => chrome.runtime.getManifest(),
            openOptionsPage: wrapAsync(chrome.runtime.openOptionsPage, chrome.runtime),
            onMessage: chrome.runtime.onMessage,
            onInstalled: chrome.runtime.onInstalled,
            onStartup: chrome.runtime.onStartup
        };
    }

    // tabs
    if (chrome.tabs) {
        browser.tabs = {
            query: wrapAsync(chrome.tabs.query, chrome.tabs),
            get: wrapAsync(chrome.tabs.get, chrome.tabs),
            create: wrapAsync(chrome.tabs.create, chrome.tabs),
            sendMessage: wrapAsync(chrome.tabs.sendMessage, chrome.tabs),
            onUpdated: chrome.tabs.onUpdated,
        };
    }

    // storage
    if (chrome.storage) {
        browser.storage = {
            local: {
                get: wrapAsync(chrome.storage.local.get, chrome.storage.local),
                set: wrapAsync(chrome.storage.local.set, chrome.storage.local)
            },
            onChanged: chrome.storage.onChanged
        };
    }

    // cookies
    if (chrome.cookies) {
        browser.cookies = {
            getAll: wrapAsync(chrome.cookies.getAll, chrome.cookies)
        };
    }

    // downloads
    if (chrome.downloads) {
        browser.downloads = {
            download: wrapAsync(chrome.downloads.download, chrome.downloads)
        };
    }

    // menus (was contextMenus or menus)
    if (chrome.contextMenus) {
        browser.menus = {
            create: (props, callback) => chrome.contextMenus.create(props, callback),
            update: wrapAsync(chrome.contextMenus.update, chrome.contextMenus),
            onClicked: chrome.contextMenus.onClicked,
            removeAll: wrapAsync(chrome.contextMenus.removeAll, chrome.contextMenus)
        };
    }
    
    // permissions
    if (chrome.permissions) {
        browser.permissions = {
            contains: wrapAsync(chrome.permissions.contains, chrome.permissions),
            request: wrapAsync(chrome.permissions.request, chrome.permissions)
        };
    }
    
    // notifications
    if (chrome.notifications) {
        browser.notifications = {
            create: wrapAsync(chrome.notifications.create, chrome.notifications)
        };
    }

    // commands
    if (chrome.commands) {
        browser.commands = {
            onCommand: chrome.commands.onCommand
        };
    }
}

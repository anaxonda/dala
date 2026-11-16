const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function exercise(directory, namespace) {
    const source = fs.readFileSync(`${directory}/background.js`, 'utf8');
    const helper = source.slice(source.indexOf('// Keep existing IDs'), source.indexOf('// Command Listener'));
    const registry = new Map();
    const calls = {created: 0, removed: 0, queued: [], downloaded: []};
    let failCreate = false;
    const runtime = {onStartup: {listeners: [], addListener(fn) {this.listeners.push(fn);}}};
    const clicked = {listeners: [], addListener(fn) {this.listeners.push(fn);}};
    const menus = {
        onClicked: clicked,
        update: async (id, props) => {
            if (!registry.has(id)) throw new Error('Menu not found');
            registry.set(id, {...registry.get(id), ...props});
        },
        create: (props, callback) => {
            calls.created++;
            queueMicrotask(() => {
                if (failCreate || registry.has(props.id)) {
                    runtime.lastError = {message: 'Could not create menu'};
                    callback();
                    delete runtime.lastError;
                } else {
                    registry.set(props.id, props);
                    callback();
                }
            });
            return props.id;
        },
        removeAll: () => {calls.removed++; registry.clear();}
    };
    const tabs = [
        {id: 7, url: 'https://example.com/page', title: 'Current page'},
        {id: 8, url: 'https://example.com/other', title: 'Other page'},
        {id: 9, url: 'about:config', title: 'Not a web page'}
    ];
    const browser = {runtime, tabs: {query: async () => tabs}};
    if (namespace !== 'absent') browser[namespace] = menus;
    const context = vm.createContext({URL, console: {warn() {}, error() {}}, browser,
        addToQueue: async (url, title) => calls.queued.push({url, title}),
        downloadSingleFromContext: async (url, tabId) => calls.downloaded.push({url, tabId})});
    vm.runInContext(helper, context);
    if (namespace === 'absent') {
        assert.equal(await context.ensureContextMenus(), false);
        assert.equal(calls.created, 0);
        return;
    }
    assert.equal(await context.ensureContextMenus(), true);
    assert.equal(calls.created, 3, 'Create missing menus when the background starts');
    assert.deepEqual([...registry.keys()], ['add-to-queue', 'add-selected-to-queue', 'download-page']);
    for (const menu of registry.values()) {
        for (const kind of ['page', 'link', 'selection', 'image', 'video', 'audio', 'frame', 'editable']) {
            assert(menu.contexts.includes(kind), `${menu.id} must support ${kind}`);
        }
    }
    await Promise.all([context.ensureContextMenus(), context.ensureContextMenus()]);
    assert.equal(calls.created, 3, 'Do not duplicate menus on worker wakeup');
    assert.equal(calls.removed, 0, 'Do not remove menus while they may be displayed');

    // Firefox's persistent background starts again without its old menu state.
    registry.clear();
    await runtime.onStartup.listeners[0]();
    assert.equal(registry.size, 3, 'Restore menus after browser startup');
    registry.delete('download-page');
    await context.ensureContextMenus();
    assert.equal(registry.size, 3, 'Repair a missing menu without deleting others');
    const beforeError = calls.created;
    registry.clear();
    failCreate = true;
    assert.equal(await context.ensureContextMenus(), false, 'Handle asynchronous create errors');
    failCreate = false;
    assert.equal(await context.ensureContextMenus(), true, 'Allow retry after a registration failure');
    assert(calls.created > beforeError);

    const click = clicked.listeners[0];
    await click({menuItemId: 'add-to-queue'}, tabs[0]);
    await click({menuItemId: 'add-to-queue', linkUrl: 'https://example.com/linked', linkText: 'Linked article'}, tabs[0]);
    await click({menuItemId: 'add-to-queue', srcUrl: 'https://example.com/picture.png'}, tabs[0]);
    assert.deepEqual(calls.queued.slice(0, 3), [
        {url: tabs[0].url, title: 'Current page'},
        {url: 'https://example.com/linked', title: 'Linked article'},
        {url: tabs[0].url, title: 'Current page'}
    ]);
    await click({menuItemId: 'download-page'}, tabs[0]);
    await click({menuItemId: 'download-page', linkUrl: 'https://example.com/linked'}, tabs[0]);
    await click({menuItemId: 'download-page', pageUrl: 'https://example.com/no-tab'}, undefined);
    assert.deepEqual(calls.downloaded, [
        {url: tabs[0].url, tabId: 7},
        {url: 'https://example.com/linked', tabId: null},
        {url: 'https://example.com/no-tab', tabId: null}
    ]);
    await click({menuItemId: 'add-selected-to-queue'}, tabs[0]);
    assert.deepEqual(calls.queued.slice(-2), tabs.slice(0, 2).map(tab => ({url: tab.url, title: tab.title})));
    const length = calls.queued.length;
    await click({menuItemId: 'add-to-queue', linkUrl: 'javascript:alert(1)'}, tabs[0]);
    await click({menuItemId: 'add-to-queue'}, tabs[2]);
    assert.equal(calls.queued.length, length);
}

async function checkChromeShim(directory) {
    let created = false;
    const startup = {addListener() {}};
    const chrome = {runtime: {onStartup: startup, getManifest: () => ({version: '2.4.7'})},
        tabs: {get: (id, callback) => callback({id, url: 'https://example.com/page'})}, contextMenus: {
        create: (props, callback) => {created = true; callback(); return props.id;},
        update: (id, props, callback) => callback(),
        removeAll: callback => callback(), onClicked: {addListener() {}}
    }};
    const context = vm.createContext({chrome});
    vm.runInContext(fs.readFileSync(`${directory}/chrome-shim.js`, 'utf8'), context);
    assert.equal(context.browser.runtime.onStartup, startup);
    assert.equal(context.browser.runtime.getManifest().version, '2.4.7');
    assert.equal((await context.browser.tabs.get(7)).url, 'https://example.com/page');
    await new Promise(resolve => context.browser.menus.create({id: 'test'}, resolve));
    assert(created, 'Pass the create completion callback through the Chrome shim');
    await context.browser.menus.update('test', {title: 'Updated'});
    chrome.contextMenus.update = (id, props, callback) => {
        chrome.runtime.lastError = {message: 'Update failed'};
        callback();
        delete chrome.runtime.lastError;
    };
    // The existing wrapper captured the original method; load a fresh shim.
    const failing = vm.createContext({chrome});
    vm.runInContext(fs.readFileSync(`${directory}/chrome-shim.js`, 'utf8'), failing);
    await assert.rejects(failing.browser.menus.update('test', {}), error => error.message === 'Update failed');
}

(async () => {
    await exercise(process.argv[2], process.argv[3]);
    if (process.argv[2].endsWith('extension_chrome')) await checkChromeShim(process.argv[2]);
})().catch(error => {console.error(error); process.exitCode = 1;});

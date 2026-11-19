// Kept identical in the Chrome and Firefox packages.
let displayedQueue = [];
const selectedQueueUrls = new Set();
let queueOperations = Promise.resolve();
let queueEditorDirty = false;
let queueEditorBase = new Set();
let queueEditorTimer = null;
let queueRefreshGeneration = 0;

function queueSavedAtNow() { return new Date().toISOString(); }
function cleanQueueTitle(title) {
    return typeof title === 'string' ? title.replace(/\s+/g, ' ').trim().slice(0, 500) : '';
}
function normalizeQueueEntry(entry, fallbackSavedAt = null) {
    const value = typeof entry === 'string' ? entry : entry?.url;
    const url = typeof value === 'string' ? value.trim() : '';
    if (!isValidUrl(url)) return null;
    const item = { url, saved_at: entry?.saved_at || fallbackSavedAt || queueSavedAtNow() };
    const title = cleanQueueTitle(entry?.title);
    if (title) item.title = title;
    return item;
}
function normalizeQueueItems(rawQueue, fallbackSavedAt = null) {
    const items = new Map();
    for (const raw of Array.isArray(rawQueue) ? rawQueue : []) {
        const item = normalizeQueueEntry(raw, fallbackSavedAt);
        if (!item) continue;
        const existing = items.get(item.url);
        if (!existing) items.set(item.url, item);
        else if (!existing.title && item.title) existing.title = item.title;
    }
    return [...items.values()];
}
function queueDisplayTitle(item) {
    if (item.title) return item.title;
    const url = new URL(item.url);
    let path = url.pathname.split('/').filter(Boolean).pop() || '';
    try { path = decodeURIComponent(path); } catch (_) { /* Keep malformed escapes. */ }
    path = path.replace(/\.(?:html?|pdf)$/i, '').replace(/[-_]+/g, ' ');
    return path ? `${path}${url.search} · ${url.hostname}` : url.hostname;
}
function syncQueueSelection() {
    const count = displayedQueue.filter(item => selectedQueueUrls.has(item.url)).length;
    const all = document.getElementById('queue-select-all');
    all.checked = displayedQueue.length > 0 && count === displayedQueue.length;
    all.indeterminate = count > 0 && count < displayedQueue.length;
    all.disabled = displayedQueue.length === 0;
    document.getElementById('queue-selection-count').textContent = `${count} selected`;
    const remove = document.getElementById('btn-delete-selected');
    remove.disabled = count === 0;
    remove.textContent = count ? `Delete selected (${count})` : 'Delete selected';
    document.querySelectorAll('.queue-item').forEach(row => {
        const checked = selectedQueueUrls.has(row.dataset.url);
        row.classList.toggle('selected', checked);
        row.querySelector('input').checked = checked;
    });
}
function renderQueue(queue) {
    displayedQueue = queue;
    const present = new Set(queue.map(item => item.url));
    for (const url of selectedQueueUrls) if (!present.has(url)) selectedQueueUrls.delete(url);
    const fragment = document.createDocumentFragment();
    for (const item of queue) {
        const title = queueDisplayTitle(item);
        const row = document.createElement('li');
        row.className = 'queue-item';
        row.dataset.url = item.url;
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.setAttribute('aria-label', `Select ${title}`);
        checkbox.addEventListener('change', () => {
            if (checkbox.checked) selectedQueueUrls.add(item.url);
            else selectedQueueUrls.delete(item.url);
            syncQueueSelection();
        });
        const text = document.createElement('a');
        text.className = 'queue-item-text';
        text.href = item.url;
        text.target = '_blank';
        text.rel = 'noopener noreferrer';
        const heading = document.createElement('span');
        heading.className = 'queue-item-title';
        heading.textContent = title;
        heading.title = title;
        const address = document.createElement('span');
        address.className = 'queue-item-url';
        address.textContent = item.url.replace(/^https?:\/\//, '');
        address.title = item.url;
        text.append(heading, address);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'queue-item-remove';
        remove.textContent = '×';
        remove.title = `Remove ${title}`;
        remove.setAttribute('aria-label', `Remove ${title}`);
        remove.addEventListener('click', () => deleteQueueUrls(new Set([item.url])).catch(queueError));
        row.append(checkbox, text, remove);
        fragment.append(row);
    }
    const list = document.getElementById('queue-list');
    list.replaceChildren(fragment);
    list.classList.toggle('hidden', queue.length === 0);
    document.getElementById('queue-empty').classList.toggle('hidden', queue.length > 0);
    document.getElementById('queue-count').textContent = `(${queue.length})`;
    if (!queueEditorDirty) document.getElementById('queue-editor').value = queue.map(item => item.url).join('\n');
    syncQueueSelection();
    updateBadgeCount(queue.length).catch(queueError);
}
function queueError(error) {
    console.error('Queue update failed', error);
    showStatus(`Queue update failed: ${error.message}`);
}
function mutateQueue(transform) {
    const operation = queueOperations.then(async () => {
        const res = await browser.storage.local.get('urlQueue');
        const next = transform(normalizeQueueItems(res.urlQueue || []));
        await browser.storage.local.set({ urlQueue: next });
        renderQueue(next);
        return next;
    });
    queueOperations = operation.catch(() => {});
    return operation;
}
async function refreshQueue() {
    const generation = ++queueRefreshGeneration;
    const res = await browser.storage.local.get('urlQueue');
    const queue = normalizeQueueItems(res.urlQueue || []);
    try {
        const tabs = await browser.tabs.query({});
        const titles = new Map(tabs.map(tab => [tab.url, cleanQueueTitle(tab.title)]));
        for (const item of queue) if (!item.title && titles.get(item.url)) item.title = titles.get(item.url);
    } catch (_) { /* Closed/pasted URLs use readable fallback labels. */ }
    if (generation !== queueRefreshGeneration) return;
    if (JSON.stringify(queue) !== JSON.stringify(res.urlQueue || [])) await browser.storage.local.set({ urlQueue: queue });
    renderQueue(queue);
}
async function addToQueue(url, title = '') {
    if (!isValidUrl(url)) return;
    await saveQueueFromEditor();
    return mutateQueue(queue => {
        const existing = queue.find(item => item.url === url);
        if (existing) {
            if (cleanQueueTitle(title)) existing.title = cleanQueueTitle(title);
        } else queue.push(normalizeQueueEntry({ url, title }));
        return queue;
    });
}
async function deleteQueueUrls(urls) {
    await saveQueueFromEditor();
    let removed = 0;
    const queue = await mutateQueue(items => {
        const next = items.filter(item => !urls.has(item.url));
        removed = items.length - next.length;
        return next;
    });
    showStatus(`${removed} item${removed === 1 ? '' : 's'} removed · ${queue.length} in queue`);
}
async function clearQueue() {
    clearTimeout(queueEditorTimer);
    queueEditorDirty = false;
    selectedQueueUrls.clear();
    await mutateQueue(() => []);
}
async function saveQueueFromEditor() {
    if (!queueEditorDirty) return queueOperations;
    clearTimeout(queueEditorTimer);
    const urls = [...new Set(document.getElementById('queue-editor').value.split('\n').map(line => line.trim()).filter(isValidUrl))];
    const base = new Set(queueEditorBase);
    queueEditorDirty = false;
    return mutateQueue(queue => {
        const existing = new Map(queue.map(item => [item.url, item]));
        const next = urls.map(url => existing.get(url) || normalizeQueueEntry(url));
        // Do not discard URLs added elsewhere while the raw editor was open.
        next.push(...queue.filter(item => !base.has(item.url) && !urls.includes(item.url)));
        return next;
    });
}
function initializeQueueUI() {
    document.getElementById('queue-editor').addEventListener('input', () => {
        if (!queueEditorDirty) queueEditorBase = new Set(displayedQueue.map(item => item.url));
        queueEditorDirty = true;
        clearTimeout(queueEditorTimer);
        queueEditorTimer = setTimeout(() => saveQueueFromEditor().catch(queueError), 300);
    });
    document.getElementById('queue-select-all').addEventListener('change', event => {
        selectedQueueUrls.clear();
        if (event.target.checked) for (const item of displayedQueue) selectedQueueUrls.add(item.url);
        syncQueueSelection();
    });
    document.getElementById('btn-delete-selected').addEventListener('click', () => deleteQueueUrls(new Set(selectedQueueUrls)).catch(queueError));
    browser.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && changes.urlQueue) {
            queueRefreshGeneration++;
            renderQueue(normalizeQueueItems(changes.urlQueue.newValue || []));
        }
    });
}

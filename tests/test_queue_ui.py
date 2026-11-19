from pathlib import Path
import json
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_queue_ui_is_shared_between_browser_packages():
    assert (ROOT / 'extension_chrome/queue-ui.js').read_bytes() == (ROOT / 'firefox_extension/queue-ui.js').read_bytes()


@pytest.mark.parametrize('directory', ['extension_chrome', 'firefox_extension'])
def test_background_queue_writes_preserve_titles_and_timestamps(directory):
    script = r'''
        const fs = require('node:fs'), vm = require('node:vm');
        const source = fs.readFileSync(process.argv[1], 'utf8');
        const model = source.slice(source.indexOf('function savedAtNow('), source.indexOf('function setDownloadBadge('));
        const add = source.slice(source.indexOf('async function addToQueue('), source.indexOf('function parsePageSpecInput('));
        let queue = [{url: 'https://example.com/a', title: 'Existing title', saved_at: '2026-11-16T12:00:00Z'}];
        const context = vm.createContext({updateBadge: () => {}, browser: {storage: {local: {
            get: async () => ({urlQueue: structuredClone(queue)}),
            set: async value => {queue = structuredClone(value.urlQueue);}
        }}}});
        vm.runInContext(model + add, context);
        (async () => {
            await context.addToQueue('https://example.com/b', ' New   tab title ');
            await context.addToQueue('https://example.com/a', 'Updated title');
            console.log(JSON.stringify(queue));
        })().catch(error => {console.error(error); process.exitCode = 1;});
    '''
    queue = json.loads(subprocess.check_output(['node', '-e', script, str(ROOT / directory / 'background.js')], text=True))
    assert queue[0] == {'url': 'https://example.com/a', 'title': 'Updated title', 'saved_at': '2026-11-16T12:00:00Z'}
    assert queue[1]['title'] == 'New tab title'
    assert queue[1]['saved_at']


async def open_queue_page(context, directory, queue):
    page = await context.new_page()
    html = (ROOT / directory / 'popup.html').read_text()
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    html = re.sub(r'<link\b[^>]*>', '', html)
    await page.set_content(html)
    await page.add_style_tag(content=(ROOT / directory / 'popup.css').read_text())
    await page.evaluate('''queue => {
        window.queueState = {urlQueue: queue};
        window.queueListeners = [];
        window.failQueueWrites = false;
        window.browser = {
            tabs: {query: async () => [{url: 'https://example.com/old-article', title: 'A saved legacy tab'}]},
            storage: {
                local: {
                    get: async key => ({[key]: structuredClone(window.queueState[key])}),
                    set: async values => {
                        if (window.failQueueWrites) throw new Error('Storage unavailable');
                        const changes = {};
                        for (const [key, value] of Object.entries(values)) {
                            changes[key] = {oldValue: structuredClone(window.queueState[key]), newValue: structuredClone(value)};
                            window.queueState[key] = structuredClone(value);
                        }
                        for (const listener of window.queueListeners) listener(changes, 'local');
                    }
                },
                onChanged: {addListener: listener => window.queueListeners.push(listener)}
            }
        };
        window.showStatus = text => { document.getElementById('status-bar').textContent = text; };
        window.updateBadgeCount = async count => { window.queueBadge = count; };
    }''', queue)
    popup = (ROOT / directory / 'popup.js').read_text()
    validation = popup[popup.index('function isValidUrl('):popup.index('function normalizeServerUrl(')]
    await page.add_script_tag(content=validation)
    await page.add_script_tag(content=(ROOT / directory / 'queue-ui.js').read_text())
    await page.evaluate('''async () => {
        initializeQueueUI();
        document.getElementById('view-single').classList.remove('active');
        document.getElementById('view-queue').classList.add('active');
        document.getElementById('tab-single').classList.remove('active');
        document.getElementById('tab-queue').classList.add('active');
        document.getElementById('btn-clear').onclick = clearQueue;
        await refreshQueue();
    }''')
    return page


@pytest.mark.asyncio
@pytest.mark.parametrize('directory', ['extension_chrome', 'firefox_extension'])
async def test_queue_titles_bulk_delete_and_pending_editor_updates(tmp_path, directory):
    from playwright.async_api import async_playwright

    saved = '2026-11-16T12:00:00Z'
    queue = [
        {'url': 'https://example.com/a', 'title': 'A very long article title ' * 12, 'saved_at': saved},
        'https://example.com/old-article',
        {'url': 'https://example.com/b', 'title': '<img src=x onerror=alert(1)> literal title', 'saved_at': saved},
    ]
    async with async_playwright() as tool:
        browser = await tool.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
        try:
            context = await browser.new_context(viewport={'width': 340, 'height': 800})
            page = await open_queue_page(context, directory, queue)
            assert await page.locator('.queue-item').count() == 3
            assert await page.locator('.queue-item-title').nth(1).inner_text() == 'A saved legacy tab'
            assert await page.locator('.queue-item-title img').count() == 0
            assert await page.locator('.queue-item-title').first.evaluate('el => el.scrollWidth > el.clientWidth')
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert await page.locator('#btn-delete-selected').is_disabled()
            original_legacy = (await page.evaluate('window.queueState.urlQueue'))[1]

            await page.locator('.queue-item input').nth(0).check()
            await page.locator('.queue-item input').nth(2).check()
            assert await page.locator('#queue-select-all').evaluate('el => el.indeterminate')
            assert await page.locator('#queue-selection-count').inner_text() == '2 selected'
            await context.route('https://example.com/old-article', lambda route: route.fulfill(body='Saved article'))
            row = page.locator('.queue-item').nth(1)
            for link_text in ('.queue-item-title', '.queue-item-url'):
                async with context.expect_page() as opened:
                    await row.locator(link_text).click()
                article = await opened.value
                await article.wait_for_url('https://example.com/old-article')
                assert await article.evaluate('() => window.opener === null')
                await article.close()
            assert not await row.get_by_role('checkbox').is_checked()
            assert await page.locator('#queue-selection-count').inner_text() == '2 selected'
            await page.locator('#view-queue').screenshot(path=str(tmp_path / f'{directory}-queue.png'))

            # A pending raw edit and another browser context's addition must
            # survive deleting selected rows, without resurrecting those rows.
            await page.locator('.queue-editor-panel summary').click()
            await page.fill('#queue-editor', 'https://example.com/a\nhttps://example.com/old-article\nhttps://example.com/b\nhttps://example.com/d\nhttp-not-a-url')
            await page.evaluate('''async () => {
                await browser.storage.local.set({urlQueue: [...window.queueState.urlQueue,
                    {url: 'https://example.com/e', title: 'Added elsewhere', saved_at: '2026-11-16T13:00:00Z'}]});
            }''')
            await page.click('#btn-delete-selected')
            await page.wait_for_function("window.queueState.urlQueue.length === 3 && !window.queueState.urlQueue.some(item => item.url === 'https://example.com/a')")
            await page.wait_for_timeout(400)
            stored = await page.evaluate('window.queueState.urlQueue')
            assert [item['url'] for item in stored] == ['https://example.com/old-article', 'https://example.com/d', 'https://example.com/e']
            assert stored[0] == original_legacy
            assert stored[2]['title'] == 'Added elsewhere'
            assert await page.locator('#queue-count').inner_text() == '(3)'
            assert await page.evaluate('window.queueBadge') == 3

            # Selection is transient; a new popup retains titles and timestamps.
            reopened = await open_queue_page(context, directory, stored)
            assert await reopened.locator('#queue-selection-count').inner_text() == '0 selected'
            assert await reopened.locator('.queue-item-title').first.inner_text() == 'A saved legacy tab'
            await reopened.locator('.queue-item-remove').last.click()
            await reopened.wait_for_function('window.queueState.urlQueue.length === 2')
            await reopened.locator('#queue-select-all').check()
            assert await reopened.locator('#queue-selection-count').inner_text() == '2 selected'
            await reopened.click('#btn-delete-selected')
            await reopened.wait_for_function('window.queueState.urlQueue.length === 0')
            assert await reopened.locator('#queue-empty').is_visible()
            assert await reopened.locator('#queue-select-all').is_disabled()
            assert await reopened.locator('#btn-delete-selected').is_disabled()
            assert await reopened.evaluate('window.queueBadge') == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_queue_clear_cancels_pending_edits_and_storage_errors_are_visible():
    from playwright.async_api import async_playwright

    async with async_playwright() as tool:
        browser = await tool.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
        try:
            context = await browser.new_context(viewport={'width': 340, 'height': 800})
            page = await open_queue_page(context, 'extension_chrome', ['https://example.com/a'])
            await page.locator('.queue-item input').check()
            await page.evaluate('window.failQueueWrites = true')
            await page.click('#btn-delete-selected')
            await page.wait_for_function("document.getElementById('status-bar').textContent.includes('Storage unavailable')")
            assert await page.locator('.queue-item').count() == 1
            await page.evaluate('window.failQueueWrites = false')
            await page.locator('.queue-editor-panel summary').click()
            await page.fill('#queue-editor', 'https://example.com/a\nhttps://example.com/b')
            await page.click('#btn-clear')
            await page.wait_for_timeout(400)
            assert await page.evaluate('window.queueState.urlQueue') == []
            assert await page.locator('#queue-empty').is_visible()
        finally:
            await browser.close()

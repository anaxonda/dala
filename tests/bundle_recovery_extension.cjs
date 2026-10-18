const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function exercise(directory) {
    const source = fs.readFileSync(`${directory}/background.js`, 'utf8');
    const helper = source.slice(source.indexOf('function sourceTabKey('), source.indexOf('async function runServerJob('));
    let tabs = [{ id: 7, url: 'https://example.com/article/#section', status: 'complete' }];
    let capturedUrl = tabs[0].url;
    let status = 422;
    let now = 10000;
    const created = [], sent = [], capturedTabs = [];
    const context = vm.createContext({
        URL, Map, DOMException, AbortController, setTimeout, clearTimeout, console, Date: { now: () => now },
        location: {}, document: {},
        browser: {
            tabs: {
                query: async () => tabs,
                create: async value => { created.push(value); },
                executeScript: async (id) => { capturedTabs.push(id); return [{ url: capturedUrl, html: '<article>Readable content</article>' }]; }
            },
            notifications: { create: () => {} }
        },
        chrome: {
            runtime: {}, scripting: { executeScript: (options, callback) => {
                capturedTabs.push(options.target.tabId);
                callback([{ result: { url: capturedUrl, html: '<article>Readable content</article>' } }]);
            } }
        },
        fetch: async (url, options) => {
            sent.push({ url, body: JSON.parse(options.body) });
            return { ok: status === 200, status };
        },
        abortableDelay: async () => new Promise(() => {}),
        getCookiesForUrl: async () => { throw new Error('Cookies were not authorized'); }
    });
    vm.runInContext(helper, context);
    const pending = { recovery_id: 'recovery', url: 'https://example.com/article' };
    const job = { job_id: 'job', source_recoveries: [pending] };
    const payload = { sources: [{ url: pending.url }] };
    const state = new Map();
    const run = () => context.recoverBundleSources(job, 'http://localhost:8000', payload, state);
    await run();
    assert.equal(created.length, 0, 'Reuse an existing tab despite fragment or trailing slash');
    assert.equal(capturedTabs[0], 7);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].body.url, pending.url);
    assert.equal(sent[0].body.cookies, undefined);
    await run();
    assert.equal(sent.length, 1, 'Throttle challenge recapture');
    now += 3001;
    status = 200;
    await run();
    assert.equal(sent.length, 2, 'Recapture after the challenge is resolved');
    now += 3001;
    await run();
    assert.equal(sent.length, 2, 'Submit each recovery once after acceptance');

    tabs = [];
    const missing = new Map();
    await context.recoverBundleSources(job, 'http://localhost:8000', payload, missing);
    assert.equal(created.length, 1, 'Open a tab when there is no matching tab');
    now += 3001;
    await context.recoverBundleSources(job, 'http://localhost:8000', payload, missing);
    assert.equal(created.length, 1, 'Do not open repeated tabs');

    tabs = [{ id: 8, url: pending.url, status: 'complete' }];
    capturedUrl = 'https://example.com/unrelated';
    await context.recoverBundleSources(job, 'http://localhost:8000', payload, new Map());
    assert.equal(sent.length, 2, 'Never submit content from a tab that navigated away');
    await context.recoverBundleSources({ ...job, source_recoveries: [] }, 'http://localhost:8000', payload, new Map());
    assert.equal(sent.length, 2, 'Expired recovery is not captured');

    await assert.rejects(
        context.recoverBundleSources(job, 'http://localhost:8000', payload, new Map(), { aborted: true }),
        { name: 'AbortError' }
    );
}
exercise(process.argv[2]).catch(error => { console.error(error); process.exitCode = 1; });

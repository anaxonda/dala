import json
import gzip
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from dala.server import app


def test_debug_capture_saves_source_and_diagnostics_privately(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    payload = {"url": "https://app.ft.com/content/test", "html": "<article>Story</article>",
               "rendered_html": '<article><img src="selected.jpg"></article>',
               "images": [{"current_src": "selected.jpg", "ancestors": [{"tag": "BUTTON"}]}]}
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", json=payload)
    assert response.status_code == 200
    result = response.json()
    directory = Path(result["directory"])
    assert directory.parent == tmp_path
    assert (directory / "page.html").read_text() == payload["html"]
    assert (directory / "rendered.html").read_text() == payload["rendered_html"]
    details = json.loads((directory / "details.json").read_text())
    assert details["images"] == payload["images"]
    assert "html" not in details
    assert directory.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())


def test_debug_capture_rejects_remote_connections(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    with TestClient(app, client=("192.168.1.2", 5000)) as client:
        response = client.post("/helper/debug-capture", json={"html": "page"})
    assert response.status_code == 403
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("body", [b"[]", b"not json", b'{"html":123}'])
def test_debug_capture_rejects_invalid_source(body):
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=body)
    assert response.status_code == 400


def test_debug_capture_enforces_upload_limit(monkeypatch):
    monkeypatch.setattr("dala.server.MAX_CAPTURE_BYTES", 32)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", json={"html": "x" * 100})
    assert response.status_code == 413


def test_debug_capture_accepts_source_larger_than_previous_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    html = '<article>' + 'x' * (26 * 1024 * 1024) + '</article>'
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", json={"html": html})
    assert response.status_code == 200
    assert Path(response.json()['files']['html']).read_text() == html


def test_debug_capture_accepts_gzip(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    payload = {"html": "<article>Compressed source</article>", "images": []}
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=gzip.compress(json.dumps(payload).encode()),
                               headers={"Content-Encoding": "gzip"})
    assert response.status_code == 200
    assert Path(response.json()['files']['html']).read_text() == payload['html']


def test_debug_capture_limits_decompressed_size(monkeypatch):
    monkeypatch.setattr("dala.server.MAX_CAPTURE_BYTES", 32)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=gzip.compress(b'{"html":"' + b'x' * 1000 + b'"}'),
                               headers={"Content-Encoding": "gzip"})
    assert response.status_code == 413


@pytest.mark.parametrize("data", [b"not gzip", gzip.compress(b'{"html":"source"}')[:-5]])
def test_debug_capture_rejects_invalid_gzip(data):
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=data, headers={"Content-Encoding": "gzip"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_browser_debug_capture_keeps_image_ancestors_and_shadow_dom(tmp_path):
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    from dala.core.browser import resolve_browser_executable

    executable = resolve_browser_executable()
    if not executable:
        pytest.skip("Chromium is not installed")

    source = (Path(__file__).resolve().parents[1] / "extension_chrome/debug-capture.js").read_text()
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable, args=["--no-sandbox"])
        try:
            page = await browser.new_page()
            await page.set_content('''<article data-content-id="requested"><h1>Story</h1>
                <figure><button><img src="https://example.com/photo.jpg"></button></figure>
                <div id="shadow-host"></div></article>''')
            await page.evaluate('''() => document.querySelector('#shadow-host').attachShadow({mode:'open'}).innerHTML = '<p>Shadow caption</p>' ''')
            capture = await page.evaluate(source + "; collectDebugPage()")
            assert capture["articles"][0]["content_id"] == "requested"
            assert capture["images"][0]["ancestors"][0]["tag"] == "BUTTON"
            assert capture["images"][0]["article_id"] == "requested"
            assert "Shadow caption" in capture["shadow_roots"][0]["html"]
            assert "cookies" not in capture
            assert "storage" not in capture
            await page.screenshot(path=str(tmp_path / "debug-page.png"))
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_debug_button_sends_capture_through_extension_background(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    from dala.core.browser import resolve_browser_executable

    executable = resolve_browser_executable()
    if not executable:
        pytest.skip("Chromium is not installed")
    captures = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}' if self.path == '/ping' else
                             b'<article data-content-id="test"><h1>Test article</h1><p>Visible content</p></article>')

        def do_POST(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            if self.headers.get('Content-Encoding') == 'gzip':
                data = gzip.decompress(data)
            captures.append(json.loads(data))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"capture_id":"test","directory":"/tmp/dala-debug-test"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    extension = Path(__file__).resolve().parents[1] / "extension_chrome"
    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(tmp_path / "profile"), executable_path=executable,
                args=["--no-sandbox", f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            )
            try:
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event('serviceworker')
                extension_id = worker.url.split('/')[2]
                await worker.evaluate('(url) => browser.storage.local.set({savedOptions: {server_url:url}})', base)
                article = await context.new_page()
                await article.goto(base + '/article')
                tabs = await worker.evaluate('browser.tabs.query({})')
                tab_id = next(tab['id'] for tab in tabs if tab['url'] == base + '/article')
                popup = await context.new_page()
                await popup.goto(f'chrome-extension://{extension_id}/popup.html')
                # A real popup is attached to the active article. This test opens
                # its document as a tab, so provide that article tab explicitly.
                await popup.evaluate('(id) => { browser.tabs.query = async () => [{id}]; }', tab_id)
                await popup.click('#btn-debug-capture')
                await popup.wait_for_function('document.querySelector("#debug-capture-status").textContent.includes("Debug capture saved:")')
                assert captures[0]['url'] == base + '/article'
                assert captures[0]['articles'][0]['content_id'] == 'test'
                assert 'Visible content' in captures[0]['html']
                assert await popup.locator('#btn-debug-capture').is_enabled()
                await popup.screenshot(path=str(tmp_path / 'debug-popup.png'), full_page=True)
            finally:
                await context.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

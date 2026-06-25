import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from dala.core.browser import (
    BrowserFetchError,
    BrowserFetchOptions,
    _cookies_for_url,
    detect_browser_challenge,
    fetch_rendered_source,
    resolve_browser_executable,
    validate_browser_options,
)


def test_validate_browser_options_rejects_missing_extension(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(BrowserFetchError, match="Browser extension path"):
        validate_browser_options(BrowserFetchOptions(extension_path=str(missing)))


def test_validate_browser_options_rejects_extension_without_manifest(tmp_path):
    extension = tmp_path / "extension"
    extension.mkdir()
    with pytest.raises(BrowserFetchError, match="manifest.json"):
        validate_browser_options(BrowserFetchOptions(extension_path=str(extension)))


def test_validate_browser_options_rejects_bad_wait_mode():
    with pytest.raises(BrowserFetchError, match="Unsupported browser wait mode"):
        validate_browser_options(BrowserFetchOptions(wait_until="idle"))


def test_validate_browser_options_rejects_bad_challenge_action():
    with pytest.raises(BrowserFetchError, match="Unsupported browser challenge action"):
        validate_browser_options(BrowserFetchOptions(challenge_action="proxy_slider"))


def test_cookies_for_url_filters_to_target_host():
    cookies = [
        {"domain": "example.com", "name": "root", "value": "1"},
        {"domain": ".example.com", "name": "dot", "value": "2"},
        {"domain": "other.com", "name": "other", "value": "3"},
    ]

    assert _cookies_for_url(cookies, "https://sub.example.com/post") == {
        "root": "1",
        "dot": "2",
    }


def test_detect_browser_challenge_identifies_datadome_page():
    html = "<html><body>Please enable JS and disable any ad blocker<script src='https://geo.captcha-delivery.com/captcha.js'></script></body></html>"

    assert detect_browser_challenge(html) == "geo.captcha-delivery.com"


def test_resolve_browser_executable_prefers_explicit_path(monkeypatch, tmp_path):
    browser = tmp_path / "chrome"
    browser.write_text("browser")
    monkeypatch.setenv("DALA_BROWSER_EXECUTABLE", "/ignored/env/chrome")

    assert resolve_browser_executable(str(browser)) == str(browser)


def test_resolve_browser_executable_uses_environment(monkeypatch, tmp_path):
    browser = tmp_path / "chrome"
    browser.write_text("browser")
    monkeypatch.setenv("DALA_BROWSER_EXECUTABLE", str(browser))

    assert resolve_browser_executable() == str(browser)


def test_resolve_browser_executable_finds_linux_google_chrome(monkeypatch):
    def fake_which(candidate):
        return "/usr/bin/google-chrome" if candidate == "google-chrome" else None

    monkeypatch.delenv("DALA_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr("dala.core.browser.shutil.which", fake_which)
    monkeypatch.setattr("dala.core.browser.Path.is_file", lambda self: False)

    assert resolve_browser_executable() == "/usr/bin/google-chrome"


def test_resolve_browser_executable_finds_macos_app_path(monkeypatch):
    expected = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    monkeypatch.delenv("DALA_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr("dala.core.browser.shutil.which", lambda candidate: None)
    monkeypatch.setattr("dala.core.browser.Path.is_file", lambda self: str(self) == expected)

    assert resolve_browser_executable() == expected


def test_resolve_browser_executable_finds_windows_program_files_path(monkeypatch):
    expected = "C:/Program Files/Google/Chrome/Application/chrome.exe"

    monkeypatch.delenv("DALA_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setenv("PROGRAMFILES", "C:/Program Files")
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr("dala.core.browser.shutil.which", lambda candidate: None)
    monkeypatch.setattr("dala.core.browser.Path.is_file", lambda self: str(self) == expected)

    assert resolve_browser_executable() == expected


def test_resolve_browser_executable_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv("DALA_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr("dala.core.browser.shutil.which", lambda candidate: None)
    monkeypatch.setattr("dala.core.browser.Path.is_file", lambda self: False)

    assert resolve_browser_executable() is None


@pytest.fixture
def browser_fixture_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/rendered.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", "session=abc123; Path=/")
                self.end_headers()
                self.wfile.write(b"""
                    <html>
                      <head><title>Rendered</title></head>
                      <body>
                        <article id="content">Before script</article>
                        <script>
                          document.querySelector('#content').textContent = 'Rendered by JavaScript';
                        </script>
                      </body>
                    </html>
                """)
                return
            if self.path == "/extension.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><article id='content'>Before extension</article></body></html>")
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _skip_if_browser_unavailable(error: BrowserFetchError):
    message = str(error)
    if "requires Playwright" in message or "Failed to launch Playwright Chromium" in message:
        pytest.skip(message)
    raise error


@pytest.mark.asyncio
async def test_fetch_rendered_source_captures_html_and_cookies(browser_fixture_server):
    try:
        result = await fetch_rendered_source(
            f"{browser_fixture_server}/rendered.html",
            BrowserFetchOptions(settle_ms=100),
        )
    except BrowserFetchError as e:
        _skip_if_browser_unavailable(e)

    assert "Rendered by JavaScript" in result.html
    assert result.cookies["session"] == "abc123"
    assert result.url == f"{browser_fixture_server}/rendered.html"


@pytest.mark.asyncio
async def test_fetch_rendered_source_loads_unpacked_extension(tmp_path, browser_fixture_server):
    extension_dir = tmp_path / "extension"
    extension_dir.mkdir()
    (extension_dir / "manifest.json").write_text(
        """
        {
          "manifest_version": 3,
          "name": "Dala Test Mutator",
          "version": "1.0",
          "content_scripts": [
            {
              "matches": ["http://127.0.0.1/*"],
              "js": ["content.js"],
              "run_at": "document_idle"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    (extension_dir / "content.js").write_text(
        "document.querySelector('#content').textContent = 'Changed by extension';\n",
        encoding="utf-8",
    )

    try:
        result = await fetch_rendered_source(
            f"{browser_fixture_server}/extension.html",
            BrowserFetchOptions(extension_path=str(extension_dir), settle_ms=500),
        )
    except BrowserFetchError as e:
        _skip_if_browser_unavailable(e)

    assert "Changed by extension" in result.html


@pytest.mark.asyncio
async def test_browser_fetches_are_serializable_under_asyncio(browser_fixture_server):
    try:
        results = await asyncio.gather(
            fetch_rendered_source(f"{browser_fixture_server}/rendered.html", BrowserFetchOptions(settle_ms=100)),
            fetch_rendered_source(f"{browser_fixture_server}/rendered.html", BrowserFetchOptions(settle_ms=100)),
        )
    except BrowserFetchError as e:
        _skip_if_browser_unavailable(e)

    assert all("Rendered by JavaScript" in result.html for result in results)

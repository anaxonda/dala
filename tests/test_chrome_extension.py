from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

import pytest


@pytest.fixture
def ping_server():
    try:
        with urlopen("http://127.0.0.1:8000/ping", timeout=1) as resp:
            if resp.status == 200:
                yield
                return
    except Exception:
        pass

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/ping":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _extension_path() -> str:
    return str((Path(__file__).resolve().parents[1] / "extension_chrome").resolve())


def test_forum_mode_forces_cookie_backed_extension_enrichment():
    root = Path(__file__).resolve().parents[1]
    chrome_bg = (root / "extension_chrome" / "background.js").read_text()
    firefox_bg = (root / "firefox_extension" / "background.js").read_text()
    chrome_shared = (root / "extension_chrome" / "shared-forum.js").read_text()
    firefox_shared = (root / "firefox_extension" / "shared-forum.js").read_text()

    for source in (chrome_bg, firefox_bg):
        assert "const include_cookies = options.include_cookies || is_forum;" in source
        assert "const shouldFetchAssets = forceForum || sourceItems.some(item => isLikelyForumUrl(item.url));" in source
        assert "function includeCookiesForSavedOptions(opts)" in source
        assert "return isLocalServerUrl(saved.server_url);" in source
        assert "const include_cookies = includeCookiesForSavedOptions(opts) || is_forum;" in source
        assert "scrapeArticleAssetsFromTab" in source
        assert "article image assets" in source
        assert "collectBrowserContextForDownload" in source
        assert "downloadFromShortcut(tab.url, html, tab.id)" in source
        assert "downloadFromShortcut(target, message.html || null, tabId || lastShortcutTabId)" in source
        assert "const shouldScrapeArticleAssets = !!tabId && !is_forum;" in source
        assert "downloadSingleFromContext(url, tabIdForAssets)" in source
        assert "fetch_assets: is_forum" in source
        assert 'browser_executable: (savedOpts.browser_executable || "").trim() || null' in source
        assert "Forum mode enabled: using browser cookies automatically." in source
        assert "Forum auto-detected for" in source
        assert "svg[data-inject-url]" in source
        assert "data-inject-url" in source
        assert 'url.includes(".svg")' not in source

    assert "shared-forum.js" in chrome_bg
    assert "shared-forum.js" in (root / "firefox_extension" / "manifest.json").read_text()
    for source in (chrome_shared, firefox_shared):
        assert "function isLikelyForumUrl(url)" in source
        assert "function isLikelyForumHtml(html)" in source


def test_popup_exposes_simplified_image_and_pdf_translation_controls():
    root = Path(__file__).resolve().parents[1]
    for popup_path in (root / "extension_chrome" / "popup.html", root / "firefox_extension" / "popup.html"):
        source = popup_path.read_text()
        assert '<option value="compact">Compact</option>' in source
        assert '<option value="balanced">Balanced</option>' in source
        assert '<option value="full">Full</option>' in source
        assert '<option value="kobo_clara">E-reader</option>' in source
        assert "Kobo Clara" not in source
        assert '<option value="baseline">' not in source
        assert '<option value="optimized">' not in source
        assert 'id="image-settings"' in source
        assert "No images" in source
        assert "Text Only" not in source
        assert 'id="btn-retry-failed"' not in source
        assert 'id="opt-date-sort-desc"' in source

    for popup_js_path in (root / "extension_chrome" / "popup.js", root / "firefox_extension" / "popup.js"):
        source = popup_js_path.read_text()
        assert 'if (preset === "baseline") return "balanced";' in source
        assert 'if (preset === "optimized") return "compact";' in source
        assert "imagePreset.disabled = noImages" in source
        assert "grayscale.disabled = noImages" in source
        assert "thumbnails.disabled = noImages" in source
        assert 'date_sort: document.getElementById(\'opt-date-sort-desc\')?.checked ? "desc" : "asc"' in source
        assert "footnoteOption.hidden = isPdf" in source
        assert 'display.value = "underneath";' in source


def _skip_if_playwright_unavailable(exc: Exception):
    message = str(exc)
    if "playwright" in message.lower() or "executable doesn't exist" in message.lower():
        pytest.skip(message)
    raise exc


@pytest.mark.asyncio
async def test_current_chrome_extension_popup_loads_and_updates_queue(tmp_path, ping_server):
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        pytest.skip(f"Playwright is not installed: {exc}")

    extension_path = _extension_path()
    user_data_dir = str(tmp_path / "profile")
    args = [
        f"--disable-extensions-except={extension_path}",
        f"--load-extension={extension_path}",
    ]

    try:
        async with async_playwright() as p:
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir,
                    executable_path="/usr/bin/chromium",
                    headless=True,
                    args=args,
                )
            except Exception as exc:
                _skip_if_playwright_unavailable(exc)

            try:
                workers = context.service_workers
                service_worker = workers[0] if workers else await context.wait_for_event("serviceworker")
                extension_id = service_worker.url.split("/")[2]

                page = await context.new_page()
                await page.goto("http://example.com/test-article")
                await page.goto(f"chrome-extension://{extension_id}/popup.html")

                await page.wait_for_selector("#server-status.online")
                assert "Server Online" in (await page.locator("#server-status").get_attribute("title"))
                await page.wait_for_selector("#btn-download-single")
                assert await page.locator("#pdf-options").evaluate("el => el.classList.contains('hidden')")
                await page.select_option("#opt-output-format", "pdf")
                assert not await page.locator("#pdf-options").evaluate("el => el.classList.contains('hidden')")
                await page.select_option("#opt-pdf-page-size", "kobo_clara")
                await page.select_option("#opt-image-preset", "compact")
                assert await page.locator("#opt-image-color").count() == 0
                await page.click("#opt-grayscale")
                await page.click("#opt-noimages")
                assert await page.locator("#opt-image-preset").is_disabled()
                assert await page.locator("#opt-grayscale").is_disabled()
                assert await page.locator("#opt-thumbnails").is_disabled()
                assert not await page.locator("#date-options").evaluate("el => el.open")
                assert not await page.locator("#forum-options").evaluate("el => el.open")
                assert not await page.locator("#translation-options").evaluate("el => el.open")
                await page.click("#date-options summary")
                await page.click("#forum-options summary")
                await page.click("#translation-options summary")
                assert await page.locator("#date-options").evaluate("el => el.open")
                assert await page.locator("#forum-options").evaluate("el => el.open")
                assert await page.locator("#translation-options").evaluate("el => el.open")
                await page.check("#opt-translate")
                await page.fill("#opt-translation-target", "es")
                assert await page.locator("#translation-language-list option").count() >= 80
                assert await page.locator("#translation-language-list option[value='pt-BR']").count() == 1
                assert await page.locator("#translation-language-list option[value='zh-TW']").count() == 1
                await page.select_option("#opt-translation-provider", "google")
                await page.select_option("#opt-translation-display", "side_by_side")
                assert await page.locator("#opt-translation-display option[value='replace']").count() == 1
                assert await page.locator("#opt-cookies").is_checked()
                await page.check("#opt-forum")
                assert await page.locator("#opt-cookies").is_checked()
                await page.click("#opt-cookies")
                assert await page.locator("#opt-cookies").is_checked()
                await page.uncheck("#opt-forum")
                await page.uncheck("#opt-cookies")
                assert await page.locator("#opt-start-date").get_attribute("type") == "text"
                assert await page.locator("#opt-start-date-picker").get_attribute("type") == "date"
                await page.fill("#opt-start-date", "20260623")
                assert await page.locator("#opt-start-date").input_value() == "2026-06-23"
                await page.click("#opt-start-date-btn")
                await page.wait_for_selector('.date-popover[data-target="opt-start-date"]')
                today_value = await page.evaluate("""
                    () => {
                        const now = new Date();
                        return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
                    }
                """)
                assert await page.locator(".date-popover-year-btn").count() == 2
                assert await page.locator(".date-popover-today").inner_text() == f"Today: {today_value}"
                assert await page.locator(f'.date-popover-day.today[data-date="{today_value}"]').count() == 1
                await page.locator(".date-popover-year-btn").nth(1).click()
                await page.wait_for_function("""
                    () => document.querySelector('.date-popover-title')?.textContent.includes('2027')
                """)
                await page.locator(".date-popover-year-btn").nth(0).click()
                await page.wait_for_function("""
                    () => document.querySelector('.date-popover-title')?.textContent.includes('2026')
                """)
                assert await page.locator('.date-popover-day[data-date="2026-06-23"]').count() == 1
                await page.click('.date-popover-day[data-date="2026-06-23"]')
                assert await page.locator("#opt-start-date").input_value() == "2026-06-23"
                await page.fill("#opt-start-date", "2025-08")
                await page.check("#opt-date-sort-desc")
                await page.evaluate("""
                    const picker = document.querySelector('#opt-end-date-picker');
                    picker.value = '2025-08-31';
                    picker.dispatchEvent(new Event('change', { bubbles: true }));
                """)
                await page.click("#opt-clear-dates")
                cleared_options = await page.evaluate("browser.storage.local.get('savedOptions')")
                assert cleared_options["savedOptions"]["start_date"] == ""
                assert cleared_options["savedOptions"]["end_date"] == ""
                await page.evaluate("""
                    const picker = document.querySelector('#opt-end-date-picker');
                    picker.value = '2025-08-31';
                    picker.dispatchEvent(new Event('change', { bubbles: true }));
                """)
                await page.fill("#opt-start-date", "20260623")
                assert await page.locator("#opt-start-date").input_value() == "2026-06-23"
                assert await page.locator("#opt-end-date").input_value() == ""
                invalid_range_options = await page.evaluate("browser.storage.local.get('savedOptions')")
                assert invalid_range_options["savedOptions"]["start_date"] == "2026-06-23"
                assert invalid_range_options["savedOptions"]["end_date"] == ""
                await page.fill("#opt-start-date", "2025-08")
                await page.evaluate("""
                    const picker = document.querySelector('#opt-end-date-picker');
                    picker.value = '2025-08-31';
                    picker.dispatchEvent(new Event('change', { bubbles: true }));
                """)
                assert not await page.locator("#opt-open-challenge-user-browser").is_checked()
                await page.check("#opt-open-challenge-user-browser")
                assert await page.locator("#opt-date-fallback").count() == 0
                assert await page.locator("#opt-include-undated").count() == 0

                options_page = await context.new_page()
                await options_page.goto(f"chrome-extension://{extension_id}/options.html")
                await options_page.wait_for_selector("#opt-browser-fallback")
                assert await options_page.locator("#opt-browser-fallback").is_checked()
                assert await options_page.locator("#opt-open-challenge-user-browser").is_checked()
                assert await options_page.locator("#init-browser-profile").count() == 1
                await options_page.click("#check-browser-fallback")
                await options_page.wait_for_function("""
                    () => document.querySelector('#server-diagnostics')?.textContent.includes('Server:')
                """)
                await options_page.fill("#opt-server-url", "192.168.1.10:8000/")
                await options_page.dispatch_event("#opt-server-url", "change")
                await options_page.fill("#opt-browser-extension-path", "/tmp/bpc")
                await options_page.dispatch_event("#opt-browser-extension-path", "change")
                await options_page.fill("#opt-browser-profile-dir", "/tmp/dala-chromium-profile")
                await options_page.dispatch_event("#opt-browser-profile-dir", "change")
                await options_page.fill("#opt-browser-executable", "/usr/bin/google-chrome")
                await options_page.dispatch_event("#opt-browser-executable", "change")
                await options_page.check("#opt-open-challenge-user-browser")

                saved_options = await page.evaluate("browser.storage.local.get('savedOptions')")
                assert saved_options["savedOptions"]["output_format"] == "pdf"
                assert saved_options["savedOptions"]["pdf_page_size"] == "kobo_clara"
                assert saved_options["savedOptions"]["pdf_preset"] == "ereader"
                assert saved_options["savedOptions"]["image_preset"] == "compact"
                assert saved_options["savedOptions"]["image_color"] == "grayscale"
                assert saved_options["savedOptions"]["no_images"] is True
                assert saved_options["savedOptions"]["start_date"] == "2025-08"
                assert saved_options["savedOptions"]["end_date"] == "2025-08-31"
                assert saved_options["savedOptions"]["date_sort"] == "desc"
                assert saved_options["savedOptions"]["date_fallback"] == "auto"
                assert saved_options["savedOptions"]["include_undated"] is False
                assert saved_options["savedOptions"]["browser_challenge_action"] == "user_browser"
                assert saved_options["savedOptions"]["server_url"] == "http://192.168.1.10:8000"
                assert saved_options["savedOptions"]["browser_fallback"] is True
                assert saved_options["savedOptions"]["browser_challenge_action"] == "user_browser"
                assert saved_options["savedOptions"]["browser_extension_path"] == "/tmp/bpc"
                assert saved_options["savedOptions"]["browser_profile_dir"] == "/tmp/dala-chromium-profile"
                assert saved_options["savedOptions"]["browser_executable"] == "/usr/bin/google-chrome"

                await page.click("#tab-queue")
                assert await page.locator("#btn-retry-failed").count() == 0
                assert await page.locator("#date-options").evaluate("el => el.classList.contains('hidden')")
                assert await page.locator("#forum-options").evaluate("el => el.classList.contains('hidden')")

                await page.fill("#queue-editor", "https://example.com/a\nhttps://example.com/a\nnot-a-url\nhttps://example.com/b")
                await page.wait_for_timeout(500)

                queue = await page.evaluate("browser.storage.local.get('urlQueue')")
                assert [item["url"] for item in queue["urlQueue"]] == ["https://example.com/a", "https://example.com/b"]
                assert all(item["saved_at"] for item in queue["urlQueue"])
                assert await page.locator("#queue-count").inner_text() == "(2)"
            finally:
                await context.close()
    except Exception as exc:
        _skip_if_playwright_unavailable(exc)

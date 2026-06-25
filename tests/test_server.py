import pytest
import time
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
import server # Import to make sure app is loaded
from server import app
from dala.models import BookData, Chapter, Source
from dala.core.browser import BrowserChallengeError

client = TestClient(app)

def test_ping():
    response = client.get("/ping")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "server_version" in body
    assert "job_retention_seconds" in body
    assert "browser_fallback_available" in body
    assert "browser_executable_found" in body
    assert "playwright_available" in body
    assert "pdf_available" in body
    assert "bpc_extension_valid" in body


def test_browser_config_status_requires_playwright_for_browser_and_pdf(monkeypatch):
    monkeypatch.setattr(server, "is_playwright_available", lambda: False)
    monkeypatch.setattr(server, "resolve_browser_executable", lambda configured=None: "/usr/bin/chromium")
    monkeypatch.setattr(server, "browser_executable_exists", lambda executable: True)

    status = server._browser_config_status()

    assert status["browser_executable_found"] is True
    assert status["playwright_available"] is False
    assert status["browser_fallback_available"] is False
    assert status["pdf_available"] is False


def test_browser_config_status_reports_pdf_available_with_playwright_and_chromium(monkeypatch):
    monkeypatch.setattr(server, "is_playwright_available", lambda: True)
    monkeypatch.setattr(server, "resolve_browser_executable", lambda configured=None: "/usr/bin/google-chrome")
    monkeypatch.setattr(server, "browser_executable_exists", lambda executable: True)

    status = server._browser_config_status()

    assert status["browser_fallback_available"] is True
    assert status["pdf_available"] is True
    assert status["browser_executable"] == "/usr/bin/google-chrome"


def test_build_options_normalizes_legacy_image_preset():
    req = server.ConversionRequest(
        sources=[server.SourceItem(url="https://example.com/article")],
        image_preset="optimized",
    )

    options, sources = server._build_options_and_sources(req)

    assert options.image_preset == "compact"
    assert sources[0].url == "https://example.com/article"


@pytest.mark.asyncio
async def test_cleanup_finished_jobs_removes_old_output(tmp_path):
    output = tmp_path / "old.epub"
    output.write_text("old")
    old = server.JobRecord(
        job_id="old-job",
        status="completed",
        created_at="2020-01-01T00:00:00+00:00",
        updated_at="2020-01-01T00:00:00+00:00",
        output_path=str(output),
    )
    async with server.JOBS_LOCK:
        server.JOBS[old.job_id] = old

    removed = await server.cleanup_finished_jobs(retention_seconds=0)

    assert removed >= 1
    assert not output.exists()
    async with server.JOBS_LOCK:
        assert "old-job" not in server.JOBS


def test_single_title_override_ignores_generic_youtube_title():
    assert server.should_apply_single_title_override(
        "YouTube",
        "Actual Video Title",
        "https://www.youtube.com/watch?v=abc123",
    ) is False


def test_single_title_override_allows_custom_youtube_title():
    assert server.should_apply_single_title_override(
        "My Custom Video Notes",
        "Actual Video Title",
        "https://www.youtube.com/watch?v=abc123",
    ) is True


def test_extract_links_returns_xenforo_next_page_url():
    response = client.post(
        "/helper/extract-links",
        json={
            "url": "https://www.mtbr.com/threads/example.123/",
            "html": """
                <html>
                  <head><title>Thread</title></head>
                  <body>
                    <nav class="pageNavWrapper">
                      <a class="pageNav-page" href="/threads/example.123/page-2">2</a>
                      <a class="pageNav-jump pageNav-jump--next" href="/threads/example.123/page-2">Next</a>
                    </nav>
                    <article class="message message--post">
                      <div class="bbWrapper">
                        <img src="/attachments/image-jpg.100/?auto=webp">
                      </div>
                    </article>
                  </body>
                </html>
            """,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["next_page_url"] == "https://www.mtbr.com/threads/example.123/page-2"
    assert body["next_page_num"] == 2
    assert body["assets"][0]["url"] == "https://www.mtbr.com/attachments/image-jpg.100/?auto=webp"


def test_extract_links_falls_back_to_generic_article_images():
    response = client.post(
        "/helper/extract-links",
        json={
            "url": "https://www.science.org/content/article/example",
            "html": """
                <html>
                  <body>
                    <head>
                      <meta property="og:image" content="/hero.jpg">
                    </head>
                    <article>
                      <p>Article body</p>
                      <img src="/small.jpg"
                           srcset="/image-400.jpg 400w, /image-1200.jpg 1200w"
                           alt="Main article image">
                      <figure>
                        <img data-src="/hidden-timelapse-2024.png" alt="Map showing 2024">
                        <!--<img src="/commented-timelapse-2000.png" alt="Map showing 2000">-->
                      </figure>
                      <figure>
                        <svg data-inject-url="/cms/asset/wolf_population_locator.svg"></svg>
                        <figcaption>The mapped range shows permanent wolf presence.</figcaption>
                      </figure>
                      <img src="/fence.jpg" alt="A person installing a fence with a t-shirt showing a logo and the words Wolf Fencing Team Belgium.">
                      <svg data-inject-url="/assets/logo.svg"></svg>
                    </article>
                  </body>
                </html>
            """,
        },
    )

    assert response.status_code == 200
    body = response.json()
    urls = [item["url"] for item in body["assets"]]
    assert "https://www.science.org/image-1200.jpg" in urls
    assert "https://www.science.org/hidden-timelapse-2024.png" in urls
    assert "https://www.science.org/hero.jpg" in urls
    assert "https://www.science.org/commented-timelapse-2000.png" in urls
    assert "https://www.science.org/fence.jpg" in urls
    assert "https://www.science.org/cms/asset/wolf_population_locator.svg" in urls
    assert "https://www.science.org/assets/logo.svg" not in urls


@patch("main.process_urls", new_callable=AsyncMock)
@patch("server.write_output_book", new_callable=AsyncMock)
def test_convert_endpoint(mock_write, mock_process):
    # Mock the core processing to return a dummy book
    dummy_book = BookData(
        title="Test Book",
        author="Test Author",
        uid="urn:test",
        language="en",
        description="desc",
        source_url="http://example.com",
        chapters=[Chapter(title="C1", filename="c1.xhtml", content_html="<p>Hi</p>", uid="c1")]
    )
    mock_process.return_value = [dummy_book]

    payload = {
        "sources": [
            {
                "url": "http://example.com/article",
                "html": "<html>...</html>",
                "is_forum": False
            }
        ],
        "no_images": True,
        "bundle_title": "My Bundle",
        "image_preset": "optimized",
        "image_color": "grayscale",
        "max_bundle_images": 12,
        "max_image_bytes_mb": 34,
        "browser_fallback": True,
        "browser_extension_path": "/tmp/bpc",
        "browser_timeout_ms": 12345,
        "browser_wait_until": "domcontentloaded",
        "browser_settle_ms": 250,
        "browser_challenge_action": "user_browser",
        "llm_provider": "gemini",
        "llm_model": "gemini-3.1-flash-lite",
        "translation_enabled": True,
        "translation_provider": "google",
        "translation_target_lang": "es",
        "translation_source_lang": "en",
        "translation_display": "side_by_side",
        "translation_scope": "all-readable",
        "translation_glossary": "KOReader=KOReader",
        "translation_cache": False,
    }
    
    response = client.post("/convert", json=payload)
    
    # Debug info if failed
    if response.status_code != 200:
        print(response.json())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/epub+zip"
    
    # Verify core.process_urls was called correctly
    assert mock_process.called
    args, kwargs = mock_process.call_args
    
    # args[0] is sources list
    sources = args[0]
    assert len(sources) == 1
    assert sources[0].url == "http://example.com/article"
    assert sources[0].html == "<html>...</html>"
    
    # args[1] is options
    options = args[1]
    assert options.no_images is True
    assert options.image_preset == "compact"
    assert options.image_color == "grayscale"
    assert options.max_bundle_images == 12
    assert options.max_image_bytes_mb == 34
    assert options.browser_fallback is True
    assert options.browser_extension_path == "/tmp/bpc"
    assert options.browser_profile_dir == server.DEFAULT_BROWSER_PROFILE_DIR
    assert options.browser_timeout_ms == 12345
    assert options.browser_wait_until == "domcontentloaded"
    assert options.browser_settle_ms == 250
    assert options.browser_challenge_action == "user_browser"
    assert options.llm_provider == "gemini"
    assert options.llm_model == "gemini-3.1-flash-lite"
    assert options.translation_enabled is True
    assert options.translation_provider == "google"
    assert options.translation_target_lang == "es"
    assert options.translation_source_lang == "en"
    assert options.translation_display == "side_by_side"
    assert options.translation_scope == "all-readable"
    assert options.translation_glossary == "KOReader=KOReader"
    assert options.translation_cache is False
    
    # Verify bundle creation logic (if applicable) or just single book return
    # The server logic handles single vs bundle.
    assert "filename" in response.headers["content-disposition"]


@patch("server.TranslationProcessor.test_provider", new_callable=AsyncMock)
def test_translation_test_helper(mock_test_provider):
    mock_test_provider.return_value = "Hola mundo."

    response = client.post("/helper/translation/test", json={
        "text": "Hello world.",
        "translation_provider": "llm",
        "translation_target_lang": "es",
        "translation_source_lang": "en",
        "translation_glossary": "KOReader=KOReader",
        "llm_provider": "openrouter",
        "llm_model": "deepseek/deepseek-v4-flash",
    })

    assert response.status_code == 200
    assert response.json()["translated_text"] == "Hola mundo."
    options = mock_test_provider.call_args.args[1]
    assert options.translation_provider == "llm"
    assert options.translation_target_lang == "es"
    assert options.translation_glossary == "KOReader=KOReader"
    assert options.llm_provider == "openrouter"
    assert options.llm_model == "deepseek/deepseek-v4-flash"


def test_translation_status_helper_does_not_expose_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = client.get("/helper/translation/status")

    assert response.status_code == 200
    body = response.json()
    assert body["keys"] == {"gemini": True, "openrouter": False, "openai": False}
    assert body["recommended_provider"] == "gemini"
    assert body["recommended_model"] == "gemini-3.1-flash-lite"
    assert "secret-gemini" not in response.text


def test_translation_cache_clear_helper(monkeypatch, tmp_path):
    cache_path = tmp_path / "translations.sqlite"
    cache_path.write_text("cache")
    monkeypatch.setenv("DALA_TRANSLATION_CACHE", str(cache_path))

    response = client.post("/helper/translation/cache/clear")

    assert response.status_code == 200
    assert response.json()["cleared"] is True
    assert not cache_path.exists()


@patch("main.process_urls", new_callable=AsyncMock)
@patch("server.write_output_book", new_callable=AsyncMock)
def test_convert_keeps_extracted_youtube_title_when_popup_title_is_generic(mock_write, mock_process):
    dummy_book = BookData(
        title="Actual Video Title",
        author="Channel",
        uid="urn:youtube:test",
        language="en",
        description="desc",
        source_url="https://www.youtube.com/watch?v=abc123",
        chapters=[Chapter(title="Actual Video Title", filename="video.xhtml", content_html="<p>Hi</p>", uid="video")]
    )
    mock_process.return_value = [dummy_book]

    response = client.post("/convert", json={
        "sources": [{"url": "https://www.youtube.com/watch?v=abc123"}],
        "bundle_title": "YouTube",
    })

    assert response.status_code == 200
    assert "Actual_Video_Title.epub" in response.headers["content-disposition"]
    assert "YouTube.epub" not in response.headers["content-disposition"]


@patch("main.process_urls", new_callable=AsyncMock)
@patch("server.write_output_book", new_callable=AsyncMock)
def test_jobs_endpoint_lifecycle(mock_write, mock_process):
    dummy_book = BookData(
        title="Job Book",
        author="Test Author",
        uid="urn:test-job",
        language="en",
        description="desc",
        source_url="http://example.com",
        chapters=[Chapter(title="C1", filename="c1.xhtml", content_html="<p>Hi</p>", uid="c1")]
    )
    mock_process.return_value = [dummy_book]

    payload = {
        "sources": [
            {
                "url": "http://example.com/article",
                "html": "<html>...</html>",
                "is_forum": False
            }
        ],
        "no_images": True
    }

    submitted = client.post("/jobs", json=payload)
    assert submitted.status_code == 200
    job_id = submitted.json()["job_id"]

    status = None
    for _ in range(20):
        status = client.get(f"/jobs/{job_id}")
        assert status.status_code == 200
        if status.json()["status"] == "completed":
            break
        time.sleep(0.05)

    body = status.json()
    assert body["status"] == "completed"
    assert body["download_ready"] is True
    assert body["processed_sources"] == 1

    downloaded = client.get(f"/jobs/{job_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/epub+zip"


@patch("server.BROWSER_WARM_MANAGER.start_session", new_callable=AsyncMock)
@patch("main.process_urls", new_callable=AsyncMock)
def test_jobs_endpoint_pauses_for_browser_verification(mock_process, mock_warm):
    mock_process.side_effect = BrowserChallengeError("https://www.wsj.com/article", "verification required")
    mock_warm.return_value = server.WarmSession(
        warm_id="warm-token",
        url="https://www.wsj.com/article",
        created_at=server._utc_now(),
        expires_at=time.time() + 60,
        job_id=None,
        marker="verification required",
    )

    submitted = client.post("/jobs", json={
        "sources": [{"url": "https://www.wsj.com/article"}],
        "browser_fallback": True,
        "browser_challenge_action": "warm",
    })
    assert submitted.status_code == 200
    job_id = submitted.json()["job_id"]

    status = None
    for _ in range(20):
        status = client.get(f"/jobs/{job_id}")
        assert status.status_code == 200
        if status.json()["status"] == "verification_required":
            break
        time.sleep(0.05)

    body = status.json()
    assert body["status"] == "verification_required"
    assert body["verification_url"] == "/browser/warm/warm-token"
    assert body["verification_token"] == "warm-token"
    assert body["verification_marker"] == "verification required"
    assert body["verification_source_url"] == "https://www.wsj.com/article"


@patch("main.process_urls", new_callable=AsyncMock)
def test_jobs_endpoint_opens_challenge_in_user_browser(mock_process):
    mock_process.side_effect = BrowserChallengeError("https://www.nytimes.com/article", "geo.captcha-delivery.com")

    submitted = client.post("/jobs", json={
        "sources": [{"url": "https://www.nytimes.com/article"}],
        "browser_fallback": True,
        "browser_challenge_action": "user_browser",
    })
    assert submitted.status_code == 200
    job_id = submitted.json()["job_id"]

    status = None
    for _ in range(20):
        status = client.get(f"/jobs/{job_id}")
        assert status.status_code == 200
        if status.json()["status"] == "user_browser_required":
            break
        time.sleep(0.05)

    body = status.json()
    assert body["status"] == "user_browser_required"
    assert body["user_browser_url"] == "https://www.nytimes.com/article"
    assert body["verification_marker"] == "geo.captcha-delivery.com"


@patch("main.process_urls", new_callable=AsyncMock)
@patch("server.write_output_book", new_callable=AsyncMock)
def test_convert_endpoint_pdf_output(mock_write, mock_process):
    dummy_book = BookData(
        title="PDF Book",
        author="Test Author",
        uid="urn:test-pdf",
        language="en",
        description="desc",
        source_url="http://example.com",
        chapters=[Chapter(title="C1", filename="c1.xhtml", content_html="<p>Hi</p>", uid="c1")]
    )
    mock_process.return_value = [dummy_book]

    response = client.post("/convert", json={
        "sources": [{"url": "http://example.com/article", "html": "<html>...</html>"}],
        "output_format": "pdf",
        "pdf_preset": "ereader",
        "pdf_page_size": "kobo_clara",
    })

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "PDF_Book.pdf" in response.headers["content-disposition"]
    args, kwargs = mock_write.call_args
    options = args[2]
    assert options.output_format == "pdf"
    assert options.pdf_preset == "ereader"
    assert options.pdf_page_size == "kobo_clara"


@patch("server.discover_posts_for_sources", new_callable=AsyncMock)
@patch("main.process_urls", new_callable=AsyncMock)
@patch("server.write_output_book", new_callable=AsyncMock)
def test_convert_endpoint_date_range_discovers_sources(mock_write, mock_process, mock_discover):
    dummy_book = BookData(
        title="Discovered Book",
        author="Test Author",
        uid="urn:test-discovered",
        language="en",
        description="desc",
        source_url="http://example.com/2025/08/15/post",
        chapters=[Chapter(title="C1", filename="c1.xhtml", content_html="<p>Hi</p>", uid="c1")]
    )
    mock_discover.return_value = [Source(url="http://example.com/2025/08/15/post")]
    mock_process.return_value = [dummy_book]

    response = client.post("/convert", json={
        "sources": [{"url": "http://example.com/2025/08/"}],
        "start_date": "2025-08-01",
        "end_date": "2025-08-31",
        "date_fallback": "metadata",
        "max_discovery_pages": 2,
        "max_discovered_posts": 10,
    })

    assert response.status_code == 200
    assert mock_discover.called
    discovered_options = mock_discover.call_args.args[2]
    assert discovered_options.start_date == "2025-08-01"
    assert discovered_options.end_date == "2025-08-31"
    assert discovered_options.max_discovery_pages == 2
    assert discovered_options.max_discovered_posts == 10
    process_sources = mock_process.call_args.args[0]
    assert [source.url for source in process_sources] == ["http://example.com/2025/08/15/post"]

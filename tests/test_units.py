import pytest
from bs4 import BeautifulSoup
import main
from dala.core.dispatcher import DriverDispatcher
from dala.core.extractor import ArticleExtractor
from dala.core.image_processor import BaseImageProcessor, ForumImageProcessor, ImageProcessor
from dala.core.browser import BrowserFetchError, BrowserFetchOptions, BrowserFetchResult
from dala.drivers.forum import ForumDriver
from dala.drivers.generic import GenericDriver
from dala.drivers.hn import HackerNewsDriver
from dala.drivers.substack import SubstackDriver
from dala.models import BookData, Chapter, ConversionOptions, Source


def test_legacy_web_to_epub_shim_exports_public_symbols():
    import web_to_epub

    assert web_to_epub.Source is Source
    assert web_to_epub.DriverDispatcher is DriverDispatcher
    assert web_to_epub.ForumImageProcessor is ForumImageProcessor
    assert callable(web_to_epub.process_urls)


def test_driver_dispatch_explicit_forum():
    src = Source(url="http://example.com", is_forum=True)
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, ForumDriver)

def test_driver_dispatch_hn():
    src = Source(url="https://news.ycombinator.com/item?id=123")
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, HackerNewsDriver)

def test_driver_dispatch_substack():
    src = Source(url="https://test.substack.com/p/123")
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, SubstackDriver)

def test_driver_dispatch_generic():
    src = Source(url="https://example.com")
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, GenericDriver)

def test_is_junk_generic():
    assert BaseImageProcessor.is_junk("https://example.com/spacer.gif")
    assert not BaseImageProcessor.is_junk("https://example.com/photo.jpg")


def test_srcset_parser_preserves_commas_inside_urls():
    srcset = (
        "https://i.cbc.ca/ais/3130066c-279a-4d0c-af51-2d594b78680f,1782062951783/full/max/0/default.jpg?"
        "im=Crop%2Crect%3D%280%2C411%2C3500%2C1968%29%3BResize%3D860 860w,"
        "https://i.cbc.ca/ais/3130066c-279a-4d0c-af51-2d594b78680f,1782062951783/full/max/0/default.jpg?"
        "im=Crop%2Crect%3D%280%2C411%2C3500%2C1968%29%3BResize%3D1280 1280w"
    )

    parsed = ImageProcessor.parse_srcset_with_width(srcset)

    assert parsed[0] == (
        1280,
        "https://i.cbc.ca/ais/3130066c-279a-4d0c-af51-2d594b78680f,1782062951783/full/max/0/default.jpg?"
        "im=Crop%2Crect%3D%280%2C411%2C3500%2C1968%29%3BResize%3D1280",
    )
    assert parsed[1][1].startswith("https://i.cbc.ca/ais/3130066c-")


def test_interactive_timelapse_sequence_collapses_to_latest_frame():
    soup = BeautifulSoup(
        """
        <article>
          <figure class="news-article__figure">
            <figcaption class="news-article__figure__upper-caption">
              <h3>A stunning recovery</h3>
              <p>Drag the slider or press the play button to see the change.</p>
            </figcaption>
            <div id="timelapse_images">
              <img src="/do/story/files/_20260618_nf_wolves_timelapse_2000.png" alt="Map showing wolf range in monitoring year 2000"/>
              <img src="/do/story/files/_20260618_nf_wolves_timelapse_2001.png" alt="Map showing wolf range in monitoring year 2001"/>
              <img src="/do/story/files/_20260618_nf_wolves_timelapse_2002.png" alt="Map showing wolf range in monitoring year 2002"/>
              <img src="/do/story/files/_20260618_nf_wolves_timelapse_2024.png" alt="Map showing wolf range in monitoring year 2024"/>
            </div>
            <div class="timelapse_controls"><button>Play</button></div>
            <div class="text-xs letter-spacing-default mt-2">The map shows wolf territories.</div>
          </figure>
        </article>
        """,
        "html.parser",
    )

    ImageProcessor._simplify_interactive_image_sequences(soup)

    imgs = soup.find_all("img")
    assert len(imgs) == 1
    assert imgs[0]["src"].endswith("timelapse_2024.png")
    assert "timelapse_controls" not in str(soup)


def test_is_junk_forum():
    assert ForumImageProcessor.is_junk("https://example.com/reaction_id=5")
    assert not ForumImageProcessor.is_junk("https://example.com/attachment/123.jpg")

def test_build_meta_block_uses_compact_source_label():
    meta = ArticleExtractor.build_meta_block(
        "https://velovefamily.wordpress.com/2025/08/15/coye-la-foret-paris-coye-la-foret",
        {"author": "Unknown", "date": "2025-08-15", "sitename": "VeLove Family"},
    )

    assert "Article Source:" not in meta
    assert "<strong>Source:</strong>" in meta
    assert ">VeLove Family</a>" in meta
    assert "href=\"https://velovefamily.wordpress.com/2025/08/15/coye-la-foret-paris-coye-la-foret\"" in meta
    assert "<strong>Date:</strong> 2025-08-15" in meta

def test_cookies_for_source_url_matches_any_source():
    entries = [
        {"domain": "example.com", "name": "session", "value": "abc"},
        {"domain": "other.com", "name": "ignored", "value": "no"},
    ]

    assert main.cookies_for_source_url(entries, "https://example.com/article") == {"session": "abc"}
    assert main.cookies_for_source_url(entries, "https://sub.example.com/article") == {"session": "abc"}
    assert main.cookies_for_source_url(entries, "https://example.com:8443/article") == {"session": "abc"}
    assert main.cookies_for_source_url(entries, "https://other.net/article") is None

def test_parse_args_accepts_browser_flags(monkeypatch, tmp_path):
    extension = tmp_path / "extension"
    profile = tmp_path / "profile"
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "--browser",
            "--browser-extension",
            str(extension),
            "--browser-profile",
            str(profile),
            "--browser-executable",
            "/usr/bin/chromium",
            "--headed",
            "--browser-timeout-ms",
            "12000",
            "--browser-wait-until",
            "domcontentloaded",
            "--browser-settle-ms",
            "250",
            "--image-preset",
            "compact",
            "--image-color",
            "grayscale",
            "--max-bundle-images",
            "10",
            "--max-image-bytes-mb",
            "25",
            "--format",
            "pdf",
            "--pdf-preset",
            "ereader",
            "--pdf-page-size",
            "kobo_clara",
            "--start-date",
            "2025-08-01",
            "--end-date",
            "2025-08-31",
            "--date-fallback",
            "shallow",
            "--include-undated",
            "--max-discovery-pages",
            "3",
            "--max-discovered-posts",
            "12",
            "--translate",
            "es",
            "--translation-provider",
            "google",
            "--translation-source",
            "en",
            "--translation-display",
            "side-by-side",
            "--translation-scope",
            "all-readable",
            "--translation-glossary",
            "glossary.txt",
            "--no-translation-cache",
            "https://example.com",
        ],
    )

    args = main.parse_args()

    assert args.browser is True
    assert args.browser_extension == str(extension)
    assert args.browser_profile == str(profile)
    assert args.browser_executable == "/usr/bin/chromium"
    assert args.headed is True
    assert args.browser_timeout_ms == 12000
    assert args.browser_wait_until == "domcontentloaded"
    assert args.browser_settle_ms == 250
    assert args.image_preset == "compact"
    assert args.image_color == "grayscale"
    assert args.max_bundle_images == 10
    assert args.max_image_bytes_mb == 25
    assert args.output_format == "pdf"
    assert args.pdf_preset == "ereader"
    assert args.pdf_page_size == "kobo_clara"
    assert args.start_date == "2025-08-01"
    assert args.end_date == "2025-08-31"
    assert args.date_fallback == "shallow"
    assert args.include_undated is True
    assert args.max_discovery_pages == 3
    assert args.max_discovered_posts == 12
    assert args.translation_target_lang == "es"
    assert args.translation_provider == "google"
    assert args.translation_source == "en"
    assert args.translation_display == "side-by-side"
    assert args.translation_scope == "all-readable"
    assert args.translation_glossary == "glossary.txt"
    assert args.no_translation_cache is True


def test_parse_args_accepts_replace_translation_display(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "--translate",
            "es",
            "--translation-display",
            "replace",
            "https://example.com",
        ],
    )

    args = main.parse_args()

    assert args.translation_target_lang == "es"
    assert args.translation_display == "replace"


@pytest.mark.asyncio
async def test_process_urls_retries_generic_when_forced_forum_has_no_posts(monkeypatch):
    calls = {"forum": 0, "generic": 0}

    async def fake_forum_prepare(self, context, source):
        calls["forum"] += 1
        return None

    async def fake_generic_prepare(self, context, source):
        calls["generic"] += 1
        assert source.is_forum is False
        return BookData(
            title="Article",
            author="Author",
            uid="urn:article",
            language="en",
            description="",
            source_url=source.url,
            chapters=[Chapter(title="Article", filename="index.xhtml", content_html="<p>Body</p>", uid="article")],
        )

    monkeypatch.setattr(ForumDriver, "prepare_book_data", fake_forum_prepare)
    monkeypatch.setattr(GenericDriver, "prepare_book_data", fake_generic_prepare)

    books = await main.process_urls(
        [Source(url="https://www.theguardian.com/us-news/2026/jun/23/example", is_forum=True)],
        ConversionOptions(),
        session=object(),
    )

    assert len(books) == 1
    assert books[0].title == "Article"
    assert calls == {"forum": 1, "generic": 1}

@pytest.mark.asyncio
async def test_acquire_browser_sources_merges_rendered_html_and_cookies(monkeypatch):
    async def fake_fetch(url, options):
        assert isinstance(options, BrowserFetchOptions)
        return BrowserFetchResult(
            url=f"{url}/final",
            html="<html><article>Rendered</article></html>",
            cookies={"browser": "cookie"},
            assets=[{"original_url": "https://example.com/image.jpg", "content": "abc"}],
        )

    monkeypatch.setattr(main, "fetch_rendered_source", fake_fetch)
    sources = [Source(url="https://example.com/article", cookies={"existing": "cookie"}, is_forum=True)]

    captured = await main.acquire_browser_sources(sources, BrowserFetchOptions())

    assert len(captured) == 1
    assert captured[0].url == "https://example.com/article/final"
    assert captured[0].html == "<html><article>Rendered</article></html>"
    assert captured[0].cookies == {"existing": "cookie", "browser": "cookie"}
    assert captured[0].assets == [{"original_url": "https://example.com/image.jpg", "content": "abc"}]
    assert captured[0].is_forum is True


@pytest.mark.asyncio
async def test_acquire_browser_sources_keeps_source_when_browser_fetch_fails(monkeypatch):
    async def fake_fetch(url, options):
        raise BrowserFetchError("challenge")

    monkeypatch.setattr(main, "fetch_rendered_source", fake_fetch)
    source = Source(url="https://example.com/article", cookies={"existing": "cookie"})

    captured = await main.acquire_browser_sources([source], BrowserFetchOptions())

    assert captured == [source]

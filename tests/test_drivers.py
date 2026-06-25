import asyncio
import base64
import logging
import pytest
import aiohttp
from PIL import Image
from io import BytesIO
from unittest.mock import patch, MagicMock
from aioresponses import aioresponses
from bs4 import BeautifulSoup
from dala.core.browser import BrowserChallengeError, BrowserFetchOptions, BrowserFetchResult
from dala.core.dispatcher import DriverDispatcher
from dala.core.extractor import ArticleExtractor
from dala.core.image_processor import ImageProcessor
from dala.drivers.forum import ForumDriver
from dala.drivers.generic import GenericDriver
from dala.drivers.wordpress import WordPressDriver
from dala.drivers.youtube import YouTubeDriver
from dala.models import ARCHIVE_ORG_API_BASE, ConversionContext, ConversionOptions, ImageAsset, Source

@pytest.mark.asyncio
async def test_generic_driver_fetch_success():
    url = "https://example.com/article"
    html = """<html><head><title>Test Article</title></head>
              <body><h1>Test Article</h1><p>Some content.</p></body></html>"""
    
    with aioresponses() as m:
        m.get(url, status=200, body=html)
        
        async with aiohttp.ClientSession() as session:
            options = ConversionOptions()
            context = ConversionContext(session=session, options=options)
            source = Source(url=url)
            driver = GenericDriver()
            
            book = await driver.prepare_book_data(context, source)
            
            assert book is not None
            assert book.title == "Test Article"
            assert "Some content" in book.chapters[0].content_html
            assert book.source_url == url


class FakeFetchedTranscript:
    def __init__(self, rows):
        self._rows = rows

    def to_raw_data(self):
        return self._rows


class FakeYouTubeTranscript:
    def __init__(self, language_code, text, is_generated=False, translated=None, translate_error=None):
        self.language_code = language_code
        self.is_generated = is_generated
        self.text = text
        self.translated = translated
        self.translate_error = translate_error
        self.translate_calls = []

    def translate(self, language_code):
        self.translate_calls.append(language_code)
        if self.translate_error:
            raise self.translate_error
        if self.translated:
            return self.translated
        return FakeYouTubeTranscript(language_code, f"{language_code}: {self.text}", is_generated=self.is_generated)

    def fetch(self):
        return FakeFetchedTranscript([
            {"text": self.text, "start": 0.0, "duration": 1.0},
            {"text": "Second sentence.", "start": 2.0, "duration": 1.0},
        ])


class FakeYouTubeTranscriptApi:
    transcripts = []

    def list(self, video_id):
        return list(self.transcripts)


async def prepare_fake_youtube_book(monkeypatch, transcripts, options):
    FakeYouTubeTranscriptApi.transcripts = transcripts

    async def fake_fetch_with_retry(session, target_url, response_type="json", **kwargs):
        html = """
        <html><head>
          <meta property="og:title" content="Video Title">
          <meta property="og:description" content="Description">
        </head><body></body></html>
        """
        return html, target_url

    monkeypatch.setattr("dala.drivers.youtube.YouTubeTranscriptApi", FakeYouTubeTranscriptApi)
    monkeypatch.setattr("dala.drivers.youtube.fetch_with_retry", fake_fetch_with_retry)

    async with aiohttp.ClientSession() as session:
        return await YouTubeDriver().prepare_book_data(
            ConversionContext(session=session, options=options),
            Source(url="https://www.youtube.com/watch?v=abc123"),
        )


@pytest.mark.asyncio
async def test_youtube_replace_translation_prefers_target_language_transcript(monkeypatch):
    en = FakeYouTubeTranscript("en", "English transcript.")
    es = FakeYouTubeTranscript("es", "Transcripción española.")

    book = await prepare_fake_youtube_book(
        monkeypatch,
        [en, es],
        ConversionOptions(
            no_images=True,
            no_comments=True,
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="replace",
        ),
    )

    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    transcript = soup.select_one(".transcript-body")

    assert "Transcripción española." in transcript.get_text(" ", strip=True)
    assert "dala-translation-skip" in transcript.get("class")
    assert transcript.get("lang") == "es"
    assert en.translate_calls == []


@pytest.mark.asyncio
async def test_youtube_replace_translation_uses_youtube_api_translation(monkeypatch):
    translated = FakeYouTubeTranscript("es", "Traducción de YouTube.")
    en = FakeYouTubeTranscript("en", "English transcript.", translated=translated)

    book = await prepare_fake_youtube_book(
        monkeypatch,
        [en],
        ConversionOptions(
            no_images=True,
            no_comments=True,
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="replace",
        ),
    )

    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    transcript = soup.select_one(".transcript-body")

    assert en.translate_calls == ["es"]
    assert "Traducción de YouTube." in transcript.get_text(" ", strip=True)
    assert "dala-translation-skip" in transcript.get("class")
    assert transcript.get("lang") == "es"


@pytest.mark.asyncio
async def test_youtube_replace_translation_falls_back_to_external_translation(monkeypatch):
    en = FakeYouTubeTranscript("en", "English transcript.", translate_error=RuntimeError("unavailable"))

    book = await prepare_fake_youtube_book(
        monkeypatch,
        [en],
        ConversionOptions(
            no_images=True,
            no_comments=True,
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="replace",
        ),
    )

    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    transcript = soup.select_one(".transcript-body")

    assert en.translate_calls == ["es"]
    assert "English transcript." in transcript.get_text(" ", strip=True)
    assert "dala-translation-skip" not in transcript.get("class")


@pytest.mark.asyncio
async def test_youtube_additive_translation_keeps_youtube_language_preference(monkeypatch):
    en = FakeYouTubeTranscript("en", "English transcript.")
    es = FakeYouTubeTranscript("es", "Transcripción española.")

    book = await prepare_fake_youtube_book(
        monkeypatch,
        [es, en],
        ConversionOptions(
            no_images=True,
            no_comments=True,
            youtube_lang="en",
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="underneath",
        ),
    )

    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    transcript = soup.select_one(".transcript-body")

    assert "English transcript." in transcript.get_text(" ", strip=True)
    assert "dala-translation-skip" not in transcript.get("class")
    assert es.translate_calls == []


def test_article_extractor_preserves_commented_article_images():
    html = """
    <html>
      <head><title>Commented image story</title></head>
          <body>
            <article>
              <p>Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.
              Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.
              Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.</p>
          <figure>
            <div id="timelapse_images">
              <!--<img src="/files/map-2000.png" alt="Map showing 2000">-->
            </div>
            <figcaption>Interactive map caption.</figcaption>
          </figure>
        </article>
      </body>
    </html>
    """

    extracted = ArticleExtractor.extract_from_html(html, "https://example.com/story")

    assert extracted["success"] is True
    assert 'src="/files/map-2000.png"' in extracted["html"]
    assert "Map showing 2000" in extracted["html"]


def test_article_extractor_prefers_visible_byline_over_admin_metadata():
    html = """
    <html>
      <head>
        <title>Why the West stopped making land</title>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Article",
          "author": {"name": "wip-admin"},
          "headline": "Why the West stopped making land"
        }
        </script>
      </head>
      <body>
        <article>
          <div class="article-header__head-label">
            <span>Words by</span>
            <div class="label-name-underline__name">
              <a class="author-link" href="https://worksinprogress.co/our-authors/zigmund-forrest/">Zigmund Forrest</a>
              <span>&amp;</span>
              <a class="author-link" href="https://worksinprogress.co/our-authors/maxwell-tabarrok/">Maxwell Tabarrok</a>
            </div>
          </div>
          <p>Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.
          Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.
          Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.</p>
        </article>
      </body>
    </html>
    """

    extracted = ArticleExtractor.extract_from_html(html, "https://worksinprogress.co/issue/example/")

    assert extracted["success"] is True
    assert extracted["author"] == "Zigmund Forrest & Maxwell Tabarrok"


def test_article_extractor_keeps_good_metadata_author_over_generic_author_links():
    html = """
    <html>
      <head>
        <title>Story</title>
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Article", "author": {"name": "Reporter Name"}}
        </script>
      </head>
      <body>
        <article>
          <a class="author-link" href="/author/archive/">Archive</a>
          <p>Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.
          Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.</p>
        </article>
      </body>
    </html>
    """

    extracted = ArticleExtractor.extract_from_html(html, "https://example.com/story")

    assert extracted["success"] is True
    assert extracted["author"] == "Reporter Name"


def test_article_extractor_converts_injected_svg_graphic_to_image():
    soup = BeautifulSoup(
        """
        <article>
          <p>Article body.</p>
          <figure class="news-article__figure">
            <figcaption class="news-article__figure__upper-caption">
              <h3>A species divided</h3>
            </figcaption>
            <svg data-inject-url="/cms/asset/wolf_population_locator.svg"></svg>
            <figcaption class="news-article__figure__caption">
              The mapped range shows areas where wolf presence is considered permanent as of 2023.
              (Graphic) V. Penney/Science; (Data) Large Carnivores Initiative for Europe.
            </figcaption>
          </figure>
          <svg data-inject-url="/assets/logo.svg"></svg>
        </article>
        """,
        "html.parser",
    )

    ArticleExtractor._clean_soup(soup.article)

    img = soup.find("img", src="/cms/asset/wolf_population_locator.svg")
    assert img is not None
    assert "The mapped range shows" in img.get("alt", "")
    assert soup.find("svg") is None
    assert soup.find("img", src="/assets/logo.svg") is None


def test_article_extractor_converts_inline_figure_svg_to_data_image():
    html = """
    <html>
      <body>
        <article>
          <p>Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.
          Enough article text for extraction. Enough article text for extraction. Enough article text for extraction.</p>
          <figure>
            <figcaption>A species divided</figcaption>
            <svg data-name="europe-map" viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
              <path d="M1 1 L199 1 L199 99 L1 99 Z"/>
              <text x="10" y="50">Large Europe map</text>
            </svg>
            <figcaption>The mapped range shows areas where wolf presence is considered permanent as of 2023.</figcaption>
          </figure>
        </article>
      </body>
    </html>
    """

    extracted = ArticleExtractor.extract_from_html(html, "https://example.com/story")

    assert extracted["success"] is True
    assert "<svg" not in extracted["html"]
    assert 'src="data:image/svg+xml;base64,' in extracted["html"]
    assert 'data-name="europe-map"' in extracted["html"]


@pytest.mark.asyncio
async def test_generic_driver_404():
    url = "https://example.com/404"
    
    # Mock requests response
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = ""

    with aioresponses() as m:
        # 1. Main fetch fails (aiohttp)
        m.get(url, status=404)
        
        # 2. Archive fetch fails (mocking the API call)
        # We use a regex to match the archive URL loosely
        import re
        archive_pattern = re.compile(f"^{re.escape(ARCHIVE_ORG_API_BASE)}.*")
        m.get(archive_pattern, status=200, payload={})
        
        # Patch requests.get for the synchronous fallback
        with patch("requests.get", return_value=mock_resp):
            # Patch asyncio.sleep to skip retry delays
            with patch("asyncio.sleep", return_value=None):
                async with aiohttp.ClientSession() as session:
                    options = ConversionOptions()
                    context = ConversionContext(session=session, options=options)
                    source = Source(url=url)
                    driver = GenericDriver()
                    
                    book = await driver.prepare_book_data(context, source)
                    assert book is None


@pytest.mark.asyncio
async def test_article_extractor_uses_browser_fallback_before_archive(monkeypatch):
    url = "https://example.com/paywalled"
    rendered_html = """
    <html><head><title>Rendered Article</title></head>
    <body><article><p>Full rendered content from browser fallback.</p></article></body></html>
    """
    calls = {"browser": 0, "archive": 0}

    async def fake_fetch(session, target_url, response_type="text", **kwargs):
        return None, target_url

    async def fake_requests_fetch(session, target_url):
        return None, None

    async def fake_browser_fetch(target_url, browser_options):
        calls["browser"] += 1
        return BrowserFetchResult(url=target_url, html=rendered_html, cookies={})

    async def fake_wayback(session, target_url):
        calls["archive"] += 1
        return None

    monkeypatch.setattr("dala.core.extractor.fetch_with_retry", fake_fetch)
    monkeypatch.setattr(ArticleExtractor, "_requests_fetch", fake_requests_fetch)
    monkeypatch.setattr("dala.core.extractor.fetch_rendered_source", fake_browser_fetch)
    monkeypatch.setattr(ArticleExtractor, "get_wayback_url", fake_wayback)

    data = await ArticleExtractor.get_article_content(
        object(),
        url,
        browser_options=BrowserFetchOptions(settle_ms=0),
    )

    assert data["success"] is True
    assert data["title"] == "Rendered Article"
    assert "Full rendered content" in data["html"]
    assert calls == {"browser": 1, "archive": 0}


@pytest.mark.asyncio
async def test_article_extractor_raises_browser_challenge_from_raw_html():
    async with aiohttp.ClientSession() as session:
        with pytest.raises(BrowserChallengeError, match="verification required"):
            await ArticleExtractor.get_article_content(
                session,
                "https://www.wsj.com/article",
                raw_html="<html><body>Verification Required Slide right to secure your access</body></html>",
                browser_options=BrowserFetchOptions(challenge_action="user_browser"),
            )


@pytest.mark.asyncio
async def test_article_extractor_falls_back_to_archive_on_browser_challenge(monkeypatch):
    url = "https://www.nytimes.com/challenged"
    calls = {"archive": 0}

    async def fake_fetch(session, target_url, response_type="text", **kwargs):
        if "web.archive.org" in target_url:
            return (
                "<html><head><title>Archive Article</title></head>"
                "<body><article><p>Archived article content.</p></article></body></html>",
                target_url,
            )
        return None, target_url

    async def fake_requests_fetch(session, target_url):
        return None, None

    async def fake_browser_fetch(target_url, browser_options):
        raise BrowserChallengeError(target_url, "geo.captcha-delivery.com")

    async def fake_wayback(session, target_url):
        calls["archive"] += 1
        return "https://web.archive.org/web/20260101000000/https://www.nytimes.com/challenged"

    monkeypatch.setattr("dala.core.extractor.fetch_with_retry", fake_fetch)
    monkeypatch.setattr(ArticleExtractor, "_requests_fetch", fake_requests_fetch)
    monkeypatch.setattr("dala.core.extractor.fetch_rendered_source", fake_browser_fetch)
    monkeypatch.setattr(ArticleExtractor, "get_wayback_url", fake_wayback)

    data = await ArticleExtractor.get_article_content(
        object(),
        url,
        browser_options=BrowserFetchOptions(challenge_action="archive"),
    )

    assert data["success"] is True
    assert data["was_archived"] is True
    assert data["title"] == "Archive Article"
    assert calls["archive"] == 1


@pytest.mark.asyncio
async def test_generic_driver_switches_to_forum():
    url = "https://example.com/unknown-forum/thread"
    # HTML that triggers the switch AND contains valid posts for ForumDriver
    forum_html = """
    <html>
    <body>
        <div data-template="thread_view">
            <article class="message message--post" id="post-1">
                <div class="message-inner">
                    <div class="message-cell message-cell--user">
                        <div class="message-user">
                            <h4 class="message-name"><a href="#" class="username">TestUser</a></h4>
                        </div>
                    </div>
                    <div class="message-cell message-cell--main">
                        <div class="message-content">
                            <div class="message-body">Hello Forum</div>
                        </div>
                    </div>
                </div>
            </article>
        </div>
    </body>
    </html>
    """
    
    with aioresponses() as m:
        # 1. GenericDriver fetch
        m.get(url, status=200, body=forum_html)
        # 2. ForumDriver fetch (it starts over)
        m.get(url, status=200, body=forum_html)
        
        async with aiohttp.ClientSession() as session:
            options = ConversionOptions()
            context = ConversionContext(session=session, options=options)
            source = Source(url=url)
            driver = GenericDriver()
            
            # This should return a BookData object from ForumDriver
            book = await driver.prepare_book_data(context, source)
            
            assert book is not None
            # ForumDriver sets author="Forum" usually
            assert book.author == "Forum" 
            assert "urn:forum:" in book.uid


@pytest.mark.asyncio
async def test_forum_driver_uses_prefetched_html_for_first_page():
    url = "https://www.mtbr.com/threads/example.123/"
    forum_html = """
    <html>
    <head><title>Rendered Forum</title></head>
    <body>
        <article class="message message--post" id="post-1" data-author="RenderedUser">
            <div class="message-content">
                <div class="bbWrapper">Rendered forum post body.</div>
            </div>
        </article>
    </body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        context = ConversionContext(session=session, options=ConversionOptions(no_images=True))
        source = Source(url=url, html=forum_html)
        book = await ForumDriver().prepare_book_data(context, source)

    assert book is not None
    assert book.author == "Forum"
    assert "RenderedUser" in book.chapters[0].content_html
    assert "Rendered forum post body." in book.chapters[0].content_html


@pytest.mark.asyncio
async def test_forum_driver_uses_browser_fetched_page_htmls():
    url = "https://www.mtbr.com/threads/example.123/"
    page_1_html = """
    <html>
    <head><title>Rendered Forum</title><link rel="next" href="/threads/example.123/page-2"></head>
    <body>
        <article class="message message--post" id="post-1" data-author="PageOne">
            <div class="message-content"><div class="bbWrapper">First page body.</div></div>
        </article>
        <a rel="next" href="/threads/example.123/page-2">Next</a>
    </body>
    </html>
    """
    page_2_html = """
    <html>
    <head><title>Rendered Forum</title></head>
    <body>
        <article class="message message--post" id="post-2" data-author="PageTwo">
            <div class="message-content"><div class="bbWrapper">Second page body.</div></div>
        </article>
    </body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        context = ConversionContext(session=session, options=ConversionOptions(no_images=True))
        source = Source(
            url=url,
            page_htmls=[
                {"page": 1, "url": url, "html": page_1_html},
                {"page": 2, "url": f"{url}page-2", "html": page_2_html},
            ],
        )
        book = await ForumDriver().prepare_book_data(context, source)

    assert book is not None
    html = book.chapters[0].content_html
    assert "page_1" in html
    assert "page_2" in html
    assert "PageOne" in html
    assert "PageTwo" in html
    assert "Second page body." in html
    toc_parent, toc_children = book.toc_structure[0]
    assert toc_parent.href == "thread.xhtml"
    assert [child.href for child in toc_children] == ["thread.xhtml#page_1", "thread.xhtml#page_2"]
    assert [child.title for child in toc_children] == ["Page 1", "Page 2"]


@pytest.mark.asyncio
async def test_forum_driver_advances_through_browser_fetched_pages_without_next_link():
    url = "https://www.mtbr.com/threads/example.123/"
    page_1_html = """
    <html>
    <head><title>Rendered Forum</title></head>
    <body>
        <article class="message message--post" id="post-1" data-author="PageOne">
            <div class="message-content"><div class="bbWrapper">First page body.</div></div>
        </article>
    </body>
    </html>
    """
    page_2_html = """
    <html>
    <head><title>Rendered Forum</title></head>
    <body>
        <article class="message message--post" id="post-2" data-author="PageTwo">
            <div class="message-content"><div class="bbWrapper">Second page body.</div></div>
        </article>
    </body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        context = ConversionContext(session=session, options=ConversionOptions(no_images=True))
        source = Source(
            url=url,
            page_htmls=[
                {"page": 1, "url": url, "html": page_1_html},
                {"page": 2, "url": f"{url}page-2", "html": page_2_html},
            ],
        )
        book = await ForumDriver().prepare_book_data(context, source)

    assert book is not None
    html = book.chapters[0].content_html
    assert "page_1" in html
    assert "page_2" in html
    assert "PageOne" in html
    assert "PageTwo" in html
    assert "Second page body." in html


@pytest.mark.asyncio
async def test_forum_image_processor_drops_chrome_images_and_skips_non_attachment_fetch(monkeypatch):
    from dala.core.image_processor import ForumImageProcessor

    html = """
    <html><body>
        <div class="message-content">
            <img src="https://images.platforum.cloud/logos/mtbr_com.svg">
            <img src="https://cdn.example.com/thread-inline.jpg">
        </div>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    calls = []

    async def fake_fetch(*args, **kwargs):
        calls.append(args)
        raise AssertionError("non-attachment chrome images should not be fetched")

    monkeypatch.setattr(ForumImageProcessor, "fetch_image_data", fake_fetch)

    async with aiohttp.ClientSession() as session:
        await ForumImageProcessor.process_images(
            session,
            soup,
            "https://www.mtbr.com/threads/example.123/",
            [],
            preloaded_assets=[],
            options=ConversionOptions(),
        )

    assert calls == []
    assert soup.find("img") is None


@pytest.mark.asyncio
async def test_forum_image_fetch_dedupes_attachment_targets(monkeypatch):
    from dala.core.image_processor import ForumImageProcessor

    url = "https://www.mtbr.com/attachments/example-jpg.12345/"
    calls = []

    async def fake_requests_fetch(session, target, img_headers, referer):
        calls.append(target)
        return None, None, None

    monkeypatch.setattr(ForumImageProcessor, "_requests_fetch", fake_requests_fetch)

    async with aiohttp.ClientSession() as session:
        headers, data, err = await ForumImageProcessor.fetch_image_data(
            session,
            url,
            referer="https://www.mtbr.com/threads/example.123/",
            viewer_url=url,
        )

    assert headers is None
    assert data is None
    assert err == "No data"
    assert calls == [url]


@pytest.mark.asyncio
async def test_forum_image_pass_logs_summary_after_timeout(monkeypatch, caplog):
    from dala.core.image_processor import ForumImageProcessor

    soup = BeautifulSoup(
        """
        <html><body>
          <img src="https://www.mtbr.com/attachments/example-jpg.12345/"/>
        </body></html>
        """,
        "html.parser",
    )

    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(1)
        return None, None, "too slow"

    monkeypatch.setattr("dala.core.image_processor.IMG_MAX_PER_IMAGE_SEC", 0.01)
    monkeypatch.setattr(ForumImageProcessor, "fetch_image_data", slow_fetch)

    async with aiohttp.ClientSession() as session:
        with caplog.at_level(logging.INFO):
            await ForumImageProcessor.process_images(
                session,
                soup,
                "https://www.mtbr.com/threads/example.123/",
                [],
                preloaded_assets=[],
                options=ConversionOptions(),
            )

    assert "Forum image pass done:" in caplog.text
    assert "images=1" in caplog.text
    assert "timed_out=1" in caplog.text


@pytest.mark.asyncio
async def test_wordpress_driver():
    url = "https://example.wordpress.com/2023/01/01/test-post"
    html_content = """
    <html>
    <head><title>WP Post</title></head>
    <body class="post-template-default">
        <article class="post">
            <div class="entry-content"><p>Article Content</p></div>
        </article>
        <div id="comments" class="comments-area">
            <ol class="comment-list">
                <li id="comment-1" class="comment depth-1 parent">
                    <article id="div-comment-1" class="comment-body">
                        <footer class="comment-meta">
                            <div class="comment-author vcard"><span class="fn">User A</span></div>
                            <div class="comment-metadata"><time datetime="2023-01-01T12:00:00Z">Jan 1</time></div>
                        </footer>
                        <div class="comment-content"><p>Top level comment</p></div>
                    </article>
                    <ol class="children">
                        <li id="comment-2" class="comment depth-2">
                            <article id="div-comment-2" class="comment-body">
                                <footer class="comment-meta">
                                    <div class="comment-author vcard"><span class="fn">User B</span></div>
                                    <div class="comment-metadata"><time datetime="2023-01-01T13:00:00Z">Jan 1</time></div>
                                </footer>
                                <div class="comment-content"><p>Reply</p></div>
                            </article>
                        </li>
                    </ol>
                </li>
            </ol>
        </div>
    </body>
    </html>
    """
    
    with aioresponses() as m:
        m.get(url, status=200, body=html_content)
        
        async with aiohttp.ClientSession() as session:
            options = ConversionOptions()
            context = ConversionContext(session=session, options=options)
            source = Source(url=url)
            
            # Dispatcher should pick WordPressDriver
            driver = DriverDispatcher.get_driver(source)
            assert isinstance(driver, WordPressDriver)
            
            book = await driver.prepare_book_data(context, source)
            
            assert book is not None
            assert book.title == "WP Post"
            assert len(book.chapters) == 2 # Article + Comments
            
            comments_chap = book.chapters[1]
            assert "User A" in comments_chap.content_html
            assert "User B" in comments_chap.content_html
            assert "Top level comment" in comments_chap.content_html

def test_wordpress_driver_removes_theme_title_nav_and_meta():
    html_content = """
    <html>
    <head><title>Coye la forêt – Paris – Coye la forêt</title></head>
    <body class="post-template-default">
        <main id="content" role="main">
            <div id="nav-above">
                <div class="nav-previous">Previous: Old Ride</div>
                <div class="nav-next">Next: New Ride</div>
            </div>
            <div id="post-123" class="post">
                <h2><a href="/post">Coye la forêt – Paris – Coye la forêt</a></h2>
                <div>Publié le août 15, 2025 par velovefamily</div>
                <div class="entry-content">
                    <p>Article Content</p>
                    <figure>
                        <img src="https://example.com/photo.jpg" alt="Photo"/>
                        <figcaption>Forest path outside Paris</figcaption>
                    </figure>
                </div>
            </div>
        </main>
    </body>
    </html>
    """

    soup = BeautifulSoup(html_content, "html.parser")
    article_body = WordPressDriver._clean_article_body(
        soup.body,
        "Coye la forêt – Paris – Coye la forêt",
    )
    chapter_html = article_body.prettify()

    assert "Coye la forêt – Paris – Coye la forêt" not in chapter_html
    assert "Previous: Old Ride" not in chapter_html
    assert "Next: New Ride" not in chapter_html
    assert "Publié le août 15, 2025" not in chapter_html
    assert "Article Content" in chapter_html
    assert "Forest path outside Paris" in chapter_html


@pytest.mark.asyncio
async def test_generic_driver_seeds_metadata_image_when_body_has_no_img(monkeypatch):
    url = "https://www.nytimes.com/2026/06/18/us/politics/example.html"
    raw_html = """
    <html>
      <head>
        <title>NYT Article</title>
        <meta property="og:image" content="https://static01.nyt.com/images/2026/06/18/us/politics/photo.jpg"/>
        <script type="application/ld+json">
        {
          "@type": "NewsArticle",
          "headline": "NYT Article",
          "image": {
            "@type": "ImageObject",
            "url": "https://static01.nyt.com/images/2026/06/18/us/politics/photo.jpg",
            "caption": "Officers outside a warehouse."
          }
        }
        </script>
      </head>
      <body><article><p>Article body long enough for extraction to succeed.</p></article></body>
    </html>
    """

    img = Image.new("RGB", (40, 40), color="white")
    out = BytesIO()
    img.save(out, format="JPEG")

    async def fake_fetch_image(session, target_url, referer=None):
        return {"Content-Type": "image/jpeg"}, out.getvalue(), None

    monkeypatch.setattr("dala.core.image_processor.ImageProcessor.fetch_image_data", fake_fetch_image)

    with aioresponses() as m:
        m.get(url, status=200, body=raw_html)
        async with aiohttp.ClientSession() as session:
            context = ConversionContext(session=session, options=ConversionOptions())
            book = await GenericDriver().prepare_book_data(context, Source(url=url))

    assert book is not None
    assert len(book.images) == 1
    assert "images/" in book.chapters[0].content_html
    assert "Officers outside a warehouse." in book.chapters[0].content_html


@pytest.mark.asyncio
async def test_nextjs_seeded_image_is_inserted_when_body_is_tag(monkeypatch):
    raw_html = """
    <html>
      <head>
        <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "globalContent": {
                "content_elements": [
                  {
                    "type": "image",
                    "_id": "lede-image",
                    "url": "https://static01.nyt.com/images/2026/06/18/us/politics/warehouse.jpg",
                    "caption": "The warehouse that ICE recently purchased."
                  }
                ]
              }
            }
          }
        }
        </script>
      </head>
      <body><article><p>Article body.</p></article></body>
    </html>
    """
    soup = BeautifulSoup("<body><article><p>Article body.</p></article></body>", "html.parser")
    img = Image.new("RGB", (40, 40), color="white")
    out = BytesIO()
    img.save(out, format="JPEG")

    async def fake_fetch_image(session, target_url, referer=None):
        return {"Content-Type": "image/jpeg"}, out.getvalue(), None

    monkeypatch.setattr("dala.core.image_processor.ImageProcessor.fetch_image_data", fake_fetch_image)

    async with aiohttp.ClientSession() as session:
        assets = []
        await ImageProcessor._seed_images_from_nextjs_data(
            raw_html,
            soup.body,
            "https://www.nytimes.com/2026/06/18/us/politics/example.html",
            assets,
            session,
        )

    assert len(assets) == 1
    assert soup.body.find("img", src=assets[0].filename) is not None
    assert "The warehouse that ICE recently purchased." in str(soup)


@pytest.mark.asyncio
async def test_image_processor_preserves_picture_source_srcset(monkeypatch):
    soup = BeautifulSoup(
        """
        <body>
          <figure>
            <picture>
              <source media="(min-width: 600px)" srcset="https://static01.nyt.com/images/photo-large.jpg 1200w"/>
              <img alt="Warehouse exterior"/>
            </picture>
            <figcaption>Warehouse caption</figcaption>
          </figure>
        </body>
        """,
        "html.parser",
    )
    img = Image.new("RGB", (40, 40), color="white")
    out = BytesIO()
    img.save(out, format="JPEG")
    seen = []

    async def fake_fetch_image(session, target_url, referer=None):
        seen.append(target_url)
        return {"Content-Type": "image/jpeg"}, out.getvalue(), None

    monkeypatch.setattr("dala.core.image_processor.ImageProcessor.fetch_image_data", fake_fetch_image)

    async with aiohttp.ClientSession() as session:
        assets = []
        from dala.core.image_processor import ImageProcessor
        await ImageProcessor.process_images(session, soup, "https://www.nytimes.com/article", assets)

    assert seen == ["https://static01.nyt.com/images/photo-large.jpg"]
    assert len(assets) == 1
    assert "Warehouse caption" in str(soup)


@pytest.mark.asyncio
async def test_image_processor_fetches_srcset_urls_with_commas(monkeypatch):
    soup = BeautifulSoup(
        """
        <body>
          <figure>
            <img
              alt="Terrasse-Vaudreuil sign"
              src="https://i.cbc.ca/ais/3130066c-279a-4d0c-af51-2d594b78680f,1782062951783/full/max/0/default.jpg?im=Crop%2Crect%3D%280%2C411%2C3500%2C1968%29%3B"
              srcset="https://i.cbc.ca/ais/3130066c-279a-4d0c-af51-2d594b78680f,1782062951783/full/max/0/default.jpg?im=Crop%2Crect%3D%280%2C411%2C3500%2C1968%29%3BResize%3D860 860w,https://i.cbc.ca/ais/3130066c-279a-4d0c-af51-2d594b78680f,1782062951783/full/max/0/default.jpg?im=Crop%2Crect%3D%280%2C411%2C3500%2C1968%29%3BResize%3D1280 1280w"
            />
            <figcaption>A roadside sign.</figcaption>
          </figure>
        </body>
        """,
        "html.parser",
    )
    img = Image.new("RGB", (40, 40), color="white")
    out = BytesIO()
    img.save(out, format="JPEG")
    seen = []

    async def fake_fetch_image(session, target_url, referer=None):
        seen.append(target_url)
        return {"Content-Type": "image/jpeg"}, out.getvalue(), None

    monkeypatch.setattr("dala.core.image_processor.ImageProcessor.fetch_image_data", fake_fetch_image)

    async with aiohttp.ClientSession() as session:
        assets = []
        await ImageProcessor.process_images(session, soup, "https://www.cbc.ca/news/canada/montreal/story", assets)

    assert seen[0].startswith("https://i.cbc.ca/ais/3130066c-279a-4d0c-af51-2d594b78680f,1782062951783/")
    assert "/news/canada/montreal/1782062951783/" not in seen[0]
    assert len(assets) == 1


@pytest.mark.asyncio
async def test_image_processor_keeps_distinct_generic_default_filenames(monkeypatch):
    soup = BeautifulSoup(
        """
        <body>
          <figure>
            <img alt="Terrasse-Vaudreuil sign" src="https://i.cbc.ca/ais/sign/full/max/0/default.jpg?Resize=1280"/>
            <figcaption>A roadside sign.</figcaption>
          </figure>
          <figure>
            <img alt="Trees are shown in the municipality" src="https://i.cbc.ca/ais/trees/full/max/0/default.jpg?Resize=1280"/>
            <figcaption>Trees in the municipality.</figcaption>
          </figure>
        </body>
        """,
        "html.parser",
    )
    red = BytesIO()
    blue = BytesIO()
    Image.new("RGB", (40, 40), color="red").save(red, format="JPEG")
    Image.new("RGB", (40, 40), color="blue").save(blue, format="JPEG")

    async def fake_fetch_image(session, target_url, referer=None):
        data = red.getvalue() if "/sign/" in target_url else blue.getvalue()
        return {"Content-Type": "image/jpeg"}, data, None

    monkeypatch.setattr("dala.core.image_processor.ImageProcessor.fetch_image_data", fake_fetch_image)

    async with aiohttp.ClientSession() as session:
        assets = []
        await ImageProcessor.process_images(session, soup, "https://www.cbc.ca/news/canada/montreal/story", assets)

    image_srcs = [img.get("src") for img in soup.find_all("img")]
    assert len(assets) == 2
    assert len(set(asset.filename for asset in assets)) == 2
    assert len(set(image_srcs)) == 2
    assert all(src.startswith("images/default_") for src in image_srcs)
    assert soup.find("img", src=assets[0].filename) is not None


@pytest.mark.asyncio
async def test_generic_driver_uses_preloaded_browser_assets():
    img = Image.new("RGB", (20, 20), color="white")
    out = BytesIO()
    img.save(out, format="JPEG")
    image_url = "https://cdn.example.com/protected/photo.jpg?token=browser"
    body_text = " ".join(["Article body text with enough content for extraction."] * 12)
    html = f"""
    <html>
      <head><title>Protected Image Article</title></head>
      <body><article><p>{body_text}</p><img src="{image_url}" alt="Protected photo"/></article></body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(
            ConversionContext(session=session, options=ConversionOptions()),
            Source(
                url="https://example.com/article",
                html=html,
                assets=[{
                    "original_url": image_url,
                    "canonical_url": image_url.split("?", 1)[0],
                    "content_type": "image/jpeg",
                    "content": base64.b64encode(out.getvalue()).decode("ascii"),
                }, {
                    "original_url": "https://cdn.example.com/protected/unused.jpg",
                    "canonical_url": "https://cdn.example.com/protected/unused.jpg",
                    "content_type": "image/jpeg",
                    "content": base64.b64encode(out.getvalue()).decode("ascii"),
                }],
            ),
        )

    assert book is not None
    assert len(book.images) == 1
    assert book.images[0].filename.endswith("photo.jpg")
    assert 'src="images/photo.jpg"' in book.chapters[0].content_html


@pytest.mark.asyncio
async def test_generic_driver_inserts_preloaded_metadata_hero_image():
    img = Image.new("RGB", (20, 20), color="white")
    out = BytesIO()
    img.save(out, format="JPEG")
    hero_url = "https://www.science.org/cms/asset/d1b394f5-66b9-4de1-b798-c6f75c88afa3/_20260618_nf_wolves_night.jpg"
    body_text = " ".join(["Article body text with enough content for extraction."] * 12)
    html = f"""
    <html>
      <head>
        <title>Science Article</title>
        <meta property="og:image" content="{hero_url}"/>
      </head>
      <body><article><p>{body_text}</p></article></body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(
            ConversionContext(session=session, options=ConversionOptions()),
            Source(
                url="https://www.science.org/content/article/story",
                html=html,
                assets=[{
                    "original_url": hero_url,
                    "canonical_url": hero_url,
                    "content_type": "image/jpeg",
                    "content": base64.b64encode(out.getvalue()).decode("ascii"),
                }],
            ),
        )

    assert book is not None
    assert len(book.images) == 1
    assert book.images[0].filename.endswith("20260618_nf_wolves_night.jpg")
    image_pos = book.chapters[0].content_html.index('src="images/20260618_nf_wolves_night.jpg"')
    body_pos = book.chapters[0].content_html.index("Article body text")
    assert image_pos < body_pos


@pytest.mark.asyncio
async def test_science_map_caption_uses_lower_caption_not_upper_explainer():
    img = Image.new("RGB", (20, 20), color="white")
    out = BytesIO()
    img.save(out, format="PNG")
    map_url = "https://www.science.org/do/10.1126/science.zk8ch0m/files/_20260618_nf_wolves_timelapse_2024.png"
    body_text = " ".join(["Article body text with enough content for extraction."] * 12)
    html = f"""
    <html>
      <head><title>Science Map</title></head>
      <body>
        <article>
          <p>{body_text}</p>
          <figure class="news-article__figure">
            <figcaption class="news-article__figure__upper-caption">
              <h3>A stunning recovery</h3>
              <p>Drag the slider or press the play button to see the change in wolf territories over time.</p>
            </figcaption>
            <div id="timelapse_images">
              <img src="{map_url}" alt="Map showing wolf range in monitoring year 2024"/>
            </div>
            <div class="text-xs letter-spacing-default mt-2">
              The map shows wolf territories of 200 square kilometers (km²) each for the Central European population.
            </div>
            <figcaption class="news-article__figure__caption mt-2">
              <span>(Graphic) V. Penney/<em>Science</em>; (Data) BIJ12</span>
            </figcaption>
          </figure>
        </article>
      </body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(
            ConversionContext(session=session, options=ConversionOptions()),
            Source(
                url="https://www.science.org/content/article/story",
                html=html,
                assets=[{
                    "original_url": map_url,
                    "canonical_url": map_url,
                    "content_type": "image/png",
                    "content": base64.b64encode(out.getvalue()).decode("ascii"),
                }],
            ),
        )

    assert book is not None
    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    caption = soup.find("p", class_="image-caption") or soup.find("p", class_="caption")
    assert caption is not None
    caption_text = caption.get_text(" ", strip=True)
    assert caption_text.startswith("The map shows wolf territories")
    assert "V. Penney" in caption_text
    assert "BIJ12" in caption_text
    assert "Drag the slider" not in caption_text


@pytest.mark.asyncio
async def test_science_locator_svg_uses_adjacent_range_caption():
    svg_url = "https://www.science.org/cms/asset/b4dcde7f-6e53-4719-8159-380972b19307/wolf_population_locator.svg"
    map_url = "https://www.science.org/cms/asset/65597b08-aeb6-4247-84a0-06addb7ee95e/wolf_population_map.svg"
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80"/></svg>'
    map_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="420"><path d="M1 1 L599 1 L599 419 L1 419 Z"/></svg>'
    body_text = " ".join(["Article body text with enough content for extraction."] * 12)
    html = f"""
    <html>
      <head><title>Science Locator</title></head>
      <body>
        <article>
          <p>{body_text}</p>
          <figure>
            <img class="epub-image" src="images/timelapse_2024.jpg" alt="Map showing wolf range in monitoring year 2024"/>
            <figcaption>The map shows wolf territories of 200 square kilometers.</figcaption>
          </figure>
          <figure class="news-article__figure">
            <figcaption class="news-article__figure__upper-caption">
              <h3>A species divided</h3>
              <p>Wolves now live in every country in continental Europe.</p>
            </figcaption>
            <figure class="plain quarter float-right">
              <svg data-inject-url="{svg_url}" data-name="locator"></svg>
            </figure>
            <div>
              The mapped range shows areas where wolf presence is considered permanent as of 2023, except for Italy,
              which reported data from one intensive monitoring period from 2020–21.
            </div>
            <figcaption class="news-article__figure__caption">
              (Graphic) V. Penney/Science; (Data) Large Carnivores Initiative for Europe wolf distribution map (2017–22/23)
            </figcaption>
          </figure>
        </article>
      </body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(
            ConversionContext(session=session, options=ConversionOptions()),
            Source(
                url="https://www.science.org/content/article/story",
                html=html,
                assets=[{
                    "original_url": svg_url,
                    "canonical_url": svg_url,
                    "content_type": "image/svg+xml",
                    "content": base64.b64encode(svg).decode("ascii"),
                }, {
                    "original_url": map_url,
                    "canonical_url": map_url,
                    "content_type": "image/svg+xml",
                    "content": base64.b64encode(map_svg).decode("ascii"),
                }],
            ),
        )

    assert book is not None
    assert {image.filename for image in book.images} == {
        "images/wolf_population_locator.svg",
        "images/wolf_population_map.svg",
    }
    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    img = soup.find("img", src="images/wolf_population_locator.svg")
    assert img is not None
    wrapper = img.find_parent("div", class_="img-block") or img.find_parent("figure")
    map_img = wrapper.find("img", src="images/wolf_population_map.svg")
    assert map_img is not None
    first_map_wrapper = soup.find("img", alt="Map showing wolf range in monitoring year 2024").find_parent(["div", "figure"])
    assert first_map_wrapper.find("img", src="images/wolf_population_map.svg") is None
    caption = wrapper.find("p", class_="image-caption") or wrapper.find("p", class_="caption")
    assert caption is not None
    caption_text = caption.get_text(" ", strip=True)
    assert caption_text.startswith("The mapped range shows")
    assert "Large Carnivores Initiative for Europe" in caption_text
    assert "A species divided" not in caption_text


@pytest.mark.asyncio
async def test_science_locator_svg_uses_caption_after_parent_figure():
    svg_url = "https://www.science.org/cms/asset/b4dcde7f-6e53-4719-8159-380972b19307/wolf_population_locator.svg"
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80"/></svg>'
    body_text = " ".join(["Article body text with enough content for extraction."] * 12)
    html = f"""
    <html>
      <head><title>Science Locator</title></head>
      <body>
        <article>
          <p>{body_text}</p>
          <figure>
            <figcaption>
              <h3>A species divided</h3>
              <p>Wolves now live in every country in continental Europe.</p>
            </figcaption>
            <svg data-inject-url="{svg_url}" data-name="locator"></svg>
          </figure>
          <div>
            The mapped range shows areas where wolf presence is considered permanent as of 2023.
          </div>
          <figcaption>
            (Graphic) V. Penney/Science; (Data) Large Carnivores Initiative for Europe wolf distribution map (2017–22/23)
          </figcaption>
          <p>Next paragraph.</p>
        </article>
      </body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(
            ConversionContext(session=session, options=ConversionOptions()),
            Source(
                url="https://www.science.org/content/article/story",
                html=html,
                assets=[{
                    "original_url": svg_url,
                    "canonical_url": svg_url,
                    "content_type": "image/svg+xml",
                    "content": base64.b64encode(svg).decode("ascii"),
                }],
            ),
        )

    assert book is not None
    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    img = soup.find("img", src="images/wolf_population_locator.svg")
    assert img is not None
    wrapper = img.find_parent("div", class_="img-block") or img.find_parent("figure")
    caption = wrapper.find("p", class_="image-caption") or wrapper.find("p", class_="caption")
    assert caption is not None
    caption_text = caption.get_text(" ", strip=True)
    assert caption_text.startswith("The mapped range shows")
    assert "Large Carnivores Initiative for Europe" in caption_text
    assert "A species divided" not in caption_text


@pytest.mark.asyncio
async def test_generic_driver_packages_inline_svg_graphic():
    body_text = " ".join(["Article body text with enough content for extraction."] * 12)
    html = f"""
    <html>
      <head><title>Inline SVG Graphic</title></head>
      <body>
        <article>
          <p>{body_text}</p>
          <figure>
            <figcaption>A species divided</figcaption>
            <svg data-name="europe-map" viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">
              <path d="M1 1 L199 1 L199 99 L1 99 Z"/>
              <text x="10" y="50">Large Europe map</text>
            </svg>
            <figcaption>
              The mapped range shows areas where wolf presence is considered permanent as of 2023.
              (Graphic) V. Penney/Science; (Data) Large Carnivores Initiative for Europe.
            </figcaption>
          </figure>
        </article>
      </body>
    </html>
    """

    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(
            ConversionContext(session=session, options=ConversionOptions()),
            Source(url="https://www.science.org/content/article/story", html=html),
        )

    assert book is not None
    assert len(book.images) == 1
    assert book.images[0].media_type == "image/svg+xml"
    assert book.images[0].filename.startswith("images/europe-map")
    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    img = soup.find("img", src=book.images[0].filename)
    assert img is not None
    caption = img.find_parent("div", class_="img-block").find("p", class_="caption")
    assert caption is not None
    assert caption.get_text(" ", strip=True).startswith("The mapped range shows")


@pytest.mark.asyncio
async def test_generic_driver_appends_same_site_linked_reference_tables():
    article_url = "https://www.bbc.com/sport/football/articles/example"
    schedule_url = "https://www.bbc.com/sport/football/world-cup/schedule"
    body_text = " ".join(["World Cup article body with enough text for extraction."] * 12)
    article_html = f"""
    <html>
      <head><title>World Cup schedule article</title></head>
      <body>
        <article>
          <p>{body_text}</p>
          <p><a href="/sport/football/world-cup/schedule#KnockoutStage">Click here to view the World Cup knockout stages as it stands</a></p>
        </article>
      </body>
    </html>
    """
    schedule_html = """
    <html><body>
      <main>
        <h2>Group A</h2>
        <table data-testid="football-table">
          <thead><tr><th>Team</th><th>Played</th><th>Points</th><th>Form, Last 6 games</th></tr></thead>
          <tbody>
            <tr><td>Mexico</td><td>3</td><td>7</td><td>No Result W Result Win</td></tr>
            <tr><td>South Africa</td><td>3</td><td>5</td><td>No Result D Result Draw</td></tr>
          </tbody>
        </table>
        <h2>3rd Place Ranking</h2>
        <table data-testid="football-table">
          <thead><tr><th>Team</th><th>Played</th><th>Points</th></tr></thead>
          <tbody>
            <tr><td>Scotland</td><td>3</td><td>4</td></tr>
            <tr><td>England</td><td>3</td><td>3</td></tr>
          </tbody>
        </table>
      </main>
    </body></html>
    """

    with aioresponses() as m:
        m.get(schedule_url, status=200, body=schedule_html)
        async with aiohttp.ClientSession() as session:
            book = await GenericDriver().prepare_book_data(
                ConversionContext(session=session, options=ConversionOptions()),
                Source(url=article_url, html=article_html),
            )

    assert book is not None
    soup = BeautifulSoup(book.chapters[0].content_html, "html.parser")
    tables = soup.select("section.linked-reference-tables table")
    assert len(tables) == 2
    text = soup.get_text(" ", strip=True)
    assert "Linked Tables" in text
    assert "Group A" in text
    assert "3rd Place Ranking" in text
    assert "Scotland" in text
    assert "No Result" not in text


@pytest.mark.asyncio
async def test_metadata_image_seed_reuses_preloaded_asset_by_basename():
    body = BeautifulSoup("<body><p>Article body.</p></body>", "html.parser").body
    raw_html = """
    <html><head>
      <meta property="og:image" content="https://www.science.org/cms/asset/d1b394f5/_20260618_nf_wolves_night.jpg"/>
    </head><body></body></html>
    """
    assets = [
        ImageAsset(
            uid="hero",
            filename="images/_20260618_nf_wolves_night.jpg",
            media_type="image/jpeg",
            content=b"jpeg",
            original_url="https://www.science.org/do/10.1126/science.zk8ch0m/full/_20260618_nf_wolves_night.jpg",
        )
    ]

    async with aiohttp.ClientSession() as session:
        await ImageProcessor._seed_images_from_metadata(
            raw_html,
            body,
            "https://www.science.org/content/article/example",
            assets,
            session,
            options=ConversionOptions(),
        )

    assert len(assets) == 1
    assert body.find("img", src="images/_20260618_nf_wolves_night.jpg") is not None


@pytest.mark.asyncio
async def test_image_processor_prefers_lazy_source_over_blur_preview(monkeypatch):
    soup = BeautifulSoup(
        """
        <body>
          <div class="collection__gallery__cell">
            <img
              src="https://pdr-assets.b-cdn.net/collections/art-of-kite-flying/kite-flying-00010.jpg?width=600&amp;height=1200&amp;blur=70&amp;q=20"
              data-blursrc="https://pdr-assets.b-cdn.net/collections/art-of-kite-flying/kite-flying-00010.jpg?width=600&amp;height=1200&amp;blur=70&amp;q=20"
              data-src="https://pdr-assets.b-cdn.net/collections/art-of-kite-flying/kite-flying-00010.jpg?width=600&amp;height=1200"
            />
            <div class="collection__gallery__caption"><p>Lawrence Hargrave with kites.</p></div>
          </div>
        </body>
        """,
        "html.parser",
    )
    img = Image.new("RGB", (40, 40), color="white")
    out = BytesIO()
    img.save(out, format="JPEG")
    seen = []

    async def fake_fetch_image(session, target_url, referer=None):
        seen.append(target_url)
        return {"Content-Type": "image/jpeg"}, out.getvalue(), None

    monkeypatch.setattr("dala.core.image_processor.ImageProcessor.fetch_image_data", fake_fetch_image)

    async with aiohttp.ClientSession() as session:
        assets = []
        await ImageProcessor.process_images(session, soup, "https://publicdomainreview.org/collection/art-of-kite-flying/", assets)

    assert seen[0] == "https://pdr-assets.b-cdn.net/collections/art-of-kite-flying/kite-flying-00010.jpg?width=600&height=1200"
    assert len(assets) == 1
    assert assets[0].original_url == seen[0]
    assert "Lawrence Hargrave with kites." in str(soup)
    assert "data-blursrc" not in str(soup)

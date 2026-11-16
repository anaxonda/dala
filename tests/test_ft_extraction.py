import pytest

from dala.core.extractor import ArticleExtractor


APP_URL = "https://app.ft.com/content/5203e527-ebdd-45d2-9019-6c440b4c4e93"
WEB_URL = APP_URL.replace("app.ft.com", "www.ft.com")
# Representative FT startup DOM, without scripts, styling or article text.
SHELL = """<html><head><title>FT</title></head><body>
<div id="splash">Continue</div>
<div id="jserror"><section><h1>The App has failed to load</h1>
<p>Please try again and upgrade the app if you continue to have problems</p>
<p>Have a question? Visit the FT Help Centre</p></section></div>
<noscript><div id="nojs">You appear to have JavaScript disabled on your device.
Please enable JavaScript in order to access the FT app, or visit FT.com</div></noscript>
<div id="js-app"><main id="columnscontainer"><section></section></main>
<div id="js-app-root"></div></div></body></html>"""
ARTICLE = """<article><h1>Rendered story</h1><p>Reporting from the full article.
The article contains enough readable text to distinguish it from startup UI.
This paragraph represents the story captured after the application has finished
loading its content and the reader can see the article in their browser.</p></article>"""
CHALLENGE = """<html><head><title>Security Verification</title></head><body>
<h1>Security Verification</h1><p>For help please visit help.ft.com.</p>
<noscript>Enable JavaScript and cookies to continue</noscript>
<p>The following information can help our support team to resolve this issue.</p>
<ul><li>Error Code</li><li>CG000 / 403</li></ul></body></html>"""


def test_ft_app_shell_is_not_an_article():
    result = ArticleExtractor.extract_from_html(SHELL, APP_URL)
    assert result["success"] is False
    assert result["html"] is None


def test_ft_rendered_article_excludes_startup_messages():
    captured = SHELL.replace('<div id="js-app-root"></div>', ARTICLE)
    result = ArticleExtractor.extract_from_html(captured, APP_URL)
    assert result["success"] is True
    assert "Reporting from the full article" in result["html"]
    assert "upgrade the app" not in result["html"]


def test_ft_security_challenge_is_not_an_article():
    result = ArticleExtractor.extract_from_html(CHALLENGE, WEB_URL)
    assert result["success"] is False
    assert result["html"] is None
    assert "Browser challenge" in result["error"]


def test_ft_app_selects_requested_article_in_carousel():
    wrong = ARTICLE.replace('<article>', '<article data-content-id="f591211b-497f-4b20-a0ab-7c37d5422761">')
    requested = ARTICLE.replace('<article>', f'<article data-content-id="{APP_URL.rsplit("/", 1)[-1]}">')
    requested = requested.replace('Rendered story', 'Requested story').replace('Reporting from the full article.', 'Requested article text.')
    captured = SHELL.replace('<div id="js-app-root"></div>', wrong + requested)
    result = ArticleExtractor.extract_from_html(captured, APP_URL)
    assert result['success'] is True
    assert result['title'] == 'Requested story'
    assert 'Requested article text.' in result['html']
    assert 'Reporting from the full article.' not in result['html']
    assert 'f591211b' not in result['html']


def test_ft_app_does_not_substitute_adjacent_article():
    wrong = ARTICLE.replace('<article>', '<article data-content-id="f591211b-497f-4b20-a0ab-7c37d5422761">')
    captured = SHELL.replace('<div id="js-app-root"></div>', wrong)
    result = ArticleExtractor.extract_from_html(captured, APP_URL)
    assert result['success'] is False
    assert result['html'] is None
    assert 'missing' in result['error']


def test_ft_zoom_photos_and_lead_survive_cleanup_without_app_widgets():
    captured = f'''<article data-content-id="{APP_URL.rsplit('/', 1)[-1]}">
      <h1>Requested story</h1>
      <div class="o-topper__read-next"><h3>Adjacent story</h3></div>
      <figure><picture><source srcset="https://images.test/lead.jpg 1x">
        <img class="o-topper__image o-topper__load-fail-test" src="data:image/gif;base64,AAAAinvalid">
      </picture><figcaption>Lead credit</figcaption></figure>
      <article class="n-content-body">{ARTICLE}
        <figure data-component="image-set"><button data-trackable="pinch-to-zoom-image-open-lightbox-button">
          <picture><source srcset="https://images.test/photo.jpg 1x">
          <img src="https://images.test/photo.jpg" alt="Street market"></picture></button>
          <dialog></dialog><figcaption>Market caption</figcaption></figure>
        <div data-component="flourish"><figure><div class="flourish-embed">
          <iframe src="https://flo.uri.sh/visualisation/123/embed"></iframe>
        </div></figure></div>
        <custom-code-component><p>Some content could not load.</p></custom-code-component>
      </article>
      <div class="article--rail">Latest on African economy</div>
      <div class="article--footer">Swipe for more. Adjacent story</div>
    </article>'''
    result = ArticleExtractor.extract_from_html(captured, APP_URL)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert result['success'] is True
    assert len(soup.find_all('img')) == 2
    assert len(soup.find_all('figcaption')) == 2
    assert not soup.find(['button', 'dialog', 'iframe'])
    assert soup.find('a', href='https://flo.uri.sh/visualisation/123/embed')
    assert 'Adjacent story' not in soup.get_text()
    assert 'Some content could not load' not in soup.get_text()
    assert 'Latest on' not in soup.get_text()


@pytest.mark.asyncio
async def test_ft_photos_stay_inline_and_epub_image_urls_resolve(tmp_path, monkeypatch):
    from io import BytesIO
    from zipfile import ZipFile
    from urllib.parse import unquote
    import aiohttp
    from PIL import Image
    from bs4 import BeautifulSoup
    from dala.core.image_processor import ImageProcessor
    from dala.core.writer import write_output_book
    from dala.drivers.generic import GenericDriver
    from dala.models import Source, ConversionContext, ConversionOptions

    image_url = 'https://images.test/raw/ftcms%3Aphoto?width=1200'
    captured = f'''<article data-content-id="{APP_URL.rsplit('/', 1)[-1]}"><h1>Requested story</h1>
        {ARTICLE}<figure><button><picture><source srcset="{image_url} 1x">
        <img src="data:image/gif;base64,AAAAinvalid" alt="Market"></picture></button>
        <figcaption>Photo caption</figcaption></figure><p>Following paragraph.</p></article>'''
    data = BytesIO()
    Image.new('RGB', (100, 80), 'blue').save(data, 'JPEG')

    async def unexpected_fetch(*args, **kwargs):
        pytest.fail('Captured photo should be reused without fetching')

    monkeypatch.setattr(ImageProcessor, 'fetch_image_data', unexpected_fetch)
    options = ConversionOptions(no_comments=True)
    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(ConversionContext(session, options), Source(
            url=APP_URL, html=captured, assets=[{'original_url': image_url, 'content': data.getvalue(), 'media_type': 'image/jpeg'}]))
    assert len(book.images) == 1
    output = tmp_path / 'article.epub'
    await write_output_book(book, str(output), options)
    with ZipFile(output) as archive:
        soup = BeautifulSoup(archive.read('EPUB/index.xhtml'), 'xml')
        img = soup.find('img')
        assert archive.read('EPUB/' + unquote(img['src']))
        assert 'Photo caption' in soup.get_text()
        assert str(soup).index('<img') < str(soup).index('Following paragraph')


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [None, SHELL])
async def test_ft_app_fetch_uses_web_article(monkeypatch, capture):
    calls = []

    async def fetch(session, url, response_type, **kwargs):
        calls.append(url)
        return ARTICLE, url

    monkeypatch.setattr("dala.core.extractor.fetch_with_retry", fetch)
    result = await ArticleExtractor.get_article_content(object(), APP_URL, raw_html=capture)
    assert calls == [WEB_URL]
    assert result["success"] is True
    assert result["source_url"] == WEB_URL


@pytest.mark.asyncio
async def test_ft_app_capture_is_used_without_network(monkeypatch):
    async def unexpected_fetch(*args, **kwargs):
        pytest.fail("Valid captured article should not require a network fetch")

    monkeypatch.setattr("dala.core.extractor.fetch_with_retry", unexpected_fetch)
    result = await ArticleExtractor.get_article_content(
        object(), APP_URL, raw_html=SHELL.replace('<div id="js-app-root"></div>', ARTICLE)
    )
    assert result["success"] is True
    assert result["source_url"] == APP_URL


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_html, success", [(ARTICLE, True), (CHALLENGE, False)])
async def test_ft_challenge_tries_web_archive(monkeypatch, snapshot_html, success):
    snapshot = f"https://web.archive.org/web/20261001000000/{WEB_URL}"
    lookups = []

    async def fetch(session, url, response_type, **kwargs):
        return (snapshot_html if url == snapshot else CHALLENGE), url

    async def wayback(session, url):
        lookups.append(url)
        return snapshot

    monkeypatch.setattr("dala.core.extractor.fetch_with_retry", fetch)
    monkeypatch.setattr(ArticleExtractor, "get_wayback_url", wayback)
    result = await ArticleExtractor.get_article_content(object(), APP_URL)
    assert lookups == [WEB_URL]
    assert result["success"] is success
    if success:
        assert result["was_archived"] is True
        assert "Reporting from the full article" in result["html"]

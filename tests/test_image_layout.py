import base64
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import pytest
from bs4 import BeautifulSoup
from PIL import Image

from dala.core.image_layout import normalize_image_layout, validate_book_media
from dala.core.image_processor import ImageProcessor
from dala.core.extractor import ArticleExtractor
from dala.core.image_budget import prepare_books_for_bundle
from dala.drivers.generic import GenericDriver
from dala.models import BookData, Chapter, ConversionOptions, ImageAsset, Source


def image_bytes():
    output = BytesIO()
    Image.new('RGB', (80, 60), 'blue').save(output, 'PNG')
    return output.getvalue()


def book_with(html, assets=None):
    return BookData(title='Article', author='Author', uid='article', language='en', description='',
                    source_url='https://journal.example/paper', chapters=[Chapter(title='Article', filename='index.xhtml', content_html=html, uid='chapter')],
                    images=assets or [])


def linked_layout(count=15, host='https://cdn.example', prefix='display'):
    inline, gallery = [], []
    for i in range(count):
        caption = f'Illustration {i + 1}. Caption with credit.'
        inline.append(f'<div id="in-{i}"><div href="#{prefix}-{i}"><img src="preview-{i}.jpg"/><a href="#{prefix}-{i}"></a></div><div><b>{caption}</b></div></div>')
        gallery.append(f'<div id="{prefix}-{i}"><div>{caption}</div><img src="{host}/unrelated-original-{i}.png"/><div>{caption}</div></div>')
    return '<html><body><article><h1>Paper</h1>' + ''.join(inline) + '</article><section>' + ''.join(gallery) + '</section></body></html>'


@pytest.mark.asyncio
@pytest.mark.parametrize('host,prefix', [('https://cdn.example', 'viewer'), ('https://unrelated.test', 'random')])
async def test_all_figures_reuse_capture_and_stay_inline_without_site_rules(monkeypatch, host, prefix):
    raw = linked_layout(host=host, prefix=prefix)
    fetch = AsyncMock(side_effect=AssertionError('Captured figures must not be downloaded again'))
    monkeypatch.setattr(ImageProcessor, 'fetch_image_data', fetch)
    assets = [dict(original_url=f'{host}/unrelated-original-{i}.png', content_type='image/png', content=base64.b64encode(image_bytes()).decode()) for i in range(15)]
    source = Source(url='https://journal.example/paper', html=raw, assets=assets)
    from dala.models import ConversionContext
    async with aiohttp.ClientSession() as session:
        book = await GenericDriver().prepare_book_data(ConversionContext(session, ConversionOptions()), source)
    assert book
    validate_book_media(book)
    soup = BeautifulSoup(book.chapters[0].content_html, 'html.parser')
    assert len(soup.find_all('img')) == 15
    assert all(img['src'].startswith('images/') for img in soup.find_all('img'))
    assert all(soup.find(id=f'in-{i}').find('img') for i in range(15))
    assert all(soup.find(id=f'{prefix}-{i}') for i in range(15))
    assert not book.media_warnings
    assert not fetch.called
    for i in range(15):
        assert soup.get_text(' ', strip=True).count(f'Illustration {i + 1}. Caption with credit.') == 1


def test_normalizer_does_not_move_ambiguous_gallery_or_unrelated_prose():
    html = '<article><figure><a href="#gallery"><img src="thumb.jpg"/></a><figcaption>Caption</figcaption></figure></article><div id="gallery"><img src="one.png"/><img src="two.png"/><p>Caption</p></div>'
    soup = BeautifulSoup(html, 'html.parser')
    normalize_image_layout(soup, 'https://example.test/paper')
    assert len(soup.find_all('img')) == 3
    assert soup.find(id='gallery')
    html = '<figure id="a"><a href="#target"><img src="thumb.jpg"/></a><figcaption>Caption</figcaption></figure><div id="target"><figure><img src="full.png"/><figcaption>Caption</figcaption></figure><p>Independent discussion must survive.</p></div>'
    soup = BeautifulSoup(html, 'html.parser')
    normalize_image_layout(soup, 'https://example.test/paper')
    assert 'Independent discussion must survive.' in soup.get_text()
    assert soup.find(id='target')


def test_image_identity_preserves_domains_paths_queries_and_ambiguity():
    a = ImageAsset('a', 'images/a.png', 'image/png', b'a', 'https://cdn.test/asset.png?id=one', ['https://journal.test/preview-one.jpg'])
    b = ImageAsset('b', 'images/b.png', 'image/png', b'b', 'https://other.test/asset.png?id=one')
    assert ImageProcessor._find_existing_asset([a, b], a.original_url) is a
    assert ImageProcessor._find_existing_asset([a, b], 'https://cdn.test/asset.png?id=two') is None
    assert ImageProcessor._find_existing_asset([a, b], 'https://cdn.test/other/asset.png?id=one') is None
    assert ImageProcessor._find_existing_asset([a, b], 'https://journal.test/preview-one.jpg') is a
    b.alt_urls = ['https://journal.test/preview-one.jpg']
    assert ImageProcessor._find_existing_asset([a, b], 'https://journal.test/preview-one.jpg') is None
    books, stats = prepare_books_for_bundle([book_with('<img src="images/a.png"/>', [a]), book_with('<img src="images/b.png"/>', [b])])
    assert sum(len(book.images) for book in books) == 2


@pytest.mark.asyncio
async def test_declared_lazy_alias_reuses_download_before_network(monkeypatch):
    asset = ImageAsset('image', 'images/full.png', 'image/png', image_bytes(), 'https://cdn.test/full.png', ['https://cdn.test/photo.png?width=1200'])
    soup = BeautifulSoup('<figure><img src="https://cdn.test/preview.png" data-src="https://cdn.test/photo.png?width=1200"/><figcaption>Caption stays.</figcaption></figure>', 'html.parser')
    fetch = AsyncMock(side_effect=AssertionError('Unexpected fetch'))
    monkeypatch.setattr(ImageProcessor, 'fetch_image_data', fetch)
    await ImageProcessor.process_images(None, soup, 'https://journal.test/paper', [asset])
    assert soup.img['src'] == asset.filename
    assert not fetch.called
    assert 'Caption stays.' in soup.get_text()


def test_actual_document_base_and_picture_candidates_are_preserved():
    soup = BeautifulSoup('<html><head><base href="https://cdn.test/content/"/></head><body><figure><picture><source srcset="large.png 1200w, small.png 400w"/><img src="preview.png"/></picture><figcaption>Caption.</figcaption></figure></body></html>', 'html.parser')
    normalize_image_layout(soup, 'https://journal.test/article')
    aliases = json.loads(soup.img['data-dala-image-urls'])
    assert 'https://cdn.test/content/large.png' in aliases
    assert soup.img['src'] == 'https://cdn.test/content/preview.png'
    assert 'https://cdn.test/content/large.png' in soup.source['srcset']


@pytest.mark.asyncio
async def test_exhausted_image_recovery_has_placeholder_caption_and_warning(monkeypatch):
    soup = BeautifulSoup('<figure><img src="https://cdn.test/missing.png" alt="A diagram"/><figcaption>Useful caption.</figcaption></figure>', 'html.parser')
    monkeypatch.setattr(ImageProcessor, 'fetch_image_data', AsyncMock(return_value=(None, None, 'HTTP 403')))
    await ImageProcessor.process_images(None, soup, 'https://journal.test/article', [])
    book = book_with(str(soup))
    validate_book_media(book)
    assert not BeautifulSoup(book.chapters[0].content_html, 'html.parser').find('img')
    assert 'Image unavailable' in book.chapters[0].content_html
    assert 'Useful caption.' in book.chapters[0].content_html
    assert book.media_warnings == [{'source_url': book.source_url, 'image_url': 'https://cdn.test/missing.png', 'reason': 'remote_image_unavailable'}]
    validate_book_media(book)
    assert len(book.media_warnings) == 1


@pytest.mark.asyncio
async def test_media_warnings_survive_checkpoint_bundle_and_epub(tmp_path):
    from dala.core import job_store
    from dala.cli import create_bundle
    from dala.core.writer import write_output_book
    from zipfile import ZipFile

    book = book_with('<p>Article</p><img src="images/not-embedded.png"/>')
    validate_book_media(book)
    job_id = 'a' * 32
    job_store.save_book(job_id, book.source_url, book)
    restored = job_store.load_books(job_id)[book.source_url]
    bundle = create_bundle([restored], 'Bundle', 'Author')
    assert bundle.media_warnings == book.media_warnings
    path = tmp_path / 'article.epub'
    await write_output_book(bundle, str(path), ConversionOptions())
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        html = '\n'.join(archive.read(name).decode() for name in archive.namelist() if name.endswith('.xhtml'))
        assert 'Image unavailable' in html
        assert '<img' not in html


def test_fragment_target_can_supply_missing_inline_caption():
    soup = BeautifulSoup('<article><figure id="inline"><a href="#viewer"><img src="thumb.jpg"/></a></figure></article><figure id="viewer"><img src="full.png"/><figcaption>Caption with credit.</figcaption></figure>', 'html.parser')
    normalize_image_layout(soup, 'https://example.test/article')
    assert len(soup.find_all('img')) == 1
    assert soup.find(id='inline').find('figcaption').get_text() == 'Caption with credit.'
    assert soup.find(id='viewer').name == 'span'


def test_transform_candidate_does_not_drop_image_identity_parameters():
    url = 'https://cdn.test/image.png?id=two&w=100&token=secret'
    origin = ImageProcessor._extract_origin_from_proxy(url)
    assert 'id=two' in origin and 'token=secret' in origin
    assert 'w=' not in origin
    a = ImageAsset('a', 'images/a.png', 'image/png', b'a', 'https://cdn.test/image.png?id=one&w=100')
    assert ImageProcessor._find_existing_asset([a], url) is None


def test_density_descriptors_survive_base_resolution():
    soup = BeautifulSoup('<base href="https://cdn.test/images/"/><picture><source srcset="one.png 1x, two.png 2x"/><img src="one.png"/></picture>', 'html.parser')
    normalize_image_layout(soup, 'https://example.test/article')
    assert soup.source['srcset'] == 'https://cdn.test/images/one.png 1x, https://cdn.test/images/two.png 2x'


@pytest.mark.asyncio
async def test_pdf_pipeline_uses_the_same_media_validation(monkeypatch, tmp_path):
    from dala.core.writer import write_output_book, PdfWriter
    write = AsyncMock()
    monkeypatch.setattr(PdfWriter, 'write', write)
    book = book_with('<figure><img src="https://cdn.test/missing.png"/><figcaption>Caption.</figcaption></figure>')
    await write_output_book(book, str(tmp_path / 'article.pdf'), ConversionOptions(output_format='pdf'))
    exported = write.call_args.args[0]
    assert 'Image unavailable' in exported.chapters[0].content_html
    assert 'Caption.' in exported.chapters[0].content_html
    assert exported.media_warnings


@pytest.mark.parametrize('directory', ['extension_chrome', 'firefox_extension'])
def test_browser_candidate_selection_retains_aliases_and_uses_base_uri(directory):
    import shutil
    import subprocess
    if not shutil.which('node'):
        pytest.skip('Node required for extension candidate test')
    root = Path(__file__).resolve().parents[1]
    script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const text = fs.readFileSync(process.argv[1] + '/background.js', 'utf8');
const code = text.slice(text.indexOf('function pickBestImageCandidate('), text.indexOf('async function getCookiesForUrl('));
const context = vm.createContext({URL});
vm.runInContext(code, context);
const result = context.pickBestImageCandidate({
  src: 'thumb.png?id=1', rawUrls: ['thumb.png?id=1', 'lazy.png?id=1'],
  srcset: 'medium.png 600w, full.png?id=1 1200w', baseUri: 'https://cdn.test/images/'
}, 'https://journal.test/article');
assert.equal(result.url, 'https://cdn.test/images/full.png?id=1');
assert(result.alt_urls.includes('https://cdn.test/images/thumb.png?id=1'));
assert(result.alt_urls.includes('https://cdn.test/images/lazy.png?id=1'));
assert.equal(result.alt_urls.length, 4);
'''
    subprocess.run(['node', '-e', script, directory], cwd=root, check=True, timeout=10)


def test_accessibility_control_can_link_preview_to_full_image():
    soup = BeautifulSoup('<figure id="inline"><picture><img src="preview.jpg"/></picture><button aria-controls="expanded">Open</button><figcaption>Caption with credit.</figcaption></figure><figure id="expanded"><img src="full.jpg"/><figcaption>Caption with credit.</figcaption></figure>', 'html.parser')
    normalize_image_layout(soup, 'https://example.test/article')
    assert len(soup.find_all('img')) == 1
    assert soup.img['src'] == 'https://example.test/full.jpg'


def test_declared_image_type_supports_full_size_links_without_extensions():
    from dala.core.image_layout import image_urls
    soup = BeautifulSoup('<a type="image/png" href="/download?id=two"><img src="/preview?id=two"/></a>', 'html.parser')
    assert image_urls(soup.img, 'https://example.test/article')[0] == 'https://example.test/download?id=two'


@pytest.mark.asyncio
async def test_div_caption_survives_style_cleanup_and_failed_fetch(monkeypatch):
    raw = '<article><h1>Title</h1><p>Article body is retained through extraction.</p><div class="photo"><img src="https://cdn.test/missing.jpg"/><div class="image-caption">Photo caption and credit.</div></div></article>'
    raw = raw.replace('Article body is retained through extraction.', 'Article body is retained through extraction. ' * 20)
    extracted = ArticleExtractor.extract_from_html(raw, 'https://example.test/article')
    soup = BeautifulSoup(extracted['html'], 'html.parser')
    assert soup.find('figcaption')
    monkeypatch.setattr(ImageProcessor, 'fetch_image_data', AsyncMock(return_value=(None, None, 'HTTP 403')))
    await ImageProcessor.process_images(None, soup, 'https://example.test/article', [])
    book = book_with(str(soup))
    validate_book_media(book)
    assert 'Photo caption and credit.' in book.chapters[0].content_html
    assert 'Image unavailable' in book.chapters[0].content_html


def test_external_base_does_not_turn_other_document_links_into_local_targets():
    soup = BeautifulSoup('<base href="https://cdn.test/other-document"/><figure><a href="#viewer"><img src="thumb.jpg"/></a><figcaption>Caption.</figcaption></figure><figure id="viewer"><img src="full.jpg"/><figcaption>Caption.</figcaption></figure>', 'html.parser')
    normalize_image_layout(soup, 'https://journal.test/article')
    assert len(soup.find_all('img')) == 2
    assert soup.find(id='viewer').name == 'figure'


def test_malformed_links_and_image_urls_do_not_discard_readable_content():
    soup = BeautifulSoup('<article><p>Readable content.</p><a href="https://[">Broken link</a><img src="https://[" alt="Unavailable diagram"/></article>', 'html.parser')
    normalize_image_layout(soup, 'https://example.test/article')
    book = book_with(str(soup))
    validate_book_media(book)
    assert 'Readable content.' in book.chapters[0].content_html
    assert 'Image unavailable' in book.chapters[0].content_html
    assert len(book.media_warnings) == 1


@pytest.mark.asyncio
async def test_capture_helper_handles_invalid_images_and_comma_responsive_urls():
    from dala.server import ScanRequest, extract_links
    result = await extract_links(ScanRequest(url='https://example.test/article', html='''
        <article><img src="https://["/><img src="small.jpg"
        srcset="https://cdn.test/image,width=400.jpg 400w, https://cdn.test/image,width=1200.jpg 1200w"/></article>
    '''))
    assert result['assets'][0]['url'] == 'https://cdn.test/image,width=1200.jpg'
    assert 'https://cdn.test/image,width=400.jpg' in result['assets'][0]['alt_urls']

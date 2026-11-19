from io import BytesIO
import subprocess
from unittest.mock import Mock

import aiohttp
from aioresponses import aioresponses
from bs4 import BeautifulSoup
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from dala.core.browser import BrowserFetchOptions
from dala.core.extractor import ArticleExtractor
from dala.core.dispatcher import DriverDispatcher
from dala.core.pdf_input import extract_pdf_article
from dala.drivers.generic import GenericDriver
from dala.drivers.hn import HackerNewsDriver
from dala.models import ConversionContext, ConversionOptions, HN_API_BASE_URL, Source


def make_pdf(texts=('Coffeehouses & science <history>', 'Second page findings'), *, password=None):
    writer = PdfWriter()
    writer.add_metadata({'/Title': 'Coffee & Science', '/Author': 'Test Author'})
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Helvetica'),
        })
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})
        })
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_text_metadata_pages_and_html_escaping():
    result = extract_pdf_article(make_pdf(), 'https://example.org/paper.pdf')
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert result['title'] == 'Coffee & Science'
    assert result['author'] == 'Test Author'
    assert result['warning'] is None
    assert [section['id'] for section in soup.select('section')] == ['pdf-page-1', 'pdf-page-2']
    assert not soup.find('h2')
    assert 'Coffeehouses & science <history>' in soup.get_text()
    assert soup.find('history') is None
    assert soup.a['href'] == 'https://example.org/paper.pdf'


@pytest.mark.parametrize('available', [False, True])
def test_pypdf_fallback_without_poppler_or_when_it_fails(monkeypatch, available):
    monkeypatch.setattr('dala.core.pdf_input.extract_layout', Mock(side_effect=ValueError('Unsupported layout')))
    monkeypatch.setattr('dala.core.pdf_input.shutil.which', lambda _: '/pdftotext' if available else None)
    run = Mock(side_effect=subprocess.TimeoutExpired('pdftotext', 90))
    monkeypatch.setattr('dala.core.pdf_input.subprocess.run', run)
    result = extract_pdf_article(make_pdf(), 'https://example.org/paper.pdf')
    assert 'Second page findings' in result['html']
    assert result['warning'] is None
    assert run.call_count == int(available)


def test_poppler_preserves_page_numbers_and_paragraphs(monkeypatch):
    monkeypatch.setattr('dala.core.pdf_input.extract_layout', Mock(side_effect=ValueError('Unsupported layout')))
    monkeypatch.setattr('dala.core.pdf_input.shutil.which', lambda _: '/pdftotext')
    monkeypatch.setattr('dala.core.pdf_input.subprocess.run', Mock(return_value=Mock(
        stdout=b'First paragraph\ncontinued\n\nSecond paragraph\f\fLast page\f'
    )))
    result = extract_pdf_article(make_pdf(), 'https://example.org/paper.pdf')
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert [section['id'] for section in soup.select('section')] == ['pdf-page-1', 'pdf-page-3']
    assert '<p>First paragraph continued</p>' in result['html']
    assert '<p>Second paragraph</p>' in result['html']


@pytest.mark.parametrize('body, notice', [
    (make_pdf(texts=('',)), 'OCR'),
    (make_pdf(password='secret'), 'password-protected'),
    (b'%PDF-1.7\nbroken', 'could not be read'),
])
def test_unreadable_pdf_keeps_explanation_and_source(body, notice):
    result = extract_pdf_article(body, 'https://example.org/paper.pdf')
    assert result['success']
    assert notice in result['warning']
    assert notice in result['html']
    assert 'https://example.org/paper.pdf' in result['html']


@pytest.mark.asyncio
@pytest.mark.parametrize('browser', [False, True])
@pytest.mark.parametrize('content_type, prefix', [
    ('application/pdf; charset=binary', b''),
    ('application/octet-stream', b''),
    ('text/plain', b'\n'),
])
async def test_extensionless_pdf_redirect_and_content_detection(content_type, prefix, browser, monkeypatch):
    async def unexpected_browser(*args):
        pytest.fail('PDF redirects must be detected before opening a browser viewer')

    monkeypatch.setattr('dala.core.extractor.fetch_rendered_source', unexpected_browser)
    url = 'https://example.org/download?id=42'
    final_url = 'https://cdn.example.org/document'
    with aioresponses() as mock:
        mock.get(url, status=302, headers={'Location': final_url})
        mock.get(final_url, body=prefix + make_pdf(), content_type=content_type)
        async with aiohttp.ClientSession() as session:
            result = await ArticleExtractor.get_article_content(
                session, url, browser_options=BrowserFetchOptions() if browser else None
            )
    assert result['input_format'] == 'pdf'
    assert result['source_url'] == final_url
    assert 'Second page findings' in result['html']


@pytest.mark.asyncio
async def test_pdf_ignores_captured_browser_viewer(monkeypatch):
    async def unexpected_browser(*args):
        pytest.fail('PDF must be read from bytes rather than a browser viewer')

    monkeypatch.setattr('dala.core.extractor.fetch_rendered_source', unexpected_browser)
    url = 'https://example.org/paper.PDF?download=1'
    with aioresponses() as mock:
        mock.get(url, body=make_pdf(), content_type='application/pdf')
        async with aiohttp.ClientSession() as session:
            result = await ArticleExtractor.get_article_content(
                session, url, raw_html='<html><body>PDF viewer</body></html>',
                browser_options=BrowserFetchOptions(),
            )
    assert result['input_format'] == 'pdf'


@pytest.mark.asyncio
async def test_generic_pdf_input():
    url = 'https://example.org/paper.pdf'
    with aioresponses() as mock:
        mock.get(url, body=make_pdf(), content_type='application/pdf')
        async with aiohttp.ClientSession() as session:
            book = await GenericDriver().prepare_book_data(
                ConversionContext(session=session, options=ConversionOptions()), Source(url=url)
            )
    assert book.title == 'Coffee & Science'
    assert 'Second page findings' in book.chapters[0].content_html


@pytest.mark.asyncio
@pytest.mark.parametrize('is_forum', [False, True])
@pytest.mark.parametrize('body, expected', [
    (make_pdf(), 'Second page findings'),
    (make_pdf(texts=('',)), 'OCR'),
])
async def test_hn_pdf_article_and_comments(body, expected, is_forum):
    url = 'https://example.org/paper.pdf'
    with aioresponses() as mock:
        mock.get(f'{HN_API_BASE_URL}item/42.json', payload={
            'title': 'HN PDF story', 'url': url, 'by': 'submitter', 'kids': [43],
        })
        mock.get(url, body=body, content_type='application/pdf')
        mock.get(f'{HN_API_BASE_URL}item/43.json', payload={
            'id': 43, 'text': 'Discussion of the paper', 'by': 'reader', 'time': 1,
        })
        async with aiohttp.ClientSession() as session:
            source = Source(
                url='https://news.ycombinator.com/item?id=42', is_forum=is_forum,
                html='<html><body>Captured HN discussion, not the PDF article</body></html>',
            )
            driver = DriverDispatcher.get_driver(source)
            assert isinstance(driver, HackerNewsDriver)
            book = await driver.prepare_book_data(
                ConversionContext(session=session, options=ConversionOptions()),
                source,
            )
    assert book.title == 'HN PDF story'
    assert expected in book.chapters[0].content_html
    assert book.chapters[0].is_article
    assert 'Captured HN discussion' not in book.chapters[0].content_html
    assert 'Discussion of the paper' not in book.chapters[0].content_html
    assert book.chapters[1].is_comments
    assert 'Discussion of the paper' in book.chapters[1].content_html


@pytest.mark.asyncio
async def test_archived_pdf_input(monkeypatch):
    url = 'https://example.org/paper.pdf'
    snapshot = 'https://web.archive.org/web/20261001/' + url

    async def wayback(*args):
        return snapshot

    monkeypatch.setattr(ArticleExtractor, 'get_wayback_url', wayback)
    with aioresponses() as mock:
        mock.get(snapshot, body=make_pdf(), content_type='application/pdf')
        async with aiohttp.ClientSession() as session:
            result = await ArticleExtractor.get_article_content(session, url, force_archive=True)
    assert result['was_archived']
    assert result['archive_url'] == snapshot
    assert 'Second page findings' in result['html']

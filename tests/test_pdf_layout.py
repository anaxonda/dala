from io import BytesIO
from zipfile import ZipFile

import aiohttp
from aioresponses import aioresponses
from bs4 import BeautifulSoup
from PIL import Image
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from dala.core.pdf_input import extract_pdf_article
from dala.core.writer import EpubWriter, write_output_book
from dala.drivers.generic import GenericDriver
from dala.drivers.hn import HackerNewsDriver
from dala.models import ConversionContext, ConversionOptions, HN_API_BASE_URL, Source
from tests.helpers import make_book, make_chapter


@pytest.fixture
def illustrated_pdf():
    writer = PdfWriter()
    writer.add_metadata({'/Title': 'Illustrated Paper', '/Author': 'A. Researcher'})
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    image = DecodedStreamObject()
    image.set_data(Image.new('RGB', (60, 40), '#c83529').tobytes())
    image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                  NameObject('/Width'): NumberObject(60), NameObject('/Height'): NumberObject(40),
                  NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
    image_ref = writer._add_object(image)

    def text(x, y, value, size=12):
        return f'BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET\n'

    first = (
        text(72, 740, '1. Introduction', 18)
        + text(72, 700, 'This paragraph continues with know-')
        + text(72, 685, 'ledge and evidence.')
        + text(90, 655, 'A separate indented paragraph.')
        + 'q 240 0 0 160 72 450 cm /Im1 Do Q\n'
        + text(72, 425, 'Figure 1. An embedded photograph', 10)
        + text(72, 390, 'Body after the photograph.')
        + text(72, 100, '1 A smaller footnote.', 9)
        + text(300, 25, '1')
    )
    second = (
        text(72, 740, '2. Results', 18)
        + '0 0 0 RG 72 450 240 180 re S\n'
        + '72 450 m 120 520 l 180 490 l 250 590 l S\n'
        + '72 510 m 312 510 l S\n'
        + text(90, 550, 'Vector plot label', 10)
        + text(72, 425, 'Figure 2. A vector chart', 10)
        + text(72, 390, 'Results after the chart.')
        + text(72, 350, 'Table 1. Measurements', 10)
        + text(72, 325, 'Group') + text(260, 325, 'Value')
        + text(72, 305, 'Coffee') + text(260, 305, '31')
        + text(72, 275, 'Notes: Values are illustrative.', 10)
        + text(300, 25, '2')
    )
    for commands in [first, second]:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref}),
            NameObject('/XObject'): DictionaryObject({NameObject('/Im1'): image_ref}),
        })
        stream = DecodedStreamObject()
        stream.set_data(commands.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_layout_preserves_photos_vectors_tables_and_prose(illustrated_pdf):
    result = extract_pdf_article(illustrated_pdf, 'https://example.org/illustrated.pdf')
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert result['warning'] is None
    figures = soup.select('figure')
    assert len(figures) == 3
    assert [figure.img['src'] for figure in figures] == [asset.filename for asset in result['images']]
    assert len(result['images']) == 3
    assert [f.figcaption.get_text() for f in figures] == [
        'Figure 1. An embedded photograph', 'Figure 2. A vector chart', 'Table 1. Measurements',
    ]
    assert 'This paragraph continues with knowledge and evidence.' in soup.get_text()
    assert soup.find('p', string='A separate indented paragraph.')
    assert 'A smaller footnote.' in soup.select_one('.pdf-footnotes .pdf-footnote').get_text()
    assert not soup.select_one('#pdf-page-1 .pdf-footnote')
    assert not soup.find('p', string='1')
    assert not soup.find('p', string='Vector plot label')
    assert not soup.find('p', string='Coffee 31')
    assert 'Notes: Values are illustrative.' in soup.get_text()
    assert soup.select_one('#pdf-page-2 h2').get_text() == '2. Results'
    assert [h.get_text() for h in soup.select('section[id^="pdf-page-"] h2')] == ['1. Introduction', '2. Results']
    assert len(soup.select('.pdf-contents a')) == 3
    for link in soup.select('.pdf-contents a'):
        assert soup.select_one(link['href'])
    html = str(soup)
    assert html.index('Figure 1. An embedded photograph') < html.index('Body after the photograph.')


def test_epub_write_errors_are_reported(monkeypatch, tmp_path):
    def fail_write(path, book, options):
        assert options['raise_exceptions'] is True
        raise OSError('Disk quota exceeded')

    monkeypatch.setattr('dala.core.writer.epub.write_epub', fail_write)
    with pytest.raises(OSError, match='Disk quota exceeded'):
        EpubWriter.write(make_book(), str(tmp_path / 'full.epub'))


def test_pdf_images_follow_compact_grayscale_options(illustrated_pdf):
    result = extract_pdf_article(
        illustrated_pdf, 'https://example.org/illustrated.pdf',
        image_options=ConversionOptions(image_preset='compact', image_color='grayscale'),
    )
    assert len(result['images']) == 3
    for asset in result['images']:
        assert asset.media_type == 'image/webp'
        with Image.open(BytesIO(asset.content)) as image:
            assert max(image.size) <= 720
            red, green, blue = image.convert('RGB').getpixel((image.width // 2, image.height // 2))
            assert red == green == blue


@pytest.mark.asyncio
async def test_redundant_appendix_title_keeps_contents_target(tmp_path):
    book = make_book(title='Paper', chapters=[make_chapter(title='Paper', content_html=(
        '<html><body><h1>Paper</h1><div class="pdf-contents"><a href="#appendix-title">Paper</a></div>'
        '<section><h2 id="appendix-title">Paper</h2><p>Appendix content.</p></section></body></html>'
    ))])
    output = tmp_path / 'appendix.epub'
    await write_output_book(book, str(output), ConversionOptions())
    with ZipFile(output) as archive:
        soup = BeautifulSoup(archive.read('EPUB/chapter.xhtml'), 'xml')
        assert soup.find(id='appendix-title')
        assert not soup.find('h2', string='Paper')


@pytest.mark.asyncio
@pytest.mark.parametrize('hn', [False, True])
@pytest.mark.parametrize('no_images', [False, True])
async def test_pdf_images_are_packaged_locally_and_respect_options(illustrated_pdf, tmp_path, hn, no_images):
    url = 'https://example.org/illustrated.pdf'
    source_url = 'https://news.ycombinator.com/item?id=42' if hn else url
    options = ConversionOptions(no_images=no_images, no_comments=True)
    with aioresponses() as mock:
        mock.get(url, body=illustrated_pdf, content_type='application/pdf')
        if hn:
            mock.get(f'{HN_API_BASE_URL}item/42.json', payload={
                'title': 'Illustrated story', 'url': url, 'by': 'reader',
            })
        async with aiohttp.ClientSession() as session:
            driver = HackerNewsDriver() if hn else GenericDriver()
            book = await driver.prepare_book_data(ConversionContext(session=session, options=options), Source(url=source_url))
    output = tmp_path / 'illustrated.epub'
    await write_output_book(book, str(output), options)
    with ZipFile(output) as archive:
        soup = BeautifulSoup(archive.read('EPUB/' + book.chapters[0].filename), 'xml')
        images = soup.select('img')
        if no_images:
            assert not images
            assert not book.images
        else:
            assert len(images) == len(book.images) == 3
            assert len({asset.uid for asset in book.images}) == 3
            for image in images:
                assert not image['src'].startswith(('data:', 'http:','https:'))
                content = archive.read('EPUB/' + image['src'])
                with Image.open(BytesIO(content)) as embedded:
                    assert embedded.width > 40 and embedded.height > 20

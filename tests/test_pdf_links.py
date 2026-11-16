from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from bs4 import BeautifulSoup
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, NullObject, TextStringObject

from dala.core.pdf_input import extract_pdf_article
from dala.core.pdf_footnotes import PdfFootnote, attach_note_references, finish_document, line_html, split_page_notes
from dala.core.pdf_links import _destination, normalize_label
from dala.core.writer import prepare_book_for_output, write_output_book
from dala.models import ConversionOptions, ImageAsset
from tests.helpers import make_book, make_chapter


def text(x, y, value, size=12):
    value = value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    return f'BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET\n'


def pdf_document(commands, links=(), targets=None, destination_type='named'):
    writer = PdfWriter()
    writer.add_metadata({'/Title': 'Link structures'})
    font = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    }))
    for source in commands:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(source.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    destinations = {}
    for name, (page, x, y) in (targets or {}).items():
        array = ArrayObject([writer.pages[page].indirect_reference, NameObject('/XYZ'), FloatObject(x), FloatObject(y), NullObject()])
        reference = writer.add_named_destination(name, page)
        reference.get_object()[NameObject('/D')] = array
        destinations[name] = array
    for page, rect, destination in links:
        if destination.startswith(('https:', 'javascript:', 'mailto:')):
            action = DictionaryObject({NameObject('/S'): NameObject('/URI'), NameObject('/URI'): TextStringObject(destination)})
        else:
            target = TextStringObject(destination)
            if destination_type == 'array':
                target = destinations[destination]
            elif destination_type == 'dict':
                target = DictionaryObject({NameObject('/D'): destinations[destination]})
            action = DictionaryObject({NameObject('/S'): NameObject('/GoTo'), NameObject('/D'): target})
        annotation = writer._add_object(DictionaryObject({
            NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Link'),
            NameObject('/Rect'): ArrayObject([FloatObject(n) for n in rect]), NameObject('/A'): action,
        }))
        writer.pages[page].setdefault(NameObject('/Annots'), ArrayObject()).append(annotation)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize('destination_type', ['named', 'array', 'dict'])
async def test_bibliographic_citations_external_links_and_destinations_survive_epub(tmp_path, destination_type):
    body = pdf_document([
        text(72, 700, 'See Wooldridge (2023) for details.')
        + text(72, 600, 'Open the DOI') + text(72, 550, 'Unsafe link text'),
        text(72, 740, 'References', 18) + text(72, 700, 'Wooldridge (2023). Simple estimators.'),
    ], links=[
        (0, (90, 698, 205, 714), 'arbitrary-bibliography-anchor'),
        (0, (72, 598, 150, 614), 'https://doi.org/10.1234/example'),
        (0, (72, 548, 170, 564), 'javascript:alert(1)'),
    ], targets={'arbitrary-bibliography-anchor': (1, 72, 700)}, destination_type=destination_type)
    result = extract_pdf_article(body, 'https://example.org/paper.pdf')
    soup = BeautifulSoup(result['html'], 'html.parser')
    citation = next(a for a in soup.select('a.pdf-internal-link') if 'Wooldridge' in a.get_text())
    target = soup.find(id=citation['href'][1:])
    assert target is not None
    assert 'Wooldridge (2023)' in (target.find_parent('p') or target.find_next('p')).get_text()
    assert soup.find('a', href='https://doi.org/10.1234/example')
    assert not soup.find('a', href=lambda href: href and href.startswith('javascript:'))
    assert 'Unsafe link text' in soup.get_text()
    output = tmp_path / 'citations.epub'
    await write_output_book(make_book(chapters=[make_chapter(content_html=result['html'])]), str(output), ConversionOptions())
    with ZipFile(output) as archive:
        packaged = BeautifulSoup(archive.read('EPUB/chapter.xhtml'), 'xml')
        assert packaged.select_one('.pdf-document') is not None
        for link in packaged.select('a[href^="#"]'):
            assert packaged.find(id=link['href'][1:])
        assert packaged.find('a', href='https://doi.org/10.1234/example')


@pytest.mark.parametrize('label', ['1', 'iv', 'a', '*'])
def test_unannotated_superscripts_can_link_to_distant_explicit_endnotes(label):
    body = pdf_document([
        text(72, 700, 'Reference.') + text(129, 705, label, 8),
        text(72, 700, 'More ordinary body text on another page.'),
        text(72, 740, 'Endnotes', 18) + text(72, 700, f'[{label}] A body-sized explanatory note.'),
    ])
    soup = BeautifulSoup(extract_pdf_article(body, 'https://example.org/notes.pdf')['html'], 'html.parser')
    note = soup.select_one('.pdf-footnote')
    assert note is not None
    assert 'A body-sized explanatory note.' in note.get_text()
    reference = soup.select_one('.pdf-note-ref a')
    assert reference is not None
    assert soup.find(id=reference['href'][1:]) is note


def test_two_columns_with_restarted_note_numbers_remain_independent():
    source = ''
    for y in [730, 710, 690, 670, 650]:
        source += text(72, y, 'Left column words and evidence.') + text(330, y, 'Right column words and evidence.')
    source += text(250, 735, '1', 8) + text(510, 735, '1', 8)
    source += '72 130 m 252 130 l S\n330 200 m 510 200 l S\n'
    source += text(86, 110, '1', 7) + text(90, 106, 'Left-column explanation.', 10)
    source += text(344, 180, '1', 7) + text(348, 176, 'Right-column explanation.', 10)
    soup = BeautifulSoup(extract_pdf_article(pdf_document([source]), 'https://example.org/columns.pdf', include_images=False)['html'], 'html.parser')
    notes = soup.select('.pdf-footnote')
    assert len(notes) == 2
    refs = soup.select('.pdf-note-ref a')
    assert len(refs) == 2
    assert 'Left-column explanation' in soup.find(id=refs[0]['href'][1:]).get_text()
    assert 'Right-column explanation' in soup.find(id=refs[1]['href'][1:]).get_text()
    main = soup.select_one('#pdf-page-1').get_text()
    assert main.rfind('Left column') < main.find('Right column')


@pytest.mark.parametrize('token, normalized', [('¹', '1'), ('[１２]', '12'), ('[iv]', 'iv'), ('[a]', 'a'), ('†', '†'), ('**', '**')])
def test_marker_normalization(token, normalized):
    assert normalize_label(token) == normalized


def test_ambiguous_restarted_endnote_numbers_are_not_guessed(caplog):
    notes = [PdfFootnote('1', 10, ['First chapter.'], endnote=True), PdfFootnote('1', 20, ['Second chapter.'], endnote=True)]
    html = '<section id="pdf-page-1"><p>Reference<sup class="pdf-note-ref" data-note-label="1" data-note-page="1" data-pdf-inferred="1">1</sup>.</p></section>'
    soup = BeautifulSoup(finish_document(html, notes), 'html.parser')
    assert not soup.select('.pdf-note-ref a')
    assert 'Reference1.' in soup.get_text()
    assert 'without resolved in-text references' in caplog.text


def test_mathematical_exponents_are_not_linked_to_numbered_notes():
    chars = [{'text': char, 'x0': i * 7, 'x1': i * 7 + 6, 'top': 40, 'bottom': 52,
              'size': 12, 'fontname': 'Helvetica'} for i, char in enumerate('We use x')]
    chars.append({'text': '2', 'x0': 56, 'x1': 60, 'top': 36, 'bottom': 44, 'size': 8, 'fontname': 'CMMI10'})
    line = {'text': 'We use x2', 'chars': chars}
    attach_note_references(SimpleNamespace(page_number=1, annots=[]), [line])
    assert 'pdf-note-ref' not in line_html(line)


def test_explicit_destinations_disambiguate_restarted_endnote_numbers():
    body = pdf_document([
        text(72, 700, 'Reference.') + text(129, 705, '1', 8),
        text(72, 740, 'Endnotes', 18) + text(72, 700, '[1] First chapter explanation.'),
        text(72, 740, 'Chapter Two', 18) + text(72, 700, 'Ordinary second-chapter body.'),
        text(72, 740, 'Endnotes', 18) + text(72, 700, '[1] Second chapter explanation.'),
    ], links=[(0, (128, 703, 136, 717), 'unconventional-bookmark')],
        targets={'unconventional-bookmark': (3, 72, 700)})
    soup = BeautifulSoup(extract_pdf_article(body, 'https://example.org/reset.pdf')['html'], 'html.parser')
    assert len(soup.select('.pdf-footnote')) == 2
    ref = soup.select_one('.pdf-note-ref a')
    assert ref is not None
    assert 'Second chapter explanation' in soup.find(id=ref['href'][1:]).get_text()


def test_numbered_bibliography_link_is_not_confused_with_a_footnote():
    body = pdf_document([
        text(72, 700, 'See [1] for the method.'),
        text(72, 740, 'References', 18) + text(72, 700, '[1] A bibliographic entry.')
        + '72 130 m 252 130 l S\n' + text(86, 110, '1', 7) + text(90, 106, 'A different footnote.', 10),
    ], links=[(0, (96, 698, 113, 714), 'bibliography-1')], targets={'bibliography-1': (1, 72, 700)})
    soup = BeautifulSoup(extract_pdf_article(body, 'https://example.org/numeric.pdf')['html'], 'html.parser')
    citation = soup.select_one('a.pdf-internal-link')
    assert citation is not None
    assert not citation.find_parent(class_='pdf-note-ref')
    target = soup.find(id=citation['href'][1:])
    assert 'bibliographic entry' in (target.find_parent('p') or target.find_next('p')).get_text()


def test_body_sized_notes_with_separator_and_links_inside_notes():
    body = pdf_document([
        text(72, 700, 'Evidence.') + text(125, 705, '1', 8)
        + '72 130 m 252 130 l S\n' + text(86, 110, '1', 8) + text(92, 106, 'Open the DOI.', 12),
    ], links=[(0, (92, 104, 170, 120), 'https://doi.org/10.5678/note')])
    soup = BeautifulSoup(extract_pdf_article(body, 'https://example.org/same-font.pdf')['html'], 'html.parser')
    assert len(soup.select('.pdf-footnote')) == 1
    assert soup.select_one('.pdf-footnote a[href="https://doi.org/10.5678/note"]')
    assert soup.select_one('.pdf-note-ref a')


def test_reference_inside_rendered_vector_figure_remains_clickable():
    source = (text(72, 740, 'A diagram.') + '72 450 240 180 re S\n72 510 m 312 510 l S\n72 570 m 312 570 l S\n'
              + text(100, 550, 'Figure relation.') + text(183, 555, '1', 8)
              + text(72, 425, 'Figure 1. An example', 10) + '72 130 m 252 130 l S\n'
              + text(86, 110, '1', 7) + text(90, 106, 'The diagram explanation.', 10))
    body = pdf_document([source], links=[(0, (182, 553, 191, 567), 'Hfootnote.1')], targets={'Hfootnote.1': (0, 90, 110)})
    result = extract_pdf_article(body, 'https://example.org/vector.pdf')
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert len(result['images']) == 1
    reference = soup.select_one('figure .pdf-note-ref a')
    assert reference is not None
    assert 'diagram explanation' in soup.find(id=reference['href'][1:]).get_text()


def test_same_size_bracketed_notes_use_explicit_destination_evidence():
    body = pdf_document([
        text(72, 700, 'See [1] for context.') + '72 130 m 252 130 l S\n'
        + text(86, 106, '[1] A same-size note with a hanging marker.', 12),
    ], links=[(0, (96, 698, 113, 714), 'unusual-note-target')],
        targets={'unusual-note-target': (0, 86, 106)})
    soup = BeautifulSoup(extract_pdf_article(body, 'https://example.org/plain.pdf')['html'], 'html.parser')
    assert 'A same-size note' in soup.select_one('.pdf-footnote').get_text()
    assert soup.select_one('.pdf-note-ref a')


@pytest.mark.asyncio
async def test_caption_cleanup_preserves_link_targets_urls_and_superscripts(tmp_path):
    from PIL import Image

    image = BytesIO()
    Image.new('RGB', (80, 60), 'blue').save(image, format='PNG')
    book = make_book(chapters=[make_chapter(content_html=(
        '<html><body><p><a href="#figure-title">See figure</a></p>'
        '<figure><img src="images/figure.png"/><figcaption><a id="figure-title"></a>'
        'Figure 1. <a href="https://doi.org/10.1234/caption">Source</a><sup>1</sup>'
        '</figcaption></figure></body></html>'
    ))], images=[ImageAsset('figure', 'images/figure.png', 'image/png', image.getvalue(), 'https://example.org/image')])
    output = tmp_path / 'captions.epub'
    await write_output_book(book, str(output), ConversionOptions())
    with ZipFile(output) as archive:
        soup = BeautifulSoup(archive.read('EPUB/chapter.xhtml'), 'xml')
        assert soup.find(id='figure-title')
        assert soup.select_one('.image-caption a[href="https://doi.org/10.1234/caption"]')
        assert soup.select_one('.image-caption sup').get_text() == '1'


def test_rotated_destination_keeps_a_valid_page_level_jump():
    target = SimpleNamespace(page_number=2, page_obj=SimpleNamespace(pageid=77), height=792, rotation=90)
    destination = [SimpleNamespace(objid=77), NameObject('/XYZ'), 72, 700]
    page = SimpleNamespace(pdf=SimpleNamespace(pages=[target], doc=SimpleNamespace(get_dest=lambda _: {'D': destination})))
    assert _destination(page, b'rotated-anchor') == (2, -1, -1)


def test_cyclic_destination_names_are_bounded():
    calls = []

    def get_dest(name):
        calls.append(name)
        return name

    page = SimpleNamespace(pdf=SimpleNamespace(pages=[], doc=SimpleNamespace(get_dest=get_dest)))
    assert _destination(page, b'cycle') is None
    assert len(calls) == 8


def test_link_spanning_lines_retains_text_and_one_destination():
    body = pdf_document([
        text(72, 700, 'Wooldridge') + text(72, 685, '(2023) is cited here.'),
        text(72, 700, 'Wooldridge (2023). A bibliography entry.'),
    ], links=[(0, (71, 683, 140, 714), 'citation')], targets={'citation': (1, 72, 700)})
    soup = BeautifulSoup(extract_pdf_article(body, 'https://example.org/wrapped.pdf')['html'], 'html.parser')
    links = soup.select('#pdf-page-1 a.pdf-internal-link')
    assert len(links) == 2
    assert len({a['href'] for a in links}) == 1
    assert 'Wooldridge' in links[0].get_text()
    assert '(2023)' in links[1].get_text()


def test_pdf_output_cleanup_is_repeatable_and_preserves_metadata():
    book = make_book(chapters=[make_chapter(content_html=(
        '<html><body><div class="post-meta"><p>By Researcher</p></div>'
        '<section id="pdf-page-1"><p>Paper content.</p></section></body></html>'
    ))])
    for _ in range(2):
        book = prepare_book_for_output(book, ConversionOptions())
        soup = BeautifulSoup(book.chapters[0].content_html, 'html.parser')
        assert len(soup.select('.pdf-document')) == 1
        assert soup.select_one('.post-meta').get_text() == 'By Researcher'

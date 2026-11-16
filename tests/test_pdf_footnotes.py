from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from bs4 import BeautifulSoup
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, TextStringObject

from dala.core.pdf_input import extract_pdf_article
from dala.core.pdf_footnotes import attach_note_references, finish_document, line_html, split_page_notes
from dala.core.writer import write_output_book
from dala.models import ConversionOptions
from tests.helpers import make_book, make_chapter


def _footnote_pdf():
    writer = PdfWriter()
    writer.add_metadata({'/Title': 'Scientific activity'})
    font = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    }))

    def text(x, y, value, size=12):
        return f'BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET\n'

    commands = [
        text(72, 720, 'We study short ranges.') + text(196, 725, '3', 8)
        + text(72, 680, 'Moves account for 16 percent')
        + '72 130 m 252 130 l S\n'
        + text(86, 110, '3', 7) + text(90, 106, 'The short range might seem surprising.', 10)
        + text(72, 90, 'About three-quarters of the coffeehouse mentions lie within 400 meters', 10)
        + text(300, 25, '1'),
        text(72, 740, 'of the local effect for practitioners and 38 percent for instrument shops.')
        + text(90, 710, 'Turning to mechanisms, we find evidence consistent with three channels.')
        + '72 130 m 252 130 l S\n'
        + text(72, 110, 'of his lodgings.', 10)
        + text(300, 25, '2'),
        text(72, 740, 'Discussion of the chart.')
        + '72 210 m 252 210 l S\n'
        + text(72, 190, 'Figure 2. The adjacent hexagons.', 10)
        + text(72, 170, 'Notes: Values are in percentages.', 10)
        + text(300, 25, '3'),
    ]
    for number, source in enumerate(commands):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})
        })
        stream = DecodedStreamObject()
        stream.set_data(source.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
        if number == 0:
            annotation = DictionaryObject({
                NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Link'),
                NameObject('/Rect'): ArrayObject([FloatObject(n) for n in [195, 723, 203, 735]]),
                NameObject('/A'): DictionaryObject({NameObject('/S'): NameObject('/GoTo'),
                                                   NameObject('/D'): TextStringObject('Hfootnote.3')}),
            })
            page[NameObject('/Annots')] = ArrayObject([writer._add_object(annotation)])
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


@pytest.mark.asyncio
async def test_footnote_continuation_does_not_interrupt_cross_page_sentence(tmp_path):
    result = extract_pdf_article(_footnote_pdf(), 'https://example.org/paper.pdf', include_images=False)
    soup = BeautifulSoup(result['html'], 'html.parser')
    body = soup.select('section[id^="pdf-page-"]')
    sentence = 'Moves account for 16 percent of the local effect for practitioners and 38 percent for instrument shops.'
    assert any(p.get_text(' ', strip=True) == sentence for p in soup.find_all('p'))
    assert not any('short range might seem surprising' in page.get_text() for page in body)
    notes = soup.select('.pdf-footnotes .pdf-footnote')
    assert len(notes) == 1
    assert '400 meters of his lodgings.' in notes[0].get_text()
    assert soup.select_one('#pdf-page-2 p').get_text().startswith('Turning to mechanisms')
    assert 'Figure 2. The adjacent hexagons.' in soup.select_one('#pdf-page-3').get_text()
    assert 'Notes: Values are in percentages.' in soup.select_one('#pdf-page-3').get_text()
    reference = soup.select_one('.pdf-note-ref a')
    assert reference is not None
    assert soup.find(id=reference['href'][1:]) is notes[0]
    assert notes[0].select_one('.pdf-note-backref')['href'] == '#' + reference['id']
    # Verify output cleanup retains the notes, references, and joined sentence.
    book = make_book(title=result['title'], chapters=[make_chapter(content_html=result['html'])])
    output = tmp_path / 'footnotes.epub'
    await write_output_book(book, str(output), ConversionOptions())
    with ZipFile(output) as archive:
        rendered = BeautifulSoup(archive.read('EPUB/chapter.xhtml'), 'xml')
        assert any(p.get_text(' ', strip=True) == sentence for p in rendered.find_all('p'))
        assert len(rendered.select('.pdf-footnotes .pdf-footnote')) == 1
        for link in rendered.select('.pdf-note-ref a, .pdf-note-backref'):
            assert rendered.find(id=link['href'][1:])


@pytest.mark.parametrize('ending, continuation, expected', [
    ('We study know-', 'ledge transfer.', 'We study knowledge transfer.'),
    ('The effect was 16 percent', 'of local activity.', 'The effect was 16 percent of local activity.'),
])
def test_unfinished_paragraphs_continue_across_page_boundaries(ending, continuation, expected):
    html = (f'<section id="pdf-page-1"><p>{ending}</p></section>'
            f'<section id="pdf-page-2"><p data-pdf-unindented="1">{continuation}</p></section>')
    soup = BeautifulSoup(finish_document(html, []), 'html.parser')
    assert soup.find('p', string=expected)
    assert soup.find(id='pdf-page-2') is not None
    assert not soup.select('[data-pdf-unindented]')


@pytest.mark.parametrize('first', [
    '<p>A complete sentence.<sup class="pdf-note-ref">3</sup></p>',
    '<p class="pdf-small-text">Notes: A figure description</p>',
    '<figure><img src="chart.png"/></figure>',
])
def test_complete_paragraphs_and_figure_notes_are_not_joined(first):
    html = (f'<section id="pdf-page-1">{first}</section>'
            '<section id="pdf-page-2"><p data-pdf-unindented="1">A new paragraph.</p></section>')
    soup = BeautifulSoup(finish_document(html, []), 'html.parser')
    assert soup.select_one('#pdf-page-2 p').get_text() == 'A new paragraph.'


def test_footnote_reference_survives_actualtext_ligature_expansion():
    raw = 'these effffects are local.3'
    chars = [{'text': char, 'x0': i * 6, 'x1': i * 6 + 5, 'top': 50, 'bottom': 60}
             for i, char in enumerate(raw)]
    marker = chars[-1]
    page = SimpleNamespace(page_number=5, annots=[{
        'x0': marker['x0'], 'x1': marker['x1'], 'top': 49, 'bottom': 61,
        'data': {'A': {'D': b'Hfootnote.3'}},
    }])
    line = {'text': 'these effects are local.3', 'chars': chars}
    attach_note_references(page, [line])
    soup = BeautifulSoup(line_html(line), 'html.parser')
    assert soup.select_one('.pdf-note-ref').get_text() == '3'
    assert soup.get_text() == line['text']


def _line(text, top, size=10, label=None):
    chars = [{'text': char, 'x0': 72 + i * 5, 'x1': 77 + i * 5, 'top': top,
              'bottom': top + size, 'size': 7 if label and i < len(label) else size}
             for i, char in enumerate(text)]
    return {'text': text, 'top': top, 'bottom': top + size, 'x0': 72, 'x1': 72 + len(text) * 5, 'chars': chars}


def test_short_page_note_can_start_high_without_a_rule():
    page = SimpleNamespace(page_number=111, width=595, height=842, bbox=(0, 0, 595, 842), lines=[])
    lines = [_line('first such title follows the first coffeehouse.', 73, size=12),
             _line('66The three exceptions are academic:', 95, label='66'),
             _line('the dissertations of 1666, 1694, and 1693.', 108)]
    notes = []
    body = split_page_notes(page, lines, 12, notes)
    assert body == lines[:1]
    assert [note.label for note in notes] == ['66']


def test_numbers_inside_note_text_do_not_start_new_notes():
    rule = {'top': 626, 'bottom': 626, 'x0': 72, 'x1': 252}
    page = SimpleNamespace(page_number=27, width=595, height=842, bbox=(0, 0, 595, 842), lines=[rule])
    lines = [_line('A body paragraph.', 73, size=12),
             _line('45For 56 practitioners, fathers are recorded.', 630, label='45'),
             _line('42 more have linked biographies without occupations.', 655)]
    notes = []
    split_page_notes(page, lines, 12, notes)
    assert [note.label for note in notes] == ['45']
    assert notes[0].parts[-1].startswith('42 more')


def test_dates_in_small_graphic_text_are_not_footnotes():
    page = SimpleNamespace(page_number=54, width=595, height=842, bbox=(0, 0, 595, 842), lines=[])
    lines = [_line('A.3. Figures', 73, size=14),
             _line('8 March 1675: fixes a spring.', 650, size=10.9),
             _line('7 April 1675: shows the King a watch.', 667, size=10.9),
             _line('Figure A2. Scientific collaboration', 685, size=10.9),
             _line('Notes: Diary dates.', 708)]
    notes = []
    assert split_page_notes(page, lines, 12, notes) == lines
    assert not notes

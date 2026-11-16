"""Reflow PDF prose while preserving graphical regions as inline figures."""

from collections import Counter
from contextlib import contextmanager
from html import escape
from io import BytesIO
import re
import hashlib
import threading

import pdfplumber
import pypdfium2

from ..models import ImageAsset, log
from .image_processor import ImageProcessor
from .pdf_footnotes import attach_note_references, finish_document, line_html, split_page_notes, split_endnotes
from .pdf_links import join_html_lines, render_link
from .pdf_columns import prepare_columns, reading_order


CAPTION = re.compile(r'^(?:Figure|Fig\.|Table)\s+[A-Z]?\d+[.:—–\s]', re.I)
PAGE_NUMBER = re.compile(r'^(?:[A-Z]{1,3}[-–])?\d+$')
_PDFIUM_LOCK = threading.Lock()


@contextmanager
def _renderer(body):
    # PDFium calls must not overlap between server conversion worker threads.
    with _PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(body)
    try:
        yield document
    finally:
        with _PDFIUM_LOCK:
            document.close()


def _render_page(document, page_number):
    with _PDFIUM_LOCK:
        page = document.get_page(page_number - 1)
        try:
            bitmap = page.render(scale=150 / 72)
            try:
                return bitmap.to_pil().convert('RGB')
            finally:
                bitmap.close()
        finally:
            page.close()


def _box(obj):
    return (obj['x0'], obj['top'], obj['x1'], obj['bottom'])


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _overlaps(a, b, gap=0):
    return a[0] <= b[2] + gap and a[2] + gap >= b[0] and a[1] <= b[3] + gap and a[3] + gap >= b[1]


def _line_size(line):
    sizes = Counter(round(c['size'], 1) for c in line.get('chars', []) if c['text'].strip())
    return sizes.most_common(1)[0][0] if sizes else 12


def _heading(line, body_size):
    text = line['text'].strip()
    if len(text) > 140 or CAPTION.match(text):
        return None
    if text.casefold() in {'abstract', 'references', 'bibliography', 'appendix', 'online appendix'}:
        return 'h2'
    size = _line_size(line)
    numbered = re.match(r'^(\d+(?:\.\d+)*|[A-Z](?:\.\d+)+)[.)]?\s+\S', text)
    bold = sum('bold' in c.get('fontname', '').lower() for c in line.get('chars', []))
    if size > body_size * 1.12 or (numbered and bold > len(line.get('chars', [])) / 2):
        return 'h3' if numbered and '.' in numbered[1] else 'h2'
    return None


def _graphic_regions(page, lines):
    """Group nearby drawing objects, retaining vector plots as well as photos."""
    objects = []
    for kind, items in [('image', page.images), ('drawing', page.curves + page.rects + page.lines)]:
        for obj in items:
            box = _box(obj)
            if kind == 'image' and (box[2] - box[0] < 8 or box[3] - box[1] < 8):
                continue
            # Page-sized backgrounds and isolated footnote rules are not figures.
            if (box[2] - box[0]) * (box[3] - box[1]) > page.width * page.height * .95:
                continue
            objects.append((box, kind))
    groups = []
    for box, kind in sorted(objects, key=lambda item: item[0][1]):
        # Vertical bands keep adjacent panels and their shared legend together.
        if groups and box[1] <= groups[-1]['box'][3] + 12:
            group = groups[-1]
            group['box'] = _union([group['box'], box])
            group['count'] += 1
            group['image'] |= kind == 'image'
        else:
            groups.append({'box': box, 'count': 1, 'image': kind == 'image'})
    regions = []
    for group in groups:
        box = group['box']
        if box[2] - box[0] < 35 or box[3] - box[1] < 25:
            continue
        if not group['image'] and group['count'] < 3:
            continue
        # Include plot labels that sit outside the drawing paths, but keep the
        # caption as searchable text rather than burning it into the bitmap.
        labels = [line for line in lines if not CAPTION.match(line['text'])
                  and line['top'] >= box[1] - 14 and line['bottom'] <= box[3] + 18
                  and _overlaps(_box(line), box, 25)]
        box = _union([box] + [_box(line) for line in labels])
        regions.append({'box': box, 'caption': None})
    return regions


def _regions_with_tables(page, lines, continued):
    regions = _graphic_regions(page, lines)
    # Borderless tables otherwise turn into scrambled prose. Preserve their
    # columns, symbols and alignment as a readable image, with searchable title.
    tables = [line for line in lines if re.match(r'^Table\s+[A-Z]?\d+[.:—–\s]', line['text'], re.I)]
    if continued and not tables:
        tables = [{'text': 'Table (continued)', 'top': page.bbox[1] + 35,
                   'bottom': page.bbox[1] + 35, 'x0': page.bbox[0], 'x1': page.bbox[2]}]
    for table in tables:
        below = [line for line in lines if line['top'] > table['bottom'] + 2]
        stop = next((line['top'] for line in below
                     if re.match(r'^(?:Notes?|Sources?):', line['text'], re.I)
                     or CAPTION.match(line['text'])), page.bbox[3] - 45)
        rows = [line for line in below if line['bottom'] < stop]
        if not rows:
            continue
        box = _union([_box(line) for line in rows])
        if box[3] - box[1] < 20:
            continue
        regions = [r for r in regions if not _overlaps(r['box'], box)]
        regions.append({'box': box, 'caption': table})
    # Assign each remaining caption to the closest graphic above or below it.
    for caption in [line for line in lines if CAPTION.match(line['text']) and line not in tables]:
        candidates = [r for r in regions if r['caption'] is None
                      and min(abs(caption['top'] - r['box'][3]), abs(r['box'][1] - caption['bottom'])) < 45]
        if candidates:
            region = min(candidates, key=lambda r: min(abs(caption['top'] - r['box'][3]), abs(r['box'][1] - caption['bottom'])))
            region['caption'] = caption
    return regions


def _figure(page, region, raster, number, document_id, assets, options):
    box = region['box']
    # A small white margin prevents clipping tick labels and edge strokes.
    box = (max(page.bbox[0], box[0] - 3), max(page.bbox[1], box[1] - 3),
           min(page.bbox[2], box[2] + 3), min(page.bbox[3], box[3] + 3))
    sx, sy = raster.width / page.width, raster.height / page.height
    crop = raster.crop((int((box[0] - page.bbox[0]) * sx), int((box[1] - page.bbox[1]) * sy),
                        int((box[2] - page.bbox[0]) * sx), int((box[3] - page.bbox[1]) * sy)))
    max_dim, quality, color, preference = ImageProcessor.image_optimize_params(options)
    # Apply geometry/color before the shared optimizer, whose small-image
    # fast paths deliberately preserve the original encoding.
    crop.thumbnail((max_dim, max_dim))
    if color == 'grayscale':
        crop = crop.convert('L')
    output = BytesIO()
    compact = preference == 'webp'
    crop.save(output, format='WEBP' if compact else 'PNG', **({'quality': quality} if compact else {}))
    media_type, extension, content, error = ImageProcessor.optimize_and_get_details(
        'pdf-figure.webp' if compact else 'pdf-figure.png',
        {'Content-Type': 'image/webp' if compact else 'image/png'}, output.getvalue(),
        max_dimension=max_dim, jpeg_quality=quality, color_mode=color, output_preference=preference,
    )
    if error or not content:
        raise ValueError(error or 'Empty PDF figure')
    uid = f'pdf-{document_id}-page-{page.page_number}-figure-{number}'
    filename = f'images/{uid}{extension}'
    assets.append(ImageAsset(uid=uid, filename=filename, media_type=media_type,
                             content=content, original_url=f'pdf:{document_id}#page={page.page_number}'))
    caption = region['caption']['text'] if region['caption'] else f'Graphic from PDF page {page.page_number}'
    caption_html = line_html(region['caption']) if region['caption'] else escape(caption)
    links = ''.join(render_link(link, text) + ' ' for link, text in region.get('links', []))
    adjacent_links = f'<div class="pdf-figure-links">References: {links}</div>' if links else ''
    return (f'<figure data-pdf-block-page="{page.page_number}" data-pdf-block-top="{box[1]}" data-pdf-block-x="{box[0]}">'
            f'<img class="epub-image" src="{filename}" alt="{escape(caption, quote=True)}"/>'
            f'<figcaption>{caption_html}</figcaption>{adjacent_links}</figure>')


def _paragraphs(lines, body_size, headings, page_number, title):
    result = []
    paragraph = []
    previous = None
    footnote = False
    paragraph_start = None
    margins = Counter((line.get('pdf_column'), round(line['x0'] / 3) * 3) for line in lines
                      if 'figure_html' not in line and abs(_line_size(line) - body_size) < body_size * .06)
    left_margins = {column: max((x for c, x in margins if c == column), key=lambda x: margins[(column, x)])
                    for column in {key[0] for key in margins}}

    def flush():
        if paragraph:
            text = join_html_lines(paragraph)
            css = ' class="pdf-small-text"' if footnote else ''
            left_margin = left_margins.get(paragraph_start.get('pdf_column'), 0)
            unindented = ' data-pdf-unindented="1"' if not footnote and abs(paragraph_start['x0'] - left_margin) <= body_size * .4 else ''
            location = f' data-pdf-block-page="{page_number}" data-pdf-block-top="{paragraph_start["top"]}" data-pdf-block-x="{paragraph_start["x0"]}"'
            result.append(f'<p{css}{unindented}{location}>{text}</p>')
            paragraph.clear()

    for line in lines:
        if 'figure_html' in line:
            flush()
            result.append(line['figure_html'])
            previous = None
            continue
        text = line['text'].strip()
        if not text:
            continue
        if page_number == 1 and (text.casefold() == title.casefold() or text in {'‗', '†', '‡'}):
            continue
        tag = _heading(line, body_size)
        if page_number == 1 and text.casefold() != 'abstract' and not re.match(r'^(?:\d+(?:\.\d+)*|[A-Z]\.\d+)[.)]?\s', text):
            tag = None
        if tag:
            flush()
            anchor = f'pdf-heading-{len(headings) + 1}'
            headings.append((anchor, text, tag))
            result.append(f'<{tag} id="{anchor}" data-pdf-block-page="{page_number}" data-pdf-block-top="{line["top"]}" data-pdf-block-x="{line["x0"]}">{line_html(line)}</{tag}>')
            previous = None
            continue
        small = _line_size(line) < body_size * .9
        if previous and (line['top'] - previous['bottom'] > body_size * .42
                         or line['x0'] - previous['x0'] > body_size * .8
                         or line.get('pdf_column') != previous.get('pdf_column')
                         or small != footnote):
            flush()
        footnote = small
        if not paragraph:
            paragraph_start = line
        paragraph.append(line_html(line))
        previous = line
    flush()
    return ''.join(result)


def extract_layout(body: bytes, title: str, include_images: bool = True, image_options=None, source_url='') -> tuple[str, list[ImageAsset]]:
    sections, headings, assets, notes = [], [], [], []
    document_id = hashlib.sha256(body).hexdigest()[:12]
    continued_table = False
    endnotes_active = False
    has_content = False
    with pdfplumber.open(BytesIO(body)) as document, _renderer(body) as renderer:
        # A page dominated by footnotes or plot labels should not redefine
        # normal body text and turn the remaining prose into headings.
        sizes = Counter(round(c['size'], 1) for sample in document.pages[:5]
                        for c in sample.chars if c['text'].strip())
        if sizes:
            mode, count = sizes.most_common(1)[0]
            # Caption-heavy or short pages can contain more small print than
            # prose. A substantially represented nearby larger font is a
            # stronger body-size estimate than that small-print mode.
            body_size = max(size for size, frequency in sizes.items()
                            if size <= mode * 1.35 and frequency >= count * .5)
        else:
            body_size = 12
        for page in document.pages:
            try:
                if page.page_number == 1 or page.page_number % 10 == 0:
                    log.info(f'Extracting PDF layout: page {page.page_number}/{len(document.pages)}')
                lines = page.extract_text_lines(x_tolerance=2, y_tolerance=3)
                lines = [line for line in lines if not (
                    PAGE_NUMBER.fullmatch(line['text'].strip()) and (
                        line['top'] > page.bbox[3] - 55 or line['bottom'] < page.bbox[1] + 35))]
                lines, gutter = prepare_columns(page, lines, body_size)
                attach_note_references(page, lines, source_url)
                lines, endnotes_active = split_endnotes(page, lines, body_size, notes, endnotes_active)
                lines = split_page_notes(page, lines, body_size, notes)
                regions = _regions_with_tables(page, lines, continued_table) if include_images else []
                continued_table = any('continued on next page' in line['text'].lower() for line in lines)
                raster = None
                if regions:
                    raster = _render_page(renderer, page.page_number)
                figures = []
                for number, region in enumerate(regions, 1):
                    box = region['box']
                    embedded_lines = [line for line in lines if line is not region['caption'] and (
                        box[0] - 1 <= (line['x0'] + line['x1']) / 2 <= box[2] + 1
                        and box[1] - 1 <= (line['top'] + line['bottom']) / 2 <= box[3] + 1)]
                    region['links'] = [(link, line['text'][link.start:link.end]) for line in embedded_lines
                                       for link in line.get('pdf_links', []) if not link.inferred]
                    try:
                        figure = _figure(page, region, raster, number, document_id, assets, image_options)
                    except Exception as exc:
                        log.warning(f'Could not render PDF page {page.page_number} figure: {exc}')
                        continue
                    lines = [line for line in lines if line is not region['caption'] and not (
                        box[0] - 1 <= (line['x0'] + line['x1']) / 2 <= box[2] + 1
                        and box[1] - 1 <= (line['top'] + line['bottom']) / 2 <= box[3] + 1)]
                    figures.append({'top': min(box[1], region['caption']['top'] if region['caption'] else box[1]),
                                  'x0': box[0], 'x1': box[2], 'figure_html': figure})
                lines.extend(figures)
                lines = reading_order(lines, gutter)
                content = _paragraphs(lines, body_size, headings, page.page_number, title)
                if content:
                    has_content = True
                sections.append(f'<section id="pdf-page-{page.page_number}">{content}</section>')
            finally:
                page.close()
    contents = ''
    if not has_content and not notes:
        return '', assets
    if len(headings) > 1 or notes:
        links = ''.join(f'<li><a href="#{anchor}">{escape(text)}</a></li>'
                        for anchor, text, tag in headings if tag == 'h2')
        if notes:
            links += '<li><a href="#pdf-footnotes">Footnotes</a></li>'
        contents = f'<div class="pdf-contents"><h2>Contents</h2><ul>{links}</ul></div>'
    if notes:
        log.info(f'Collected {len(notes)} PDF footnotes outside the main reading flow')
    return contents + finish_document(''.join(sections), notes), assets

"""Separate PDF page-bottom notes from continuous ebook prose."""

from collections import Counter
from dataclasses import dataclass, field
from html import escape
import re
import unicodedata

from .pdf_links import attach_pdf_links, infer_note_references, join_html_lines, normalize_label, render_line, render_line_slice, resolve_internal_links
from ..models import log

from bs4 import BeautifulSoup


@dataclass
class PdfFootnote:
    label: str
    page: int
    parts: list[str] = field(default_factory=list)
    last_page: int = 0
    top: float = -1
    x0: float = -1
    endnote: bool = False
    html_parts: list[str] = field(default_factory=list)


def line_size(line):
    sizes = Counter(round(c['size'], 1) for c in line.get('chars', []) if c['text'].strip())
    return sizes.most_common(1)[0][0] if sizes else 12


def _marker_token(line, allow_plain=True):
    text = line['text'].strip()
    normalized = unicodedata.normalize('NFKC', text)
    match = re.match(r'^(\[(?:\d{1,4}|[A-Za-z]{1,8}|[*†‡§¶]+)\]|(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,8}|[A-Za-z]|[*†‡§¶‗]+)[.)]?)', normalized)
    if not match:
        return None
    token = match[0]
    label = normalize_label(token)
    if not label:
        return None
    chars = sorted(line.get('chars', []), key=lambda c: c['x0'])
    prefix = chars[:len(token.strip('[]().'))]
    raised = prefix and all(c['size'] < line_size(line) * .85 for c in prefix)
    symbolic = bool(re.match(r'^[*†‡§¶‗]', token))
    plain = allow_plain and (token.startswith('[') or token.endswith(('.', ')')) or normalized[len(token):].startswith(' '))
    if raised or symbolic or plain:
        # NFKC can change token length (e.g. superscript digits). Count the
        # original characters that produce the normalized marker prefix.
        length = next((i for i in range(1, len(text) + 1)
                       if unicodedata.normalize('NFKC', text[:i]) == token), len(token))
        return label, length
    return None


def _marker(line, allow_plain=True):
    token = _marker_token(line, allow_plain)
    return token[0] if token else None


def split_page_notes(page, lines, body_size, notes):
    columns = {line.get('pdf_column') for line in lines if line.get('pdf_column') is not None}
    if columns == {0, 1}:
        remaining = [line for line in lines if line.get('pdf_column') is None]
        for column in (0, 1):
            remaining.extend(_split_page_notes(page, [line for line in lines if line.get('pdf_column') == column], body_size, notes))
        return remaining
    return _split_page_notes(page, lines, body_size, notes)


def _split_page_notes(page, lines, body_size, notes):
    """Use separator geometry plus note markers, not small type alone."""
    if not lines:
        return lines
    start = None
    for rule in sorted(page.lines, key=lambda r: r['top'], reverse=True):
        width = rule['x1'] - rule['x0']
        if not (abs(rule['bottom'] - rule['top']) <= 1
                and page.width * .12 <= width <= page.width * .55
                and rule['top'] > page.bbox[1] + page.height * .4):
            continue
        below = [line for line in lines if line['top'] >= rule['top'] - 1]
        linked_target = any(link.target_page == page.page_number and link.target_top >= rule['top'] - 5
                            and below and link.target_top <= below[-1]['bottom'] + 20
                            and normalize_label(line['text'][link.start:link.end])
                            for line in lines for link in line.get('pdf_links', []))
        threshold = body_size * (1.04 if below and (_marker(below[0], allow_plain=False) or linked_target) else .94)
        if not below or any(line_size(line) >= threshold for line in below):
            continue
        note_left = min(line['x0'] for line in below)
        if rule['x0'] > note_left + 5 or note_left - rule['x0'] > max(5, body_size * 2):
            continue
        if re.match(r'^(?:Notes?|Sources?|Figure|Table)\b', below[0]['text'], re.I):
            continue
        if any(_marker(line) for line in below) or (notes and notes[-1].last_page == page.page_number - 1):
            start = below[0]['top']
            break
    if start is None:
        # TeX can place an overflowing note on a page by itself. In that case
        # there is no body text or separator, and the note may start at the top.
        if (_marker(lines[0], allow_plain=False)
                and all(line_size(line) < body_size * .94 for line in lines)):
            start = lines[0]['top']
    if start is None:
        # Some PDFs omit the rule. A raised marker can follow body text at any
        # height; plain labels require a much smaller block near the bottom.
        for index, line in enumerate(lines):
            raised = _marker(line, allow_plain=False)
            plain_bottom_note = (line['top'] > page.bbox[1] + page.height * .65
                                 and line_size(line) < body_size * .8 and _marker(line))
            if ((raised or plain_bottom_note)
                    and line_size(line) < body_size * .94
                    and any(line_size(prior) >= body_size * .94 for prior in lines[:index])
                    and not any(re.match(r'^(?:Figure|Fig\.|Table|Notes?|Sources?)\b', later['text'], re.I) for later in lines[index:])
                    and all(line_size(later) < body_size * .94 for later in lines[index:])):
                start = line['top']
                break
    if start is None:
        return lines
    continuation = bool(notes and notes[-1].last_page == page.page_number - 1)
    for index, line in enumerate(line for line in lines if line['top'] >= start):
        label = _marker(line, allow_plain=index == 0 and not continuation)
        text = line['text'].strip()
        content_start = 0
        if label:
            notes.append(PdfFootnote(label=label, page=page.page_number, top=line['top'], x0=line['x0']))
            content_start = _marker_token(line, allow_plain=index == 0 and not continuation)[1]
            text = text[content_start:].lstrip()
        elif not notes:
            notes.append(PdfFootnote(label='*', page=page.page_number, top=line['top'], x0=line['x0']))
        notes[-1].parts.append(text)
        notes[-1].html_parts.append(render_line_slice(line, content_start))
        notes[-1].last_page = page.page_number
    return [line for line in lines if line['top'] < start]


def split_endnotes(page, lines, body_size, notes, active=False):
    """Recognize explicit Notes/Endnotes sections, including body-sized entries."""
    remaining = []
    for index, line in enumerate(lines):
        text = line['text'].strip()
        heading = re.fullmatch(r'(?:endnotes|footnotes|notes)(?:\s+to\s+(?:chapter|section)\b.*)?', text, re.I)
        if heading and (line_size(line) > body_size * 1.1 or any(_marker(later) for later in lines[index + 1:index + 8])):
            active = True
            continue
        token = _marker_token(line) if active else None
        if active and not token and line_size(line) > body_size * 1.12:
            active = False
        if token:
            label, length = token
            notes.append(PdfFootnote(label=label, page=page.page_number, top=line['top'], x0=line['x0'], endnote=True))
            notes[-1].parts.append(text[length:].lstrip())
            notes[-1].html_parts.append(render_line_slice(line, length))
            notes[-1].last_page = page.page_number
        elif active and notes and notes[-1].endnote:
            notes[-1].parts.append(text)
            notes[-1].html_parts.append(render_line(line))
            notes[-1].last_page = page.page_number
        else:
            remaining.append(line)
    return remaining, active


def attach_note_references(page, lines, source_url=''):
    for line in lines:
        line['pdf_page'] = page.page_number
    attach_pdf_links(page, lines, source_url)
    infer_note_references(page, lines)


def line_html(line):
    return render_line(line)


def _join_text(parts):
    return join_html_lines(parts)


def finish_document(html, notes):
    soup = BeautifulSoup(html, 'html.parser')
    previous = None
    for section in soup.select('section[id^="pdf-page-"]'):
        children = section.find_all(recursive=False)
        first = children[0] if children else None
        boundary_text = ''.join(text for text in previous.strings if not text.find_parent(class_='pdf-note-ref')) if previous is not None else ''
        if (previous is not None and first is not None
                and previous.name == first.name == 'p'
                and first.get('data-pdf-unindented') == '1'
                and 'pdf-small-text' not in (previous.get('class') or [])
                and not re.search(r'[.!?;:][\s”’"\)\]]*$', boundary_text.rstrip())):
            last = previous.contents[-1] if previous.contents else None
            if isinstance(last, str) and last.endswith('-') and re.match(r'^[a-z]', first.get_text()):
                last.replace_with(str(last)[:-1])
            else:
                previous.append(' ')
            if first.get('data-pdf-block-page'):
                previous.append(soup.new_tag('a', attrs={key: first[key] for key in
                                ('data-pdf-block-page', 'data-pdf-block-top', 'data-pdf-block-x')}))
            for child in list(first.contents):
                previous.append(child.extract())
            first.decompose()
        children = section.find_all(recursive=False)
        if children:
            previous = children[-1]
    for paragraph in soup.select('[data-pdf-unindented]'):
        del paragraph['data-pdf-unindented']

    section = None
    if notes:
        section = soup.new_tag('section', id='pdf-footnotes', attrs={'class': 'pdf-footnotes'})
        heading = soup.new_tag('h2')
        heading.string = 'Footnotes'
        section.append(heading)
        for number, note in enumerate(notes, 1):
            paragraph = soup.new_tag('p', id=f'pdf-note-{number}', attrs={'class': 'pdf-footnote',
                        'data-pdf-block-page': str(note.page), 'data-pdf-block-top': str(note.top), 'data-pdf-block-x': str(note.x0)})
            label = soup.new_tag('strong')
            label.string = f'{note.label}. '
            paragraph.append(label)
            if note.html_parts:
                fragment = BeautifulSoup(_join_text(note.html_parts), 'html.parser')
                for child in list(fragment.contents):
                    paragraph.append(child.extract())
            else:
                paragraph.append(_join_text(note.parts))
            section.append(paragraph)
        soup.append(section)
    references = {}
    for ref in soup.select('.pdf-note-ref[data-note-label], a.pdf-internal-link'):
        label = ref.get('data-note-label') or normalize_label(ref.get_text())
        page = int(ref.get('data-note-page') or ref.get('data-pdf-source-page', 0))
        target_page = int(ref.get('data-pdf-target-page', 0))
        target_top = float(ref.get('data-pdf-target-top', -1))
        target_x = float(ref.get('data-pdf-target-x', -1))
        source_x = float(ref.get('data-pdf-source-x', -1))
        candidates = [(i, note) for i, note in enumerate(notes, 1) if normalize_label(note.label) == label]
        if target_page:
            candidates = [(i, note) for i, note in candidates if note.page <= target_page <= max(note.page, note.last_page)]
        else:
            candidates = [(i, note) for i, note in candidates if abs(note.page - page) <= 1 or note.endnote]
            if len(candidates) > 1 and not any(abs(note.page - page) <= 1 for _, note in candidates):
                candidates = []  # Chapter-reset endnotes need explicit destination evidence.
        def score(item):
            note = item[1]
            if target_page:
                return ((abs(note.top - target_top) if target_top >= 0 and note.top >= 0 else 0)
                        + (abs(note.x0 - target_x) * .1 if target_x >= 0 and note.x0 >= 0 else 0))
            return abs(note.page - page) * 1000 + (abs(note.x0 - source_x) if source_x >= 0 and note.x0 >= 0 else 0)
        candidates.sort(key=score)
        if (candidates and ref.name == 'a' and target_top >= 0
                and candidates[0][1].top >= 0 and abs(candidates[0][1].top - target_top) > 35):
            candidates = []  # A numbered citation is not necessarily a footnote.
        if len(candidates) > 1 and abs(score(candidates[0]) - score(candidates[1])) < 1:
            candidates = []  # Repeated labels without destination evidence are ambiguous.
        if candidates:
            number, note = candidates[0]
            targets = references.setdefault(number, [])
            ref_id = f'pdf-note-ref-{number}-{len(targets) + 1}'
            targets.append(ref_id)
            if ref.name == 'a':
                ref.name = 'sup'
                ref['class'] = ['pdf-note-ref']
                ref.attrs.pop('href', None)
            ref.clear()
            anchor = soup.new_tag('a', href=f'#pdf-note-{number}', id=ref_id)
            anchor.string = label
            ref.append(anchor)
        elif ref.get('data-pdf-inferred') == '1':
            ref.unwrap()
    if notes:
        for number, note in enumerate(notes, 1):
            paragraph = section.find(id=f'pdf-note-{number}')
            for target in references.get(number) or [f'pdf-page-{note.page}']:
                paragraph.append(' ')
                back = soup.new_tag('a', href=f'#{target}', attrs={'class': 'pdf-note-backref'})
                back.string = '↩'
                paragraph.append(back)
        unlinked = [note.label for i, note in enumerate(notes, 1) if note.label.isdecimal() and i not in references]
        if unlinked:
            log.warning('PDF notes without resolved in-text references: %s', ', '.join(unlinked))
    resolve_internal_links(soup)
    return str(soup)

"""Preserve PDF annotations and their destinations through text reflow."""

from dataclasses import dataclass, replace
from collections import defaultdict
from difflib import SequenceMatcher
from hashlib import sha256
from html import escape
import re
import unicodedata
from urllib.parse import urljoin, urlparse

from pdfminer.pdftypes import resolve1
from pdfplumber.utils.text import chars_to_textmap


def normalize_label(value):
    value = unicodedata.normalize('NFKC', str(value)).strip().strip('[](). ')
    if value.isdecimal():
        return str(int(value))
    if re.fullmatch(r'(?:[a-zA-Z]|[ivxlcdmIVXLCDM]{1,8}|[*†‡§¶‗]+)', value):
        return value.casefold()
    return None


def unique_chars(chars):
    # TextMap can associate multiple rendered characters with one physical
    # ligature. Feeding those repeated objects back into TextMap expands twice.
    seen, result = set(), []
    for char in chars:
        if id(char) not in seen:
            seen.add(id(char))
            result.append(char)
    return result


@dataclass(frozen=True)
class PdfLink:
    start: int
    end: int
    page: int
    x: float
    href: str = ''
    target_page: int = 0
    target_top: float = -1
    target_x: float = -1
    key: str = ''
    label: str = ''
    inferred: bool = False


def _destination(page, value):
    """Resolve named, dictionary and explicit local destinations, bounded."""
    pdf = getattr(page, 'pdf', None)
    if pdf is None:
        return None
    try:
        for _ in range(8):
            value = resolve1(value)
            if isinstance(value, dict):
                value = value.get('D')
            elif isinstance(value, (str, bytes)) or hasattr(value, 'name'):
                value = pdf.doc.get_dest(getattr(value, 'name', value))
            else:
                break
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return None
        target = value[0]
        if isinstance(target, int):
            target_page = pdf.pages[target]
        else:
            page_id = getattr(target, 'objid', None)
            target_page = next((p for p in pdf.pages if p.page_obj.pageid == page_id), None)
        if target_page is None:
            return None
        kind = getattr(value[1], 'name', str(value[1]))
        x, y = None, None
        if kind == 'XYZ' and len(value) >= 4:
            x, y = value[2:4]
        elif kind in ('FitH', 'FitBH') and len(value) >= 3:
            y = value[2]
        elif kind == 'FitR' and len(value) >= 6:
            x, y = value[2], value[5]
        x = float(x) if isinstance(x, (int, float)) else -1
        top = target_page.height - float(y) if isinstance(y, (int, float)) else -1
        # Coordinates of rotated destinations are intentionally page-level:
        # guessing a text target is worse than retaining a valid page jump.
        if getattr(target_page, 'rotation', 0):
            x = top = -1
        return target_page.page_number, top, x
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None
    except Exception:
        # Malformed named-destination trees should not lose readable content.
        return None


def _span(line, selected):
    """Map physical glyphs to rendered text, including ligatures/ActualText."""
    chars = unique_chars(line.get('chars', []))
    selected_ids = {id(char) for char in selected}
    try:
        mapped = chars_to_textmap(chars, x_tolerance=2, y_tolerance=3)
        mapped_text = mapped.as_string
        positions = [i for i, (_, char) in enumerate(mapped.tuples)
                     if char is not None and id(char) in selected_ids]
    except KeyError:
        # Minimal character dictionaries from alternate extractors need not
        # contain every pdfplumber font/layout field.
        mapped_text, positions = '', []
        for char in chars:
            value = unicodedata.normalize('NFKC', char['text'])
            if id(char) in selected_ids:
                positions.extend(range(len(mapped_text), len(mapped_text) + len(value)))
            mapped_text += value
    if not positions:
        return None
    start, end = min(positions), max(positions) + 1
    text = line['text']
    if mapped_text == text:
        return start, end
    # Alignment handles inserted whitespace and producer-specific ActualText;
    # character identity remains the primary evidence for the selected span.
    aligned = []
    for block in SequenceMatcher(None, mapped_text, text, autojunk=False).get_matching_blocks():
        lo, hi = max(start, block.a), min(end, block.a + block.size)
        if lo < hi:
            aligned.extend([block.b + lo - block.a, block.b + hi - block.a])
    if aligned:
        return min(aligned), max(aligned)
    return None


def attach_pdf_links(page, lines, source_url=''):
    for annotation in getattr(page, 'annots', []):
        data = annotation.get('data') or {}
        action = data.get('A') or {}
        uri = annotation.get('uri') or action.get('URI')
        if isinstance(uri, bytes):
            uri = uri.decode('utf-8', errors='replace')
        href = urljoin(source_url, uri) if isinstance(uri, str) else ''
        if href and urlparse(href).scheme.lower() not in {'http', 'https', 'mailto', 'tel', 'ftp'}:
            href = ''
        destination = action.get('D') or data.get('Dest')
        action_type = getattr(action.get('S'), 'name', action.get('S'))
        if action_type in {'GoToR', 'Launch', 'JavaScript'}:
            continue
        target = _destination(page, destination) if destination is not None else None
        name = destination.decode('utf-8', errors='replace') if isinstance(destination, bytes) else str(destination)
        named_note = re.search(r'(?:footnote|endnote|note|fn)[._:-]?(\d+|[*†‡§¶]+|[ivxlcdm]+)$', name, re.I)
        if not href and not target and not named_note:
            continue
        for line in lines:
            selected = [char for char in unique_chars(line.get('chars', []))
                        if annotation['x0'] - .5 <= (char['x0'] + char['x1']) / 2 <= annotation['x1'] + .5
                        and annotation['top'] - .5 <= (char['top'] + char['bottom']) / 2 <= annotation['bottom'] + .5]
            if not selected:
                continue
            if named_note:
                marker_chars = r'[\d*†‡§¶‗ivxlcdm]+'
                selected = [char for char in selected if re.fullmatch(marker_chars, unicodedata.normalize('NFKC', char['text']), re.I)]
                if not selected:
                    continue
            span = _span(line, selected)
            if span is None:
                continue
            start, end = span
            label = normalize_label(line['text'][start:end]) if named_note else ''
            if named_note and not label:
                continue
            target_page, top, x = target or (0, -1, -1)
            key = sha256(repr((target, name)).encode()).hexdigest()[:16] if not href else ''
            link = PdfLink(start, end, page.page_number, line.get('x0', selected[0]['x0']), href,
                           target_page, top, x, key, label or '')
            line.setdefault('pdf_links', []).append(link)


def infer_note_references(page, lines):
    """Conservative candidates; final linking requires an unambiguous note."""
    for line in lines:
        chars = sorted(unique_chars(line.get('chars', [])), key=lambda c: c['x0'])
        if not chars or any('size' not in char for char in chars):
            continue
        sizes = [c['size'] for c in chars if c['text'].strip()]
        if not sizes:
            continue
        normal = sorted(sizes)[len(sizes) // 2]
        groups, group = [], []
        for char in chars:
            raised = (char['size'] < normal * .83
                      and char['bottom'] < max(c['bottom'] for c in chars) - normal * .12)
            mathematical = re.search(r'(?:math|cmmi|cmsy|cmex|symbol)', char.get('fontname', ''), re.I)
            if raised and not mathematical and re.fullmatch(r'[\d*†‡§¶A-Za-z]', unicodedata.normalize('NFKC', char['text'])):
                if group and char['x0'] - group[-1]['x1'] > normal * .5:
                    groups.append(group)
                    group = []
                group.append(char)
            else:
                if group:
                    groups.append(group)
                    group = []
        if group:
            groups.append(group)
        for group in groups:
            span = _span(line, group)
            if span is None:
                continue
            start, end = span
            if any(link.start < end and link.end > start for link in line.get('pdf_links', [])):
                continue
            label = normalize_label(line['text'][start:end])
            preceding = line['text'][:start].rstrip()
            # Do not reinterpret x², 10³, or a displayed equation as footnotes.
            if not label or not re.search(r'(?:[A-Za-z]{2,}[.,;:!?)]?|[.!?])$', preceding):
                continue
            line.setdefault('pdf_links', []).append(PdfLink(start, end, page.page_number, line.get('x0', group[0]['x0']), label=label, inferred=True))


def render_link(link, text):
    attributes = f' data-pdf-source-page="{link.page}" data-pdf-source-x="{link.x}"'
    if link.target_page:
        attributes += f' data-pdf-target-page="{link.target_page}" data-pdf-target-top="{link.target_top}" data-pdf-target-x="{link.target_x}"'
    if link.label:
        return (f'<sup class="pdf-note-ref" data-note-label="{escape(link.label)}" data-note-page="{link.page}"'
                f' data-pdf-inferred="{int(link.inferred)}"{attributes}>{escape(text)}</sup>')
    if link.href:
        return f'<a href="{escape(link.href, quote=True)}">{escape(text)}</a>'
    return (f'<a class="pdf-internal-link" href="#pdf-page-{link.target_page}"'
            f' data-pdf-target-key="{link.key}"{attributes}>{escape(text)}</a>')


def render_line(line):
    text, result, position = line['text'], [], 0
    for link in sorted(set(line.get('pdf_links', [])), key=lambda link: (link.start, link.inferred, -link.end)):
        if link.start < position:
            continue
        result.append(escape(text[position:link.start]))
        result.append(render_link(link, text[link.start:link.end]))
        position = link.end
    result.append(escape(text[position:]))
    location = ''
    if all(key in line for key in ('pdf_page', 'top', 'x0')):
        location = (f'<a class="pdf-line-anchor" data-pdf-block-page="{line["pdf_page"]}"'
                    f' data-pdf-block-top="{line["top"]}" data-pdf-block-x="{line["x0"]}"></a>')
    return location + ''.join(result)


def join_html_lines(parts):
    # Keep physical line positions for destination mapping while removing
    # discretionary word breaks introduced by PDF line wrapping.
    return re.sub(r'(\w)-\s+((?:<a class="pdf-line-anchor"[^>]*></a>\s*)*)([a-z])',
                  r'\1\2\3', ' '.join(parts))


def render_line_slice(line, start):
    links = [replace(link, start=max(start, link.start) - start, end=link.end - start)
             for link in line.get('pdf_links', []) if link.end > start]
    return render_line({**line, 'text': line['text'][start:], 'pdf_links': links})


def resolve_internal_links(soup):
    anchors = {}
    blocks_by_page = defaultdict(list)
    for block in soup.select('[data-pdf-block-page]'):
        blocks_by_page[block['data-pdf-block-page']].append(block)
    page_ids = {tag['id'] for tag in soup.find_all(id=True)}
    for link in soup.select('a.pdf-internal-link'):
        key = link.get('data-pdf-target-key')
        if key in anchors:
            link['href'] = '#' + anchors[key]
            continue
        page = link.get('data-pdf-target-page')
        top = float(link.get('data-pdf-target-top', -1))
        x = float(link.get('data-pdf-target-x', -1))
        blocks = blocks_by_page.get(page, []) if top >= 0 else []
        if blocks:
            block = min(blocks, key=lambda b: abs(float(b['data-pdf-block-top']) - top)
                        + (abs(float(b['data-pdf-block-x']) - x) * .1 if x >= 0 else 0))
            anchor_id = f'pdf-target-{key}'
            anchor = soup.new_tag('a', id=anchor_id)
            block.insert_before(anchor)
            anchors[key] = anchor_id
            link['href'] = '#' + anchor_id
        elif f'pdf-page-{page}' not in page_ids:
            # Do not leave a broken local href if the PDF target is unavailable.
            link.unwrap()
    for marker in soup.select('a.pdf-line-anchor'):
        marker.decompose()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            if attribute.startswith(('data-pdf-', 'data-note-')):
                del tag[attribute]

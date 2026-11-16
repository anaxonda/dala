"""Conservative column separation for PDF text and column-local notes."""

from statistics import median

from pdfplumber.utils.text import chars_to_textmap

from .pdf_links import unique_chars


def prepare_columns(page, lines, body_size):
    gaps = []
    for line in lines:
        chars = sorted(unique_chars(line.get('chars', [])), key=lambda c: c['x0'])
        regular = [c for c in chars if c['text'].strip() and abs(c['size'] - body_size) < body_size * .1]
        for index in range(12, len(regular) - 12):
            left, right = regular[index - 1]['x1'], regular[index]['x0']
            if (right - left >= max(20, body_size * 2)
                    and page.bbox[0] + page.width * .3 < (left + right) / 2 < page.bbox[0] + page.width * .7):
                gaps.append((left, right, line['top']))
    if len({round(gap[2]) for gap in gaps}) < 4:
        return lines, None
    candidates = [median([(a + b) / 2 for a, b, _ in gaps])] + [a + 1 for a, _, _ in gaps]
    gutter = max(candidates, key=lambda x: len({round(y) for a, b, y in gaps if a < x < b}))
    support = len({round(y) for a, b, y in gaps if a < gutter < b})
    if support < 4:
        return lines, None
    result = []
    for line in lines:
        chars = unique_chars(line.get('chars', []))
        left = [c for c in chars if (c['x0'] + c['x1']) / 2 < gutter]
        right = [c for c in chars if (c['x0'] + c['x1']) / 2 >= gutter]
        if left and right and min(c['x0'] for c in right) - max(c['x1'] for c in left) >= 16:
            for column, group in enumerate((left, right)):
                result.append({**line, 'text': chars_to_textmap(group, x_tolerance=2, y_tolerance=3).as_string.strip(),
                               'chars': group, 'x0': min(c['x0'] for c in group), 'x1': max(c['x1'] for c in group),
                               'pdf_column': column})
        else:
            line['pdf_column'] = None if left and right else (0 if left else 1)
            result.append(line)
    return result, gutter


def reading_order(lines, gutter):
    ordered = sorted(lines, key=lambda line: (line['top'], line['x0']))
    if gutter is None:
        return ordered
    result, pending = [], []
    for line in ordered:
        spanning = line['x0'] < gutter - 8 and line.get('x1', line['x0']) > gutter + 8 and line.get('pdf_column') is None
        if spanning:
            result.extend(sorted(pending, key=lambda item: (item['x0'] >= gutter, item['top'], item['x0'])))
            pending.clear()
            result.append(line)
        else:
            pending.append(line)
    result.extend(sorted(pending, key=lambda item: (item['x0'] >= gutter, item['top'], item['x0'])))
    return result

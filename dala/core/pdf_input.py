"""Convert text-based PDF input into the shared article HTML representation."""

from html import escape
from io import BytesIO
import re
import shutil
import subprocess
from urllib.parse import unquote, urlparse

from pypdf import PdfReader

from ..models import log
from .pdf_layout import extract_layout


def is_pdf_response(body: bytes, content_type: str = '') -> bool:
    return (
        content_type.split(';', 1)[0].strip().lower() == 'application/pdf'
        or b'%PDF-' in body[:1024]
    )


def _page_texts(body: bytes, reader: PdfReader) -> list[str]:
    # Poppler handles word spacing in tightly kerned academic PDFs better.
    executable = shutil.which('pdftotext')
    if executable:
        try:
            process = subprocess.run(
                [executable, '-enc', 'UTF-8', '-', '-'], input=body,
                capture_output=True, check=True, timeout=90,
            )
            text = process.stdout.decode('utf-8', errors='replace')
            if text.strip():
                pages = text.split('\f')
                if not pages[-1].strip():
                    pages.pop()
                return pages
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug(f'pdftotext failed; using pypdf: {exc}')
    return [page.extract_text() or '' for page in reader.pages]


def extract_pdf_article(body: bytes, url: str, include_images: bool = True, image_options=None) -> dict:
    title = unquote(urlparse(url).path.rsplit('/', 1)[-1]) or 'PDF document'
    author = None
    sections = []
    assets = []
    warning = None
    try:
        reader = PdfReader(BytesIO(body))
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('This PDF is password-protected.')
        metadata = reader.metadata
        if metadata:
            title = str(metadata.title or title).strip() or title
            author = str(metadata.author).strip() if metadata.author else None
        try:
            layout, assets = extract_layout(body, title, include_images=include_images, image_options=image_options, source_url=url)
            if layout:
                sections.append(layout)
        except Exception as exc:
            log.warning(f'PDF layout extraction failed; using plain text: {exc}')
            for number, text in enumerate(_page_texts(body, reader), 1):
                text = text.strip()
                if not text:
                    continue
                paragraphs = [
                    f'<p>{escape(" ".join(paragraph.split()))}</p>'
                    for paragraph in re.split(r'\n\s*\n', text) if paragraph.strip()
                ]
                sections.append(f'<section id="pdf-page-{number}">{"".join(paragraphs)}</section>')
        if not sections:
            warning = 'This PDF has no extractable text. It may be scanned and require OCR, which Dala does not currently provide.'
    except Exception as exc:
        log.warning(f'Could not extract PDF text from {url}: {exc}')
        warning = 'This PDF could not be read. It may be damaged or password-protected.'

    if warning:
        log.warning(f'{url}: {warning}')
    notice = f'<p>{escape(warning)}</p>' if warning else ''
    source_link = f'<p><a href="{escape(url, quote=True)}">Original PDF</a></p>'
    return {
        # Keep an explanatory source chapter even when OCR/passwords are needed.
        'success': True,
        'html': f'<html><body>{source_link}{notice}{"".join(sections)}</body></html>',
        'title': title,
        'author': author,
        'date': None,
        'sitename': urlparse(url).hostname,
        'source_url': url,
        'was_archived': False,
        'archive_url': None,
        'input_format': 'pdf',
        'warning': warning,
        'images': assets,
    }

"""Private, restart-safe article checkpoints. No browser cookies or API keys."""
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from ebooklib import epub

from ..models import BookData, Chapter, ImageAsset, log


def root_dir() -> Path:
    return Path(os.getenv('DALA_JOB_STATE_DIR', str(Path.home() / '.local/state/dala/jobs')))


def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('Invalid job ID')
    return root_dir() / job_id


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.checkpoint-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def encode_toc(item):
    if isinstance(item, epub.Link):
        return {'kind': 'link', 'href': item.href, 'title': item.title, 'uid': item.uid}
    if isinstance(item, epub.Section):
        return {'kind': 'section', 'title': item.title, 'href': item.href}
    if isinstance(item, (list, tuple)):
        return [encode_toc(child) for child in item]
    raise TypeError(f'Unsupported table of contents item: {type(item).__name__}')


def decode_toc(item):
    if isinstance(item, list):
        return tuple(decode_toc(child) for child in item)
    if item['kind'] == 'link':
        return epub.Link(item['href'], item['title'], item['uid'])
    return epub.Section(item['title'], item['href'])


def save_book(job_id: str, source_url: str, book: BookData) -> None:
    data = asdict(book)
    data['toc_structure'] = [encode_toc(item) for item in book.toc_structure]
    for image in data['images']:
        image['content'] = base64.b64encode(image['content']).decode('ascii')
    digest = hashlib.sha256(source_url.encode()).hexdigest()
    write_json(job_dir(job_id) / f'{digest}.json', {'source_url': source_url, 'book': data})


def load_books(job_id: str):
    books = {}
    for path in job_dir(job_id).glob('[0-9a-f]' * 64 + '.json'):
        try:
            data = json.loads(path.read_text())
            fields = data['book']
            fields['chapters'] = [Chapter(**chapter) for chapter in fields['chapters']]
            fields['images'] = [ImageAsset(**{**image, 'content': base64.b64decode(image['content'])}) for image in fields['images']]
            fields['toc_structure'] = [decode_toc(item) for item in fields['toc_structure']]
            books[data['source_url']] = BookData(**fields)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning(f'Could not restore article checkpoint {path.name}: {exc}')
    return books


def remove_job(job_id: str) -> None:
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        return
    shutil.rmtree(job_dir(job_id), ignore_errors=True)

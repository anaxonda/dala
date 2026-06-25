import copy
import hashlib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import BookData, Chapter, ImageAsset, ConversionOptions, normalize_image_preset, normalize_url_for_matching, sanitize_filename


class ImageBudgetExceeded(ValueError):
    pass


@dataclass(frozen=True)
class ImageBudget:
    preset: str
    max_images: Optional[int]
    max_bytes: Optional[int]


@dataclass
class ImageStats:
    image_count: int = 0
    image_bytes: int = 0
    duplicate_count: int = 0
    remapped_count: int = 0
    largest_images: List[Tuple[str, int]] = None

    def __post_init__(self):
        if self.largest_images is None:
            self.largest_images = []

    @property
    def image_mb(self) -> float:
        return self.image_bytes / (1024 * 1024)


PRESET_LIMITS = {
    "compact": ImageBudget("compact", 200, 100 * 1024 * 1024),
    "balanced": ImageBudget("balanced", 400, 200 * 1024 * 1024),
    "full": ImageBudget("full", None, None),
}


def image_budget_from_options(options: Optional[ConversionOptions]) -> ImageBudget:
    preset = normalize_image_preset(getattr(options, "image_preset", None))
    base = PRESET_LIMITS.get(preset, PRESET_LIMITS["balanced"])
    max_images = getattr(options, "max_bundle_images", None)
    max_mb = getattr(options, "max_image_bytes_mb", None)
    if max_images is None:
        env_images = os.getenv("DALA_MAX_BUNDLE_IMAGES")
        max_images = int(env_images) if env_images else base.max_images
    if max_mb is None:
        env_mb = os.getenv("DALA_MAX_IMAGE_BYTES_MB")
        max_bytes = int(env_mb) * 1024 * 1024 if env_mb else base.max_bytes
    else:
        max_bytes = int(max_mb) * 1024 * 1024
    return ImageBudget(base.preset, max_images, max_bytes)


def collect_image_stats(book: BookData) -> ImageStats:
    stats = ImageStats()
    stats.image_count = len(book.images)
    stats.image_bytes = sum(len(img.content or b"") for img in book.images)
    largest = sorted(
        ((img.filename, len(img.content or b"")) for img in book.images),
        key=lambda item: item[1],
        reverse=True,
    )
    stats.largest_images = largest[:5]
    return stats


def assert_image_budget(book: BookData, options: Optional[ConversionOptions]) -> ImageStats:
    budget = image_budget_from_options(options)
    stats = collect_image_stats(book)
    failures = []
    if budget.max_images is not None and stats.image_count > budget.max_images:
        failures.append(f"{stats.image_count} images exceeds {budget.max_images}")
    if budget.max_bytes is not None and stats.image_bytes > budget.max_bytes:
        failures.append(f"{stats.image_mb:.1f} MB optimized image bytes exceeds {budget.max_bytes / (1024 * 1024):.0f} MB")
    if failures:
        largest = ", ".join(f"{name}={size / (1024 * 1024):.1f}MB" for name, size in stats.largest_images)
        detail = "; ".join(failures)
        if largest:
            detail += f"; largest: {largest}"
        raise ImageBudgetExceeded(f"Image budget exceeded for preset '{budget.preset}': {detail}")
    return stats


def _content_hash(asset: ImageAsset) -> Optional[str]:
    if not asset.content:
        return None
    return hashlib.sha1(asset.content).hexdigest()


def _url_keys(asset: ImageAsset) -> List[str]:
    urls = []
    for u in [asset.original_url] + list(asset.alt_urls or []):
        if not u:
            continue
        urls.append(u)
        norm = normalize_url_for_matching(u)
        if norm:
            urls.append(norm)
    return list(dict.fromkeys(urls))


def _unique_filename(existing: set, original: str, book_index: int) -> str:
    parsed = urlparse(original)
    dirname = os.path.dirname(parsed.path or original) or "images"
    basename = os.path.basename(parsed.path or original) or "image"
    root, ext = os.path.splitext(basename)
    root = sanitize_filename(root) or "image"
    ext = ext or ".img"
    if not dirname.startswith("images"):
        dirname = "images"
    candidate = f"{dirname}/{root}{ext}"
    if candidate not in existing:
        return candidate
    candidate = f"{dirname}/src{book_index}_{root}{ext}"
    counter = 1
    while candidate in existing:
        counter += 1
        candidate = f"{dirname}/src{book_index}_{root}_{counter}{ext}"
    return candidate


def _remap_chapter_images(chapter: Chapter, filename_map: Dict[str, str]) -> Chapter:
    if not filename_map:
        return chapter
    soup = BeautifulSoup(chapter.content_html or "", "html.parser")
    changed = False
    for tag in soup.find_all(["img", "a"]):
        attr = "src" if tag.name == "img" else "href"
        val = tag.get(attr)
        if val in filename_map:
            tag[attr] = filename_map[val]
            changed = True
    if not changed:
        html = chapter.content_html
        for old, new in filename_map.items():
            html = html.replace(f'"{old}"', f'"{new}"').replace(f"'{old}'", f"'{new}'")
        if html == chapter.content_html:
            return chapter
        new_chapter = copy.copy(chapter)
        new_chapter.content_html = html
        return new_chapter
    new_chapter = copy.copy(chapter)
    new_chapter.content_html = str(soup)
    return new_chapter


def prepare_books_for_bundle(books: List[BookData]) -> Tuple[List[BookData], ImageStats]:
    prepared = []
    seen_filenames = set()
    seen_hashes: Dict[str, ImageAsset] = {}
    seen_urls: Dict[str, ImageAsset] = {}
    stats = ImageStats()

    for idx, book in enumerate(books, start=1):
        filename_map: Dict[str, str] = {}
        images: List[ImageAsset] = []
        for asset in book.images:
            digest = _content_hash(asset)
            existing = seen_hashes.get(digest) if digest else None
            if not existing:
                for key in _url_keys(asset):
                    if key in seen_urls:
                        existing = seen_urls[key]
                        break
            if existing:
                filename_map[asset.filename] = existing.filename
                stats.duplicate_count += 1
                continue

            new_asset = copy.copy(asset)
            new_filename = _unique_filename(seen_filenames, asset.filename, idx)
            if new_filename != asset.filename:
                filename_map[asset.filename] = new_filename
                new_asset.filename = new_filename
                stats.remapped_count += 1
            seen_filenames.add(new_asset.filename)
            if digest:
                seen_hashes[digest] = new_asset
            for key in _url_keys(new_asset):
                seen_urls[key] = new_asset
            images.append(new_asset)

        new_book = copy.copy(book)
        new_book.images = images
        new_book.chapters = [_remap_chapter_images(chap, filename_map) for chap in book.chapters]
        prepared.append(new_book)

    return prepared, stats

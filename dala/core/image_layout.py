"""Recover image relationships before publisher layout attributes are discarded."""
import json
import re
from urllib.parse import urldefrag, urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup, Tag


def resolve_image_url(base_url: str, url: str) -> str:
    try:
        return urljoin(base_url, url)
    except ValueError:
        return ""


def asset_url_keys(url: str) -> set[str]:
    from .image_processor import ImageProcessor
    keys = {url}
    # Only unwrap declared source URLs, not arbitrary query-bearing filenames.
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return keys
    if {"url", "src", "original"}.intersection(query):
        origin = ImageProcessor._extract_origin_from_proxy(url)
        if origin:
            keys.add(origin)
    return keys


def image_urls(img: Tag, base_url: str) -> list[str]:
    from .image_processor import ImageProcessor

    urls = []
    for attr in ("data-src", "data-original", "data-url"):
        if img.get(attr):
            urls.append(img[attr])
    for attr in ("data-srcset", "srcset"):
        urls.extend(ImageProcessor.parse_srcset(img.get(attr) or ""))
    if img.get("src"):
        urls.append(img["src"])
    try:
        urls.extend(json.loads(img.get("data-dala-image-urls") or "[]"))
    except (ValueError, TypeError):
        pass
    parent = img.find_parent("picture")
    if parent:
        for source in parent.find_all("source"):
            urls.extend(ImageProcessor.parse_srcset(source.get("srcset") or source.get("data-srcset") or ""))
    link = img.find_parent("a", href=True)
    linked_url = resolve_image_url(base_url, link["href"]) if link else ""
    if linked_url and (str(link.get("type") or "").startswith("image/") or urlparse(linked_url).path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"))):
        urls.insert(0, link["href"])
    return list(dict.fromkeys(
        resolve_image_url(base_url, url.strip()) for url in urls
        if isinstance(url, str) and url.strip()
        and resolve_image_url(base_url, url.strip())
        and not ImageProcessor.is_junk(url) and not ImageProcessor.is_blur_placeholder(url)
    ))


def normalize_image_layout(soup: BeautifulSoup, page_url: str) -> None:
    """Unify explicitly linked previews and display copies without site rules."""
    from .image_processor import ImageProcessor

    base = soup.find("base", href=True)
    base_url = (resolve_image_url(page_url, base["href"]) or page_url) if base else page_url
    # Capture relationships against the complete DOM, including detached dialogs.
    targets = {}
    for target in soup.find_all(id=True):
        targets.setdefault(target["id"], []).append(target)
    replacements = []
    seen_previews = set()

    def caption_and_group(img):
        group = img.find_parent("figure")
        if group is None:
            group = next((parent for parent in img.parents if parent.get("id") and len(parent.find_all("img", limit=2)) == 1), None)
        caption = ImageProcessor.find_caption(img) if group is not None and group.name == "figure" else None
        if not caption and group:
            blocks = [node for node in group.find_all(["div", "p", "figcaption"])
                      if not node.find("img") and node.parent.find("img")]
            texts = list(dict.fromkeys(" ".join(node.get_text(" ", strip=True).split()) for node in blocks))
            texts = [text for text in texts if text]
            text = texts[0] if len(texts) == 1 else " ".join(group.get_text(" ", strip=True).split())
            if 0 < len(text) <= 8192:
                caption = text
        if not caption:
            caption = ImageProcessor.find_caption(img)
        return " ".join((caption or "").split()), group

    for link in soup.find_all(lambda tag: tag.has_attr("href") or tag.has_attr("aria-controls")):
        controls = (link.get("aria-controls") or "").split()
        href = link.get("href") or ("#" + controls[0] if len(controls) == 1 else "")
        absolute, fragment = urldefrag(resolve_image_url(base_url, href))
        if not fragment or absolute != urldefrag(page_url)[0]:
            continue
        target_list = targets.get(fragment, [])
        if len(target_list) != 1:
            continue
        target = target_list[0]
        previews = link.find_all("img")
        if not previews and link.parent:
            previews = link.parent.find_all("img", limit=2)
        full_images = target.find_all("img")
        if len(previews) != 1 or len(full_images) != 1:
            continue
        preview, full = previews[0], full_images[0]
        if id(preview) in seen_previews:
            continue
        if preview is full or target in preview.parents or preview in target.parents:
            continue
        # Do not move independent multi-image galleries or unrelated prose.
        caption, group = caption_and_group(preview)
        full_caption, _ = caption_and_group(full)
        copy_caption = not caption and group is not None and not group.get_text(strip=True)
        if copy_caption:
            caption = full_caption
        if not caption or not full_caption or caption != full_caption:
            continue
        aliases = list(dict.fromkeys(image_urls(full, base_url) + image_urls(preview, base_url)))
        if not aliases:
            continue
        replacements.append((preview, target, aliases, fragment, caption, group, copy_caption))
        seen_previews.add(id(preview))

    removed = set()
    for preview, target, aliases, fragment, caption, group, copy_caption in replacements:
        if preview.parent is None:
            continue
        preview["src"] = aliases[0]
        preview["data-dala-image-urls"] = json.dumps(aliases)
        # The full-size candidate is authoritative within this explicit group.
        for attr in ("srcset", "data-srcset", "data-src", "data-original", "data-url"):
            preview.attrs.pop(attr, None)
        if copy_caption:
            node = soup.new_tag("figcaption")
            node.string = caption
            group.append(node)
        if group and " ".join(group.get_text(" ", strip=True).split()) == caption:
            group.name = "figure"
            for node in group.find_all(["div", "p", "figcaption"]):
                if not node.find("img") and " ".join(node.get_text(" ", strip=True).split()) == caption:
                    node.name = "figcaption"
                    break
        if id(target) not in removed:
            # An explicitly linked target may also contain unrelated prose.
            # Only remove a display copy consisting entirely of its caption.
            remainder = " ".join(target.get_text(" ", strip=True).split())
            if not caption or remainder.replace(caption, "").strip():
                continue
            anchor = soup.new_tag("span", id=fragment)
            preview.insert_before(anchor)
            target.decompose()
            removed.add(id(target))

    for img in soup.find_all("img"):
        # Preserve ordinary captioned div layouts before their classes vanish.
        if not img.find_parent("figure"):
            caption = ImageProcessor.find_caption(img)
            if caption:
                caption = " ".join(caption.split())
                for parent in img.parents:
                    if parent.name not in {"div", "section"}:
                        continue
                    if len(parent.find_all("img", limit=2)) != 1:
                        break
                    if " ".join(parent.get_text(" ", strip=True).split()) == caption:
                        parent.name = "figure"
                        for node in parent.find_all(["div", "p", "figcaption"]):
                            if not node.find("img") and " ".join(node.get_text(" ", strip=True).split()) == caption:
                                node.name = "figcaption"
                                break
                        break
        aliases = image_urls(img, base_url)
        if aliases:
            img["data-dala-image-urls"] = json.dumps(aliases)
        for attr in ("src", "data-src", "data-original", "data-url"):
            if img.get(attr):
                img[attr] = resolve_image_url(base_url, img[attr]) or img[attr]
    for node in soup.find_all(["img", "source"]):
        for attr in ("srcset", "data-srcset"):
            if node.get(attr):
                candidates = ImageProcessor.parse_srcset(node[attr])
                if candidates:
                    pattern = "|".join(re.escape(url) for url in sorted(set(candidates), key=len, reverse=True))
                    node[attr] = re.sub(pattern, lambda match: resolve_image_url(base_url, match[0]) or match[0], node[attr])


def validate_book_media(book, options=None) -> None:
    """Make incomplete offline media explicit without failing the article."""
    import posixpath
    from .image_processor import ImageProcessor
    from ..models import log

    if getattr(options, "no_images", False):
        return
    filenames = {asset.filename for asset in book.images if asset.content}
    warnings = {(item["source_url"], item["image_url"], item["reason"]): item for item in book.media_warnings}
    for chapter in book.chapters:
        soup = BeautifulSoup(chapter.content_html, "html.parser")
        changed = False
        for img in list(soup.find_all("img")):
            src = img.get("src") or ""
            try:
                scheme = urlparse(src).scheme
            except ValueError:
                scheme = "invalid"
            local = posixpath.normpath(posixpath.join(posixpath.dirname(chapter.filename), src))
            if src in filenames or local in filenames:
                continue
            asset = ImageProcessor._find_existing_asset(book.images, src)
            if asset and asset.content:
                img["src"] = asset.filename
                changed = True
                continue
            reason = "remote_image_unavailable" if scheme in {"http", "https"} else "missing_embedded_image"
            warning = {"source_url": book.source_url, "image_url": src, "reason": reason}
            warnings[(book.source_url, src, reason)] = warning
            placeholder = soup.new_tag("span", attrs={"class": "image-unavailable"})
            placeholder.string = "Image unavailable"
            if img.get("alt"):
                placeholder.append(": " + img["alt"])
            if scheme in {"http", "https"}:
                placeholder.append(" (")
                link = soup.new_tag("a", href=src)
                link.string = "image source"
                placeholder.append(link)
                placeholder.append(")")
            img.replace_with(placeholder)
            changed = True
        if changed:
            chapter.content_html = str(soup)
    book.media_warnings = list(warnings.values())
    if warnings:
        log.warning(f"Media incomplete for {book.source_url or book.title}: {len(warnings)} unavailable images")

import os
import io
import re
import asyncio
import aiohttp
import mimetypes
import hashlib
import json
import base64
from urllib.parse import urlparse, parse_qs, urljoin, unquote_to_bytes
from bs4 import BeautifulSoup, Tag, Comment
from typing import List, Dict, Optional, Any, Tuple
from tqdm.asyncio import tqdm_asyncio
from yarl import URL

try:
    from PIL import Image as PillowImage
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

from ..models import (
    log, IMAGE_DIR_IN_EPUB, MAX_IMAGE_DIMENSION, JPEG_QUALITY,
    REQUEST_TIMEOUT, IMG_MAX_RETRIES, IMG_RETRY_DELAY, IMAGE_TIMEOUT,
    IMG_MAX_CANDIDATES, IMG_MAX_PER_IMAGE_SEC,
    ImageAsset, SiteProfile, ConversionOptions, normalize_image_preset,
    normalize_url_for_matching, sanitize_filename
)
from .session import fetch_with_retry

IMAGE_CONCURRENCY = int(os.getenv("DALA_IMAGE_CONCURRENCY", "8"))
FORUM_IMAGE_CONCURRENCY = int(os.getenv("DALA_FORUM_IMAGE_CONCURRENCY", str(IMAGE_CONCURRENCY)))
FORUM_REQUESTS_TIMEOUT = float(os.getenv("DALA_FORUM_REQUESTS_TIMEOUT", "4"))

class BaseImageProcessor:
    GENERIC_ALT_TEXT = {
        "image",
        "photo",
        "picture",
        "pic",
        "screenshot",
        "screen shot",
        "graphic",
        "thumbnail",
        "logo",
        "avatar",
        "untitled",
        "img",
    }
    GENERIC_IMAGE_BASENAMES = {
        "default",
        "image",
        "img",
        "thumbnail",
        "thumb",
        "media",
        "asset",
        "download",
        "untitled",
        "unnamed",
    }

    @staticmethod
    def _short_stable_hash(value: str, length: int = 10) -> str:
        return hashlib.sha1((value or "").encode("utf-8", errors="ignore")).hexdigest()[:length]

    @staticmethod
    def _is_generic_image_filename(filename: str) -> bool:
        if not filename:
            return True
        base = os.path.splitext(os.path.basename(filename))[0]
        normalized = re.sub(r"[^a-z0-9]+", "", base.casefold())
        if not normalized or len(normalized) < 3:
            return True
        if normalized.isdigit():
            return True
        return normalized in BaseImageProcessor.GENERIC_IMAGE_BASENAMES

    @staticmethod
    def _image_filename_base(url_or_name: str, fallback_seed: str = "image") -> str:
        parsed_path = urlparse(url_or_name or "").path
        raw_name = os.path.splitext(os.path.basename(parsed_path or url_or_name or ""))[0]
        base = sanitize_filename(raw_name)
        if len(base) < 3:
            base = "image"
        if BaseImageProcessor._is_generic_image_filename(base):
            seed = fallback_seed or url_or_name or base
            return f"{base}_{BaseImageProcessor._short_stable_hash(seed)}"
        return base

    @staticmethod
    def _tag_factory(context: Tag):
        current = context
        while getattr(current, "parent", None) is not None:
            current = current.parent
        if isinstance(current, BeautifulSoup):
            return current
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def image_optimize_params(options: Optional[ConversionOptions] = None) -> Tuple[int, int, str, str]:
        preset = normalize_image_preset(getattr(options, "image_preset", None))
        color_mode = (getattr(options, "image_color", None) or "color").lower()
        output_format = "source"
        if preset == "compact":
            return 720, 50, color_mode, "webp"
        return MAX_IMAGE_DIMENSION, JPEG_QUALITY, color_mode, output_format

    @staticmethod
    async def _requests_fetch(session, target, img_headers, referer):
        try:
            import requests
            cookie_dict = {}
            try:
                jar = session.cookie_jar.filter_cookies(URL(target))
                cookie_dict = {k: v.value for k, v in jar.items()}
            except Exception:
                pass
            extra = getattr(session, "_extra_cookies", None)
            if isinstance(extra, dict):
                cookie_dict.update(extra)
            
            loop = asyncio.get_running_loop()
            def _do_req():
                return requests.get(target, headers={**img_headers, "Referer": referer or ""}, cookies=cookie_dict, timeout=20, allow_redirects=True)
            
            resp = await loop.run_in_executor(None, _do_req)
            if resp.content:
                return resp.headers, resp.content, resp.status_code
        except Exception as e:
            log.debug(f"Requests fetch failed for {target}: {e}")
        return None, None, None

    @staticmethod
    def optimize_and_get_details(
        url,
        headers,
        data,
        max_dimension: Optional[int] = None,
        jpeg_quality: Optional[int] = None,
        color_mode: str = "color",
        output_preference: str = "source",
    ):
        if not data:
            return None, None, None, "No Data"
        max_dimension = max_dimension or MAX_IMAGE_DIMENSION
        jpeg_quality = jpeg_quality or JPEG_QUALITY
        content_type = headers.get('Content-Type', '').split(';')[0].strip().lower()

        if 'svg' in content_type or (url and url.split('?')[0].lower().endswith('.svg')):
            return 'image/svg+xml', '.svg', data, None

        if len(data) < 12 * 1024:
            if not content_type:
                content_type = mimetypes.guess_type(url)[0] or 'application/octet-stream'
            ext = mimetypes.guess_extension(content_type) or '.img'
            return content_type, ext, data, None
        if not HAS_PILLOW:
            ext = mimetypes.guess_extension(content_type) or '.img'
            return content_type, ext, data, None

        try:
            img_io = io.BytesIO(data)
            with PillowImage.open(img_io) as img:
                img.load()
                if img.width < 20 or img.height < 20:
                    return None, None, None, "Tracking Pixel"

                if img.width > max_dimension or img.height > max_dimension:
                    reduce_factor = max(1, int(max(img.width, img.height) / (max_dimension * 2)))
                    if reduce_factor > 1:
                        img = img.reduce(reduce_factor)
                    img.thumbnail((max_dimension, max_dimension), PillowImage.Resampling.LANCZOS)

                if img.format == 'GIF' and getattr(img, "is_animated", False):
                    out_io = io.BytesIO()
                    img.save(out_io, format='GIF', optimize=True)
                    return 'image/gif', '.gif', out_io.getvalue(), None

                if img.format == 'PNG' and len(data) < 200 * 1024:
                    out_io = io.BytesIO()
                    img.save(out_io, format='PNG', optimize=True)
                    return 'image/png', '.png', out_io.getvalue(), None

                output_format = 'JPEG'
                output_mime = 'image/jpeg'
                output_ext = '.jpg'

                if img.format == 'WEBP':
                    output_format = 'WEBP'
                    output_mime = 'image/webp'
                    output_ext = '.webp'

                if output_preference == "webp" and img.format != "GIF":
                    output_format = 'WEBP'
                    output_mime = 'image/webp'
                    output_ext = '.webp'

                if output_format in ('JPEG', 'WEBP'):
                    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                        background = PillowImage.new("RGB", img.size, (255, 255, 255))
                        if img.mode == 'P':
                            img = img.convert('RGBA')
                        background.paste(img, mask=img.split()[3])
                        img = background
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')
                    if color_mode == "grayscale":
                        img = img.convert('L')

                out_io = io.BytesIO()
                save_params = {"optimize": True}
                if output_format == 'JPEG':
                    save_params["quality"] = jpeg_quality
                    save_params["subsampling"] = "4:2:0"
                if output_format == 'WEBP':
                    save_params["quality"] = jpeg_quality
                    save_params["method"] = 6
                img.save(out_io, format=output_format, **save_params)

                return output_mime, output_ext, out_io.getvalue(), None

        except Exception as e:
            return None, None, None, f"Optimization Error: {e}"

    @staticmethod
    def _clean_text(value: Optional[str]) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @classmethod
    def _normalized_text(cls, value: Optional[str]) -> str:
        return cls._clean_text(value).casefold()

    @classmethod
    def _is_low_value_alt(cls, value: Optional[str]) -> bool:
        text = cls._clean_text(value)
        if not text:
            return True
        lowered = text.casefold()
        if lowered in cls.GENERIC_ALT_TEXT:
            return True
        if lowered.startswith(("http://", "https://", "www.")):
            return True
        parsed = urlparse(text)
        if parsed.scheme or parsed.netloc:
            return True

        basename = os.path.basename(text.split("?", 1)[0].split("#", 1)[0])
        stem, ext = os.path.splitext(basename)
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".bmp", ".tif", ".tiff"}
        if ext.casefold() in image_exts:
            return True
        if re.fullmatch(r"(img|image|photo|picture|screenshot|wp|dsc|dscn|pxl|imgp)[-_ ]?\d+.*", lowered):
            return True
        if re.fullmatch(r"[a-f0-9]{12,}", lowered):
            return True
        if re.fullmatch(r"[\w.-]+\.(jpg|jpeg|png|gif|webp|svg|avif|bmp|tiff?)", lowered):
            return True
        return False

    @classmethod
    def _caption_from_container(cls, container: Tag) -> Optional[str]:
        map_caption = None
        credit_caption = None
        for found in container.find_all(["p", "div"]):
            if found.find("img"):
                continue
            text = cls._clean_text(found.get_text(" ", strip=True))
            text_cf = text.casefold()
            if 20 <= len(text) <= 1000:
                if text_cf.startswith("the map shows") or text_cf.startswith("the mapped range"):
                    map_caption = text
                elif "(graphic)" in text_cf or "(data)" in text_cf:
                    credit_caption = text

        for figcaption in container.find_all("figcaption"):
            if cls._is_upper_caption(figcaption):
                continue
            text = cls._clean_text(figcaption.get_text(" ", strip=True))
            text_cf = text.casefold()
            if text:
                if text_cf.startswith("the map shows") or text_cf.startswith("the mapped range"):
                    map_caption = text
                elif "(graphic)" in text_cf or "(data)" in text_cf:
                    credit_caption = text
                elif not map_caption and not credit_caption:
                    return text

        if map_caption and credit_caption:
            return f"{map_caption} {credit_caption}"
        if map_caption:
            return map_caption
        if credit_caption:
            return credit_caption

        for selector in [
            ".caption",
            ".wp-caption-text",
            ".gallery-caption",
            ".image-caption",
            ".news-article__figure__caption",
            "[data-image-caption]",
        ]:
            found = container.select_one(selector)
            if found:
                if cls._is_upper_caption(found):
                    continue
                if selector == "[data-image-caption]" and found.get("data-image-caption"):
                    text = cls._clean_text(BeautifulSoup(found["data-image-caption"], "html.parser").get_text(" ", strip=True))
                else:
                    text = cls._clean_text(found.get_text(" ", strip=True))
                if text:
                    return text

        for found in container.find_all(["p", "div"]):
            if found.find("img"):
                continue
            text = cls._clean_text(found.get_text(" ", strip=True))
            if 20 <= len(text) <= 1000 and (
                "(graphic)" in text.casefold()
                or "(data)" in text.casefold()
                or text.casefold().startswith("the map shows")
                or text.casefold().startswith("the mapped range")
            ):
                return text
        return None

    @staticmethod
    def _is_upper_caption(tag: Tag) -> bool:
        if tag.get("data-dala-upper-caption") == "1":
            return True
        classes = " ".join(tag.get("class") or []).casefold()
        return "upper-caption" in classes

    @classmethod
    def _caption_near_image(cls, img_tag: Tag) -> Optional[Tag]:
        for sibling in [img_tag.find_next_sibling(), img_tag.find_previous_sibling()]:
            if not isinstance(sibling, Tag):
                continue
            classes = set(sibling.get("class") or [])
            if sibling.name == "figcaption" or classes.intersection({"caption", "wp-caption-text", "gallery-caption"}):
                text = cls._clean_text(sibling.get_text(" ", strip=True))
                if text:
                    return sibling
        return None

    @classmethod
    def _adjacent_graphic_caption_after(cls, node: Tag) -> Tuple[Optional[str], List[Tag]]:
        parts: List[str] = []
        removable: List[Tag] = []
        sibling = node.next_sibling
        checked = 0
        while sibling is not None and checked < 8:
            current = sibling
            sibling = sibling.next_sibling
            if isinstance(current, str):
                if current.strip():
                    checked += 1
                continue
            if not isinstance(current, Tag):
                continue
            checked += 1
            if current.find("img"):
                break
            text = cls._clean_text(current.get_text(" ", strip=True))
            if not text:
                continue
            text_cf = text.casefold()
            is_graphic_caption = (
                text_cf.startswith("the map shows")
                or text_cf.startswith("the mapped range")
                or "(graphic)" in text_cf
                or "(data)" in text_cf
            )
            if is_graphic_caption:
                parts.append(text)
                removable.append(current)
                continue
            if parts:
                break
            if current.name in {"h1", "h2", "h3", "h4", "p"}:
                break
        if not parts:
            return None, []
        return cls._clean_text(" ".join(parts)), removable

    @classmethod
    def _adjacent_graphic_caption_near(cls, node: Tag) -> Tuple[Optional[str], List[Tag]]:
        current = node
        for _ in range(4):
            if not isinstance(current, Tag):
                break
            caption, removable = cls._adjacent_graphic_caption_after(current)
            if caption:
                return caption, removable
            current = current.parent
        return None, []

    @classmethod
    def _repair_adjacent_graphic_captions(cls, soup: BeautifulSoup) -> None:
        for wrapper in list(soup.select("div.img-block, figure")):
            img = wrapper.find("img")
            if not img:
                continue
            src = str(img.get("src") or "").casefold()
            alt = str(img.get("alt") or "").casefold()
            if not (src.endswith(".svg") or "locator" in src or alt == "locator"):
                continue
            caption_tag = wrapper.find(["p", "figcaption"], class_=lambda c: c and ("caption" in c if isinstance(c, str) else "caption" in " ".join(c)))
            current_caption = cls._clean_text(caption_tag.get_text(" ", strip=True)) if caption_tag else ""
            if current_caption.casefold().startswith(("the map shows", "the mapped range")):
                continue
            caption, removable = cls._adjacent_graphic_caption_near(wrapper)
            if not caption:
                continue
            if caption_tag:
                caption_tag.string = caption
                caption_tag["class"] = ["caption"]
            else:
                tag_factory = cls._tag_factory(soup)
                caption_tag = tag_factory.new_tag("p", attrs={"class": "caption"})
                caption_tag.string = caption
                wrapper.append(caption_tag)
            for candidate in removable:
                candidate.decompose()

    @staticmethod
    def _decode_data_image(uri: str) -> Tuple[Optional[str], Optional[bytes]]:
        if not uri or not uri.startswith("data:image/"):
            return None, None
        header, sep, payload = uri.partition(",")
        if not sep:
            return None, None
        mime = header[5:].split(";", 1)[0].strip().lower() or "image/png"
        try:
            if ";base64" in header.lower():
                data = base64.b64decode(payload, validate=False)
            else:
                data = unquote_to_bytes(payload)
        except Exception:
            return None, None
        return mime, data

    @classmethod
    def _replacement_nodes_for_image(cls, soup: BeautifulSoup, caption: Optional[str], alt_values: List[str]) -> List[Tag]:
        nodes: List[Tag] = []
        caption_text = cls._clean_text(caption)
        if caption_text:
            cap = soup.new_tag("p", attrs={"class": "image-caption"})
            cap.string = caption_text
            nodes.append(cap)

        caption_norm = cls._normalized_text(caption_text)
        useful_alts = []
        seen = set()
        for alt in alt_values:
            alt_text = cls._clean_text(alt)
            if cls._is_low_value_alt(alt_text):
                continue
            alt_norm = cls._normalized_text(alt_text)
            if alt_norm in seen:
                continue
            seen.add(alt_norm)
            if caption_norm and (alt_norm == caption_norm or alt_norm in caption_norm or caption_norm in alt_norm):
                continue
            useful_alts.append(alt_text)

        for alt_text in useful_alts:
            alt_node = soup.new_tag("p", attrs={"class": "image-alt"})
            alt_node.string = f"[Image: {alt_text}]"
            nodes.append(alt_node)
        return nodes

    @staticmethod
    def _replace_tag_with_nodes(tag: Tag, nodes: List[Tag]) -> None:
        for node in nodes:
            tag.insert_before(node)
        tag.decompose()

    @classmethod
    def _remove_empty_image_wrappers(cls, soup: BeautifulSoup) -> None:
        changed = True
        while changed:
            changed = False
            for tag in list(soup.find_all(["figure", "picture", "div", "span", "a"])):
                if tag.find(["img", "picture", "source"]):
                    continue
                text = cls._clean_text(tag.get_text(" ", strip=True))
                if text:
                    continue
                classes = set(tag.get("class") or [])
                dataid = (tag.get("data-testid") or "").lower()
                imageish = (
                    tag.name in {"figure", "picture"}
                    or classes.intersection({"img-block", "wp-caption", "caption", "gallery-caption"})
                    or dataid.startswith(("imageblock", "photoviewer"))
                )
                if imageish or not tag.attrs:
                    tag.decompose()
                    changed = True

    @classmethod
    def remove_images_for_text_output(cls, soup: BeautifulSoup) -> None:
        """Remove image elements while preserving meaningful caption/alt text."""
        if not soup:
            return

        for figure in list(soup.find_all("figure")):
            imgs = figure.find_all("img")
            if not imgs:
                continue
            caption = cls._caption_from_container(figure)
            alt_values = [img.get("alt") or img.get("title") or "" for img in imgs]
            nodes = cls._replacement_nodes_for_image(soup, caption, alt_values)
            cls._replace_tag_with_nodes(figure, nodes)

        for picture in list(soup.find_all("picture")):
            img = picture.find("img")
            caption_tag = cls._caption_near_image(picture) if isinstance(picture, Tag) else None
            caption = cls._clean_text(caption_tag.get_text(" ", strip=True)) if caption_tag else None
            alt_values = [img.get("alt") or img.get("title") or ""] if img else []
            nodes = cls._replacement_nodes_for_image(soup, caption, alt_values)
            if caption_tag:
                caption_tag.decompose()
            cls._replace_tag_with_nodes(picture, nodes)

        for img in list(soup.find_all("img")):
            caption_tag = cls._caption_near_image(img)
            caption = cls._clean_text(caption_tag.get_text(" ", strip=True)) if caption_tag else None
            alt_values = [img.get("alt") or img.get("title") or ""]
            nodes = cls._replacement_nodes_for_image(soup, caption, alt_values)
            if caption_tag:
                caption_tag.decompose()
            cls._replace_tag_with_nodes(img, nodes)

        for tag in list(soup.find_all(["source", "figcaption"])):
            tag.decompose()
        cls._remove_empty_image_wrappers(soup)

    @staticmethod
    def find_caption(element_tag):
        if not element_tag:
            return None
        fig = element_tag.find_parent('figure')
        if fig:
            caption = BaseImageProcessor._caption_from_container(fig)
            if caption:
                return caption

        current = element_tag
        for _ in range(4):
            if not isinstance(current, Tag):
                break
            classes = set(current.get("class") or [])
            if current is not element_tag and (
                current.name in {"figure", "div"}
                or classes.intersection({"wp-caption", "img-block", "gallery-item"})
            ):
                caption = BaseImageProcessor._caption_from_container(current)
                if caption:
                    return caption
            for sibling in [current.find_next_sibling(), current.find_previous_sibling()]:
                if not isinstance(sibling, Tag):
                    continue
                sibling_classes = set(sibling.get("class") or [])
                if sibling.name == "figcaption" or sibling_classes.intersection({"caption", "wp-caption-text", "gallery-caption", "image-caption"}):
                    text = BaseImageProcessor._clean_text(sibling.get_text(" ", strip=True))
                    if 5 < len(text) < 500:
                        return text
            current = current.parent
        return None

    @staticmethod
    def wrap_in_img_block(soup: BeautifulSoup, img_tag: Tag, caption_text: Optional[str]) -> None:
        if not img_tag or not soup:
            return
        fig = img_tag.find_parent("figure")
        if fig:
            if not caption_text:
                caption_text = BaseImageProcessor._caption_from_container(fig)
            if caption_text:
                caption_norm = BaseImageProcessor._normalized_text(caption_text)
                for candidate in list(fig.find_all(["figcaption", "p", "div"])):
                    if candidate.find("img"):
                        continue
                    if BaseImageProcessor._is_upper_caption(candidate):
                        continue
                    text = BaseImageProcessor._clean_text(candidate.get_text(" ", strip=True))
                    if len(text) < 20:
                        continue
                    candidate_norm = BaseImageProcessor._normalized_text(text)
                    if candidate_norm and (candidate_norm == caption_norm or candidate_norm in caption_norm):
                        candidate.decompose()
            fig.unwrap()

        tag_factory = BaseImageProcessor._tag_factory(soup)
        wrapper = tag_factory.new_tag("div", attrs={"class": "img-block"})
        parent = img_tag.parent
        if parent:
            img_tag.replace_with(wrapper)
        else:
            (soup.body or soup).append(wrapper)
        wrapper.append(img_tag)
        adjacent_caption, adjacent_nodes = BaseImageProcessor._adjacent_graphic_caption_near(wrapper)
        img_src = str(img_tag.get("src") or "").casefold()
        caption_cf = BaseImageProcessor._clean_text(caption_text).casefold()
        should_prefer_adjacent = bool(
            adjacent_caption
            and (
                not caption_text
                or img_src.endswith(".svg")
                or "locator" in img_src
                or caption_cf.startswith("a species divided")
            )
        )
        if should_prefer_adjacent:
            caption_text = adjacent_caption
            for candidate in adjacent_nodes:
                candidate.decompose()
        if caption_text:
            cap = tag_factory.new_tag("p", attrs={"class": "caption"})
            cap.string = caption_text
            wrapper.append(cap)

        parent = wrapper.parent
        while parent and parent.name in ("div", "section"):
            for fc in list(parent.find_all("figcaption", recursive=False)):
                if not BaseImageProcessor._is_upper_caption(fc):
                    fc.decompose()
            meaningful = [c for c in parent.contents if not (isinstance(c, str) and not c.strip())]
            dataid = (parent.get("data-testid") or "").lower()
            if len(meaningful) == 1 and meaningful[0] is wrapper and (dataid.startswith("imageblock") or dataid.startswith("photoviewer")):
                parent.unwrap()
                parent = wrapper.parent
                continue
            break

        sib = wrapper.next_sibling
        while sib and isinstance(sib, str) and not sib.strip():
            sib = sib.next_sibling
        if hasattr(sib, "name") and sib.name == "figcaption":
            if not BaseImageProcessor._is_upper_caption(sib):
                sib.decompose()

        parent = wrapper.parent
        if parent:
            for fc in list(parent.find_all("figcaption", recursive=False)):
                if not BaseImageProcessor._is_upper_caption(fc):
                    fc.decompose()

    @staticmethod
    def is_junk(url: str) -> bool:
        """Determines if an image URL is a known placeholder or tracking pixel."""
        if not url:
            return True
        if url.startswith("data:"):
            return True

        bad_keywords = [
            "spacer", "1x1", "transparent", "gray.gif", "pixel.gif",
            "placeholder", "loader", "blank.gif", "grey-placeholder", "gray-placeholder",
            "arc-authors", "author-bio", "avatar"
        ]
        lower_url = url.lower()
        if any(k in lower_url for k in bad_keywords):
            return True
        return False

    @staticmethod
    def is_blur_placeholder(url: str) -> bool:
        """Return true for low-quality blur preview URLs that should lose to lazy/full sources."""
        if not url:
            return False
        lower_url = url.lower()
        return (
            "data-blursrc" in lower_url
            or "blur=" in lower_url
            or "/blur/" in lower_url
            or "blurhash" in lower_url
        )

    @staticmethod
    def parse_srcset(srcset_str: str) -> list:
        if not srcset_str:
            return []
        candidates = []
        parsed = BaseImageProcessor.parse_srcset_with_width(srcset_str)
        if parsed:
            return [url for _, url in parsed]

        for p in srcset_str.split(','):
            url = p.strip().split(' ', 1)[0]
            if url:
                candidates.append(url)
        return candidates

    @staticmethod
    def parse_srcset_with_width(srcset_str: str) -> list:
        if not srcset_str:
            return []
        pairs = []
        # Split at candidate descriptors instead of raw commas. Some real image
        # URLs, notably CBC AIS URLs, contain commas in the path.
        for match in re.finditer(
            r"(?P<url>\S+)\s+(?P<descriptor>(?P<width>\d+)(?:w|x))(?=\s*,|\s*$)",
            srcset_str.strip(),
        ):
            url = match.group("url").strip().lstrip(",")
            width = 0
            try:
                width = int(match.group("width") or 0)
            except Exception:
                width = 0
            pairs.append((width, url))

        if not pairs:
            for part in srcset_str.split(","):
                part = part.strip()
                if not part:
                    continue
                url_part, *rest = part.split()
                width_val = 0
                if rest and rest[0].endswith("w"):
                    try:
                        width_val = int(rest[0][:-1])
                    except Exception:
                        width_val = 0
                pairs.append((width_val, url_part))
        pairs.sort(key=lambda x: x[0], reverse=True)
        return pairs

class ImageProcessor(BaseImageProcessor):
    PROCESSED_IMAGE_ATTRS = [
        'srcset', 'data-src', 'data-srcset', 'data-blursrc', 'data-lazy',
        'loading', 'decoding', 'style', 'class', 'width', 'height'
    ]

    @staticmethod
    def _strip_processed_image_attrs(img_tag: Tag) -> None:
        for attr in list(img_tag.attrs.keys()):
            if img_tag.has_attr(attr):
                if attr in ImageProcessor.PROCESSED_IMAGE_ATTRS or attr.startswith("data-"):
                    del img_tag[attr]

    @staticmethod
    def _find_existing_asset(book_assets: list, url: str) -> Optional[ImageAsset]:
        if not url:
            return None
        normalized = normalize_url_for_matching(url)
        target_name = os.path.basename(urlparse(url).path or "")
        allow_basename_match = bool(
            target_name
            and len(target_name) > 8
            and not ImageProcessor._is_generic_image_filename(target_name)
        )
        for asset in book_assets:
            asset_urls = [asset.original_url] + list(asset.alt_urls or [])
            for asset_url in asset_urls:
                if not asset_url:
                    continue
                if asset_url == url or normalize_url_for_matching(asset_url) == normalized:
                    return asset
                asset_name = os.path.basename(urlparse(asset_url).path or "")
                if allow_basename_match and asset_name == target_name and not ImageProcessor._is_generic_image_filename(asset_name):
                    return asset
        return None

    @staticmethod
    def _sequence_year(img: Tag) -> int:
        alt_title = " ".join(str(value or "") for value in [img.get("alt"), img.get("title")])
        preferred = re.search(r"monitoring\s+year\s+((?:19|20)\d{2})", alt_title, re.IGNORECASE)
        if preferred:
            return int(preferred.group(1))

        src_text = " ".join(str(value or "") for value in [img.get("src"), img.get("data-src")])
        preferred = re.search(r"timelapse[_-]((?:19|20)\d{2})", src_text, re.IGNORECASE)
        if preferred:
            return int(preferred.group(1))

        text = " ".join([src_text, alt_title])
        matches = re.findall(r"(?:19|20)\d{2}", text)
        return max((int(match) for match in matches), default=0)

    @staticmethod
    def _is_sequence_image(img: Tag) -> bool:
        text = " ".join(
            str(value or "")
            for value in [
                img.get("src"),
                img.get("data-src"),
                img.get("alt"),
                img.get("title"),
            ]
        ).casefold()
        return (
            "timelapse" in text
            or "monitoring year" in text
            or bool(ImageProcessor._sequence_year(img) and ("map" in text or "range" in text))
        )

    @staticmethod
    def _looks_like_interactive_sequence(container: Tag, imgs: list[Tag]) -> bool:
        if len(imgs) < 4:
            return False
        container_hint = " ".join(
            [str(container.get("id") or ""), " ".join(container.get("class") or [])]
        ).casefold()
        sequence_imgs = [img for img in imgs if ImageProcessor._is_sequence_image(img)]
        if "timelapse" in container_hint and len(sequence_imgs) >= 4:
            return True
        years = [ImageProcessor._sequence_year(img) for img in sequence_imgs]
        if len(sequence_imgs) < max(4, int(len(imgs) * 0.6)):
            return False
        alts = " ".join(str(img.get("alt") or "") for img in sequence_imgs).casefold()
        return sum(1 for year in years if year) >= 4 and len(set(years)) >= 4 and (
            "monitoring year" in alts or "range" in alts or "map" in alts
        )

    @staticmethod
    def _simplify_interactive_image_sequences(soup: BeautifulSoup) -> None:
        """Collapse scripted image sequences to one useful static frame for e-readers."""
        if not soup:
            return
        candidates = []
        for container in soup.find_all(["div", "figure"]):
            imgs = container.find_all("img")
            if ImageProcessor._looks_like_interactive_sequence(container, imgs):
                candidates.append((container, imgs))

        handled = set()
        for container, imgs in candidates:
            ident = id(container)
            if ident in handled:
                continue
            handled.add(ident)
            imgs = [img for img in imgs if getattr(img, "attrs", None) is not None]
            if not ImageProcessor._looks_like_interactive_sequence(container, imgs):
                continue
            sequence_imgs = [img for img in imgs if ImageProcessor._is_sequence_image(img)]

            active = [img for img in sequence_imgs if "active" in (img.get("class") or [])]
            chosen = None
            if active:
                chosen = max(active, key=ImageProcessor._sequence_year)
            if not chosen:
                chosen = max(sequence_imgs, key=ImageProcessor._sequence_year)

            for img in list(sequence_imgs):
                if img is not chosen:
                    img.decompose()

            parent_figure = container.find_parent("figure")
            cleanup_root = parent_figure or container
            for selector in [
                ".timelapse_controls",
                ".timelapse_slider_wrapper",
                "#timelapse_tickmarks",
                "#timelapse_label",
                "#timelapse_play",
                "input[type='range']",
                "button",
            ]:
                for tag in list(cleanup_root.select(selector)):
                    tag.decompose()

            year = ImageProcessor._sequence_year(chosen)
            if year and chosen.get("alt") and str(year) not in chosen["alt"]:
                chosen["alt"] = f"{chosen['alt']} ({year})"

    @staticmethod
    def retain_referenced_assets(soup: BeautifulSoup, book_assets: list) -> int:
        """Drop preloaded assets that never ended up referenced by article HTML."""
        if not soup or not book_assets:
            return 0
        referenced = {
            (img.get("src") or "").strip()
            for img in soup.find_all("img")
            if (img.get("src") or "").strip()
        }
        if not referenced:
            return 0
        before = len(book_assets)
        book_assets[:] = [asset for asset in book_assets if asset.filename in referenced]
        return before - len(book_assets)

    @staticmethod
    def attach_contextual_preloaded_assets(soup: BeautifulSoup, book_assets: list) -> int:
        if not soup or not book_assets:
            return 0
        referenced = {
            (img.get("src") or "").strip()
            for img in soup.find_all("img")
            if (img.get("src") or "").strip()
        }
        body_text = BaseImageProcessor._clean_text(soup.get_text(" ", strip=True)).casefold()
        if "mapped range" not in body_text and "map shows" not in body_text:
            return 0

        locator_targets = []
        caption_targets = []
        for wrapper in soup.select("div.img-block, figure"):
            text = BaseImageProcessor._clean_text(wrapper.get_text(" ", strip=True)).casefold()
            img_srcs = " ".join(str(img.get("src") or "") for img in wrapper.find_all("img")).casefold()
            if "locator" in img_srcs or "mapped range" in text:
                locator_targets.append(wrapper)
            elif "map shows" in text:
                caption_targets.append(wrapper)
        targets = locator_targets or caption_targets
        if not targets:
            return 0

        target = targets[0]
        tag_factory = BaseImageProcessor._tag_factory(soup)
        added = 0
        for asset in book_assets:
            if asset.filename in referenced:
                continue
            url_bits = " ".join(filter(None, [
                asset.filename,
                getattr(asset, "original_url", "") or "",
                " ".join(getattr(asset, "alt_urls", None) or []),
            ])).casefold()
            is_context_map = (
                asset.media_type == "image/svg+xml"
                and "map" in url_bits
                and "timelapse" not in url_bits
                and "locator" not in url_bits
                and "logo" not in url_bits
                and "sprite" not in url_bits
            )
            if not is_context_map:
                continue
            img = tag_factory.new_tag("img", attrs={
                "src": asset.filename,
                "class": "epub-image",
                "alt": "Map graphic",
            })
            caption = target.find(
                ["p", "figcaption"],
                class_=lambda c: c and ("caption" in c if isinstance(c, str) else "caption" in " ".join(c)),
            )
            if caption:
                caption.insert_before(img)
            else:
                target.append(img)
            referenced.add(asset.filename)
            added += 1
        return added

    @staticmethod
    async def fetch_image_data(session, url, referer=None):
        if url:
            url = url.strip()
        parsed = urlparse(url)
        # Wikimedia: single-shot with file-page referer (matches curl success)
        if parsed.netloc and "upload.wikimedia.org" in parsed.netloc:
            fname = os.path.basename(parsed.path)
            commons_ref = f"https://commons.wikimedia.org/wiki/File:{fname}" if fname else "https://commons.wikimedia.org/wiki/"
            headers = {
                "User-Agent": "PersonalEpubMaker/1.0 (reading project; contact: epub.research@proton.me)",
                "Referer": commons_ref,
                "Accept": "*/*",
            }
            targets = [url]
            if fname:
                targets.append(f"{url}?download=1")
            for tgt in targets:
                try:
                    log.debug(f"Wikimedia fetch attempt tgt={tgt} referer={headers.get('Referer')}")
                    async with session.get(tgt, headers=headers, allow_redirects=True, timeout=REQUEST_TIMEOUT) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            return resp.headers, data, None
                        else:
                            log.debug(f"Wikimedia fetch status {resp.status} for {tgt}")
                except Exception as e:
                    log.debug(f"Wikimedia fetch error for {tgt}: {e}")
                    continue
            log.warning(f"Wikimedia blocked for {url} (targets tried={targets})")
            return None, None, "Wikimedia blocked"

        # Default path: try with provided referer, origin, then none
        image_headers = {
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        refs = []
        if referer: refs.append(referer)
        try:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in refs:
                refs.append(origin)
        except Exception:
            pass
        refs.append(None)

        last_err = "No data"
        aiohttp_fail_reason = None
        headers = None
        data = None
        for attempt_idx, ref in enumerate(refs):
            try:
                headers = {**image_headers}
                if ref:
                    headers["Referer"] = ref
                async with session.get(url, headers=headers, allow_redirects=True, timeout=IMAGE_TIMEOUT) as resp:
                    if resp.status in {400, 401, 403, 404, 451}:
                        last_err = f"HTTP {resp.status}"
                        if attempt_idx == 0:
                            aiohttp_fail_reason = f"HTTP {resp.status} on first aiohttp attempt"
                            break
                        continue
                    if resp.status >= 400:
                        last_err = f"HTTP {resp.status}"
                        if attempt_idx == 0:
                            aiohttp_fail_reason = f"HTTP {resp.status} on first aiohttp attempt"
                            break
                        continue
                    data = await resp.read()
                    if data:
                        return resp.headers, data, None
                    last_err = "No data"
                    if attempt_idx == 0:
                        aiohttp_fail_reason = "No data on first aiohttp attempt"
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                if attempt_idx == 0: 
                    aiohttp_fail_reason = "ClientError or Timeout on first aiohttp attempt"
                    break 
                continue
            except Exception as e:
                last_err = str(e)
                if attempt_idx == 0:
                    aiohttp_fail_reason = "Unexpected error on first aiohttp attempt"
                    break
                continue

        if aiohttp_fail_reason:
            log.warning(f"aiohttp failed fast for {url} ({aiohttp_fail_reason}), trying requests fallback...")
        elif not data:
            log.warning(f"aiohttp exhausted all referers for {url}, trying requests fallback...")

        h_req, d_req, status_req = await ImageProcessor._requests_fetch(session, url, image_headers, referer)
        if d_req and (not status_req or status_req < 400):
            return h_req or {}, d_req, None

        return None, None, last_err

    @staticmethod
    def _cleanup_generic_wrapper(img_tag: Tag, caption_text: Optional[str]) -> None:
        """Flatten layout wrappers and dedupe captions for generic images."""
        if not img_tag:
            return
        wrapper = img_tag.parent
        if not wrapper or wrapper.name != "div" or "img-block" not in (wrapper.get("class") or []):
            return

        cap_p = wrapper.find("p", class_="caption")
        cap_text = cap_p.get_text(strip=True) if cap_p else caption_text

        fig = wrapper.find_parent("figure")
        if fig:
            if cap_text is None:
                figcap = fig.find("figcaption")
                if figcap:
                    cap_text = figcap.get_text(strip=True)
            for fc in fig.find_all("figcaption"):
                fc.decompose()
            fig.unwrap()
            if cap_text:
                if not cap_p:
                    cap_p = wrapper.new_tag("p", attrs={"class": "caption"})
                    cap_p.string = cap_text
                    wrapper.append(cap_p)
                else:
                    cap_p.string = cap_text

        parent = wrapper.parent
        if parent and cap_text:
            for sib in list(parent.find_all(['span', 'p'], recursive=False)):
                if sib is wrapper:
                    continue
                txt = sib.get_text(strip=True)
                if txt == cap_text:
                    sib.decompose()

        current = wrapper
        parent = current.parent
        while parent and parent.name == "div":
            meaningful_children = [c for c in parent.contents if not (isinstance(c, str) and c.strip() == "")]
            tag_children = [c for c in meaningful_children if isinstance(c, Tag)]
            attrs_ok = not parent.attrs or all(k.startswith("data-") for k in parent.attrs.keys())
            if len(tag_children) == 1 and tag_children[0] is current and attrs_ok:
                parent.unwrap()
                parent = current.parent
            else:
                break

    @staticmethod
    def _extract_origin_from_proxy(url: str, profile: Optional[SiteProfile] = None) -> Optional[str]:
        """Extracts the original source URL from common image proxy patterns."""
        try:
            parsed = urlparse(url)
            qs = dict((k, v[0]) for k, v in parse_qs(parsed.query).items() if v)
            
            if profile and profile.image_proxy_pattern and profile.image_proxy_pattern in (parsed.path or ""):
                return qs.get("src") or qs.get("url") or qs.get("original")

            if "imrs.php" in (parsed.path or "") or "resizer" in (parsed.path or parsed.netloc) or "proxy" in (parsed.path or parsed.netloc):
                return qs.get("src") or qs.get("url") or qs.get("original")
            if parsed.netloc and qs.keys() & {"w", "q", "fit", "h", "fm"}:
                if re.search(r'\.(jpe?g|png|webp|gif|svg)$', parsed.path, re.IGNORECASE):
                    return parsed._replace(query=None).geturl()
        except Exception:
            return None
        return None

    @staticmethod
    async def _seed_images_from_nextjs_data(raw_html: str, body_soup: BeautifulSoup, base_url: str, book_assets: list, session, profile: Optional[SiteProfile] = None, options: Optional[ConversionOptions] = None) -> None:
        """Parses __NEXT_DATA__ for image URLs and injects them into the article body or appends them."""
        try:
            if not raw_html:
                return
            full_soup = BeautifulSoup(raw_html, 'html.parser')
            script = full_soup.find("script", id="__NEXT_DATA__")
            if not script or not script.string:
                return
            data = json.loads(script.string)
            
            props = data.get("props", {}).get("pageProps", {})
            elems = props.get("globalContent", {}).get("content_elements", [])

            if not elems:
                candidates = []
                def _find_content_lists(node):
                    if isinstance(node, dict):
                        for k, v in node.items():
                            if k == "content_elements" and isinstance(v, list) and len(v) > 0:
                                candidates.append(v)
                            else:
                                _find_content_lists(v)
                    elif isinstance(node, list):
                        for item in node:
                            _find_content_lists(item)
                
                _find_content_lists(props)
                if candidates:
                    candidates.sort(key=len, reverse=True)
                    elems = candidates[0]

            if not elems:
                log.debug("No content_elements found in __NEXT_DATA__")
                return

            log.debug(f"Targeted __NEXT_DATA__ content elements: {len(elems)}")
            added = 0
            images_processed_count = 0
            lede_candidate = None
            tag_factory = ImageProcessor._tag_factory(body_soup)

            for el in elems:
                if not isinstance(el, dict):
                    continue
                if el.get("type") != "image":
                    continue
                
                is_first_image = (images_processed_count == 0)
                images_processed_count += 1

                url = el.get("url")
                if not url:
                    continue
                caption = el.get("credits_caption_display") or el.get("caption") or el.get("caption_display") or ""
                cap_text = caption.strip() if caption else None
                
                origin = el.get("url")
                if not origin: continue

                extracted_origin = ImageProcessor._extract_origin_from_proxy(origin, profile=profile) or origin

                headers, data_bytes, err = await ImageProcessor.fetch_image_data(session, extracted_origin, referer=base_url)
                if err or not headers or not data_bytes:
                    log.debug(f"Next.js image fetch failed for {extracted_origin}: {err}")
                    continue
                max_dim, quality, color_mode, output_pref = ImageProcessor.image_optimize_params(options)
                mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(origin, headers, data_bytes, max_dimension=max_dim, jpeg_quality=quality, color_mode=color_mode, output_preference=output_pref)
                if val_err or not final_data:
                    log.debug(f"WaPo __NEXT_DATA__ validate failed for {origin}: {val_err}")
                    continue
                fname_base = ImageProcessor._image_filename_base(origin, fallback_seed=origin)
                count = 0
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                while any(a.filename == fname for a in book_assets):
                    count += 1
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"
                uid = f"img_{ImageProcessor._short_stable_hash(fname)}"
                asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=origin, alt_urls=[origin])
                book_assets.append(asset)
                
                target_tag = body_soup.find(id=el.get("_id"))
                if not target_tag:
                    target_tag = body_soup.find(attrs={"data-id": el.get("_id")})
                if not target_tag:
                    target_tag = body_soup.find(attrs={"data-uuid": el.get("_id")})
                
                if target_tag:
                    img_block_wrapper = tag_factory.new_tag("div", attrs={"class": "img-block"})
                    img_tag = tag_factory.new_tag("img", attrs={"src": fname, "class": "epub-image"})
                    img_block_wrapper.append(img_tag)
                    if cap_text:
                        cap = tag_factory.new_tag("p", attrs={"class": "caption"})
                        cap.string = cap_text
                        img_block_wrapper.append(cap)
                    target_tag.replace_with(img_block_wrapper)
                    log.debug(f"Injected WaPo image {origin} into placeholder {el.get('_id')}")
                    added += 1
                else:
                    img_tag = tag_factory.new_tag("img", attrs={"src": fname, "class": "epub-image"})
                    
                    if is_first_image and not lede_candidate:
                        lede_candidate = (img_tag, cap_text, origin)
                        added += 1
                    else:
                        ImageProcessor.wrap_in_img_block(body_soup, img_tag, cap_text)
                        log.debug(f"Appended WaPo image {origin} (no specific placeholder found)")
                        added += 1

            if lede_candidate:
                l_img, l_cap, l_origin = lede_candidate
                if body_soup.contents:
                    body_soup.insert(0, l_img)
                else:
                    body_soup.append(l_img)
                ImageProcessor.wrap_in_img_block(body_soup, l_img, l_cap)
                log.debug(f"Prepended WaPo Lede image {l_origin}")

            if added:
                for fc in list(body_soup.find_all("figcaption")):
                    fc.decompose()
                log.info(f"Seeded {added} Next.js images from __NEXT_DATA__")
        except Exception as e:
            log.debug(f"WaPo __NEXT_DATA__ seed failed: {e}")

    @staticmethod
    def _image_items_from_json_ld(node: Any) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []

        def _add_image(value, caption: Optional[str] = None):
            if isinstance(value, str):
                items.append({"url": value, "caption": caption or ""})
            elif isinstance(value, dict):
                url = value.get("url") or value.get("contentUrl")
                if url:
                    items.append({
                        "url": url,
                        "caption": value.get("caption") or value.get("description") or caption or "",
                    })
            elif isinstance(value, list):
                for item in value:
                    _add_image(item, caption)

        def _walk(value):
            if isinstance(value, dict):
                graph = value.get("@graph")
                if isinstance(graph, list):
                    for item in graph:
                        _walk(item)
                item_type = value.get("@type")
                types = item_type if isinstance(item_type, list) else [item_type]
                caption = value.get("caption") or value.get("description") or value.get("headline")
                if any(t in {"NewsArticle", "Article", "ReportageNewsArticle", "ImageObject"} for t in types):
                    _add_image(value.get("image"), caption)
                    if value.get("@type") == "ImageObject":
                        _add_image(value, caption)
                for key in ("primaryImageOfPage", "thumbnailUrl"):
                    _add_image(value.get(key), caption)
            elif isinstance(value, list):
                for item in value:
                    _walk(item)

        _walk(node)
        return items

    @staticmethod
    async def _seed_images_from_metadata(raw_html: str, body_soup: BeautifulSoup, base_url: str, book_assets: list, session, options: Optional[ConversionOptions] = None) -> None:
        if not raw_html:
            return
        try:
            full_soup = BeautifulSoup(raw_html, "html.parser")
            candidates: List[Dict[str, str]] = []

            for script in full_soup.find_all("script", attrs={"type": "application/ld+json"}):
                text = script.string or script.get_text()
                if not text:
                    continue
                try:
                    candidates.extend(ImageProcessor._image_items_from_json_ld(json.loads(text)))
                except Exception:
                    continue

            for selector in [
                'meta[property="og:image"]',
                'meta[property="og:image:url"]',
                'meta[name="twitter:image"]',
                'meta[name="twitter:image:src"]',
            ]:
                for meta in full_soup.select(selector):
                    url = meta.get("content")
                    if url:
                        candidates.append({"url": url, "caption": ""})

            seen = set()
            unique = []
            for item in candidates:
                url = item.get("url")
                if not url:
                    continue
                full_url = urljoin(base_url, url)
                if full_url in seen or ImageProcessor.is_junk(full_url):
                    continue
                seen.add(full_url)
                unique.append({"url": full_url, "caption": item.get("caption") or ""})

            if not unique:
                return

            max_dim, quality, color_mode, output_pref = ImageProcessor.image_optimize_params(options)
            added = 0
            tag_factory = ImageProcessor._tag_factory(body_soup)
            for item in unique[:3]:
                url = item["url"]
                existing = ImageProcessor._find_existing_asset(book_assets, url)
                if existing:
                    fname = existing.filename
                else:
                    headers, data_bytes, err = await ImageProcessor.fetch_image_data(session, url, referer=base_url)
                    if err or not headers or not data_bytes:
                        continue
                    mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(
                        url,
                        headers,
                        data_bytes,
                        max_dimension=max_dim,
                        jpeg_quality=quality,
                        color_mode=color_mode,
                        output_preference=output_pref,
                    )
                    if val_err or not final_data:
                        continue
                    fname_base = ImageProcessor._image_filename_base(url, fallback_seed=url)
                    count = 0
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                    while any(a.filename == fname for a in book_assets):
                        count += 1
                        fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"
                    asset = ImageAsset(
                        uid=f"img_{ImageProcessor._short_stable_hash(fname)}",
                        filename=fname,
                        media_type=mime,
                        content=final_data,
                        original_url=url,
                        alt_urls=[url],
                    )
                    book_assets.append(asset)
                if body_soup.find("img", src=fname):
                    continue
                img_tag = tag_factory.new_tag("img", attrs={"src": fname, "class": "epub-image"})
                body_soup.insert(0, img_tag)
                ImageProcessor.wrap_in_img_block(body_soup, img_tag, item.get("caption") or None)
                added += 1

            if added:
                log.info(f"Seeded {added} images from article metadata")
        except Exception as exc:
            log.debug(f"Metadata image seed failed: {exc}")

    @staticmethod
    async def process_images(session, soup, base_url, book_assets: list, profile: Optional[SiteProfile] = None, options: Optional[ConversionOptions] = None):
        ImageProcessor._simplify_interactive_image_sequences(soup)
        for wrapper in list(soup.find_all("div")):
            dataid = (wrapper.get("data-testid") or "").lower()
            if dataid.startswith(("imageblock", "photoviewer")):
                meaningful = [c for c in wrapper.contents if not (isinstance(c, str) and not c.strip())]
                if len(meaningful) == 1:
                    wrapper.unwrap()
        for label in soup.find_all("span"):
            if (label.get_text(strip=True) or "").lower() == "image":
                label.decompose()

        for pic in soup.find_all('picture'):
            img = pic.find('img')
            if img:
                source_srcsets = [
                    source.get("srcset") or source.get("data-srcset")
                    for source in pic.find_all("source")
                    if source.get("srcset") or source.get("data-srcset")
                ]
                if source_srcsets and not img.get("srcset") and not img.get("data-srcset"):
                    img["srcset"] = ", ".join(source_srcsets)
                for source in pic.find_all('source'):
                    source.decompose()
                pic.replace_with(img)
            else:
                pic.decompose()

        img_tags = soup.find_all('img')
        tasks = []
        image_sem = asyncio.Semaphore(max(1, IMAGE_CONCURRENCY))
        max_dim, quality, color_mode, output_pref = ImageProcessor.image_optimize_params(options)

        async def _process_tag(img_tag):
            if img_tag.get('class') == ['epub-image'] or str(img_tag.get('src')).startswith(IMAGE_DIR_IN_EPUB):
                return

            log.debug(f"Processing img tag attrs={img_tag.attrs}")
            src = img_tag.get('src')
            srcset = img_tag.get('srcset')
            data_src = img_tag.get('data-src')
            data_srcset = img_tag.get('data-srcset')

            final_src = None

            candidates = []
            if data_src:
                candidates.append(data_src)
            if data_srcset:
                candidates.extend(ImageProcessor.parse_srcset(data_srcset))
            if srcset:
                candidates.extend(ImageProcessor.parse_srcset(srcset))

            wapo_origin_seed = None
            for srcset_candidate in [data_srcset, srcset]:
                if srcset_candidate and "washingtonpost.com/wp-apps/imrs.php" in srcset_candidate:
                    parsed_set = ImageProcessor.parse_srcset(srcset_candidate)
                    if parsed_set:
                        final_src = parsed_set[0] if not final_src else final_src
                        wapo_origin_seed = ImageProcessor._extract_origin_from_proxy(parsed_set[0], profile=profile)
                        break

            if src and src.startswith("data:image/"):
                final_src = src
            elif src and not ImageProcessor.is_junk(src) and not ImageProcessor.is_blur_placeholder(src):
                final_src = src
            else:
                for c in candidates:
                    if not ImageProcessor.is_junk(c) and not ImageProcessor.is_blur_placeholder(c):
                        final_src = c
                        break
                if not final_src:
                    for c in candidates:
                        if not ImageProcessor.is_junk(c):
                            final_src = c
                            break
                if not final_src and src and not ImageProcessor.is_junk(src):
                    final_src = src

            if not final_src or final_src.startswith(('mailto:', 'javascript:')) or (final_src.startswith('data:') and not final_src.startswith('data:image/')):
                if src and ImageProcessor.is_junk(src) and not any(not ImageProcessor.is_junk(c) for c in candidates):
                    img_tag.decompose()
                return

            try:
                if final_src.startswith("data:image/"):
                    mime, data = ImageProcessor._decode_data_image(final_src)
                    if not mime or not data:
                        img_tag.decompose()
                        return
                    headers = {"Content-Type": mime}
                    m2, e2, d2, val_err = ImageProcessor.optimize_and_get_details(
                        "inline.svg" if mime == "image/svg+xml" else "inline-image",
                        headers,
                        data,
                        max_dimension=max_dim,
                        jpeg_quality=quality,
                        color_mode=color_mode,
                        output_preference=output_pref,
                    )
                    if val_err or not d2:
                        log.debug(f"Skipped inline data image: {val_err}")
                        img_tag.decompose()
                        return
                    digest = hashlib.sha1(data).hexdigest()[:12]
                    fname_base = sanitize_filename(img_tag.get("data-name") or img_tag.get("alt") or f"inline_svg_{digest}")
                    if len(fname_base) < 3:
                        fname_base = f"inline_svg_{digest}"
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{e2}"
                    count = 0
                    while any(a.filename == fname for a in book_assets):
                        count += 1
                        fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{e2}"
                    asset = ImageAsset(
                        uid=f"img_{digest}",
                        filename=fname,
                        media_type=m2,
                        content=d2,
                        original_url=f"inline:{digest}",
                    )
                    book_assets.append(asset)
                    img_tag["src"] = fname
                    ImageProcessor._strip_processed_image_attrs(img_tag)
                    img_tag["class"] = "epub-image"
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ImageProcessor.wrap_in_img_block(soup, img_tag, caption_text)
                    ImageProcessor._cleanup_generic_wrapper(img_tag, caption_text)
                    return

                full_url = urljoin(base_url, final_src.strip())
                if "web.archive.org" in base_url and full_url.startswith("http://"):
                    full_url = full_url.replace("http://", "https://", 1)

                started = asyncio.get_event_loop().time()
                existing = ImageProcessor._find_existing_asset(book_assets, full_url)
                if existing:
                    img_tag['src'] = existing.filename
                    ImageProcessor._strip_processed_image_attrs(img_tag)
                    img_tag['class'] = 'epub-image'
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ImageProcessor.wrap_in_img_block(soup, img_tag, caption_text)
                    ImageProcessor._cleanup_generic_wrapper(img_tag, caption_text)
                    return

                candidate_urls = []
                def _add_candidate(u: Optional[str], prepend: bool = False):
                    if not u or ImageProcessor.is_junk(u):
                        return
                    if ImageProcessor.is_blur_placeholder(u) and not prepend:
                        return
                    if prepend:
                        if u not in candidate_urls:
                            candidate_urls.insert(0, u)
                    else:
                        if u not in candidate_urls:
                            candidate_urls.append(u)

                origin_src = ImageProcessor._extract_origin_from_proxy(full_url, profile=profile)

                lede_img = None
                parent_fig = img_tag.find_parent("figure")
                if parent_fig and parent_fig.get("data-testid") == "lede-image":
                    lede_img = parent_fig.find("img")
                if lede_img:
                    for attr in ["src", "data-src"]:
                        if lede_img.get(attr):
                            _add_candidate(urljoin(base_url, lede_img[attr]), prepend=True)
                    for srcset_attr in ["srcset", "data-srcset"]:
                        if lede_img.get(srcset_attr):
                            for w, u in ImageProcessor.parse_srcset_with_width(lede_img[srcset_attr]):
                                _add_candidate(urljoin(base_url, u), prepend=True)

                if origin_src:
                    _add_candidate(origin_src, prepend=True)
                
                if not ImageProcessor.is_junk(full_url):
                    _add_candidate(full_url, prepend=True)
                    if "?" in full_url:
                            _add_candidate(full_url.split("?", 1)[0])

                for srcset_str in filter(None, [data_srcset, srcset]):
                    parsed_set = ImageProcessor.parse_srcset_with_width(srcset_str)
                    for width, candidate in parsed_set:
                        cand_full = urljoin(base_url, candidate)
                        _add_candidate(cand_full, prepend=width >= 600)
                        origin_cand = ImageProcessor._extract_origin_from_proxy(cand_full, profile=profile)
                        if origin_cand:
                            _add_candidate(origin_cand, prepend=True)
                        
                        is_known_proxy = ("washingtonpost.com" in cand_full and "/wp-apps/imrs.php" in cand_full)
                        if profile and profile.image_proxy_pattern and profile.image_proxy_pattern in cand_full:
                            is_known_proxy = True

                        if not is_known_proxy and "?" in cand_full:
                            _add_candidate(cand_full.split("?", 1)[0])

                for u in list(candidate_urls):
                    origin = ImageProcessor._extract_origin_from_proxy(u, profile=profile)
                    if origin:
                        _add_candidate(origin, prepend=True)
                log.debug(f"Candidate URLs for img: {candidate_urls}")

                mime = ext = final_data = None
                effective_url = None
                for cand in candidate_urls[:IMG_MAX_CANDIDATES]:
                    if asyncio.get_event_loop().time() - started > IMG_MAX_PER_IMAGE_SEC:
                        log.debug(f"Image timeout for {src} after {IMG_MAX_PER_IMAGE_SEC}s")
                        break
                    headers, data, err = await ImageProcessor.fetch_image_data(session, cand, referer=base_url)
                    if err or not headers or not data:
                        continue
                    m2, e2, d2, val_err = ImageProcessor.optimize_and_get_details(cand, headers, data, max_dimension=max_dim, jpeg_quality=quality, color_mode=color_mode, output_preference=output_pref)
                    if val_err:
                        log.debug(f"Skipped image {cand}: {val_err}")
                        continue
                    mime, ext, final_data, effective_url = m2, e2, d2, cand
                    break

                if not final_data:
                    log.debug(f"Failed to fetch/validate image after candidates: {candidate_urls}")
                    img_tag['src'] = full_url
                    ImageProcessor._strip_processed_image_attrs(img_tag)
                    return

                # Double-check if asset was added by another task while we were fetching
                existing_after_fetch = ImageProcessor._find_existing_asset(book_assets, full_url)
                if existing_after_fetch:
                    img_tag['src'] = existing_after_fetch.filename
                    ImageProcessor._strip_processed_image_attrs(img_tag)
                    img_tag['class'] = 'epub-image'
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ImageProcessor.wrap_in_img_block(soup, img_tag, caption_text)
                    ImageProcessor._cleanup_generic_wrapper(img_tag, caption_text)
                    return

                alt_urls = []
                for u in candidate_urls:
                    if u:
                        alt_urls.append(u)

                fname_base = ImageProcessor._image_filename_base(effective_url or full_url, fallback_seed=effective_url or full_url)

                count = 0
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                while any(a.filename == fname for a in book_assets):
                    count += 1
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"

                uid = f"img_{ImageProcessor._short_stable_hash(fname)}"
                asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=effective_url or full_url, alt_urls=alt_urls or None)
                book_assets.append(asset)

                img_tag['src'] = fname
                ImageProcessor._strip_processed_image_attrs(img_tag)
                img_tag['class'] = 'epub-image'
                caption_text = ImageProcessor.find_caption(img_tag)
                ImageProcessor.wrap_in_img_block(soup, img_tag, caption_text)
                ImageProcessor._cleanup_generic_wrapper(img_tag, caption_text)

            except Exception as e:
                log.debug(f"Image process error {src}: {e}")

        async def _bounded_process_tag(img_tag):
            async with image_sem:
                await _process_tag(img_tag)

        for img in img_tags:
            tasks.append(_bounded_process_tag(img))

        if tasks:
            await tqdm_asyncio.gather(*tasks, desc="Optimizing Images", unit="img", leave=False)

        ImageProcessor._repair_adjacent_graphic_captions(soup)

        for fc in list(soup.find_all("figcaption")):
            if not ImageProcessor._is_upper_caption(fc):
                fc.decompose()

def __getattr__(name):
    if name == "ForumImageProcessor":
        from .forum_image_processor import ForumImageProcessor
        return ForumImageProcessor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

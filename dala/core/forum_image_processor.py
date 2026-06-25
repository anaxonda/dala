import asyncio
import hashlib
import mimetypes
import os
import re

import aiohttp
from bs4 import BeautifulSoup, Tag
from tqdm.asyncio import tqdm_asyncio
from yarl import URL
from urllib.parse import urljoin, urlparse
from typing import Any, Dict, List, Optional

from ..models import (
    IMAGE_DIR_IN_EPUB, IMAGE_TIMEOUT, IMG_MAX_PER_IMAGE_SEC,
    ConversionOptions, ImageAsset, log, normalize_url_for_matching, sanitize_filename,
)
from .image_processor import (
    BaseImageProcessor, ImageProcessor, FORUM_IMAGE_CONCURRENCY, FORUM_REQUESTS_TIMEOUT,
)

class ForumImageProcessor:
    @staticmethod
    def _per_image_timeout_seconds() -> float:
        # Preserve the legacy patch point used by tests and external callers.
        from . import image_processor as image_processor_module
        return getattr(image_processor_module, "IMG_MAX_PER_IMAGE_SEC", IMG_MAX_PER_IMAGE_SEC)

    @staticmethod
    def _normalize_for_match(url: str) -> Optional[str]:
        return normalize_url_for_matching(url) or None

    @staticmethod
    def _strip_forum_img_attrs(img_tag: Tag) -> None:
        """Remove forum/lightbox-specific attributes before styling the image."""
        attrs_to_remove = [
            'srcset', 'data-src', 'data-srcset', 'data-url', 'data-lazy',
            'loading', 'decoding', 'style', 'class', 'width', 'height',
            'data-zoom-target', 'title', 'data-lb-id', 'data-lb-src',
            'data-lb-single-image', 'data-lb-container-zoom', 'data-lb-trigger',
            'data-xf-init'
        ]
        for attr in attrs_to_remove:
            if img_tag.has_attr(attr):
                del img_tag[attr]

    @staticmethod
    def _cleanup_lightbox_wrappers(img_tag: Tag) -> None:
        """Unwrap XenForo lightbox containers, leaving only img-block + image."""
        if not img_tag:
            return
        wrapper = img_tag.parent
        if not wrapper or wrapper.name != "div" or "img-block" not in (wrapper.get("class") or []):
            return
        container = wrapper.parent
        if not container or container.name != "div":
            return
        classes = set(container.get("class") or [])
        data_xf_init = container.get("data-xf-init", "")
        if classes.intersection({"lazyloadPreSize", "lbContainer", "lbContainer--inline"}) or "lightbox" in data_xf_init:
            for zoomer in container.find_all("div", class_=re.compile(r"lbContainer-zoomer")):
                zoomer.decompose()
            wrapper.extract()
            container.replace_with(wrapper)

    @staticmethod
    def _finalize_image_tag(soup: BeautifulSoup, img_tag: Tag, caption_text: Optional[str]) -> None:
        ForumImageProcessor._strip_forum_img_attrs(img_tag)
        img_tag['class'] = 'epub-image'
        if caption_text is None:
            caption_text = ImageProcessor.find_caption(img_tag)
        ImageProcessor.wrap_in_img_block(soup, img_tag, caption_text)
        ForumImageProcessor._cleanup_lightbox_wrappers(img_tag)

    @staticmethod
    def is_junk(url: str) -> bool:
        if not url:
            return True
        if url.startswith("data:") or url.startswith("view-source:"):
            return True
        bad_keywords = [
            "spacer", "1x1", "transparent", "gray.gif", "pixel.gif",
            "placeholder", "loader", "blank.gif", "reaction_id=", "/react?", "reactions/emojione"
        ]
        lower_url = url.lower()
        if any(k in lower_url for k in bad_keywords):
            return True
        return False

    @staticmethod
    def is_forum_chrome_image(url: str) -> bool:
        if not url:
            return True
        lower_url = url.lower()
        chrome_markers = [
            "images.platforum.cloud/logos/",
            "platforum.cloud/logos/",
            "privacyoptions",
            "/banners/",
            "_banner_",
            "/styles/",
            "/assets/",
            "/reactions/",
            "/smilies/",
            "/emoji/",
            "/avatars/",
            "/avatar",
        ]
        if any(marker in lower_url for marker in chrome_markers):
            return True
        basename = os.path.basename(urlparse(lower_url).path)
        if basename in {"logo.svg", "logo.png", "favicon.ico"}:
            return True
        return False

    @staticmethod
    def _drop_image_node(img_tag: Tag) -> None:
        wrapper = img_tag.find_parent(["picture", "a"])
        if wrapper and wrapper.name == "a":
            classes = set(wrapper.get("class") or [])
            href = wrapper.get("href") or ""
            if "/attachments/" in href or classes.intersection({"file-preview", "lbContainer"}):
                img_tag.decompose()
                return
        if wrapper and wrapper.name == "picture":
            wrapper.decompose()
            return
        img_tag.decompose()

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
                return requests.get(
                    target,
                    headers={**img_headers, "Referer": referer or ""},
                    cookies=cookie_dict,
                    timeout=FORUM_REQUESTS_TIMEOUT,
                    allow_redirects=True,
                )
            
            resp = await loop.run_in_executor(None, _do_req)
            if resp.content:
                return resp.headers, resp.content, resp.status_code
        except Exception as e:
            log.warning(f"Forum requests fetch failed: {e}")
        return None, None, None

    @staticmethod
    def _unique_fetch_targets(*targets: Optional[str]) -> List[str]:
        unique = []
        seen = set()
        for target in targets:
            if not target:
                continue
            key = target.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(key)
        return unique

    @staticmethod
    def _parse_viewer_for_image(html_bytes, base_url):
        try:
            soup = BeautifulSoup(html_bytes, 'html.parser')
            img = soup.find('img')
            if img and img.get('src'):
                return urljoin(base_url, img.get('src'))
            link = soup.find('a', href=re.compile(r'\.(jpg|jpeg|png|webp|gif)(\?|$)', re.IGNORECASE))
            if link and link.get('href'):
                return urljoin(base_url, link.get('href'))
        except Exception:
            return None
        return None

    @staticmethod
    async def fetch_image_data(session, url, referer=None, viewer_url=None):
        try:
            non_retry = {401, 403, 404, 409}
            img_headers = {
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            url_without_query = url.split("?", 1)[0] if "/attachments/" in url and "?" in url else None
            targets = ForumImageProcessor._unique_fetch_targets(viewer_url, url, url_without_query)

            for target in targets:
                is_attachment = "/attachments/" in target

                headers_r, data_r, _ = await ForumImageProcessor._requests_fetch(session, target, img_headers, referer)
                if data_r:
                    ctype = str(headers_r.get('Content-Type', '')) if headers_r else ''
                    if not ctype.startswith('text/html'):
                        return headers_r or {}, data_r, None
                    viewer_img = ForumImageProcessor._parse_viewer_for_image(data_r, target)
                    if viewer_img:
                        h2, d2, _ = await ForumImageProcessor._requests_fetch(session, viewer_img, img_headers, referer or target)
                        if d2 and not str(h2.get('Content-Type','')).startswith('text/html'):
                            return h2 or {}, d2, None

                if not is_attachment:
                    headers = {**img_headers}
                    if referer:
                        headers["Referer"] = referer
                    try:
                        async with session.get(target, allow_redirects=True, headers=headers, timeout=IMAGE_TIMEOUT) as resp:
                            if resp.status in non_retry:
                                continue
                            if resp.status >= 400:
                                continue
                            data = await resp.read()
                            if data:
                                ctype = str(resp.headers.get('Content-Type', ''))
                                if not ctype.startswith('text/html'):
                                    return resp.headers, data, None
                                viewer_img = ForumImageProcessor._parse_viewer_for_image(data, target)
                                if viewer_img:
                                    h3, d3, _ = await ForumImageProcessor._requests_fetch(session, viewer_img, img_headers, referer or target)
                                    if d3 and not str(h3.get('Content-Type','')).startswith('text/html'):
                                        return h3 or {}, d3, None
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass

            return None, None, "No data"
        except Exception as e:
            return None, None, str(e)

    @staticmethod
    async def process_images(session, soup, base_url, book_assets: list, preloaded_assets: Optional[List[Dict[str, Any]]] = None, options: Optional[ConversionOptions] = None):
        preloaded_assets = preloaded_assets or []
        preload_map: Dict[str, ImageAsset] = {}
        hash_map: Dict[str, ImageAsset] = {}

        def _hash_bytes(data: bytes) -> Optional[str]:
            if not data:
                return None
            try:
                return hashlib.sha1(data).hexdigest()
            except Exception:
                return None

        def add_to_map(url_val: str, asset_obj: Optional[ImageAsset]):
            if not asset_obj or not url_val:
                return
            norm = normalize_url_for_matching(url_val)
            if url_val:
                preload_map[url_val] = asset_obj
            if norm:
                preload_map[norm] = asset_obj
            if url_val.endswith("/"):
                preload_map[url_val.rstrip("/")] = asset_obj
            if norm and norm.endswith("/"):
                preload_map[norm.rstrip("/")] = asset_obj
            try:
                parsed = urlparse(url_val)
                if "/attachments/" in parsed.path:
                    base_url = url_val.split("?", 1)[0]
                    preload_map[base_url] = asset_obj
                    norm_base = normalize_url_for_matching(base_url)
                    if norm_base:
                        preload_map[norm_base] = asset_obj
            except Exception:
                pass

        for asset in book_assets:
            urls = set()
            if asset.original_url and isinstance(asset.original_url, str):
                urls.add(asset.original_url)
            if asset.alt_urls:
                for u in asset.alt_urls:
                    if isinstance(u, str):
                        urls.add(u)
            for u in urls:
                add_to_map(u, asset)
            h = _hash_bytes(asset.content)
            if h:
                hash_map[h] = asset

        for a in preloaded_assets:
            hint_urls = [a.get("original_url"), a.get("viewer_url"), a.get("canonical_url"), a.get("url"), a.get("src")]
            hint_urls = [u for u in hint_urls if u and isinstance(u, str)]
            for h in hint_urls:
                add_to_map(h, preload_map.get(h) or preload_map.get(normalize_url_for_matching(h)))

        if preload_map:
            sample_keys = list(preload_map.keys())[:5]
            log.debug(f"Forum preload map size={len(preload_map)} sample={sample_keys}")

        for pic in soup.find_all('picture'):
            img = pic.find('img')
            if img:
                for source in pic.find_all('source'):
                    source.decompose()
                pic.replace_with(img)
            else:
                pic.decompose()

        for media in soup.find_all(['iframe']):
            href = media.get('src') or media.get('data-src')
            link = soup.new_tag('a', href=href or '#')
            link.string = href or "Embedded media"
            media.replace_with(link)

        img_tags = soup.find_all('img')
        tasks = []
        image_sem = asyncio.Semaphore(max(1, FORUM_IMAGE_CONCURRENCY))
        max_dim, quality, color_mode, output_pref = ImageProcessor.image_optimize_params(options)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        stats = {
            "matched": 0,
            "existing": 0,
            "preloaded": 0,
            "fetched": 0,
            "dropped": 0,
            "timed_out": 0,
            "errors": 0,
        }
        if img_tags:
            log.info(
                "Forum image pass start: "
                f"images={len(img_tags)} preloaded_assets={len(preloaded_assets)} "
                f"existing_assets={len(book_assets)} base={base_url}"
            )

        async def _process_tag(img_tag):
            src = img_tag.get('src')
            srcset = img_tag.get('srcset')
            data_src = img_tag.get('data-src')
            data_url = img_tag.get('data-url')
            data_lazy = img_tag.get('data-lazy')
            data_srcset = img_tag.get('data-srcset')
            link_href = None
            parent_link = img_tag.find_parent('a')
            if parent_link and parent_link.get('href'):
                link_href = parent_link.get('href')

            final_src = None
            if src and not ForumImageProcessor.is_junk(src):
                final_src = src
            else:
                candidates = []
                for cand in (data_src, data_lazy, data_url):
                    if cand:
                        candidates.append(cand)
                if data_srcset:
                    candidates.extend(ImageProcessor.parse_srcset(data_srcset))
                if srcset:
                    candidates.extend(ImageProcessor.parse_srcset(srcset))
                if link_href:
                    candidates.append(link_href)

                for c in candidates:
                    if not ForumImageProcessor.is_junk(c):
                        final_src = c
                        break

                if not final_src and src:
                    final_src = src

            if not final_src or final_src.startswith(('data:', 'mailto:', 'javascript:')):
                return

            try:
                log.debug(f"Forum img candidate src={src} data-src={data_src} data-url={data_url} data-lazy={data_lazy} srcset={srcset} data-srcset={data_srcset}")
                if final_src.startswith("view-source:"):
                    final_src = final_src.replace("view-source:", "", 1)
                if link_href and link_href.startswith("view-source:"):
                    link_href = link_href.replace("view-source:", "", 1)

                full_url = urljoin(base_url, final_src.strip())
                if "web.archive.org" in base_url and full_url.startswith("http://"):
                    full_url = full_url.replace("http://", "https://", 1)

                if ForumImageProcessor.is_forum_chrome_image(full_url):
                    ForumImageProcessor._drop_image_node(img_tag)
                    stats["dropped"] += 1
                    return

                if not re.search(r'\.(jpe?g|png|webp|gif|bmp)(\?|$)', full_url, re.IGNORECASE) and "attachments" not in full_url and "image" not in full_url:
                    return

                viewer_url = None
                attachment_base = None
                if "/attachments/" in full_url:
                    attachment_base = full_url.split("?", 1)[0]

                if link_href and re.search(r'/attachments/[^/]+\.\d+/?', link_href):
                    viewer_url = urljoin(base_url, link_href.strip())
                elif attachment_base:
                    viewer_url = attachment_base

                attach_target = viewer_url or full_url

                matched_asset = None
                urls_to_check = [full_url]
                if viewer_url:
                    urls_to_check.append(viewer_url)
                if attachment_base:
                    urls_to_check.append(attachment_base)

                for check_url in urls_to_check:
                    if check_url in preload_map:
                        matched_asset = preload_map[check_url]
                        log.debug(f"Exact match found for {check_url[:80]}")
                        break
                    norm_url = normalize_url_for_matching(check_url)
                    if norm_url and norm_url in preload_map:
                        matched_asset = preload_map[norm_url]
                        log.debug(f"Normalized match found for {check_url[:80]}")
                        break

                if matched_asset:
                    img_tag['src'] = matched_asset.filename
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ForumImageProcessor._finalize_image_tag(soup, img_tag, caption_text)
                    stats["matched"] += 1
                    return
                else:
                    log.debug(f"Forum no preload match for {full_url[:100]}")

                def _url_same(lhs, rhs):
                    norm_l = ForumImageProcessor._normalize_for_match(lhs)
                    norm_r = ForumImageProcessor._normalize_for_match(rhs)
                    return norm_l and norm_r and norm_l == norm_r

                def _matches_asset(a):
                    candidates = [full_url, viewer_url, attachment_base]
                    asset_urls = [a.original_url]
                    if getattr(a, "alt_urls", None):
                        asset_urls.extend([u for u in a.alt_urls if u])
                    for cand in candidates:
                        for au in asset_urls:
                            if _url_same(cand, au):
                                return True
                    return False

                existing = next((a for a in book_assets if _matches_asset(a)), None)
                if existing:
                    img_tag['src'] = existing.filename
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ForumImageProcessor._finalize_image_tag(soup, img_tag, caption_text)
                    stats["existing"] += 1
                    return

                preload_match = None
                for a in preloaded_assets:
                    orig = a.get("original_url")
                    view = a.get("viewer_url")
                    canonical = a.get("canonical_url")
                    extra = a.get("url") or a.get("src")
                    def same(u, v):
                        if not u or not v: return False
                        if u == v: return True
                        if "?" in u and u.split("?",1)[0] == v: return True
                        if "?" in v and v.split("?",1)[0] == u: return True
                        return False
                    if any([
                        same(orig, full_url), same(orig, viewer_url),
                        same(orig, attachment_base),
                        same(view, full_url), same(view, viewer_url),
                        same(canonical, full_url), same(canonical, viewer_url), same(canonical, attachment_base),
                        same(extra, full_url), same(extra, viewer_url), same(extra, attachment_base)
                    ]):
                        preload_match = a
                        break

                if preload_match:
                    mime = preload_match.get("media_type") or preload_match.get("content_type") or "image/jpeg"
                    data_bytes = preload_match.get("content")
                    if isinstance(data_bytes, str):
                        import base64
                        try:
                            data_bytes = base64.b64decode(data_bytes)
                        except Exception:
                            data_bytes = None
                    if data_bytes:
                        hashed = _hash_bytes(data_bytes)
                        if hashed and hashed in hash_map:
                            asset = hash_map[hashed]
                            img_tag['src'] = asset.filename
                            caption_text = ImageProcessor.find_caption(img_tag)
                            ForumImageProcessor._finalize_image_tag(soup, img_tag, caption_text)
                            return
                        fname_base = sanitize_filename(os.path.splitext(os.path.basename(urlparse(full_url).path))[0])
                        ext = os.path.splitext(fname_base)[1] or ".img"
                        if len(fname_base) < 3:
                            fname_base = f"img_{abs(hash(full_url))}"
                        count = 0
                        fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                        while any(a.filename == fname for a in book_assets):
                            count += 1
                            fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"
                        uid = f"img_{abs(hash(fname))}"
                        alt_urls = []
                        for u in [orig, view, canonical, extra, full_url, attachment_base, viewer_url]:
                            if u and isinstance(u, str):
                                alt_urls.append(u)
                                if "?" in u:
                                    alt_urls.append(u.split("?",1)[0])
                        asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=data_bytes, original_url=full_url, alt_urls=list(dict.fromkeys([u for u in alt_urls if u])))
                        book_assets.append(asset)
                        if hashed:
                            hash_map[hashed] = asset
                        for u in asset.alt_urls or []:
                            add_to_map(u, asset)
                        img_tag['src'] = fname
                        caption_text = ImageProcessor.find_caption(img_tag)
                        ForumImageProcessor._finalize_image_tag(soup, img_tag, caption_text)
                        stats["preloaded"] += 1
                        return

                if "/attachments/" not in full_url and not viewer_url:
                    ForumImageProcessor._drop_image_node(img_tag)
                    stats["dropped"] += 1
                    return

                log.debug(f"Forum fetch image {full_url} (viewer={viewer_url}) not found in preload_map")
                headers, data, err = await ForumImageProcessor.fetch_image_data(session, attach_target, referer=base_url, viewer_url=viewer_url)
                if err:
                    return

                mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(full_url, headers, data, max_dimension=max_dim, jpeg_quality=quality, color_mode=color_mode, output_preference=output_pref)
                if val_err:
                    log.debug(f"Skipped image {full_url}: {val_err}")
                    return

                hashed = _hash_bytes(final_data)
                if hashed and hashed in hash_map:
                    asset = hash_map[hashed]
                    img_tag['src'] = asset.filename
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ForumImageProcessor._finalize_image_tag(soup, img_tag, caption_text)
                    return

                alt_urls = [full_url]
                if "?" in full_url:
                    alt_urls.append(full_url.split("?", 1)[0])
                if viewer_url:
                    alt_urls.append(viewer_url)
                    if "?" in viewer_url:
                        alt_urls.append(viewer_url.split("?", 1)[0])
                if attachment_base:
                    alt_urls.append(attachment_base)

                fname_base = sanitize_filename(os.path.splitext(os.path.basename(urlparse(full_url).path))[0])
                if len(fname_base) < 3:
                    fname_base = f"img_{abs(hash(full_url))}"

                count = 0
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                while any(a.filename == fname for a in book_assets):
                    count += 1
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"

                uid = f"img_{abs(hash(fname))}"
                asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=full_url, alt_urls=list(dict.fromkeys([u for u in alt_urls if u])))
                book_assets.append(asset)
                if hashed:
                    hash_map[hashed] = asset
                for u in asset.alt_urls or []:
                    add_to_map(u, asset)

                img_tag['src'] = fname
                caption_text = ImageProcessor.find_caption(img_tag)
                ForumImageProcessor._finalize_image_tag(soup, img_tag, caption_text)
                stats["fetched"] += 1

            except Exception as e:
                stats["errors"] += 1
                log.debug(f"Image process error {src}: {e}")

        async def _bounded_process_tag(img_tag):
            async with image_sem:
                timeout_seconds = ForumImageProcessor._per_image_timeout_seconds()
                try:
                    await asyncio.wait_for(_process_tag(img_tag), timeout=timeout_seconds)
                except asyncio.TimeoutError:
                    stats["timed_out"] += 1
                    src = img_tag.get("src") or img_tag.get("data-src") or ""
                    log.info(f"Forum image processing timed out after {timeout_seconds}s for {src[:100]}")

        for img in img_tags:
            tasks.append(_bounded_process_tag(img))

        try:
            if tasks:
                await tqdm_asyncio.gather(*tasks, desc="Optimizing Images", unit="img", leave=False)
        finally:
            duration_ms = int((loop.time() - started_at) * 1000)
            log.info(
                "Forum image pass done: "
                f"images={len(img_tags)} matched={stats['matched']} existing={stats['existing']} "
                f"preloaded={stats['preloaded']} fetched={stats['fetched']} dropped={stats['dropped']} "
                f"timed_out={stats['timed_out']} errors={stats['errors']} "
                f"duration_ms={duration_ms} assets={len(book_assets)}"
            )

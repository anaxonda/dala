import os
import io
import re
import asyncio
import aiohttp
import mimetypes
import hashlib
import json
from urllib.parse import urlparse, parse_qs, urljoin
from bs4 import BeautifulSoup, Tag, Comment
from typing import List, Dict, Optional, Any, Tuple
from tqdm.asyncio import tqdm_asyncio
from yarl import URL

try:
    from PIL import Image as PillowImage
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

from . .models import (
    log, IMAGE_DIR_IN_EPUB, MAX_IMAGE_DIMENSION, JPEG_QUALITY,
    REQUEST_TIMEOUT, IMG_MAX_RETRIES, IMG_RETRY_DELAY, IMAGE_TIMEOUT,
    IMG_MAX_CANDIDATES, IMG_MAX_PER_IMAGE_SEC,
    ImageAsset, SiteProfile, normalize_url_for_matching, sanitize_filename
)
from .session import fetch_with_retry

class BaseImageProcessor:
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
    def optimize_and_get_details(url, headers, data):
        if not data:
            return None, None, None, "No Data"
        content_type = headers.get('Content-Type', '').split(';')[0].strip().lower()
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

                if img.width > MAX_IMAGE_DIMENSION or img.height > MAX_IMAGE_DIMENSION:
                    reduce_factor = max(1, int(max(img.width, img.height) / (MAX_IMAGE_DIMENSION * 2)))
                    if reduce_factor > 1:
                        img = img.reduce(reduce_factor)
                    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), PillowImage.Resampling.LANCZOS)

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

                if output_format in ('JPEG', 'WEBP'):
                    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                        background = PillowImage.new("RGB", img.size, (255, 255, 255))
                        if img.mode == 'P':
                            img = img.convert('RGBA')
                        background.paste(img, mask=img.split()[3])
                        img = background
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')

                out_io = io.BytesIO()
                save_params = {"optimize": True}
                if output_format == 'JPEG':
                    save_params["quality"] = JPEG_QUALITY
                    save_params["subsampling"] = "4:2:0"
                if output_format == 'WEBP':
                    save_params["quality"] = 70
                img.save(out_io, format=output_format, **save_params)

                return output_mime, output_ext, out_io.getvalue(), None

        except Exception as e:
            return None, None, None, f"Optimization Error: {e}"

    @staticmethod
    def find_caption(element_tag):
        if not element_tag:
            return None
        fig = element_tag.find_parent('figure')
        if fig:
            cap = fig.find('figcaption')
            if cap:
                return cap.get_text(strip=True)
        nxt = element_tag.find_next_sibling(['p', 'div', 'span', 'figcaption'])
        if nxt:
            text = nxt.get_text(strip=True)
            if 5 < len(text) < 300:
                return text
        return None

    @staticmethod
    def wrap_in_img_block(soup: BeautifulSoup, img_tag: Tag, caption_text: Optional[str]) -> None:
        if not img_tag or not soup:
            return
        fig = img_tag.find_parent("figure")
        if fig:
            if not caption_text:
                figcap = fig.find("figcaption")
                if figcap:
                    caption_text = figcap.get_text(strip=True)
                    figcap.decompose()
            fig.unwrap()

        wrapper = soup.new_tag("div", attrs={"class": "img-block"})
        parent = img_tag.parent
        if parent:
            img_tag.replace_with(wrapper)
        else:
            (soup.body or soup).append(wrapper)
        wrapper.append(img_tag)
        if caption_text:
            cap = soup.new_tag("p", attrs={"class": "caption"})
            cap.string = caption_text
            wrapper.append(cap)

        parent = wrapper.parent
        while parent and parent.name in ("div", "section"):
            for fc in list(parent.find_all("figcaption", recursive=False)):
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
            sib.decompose()

        parent = wrapper.parent
        if parent:
            for fc in list(parent.find_all("figcaption", recursive=False)):
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
    def parse_srcset(srcset_str: str) -> list:
        if not srcset_str:
            return []
        candidates = []
        parts = srcset_str.split(',')
        for p in parts:
            p = p.strip()
            if not p:
                continue
            sub = p.split(' ')
            url = sub[0]
            width = 0
            if len(sub) > 1 and sub[1].endswith('w'):
                try:
                    width = int(sub[1][:-1])
                except:
                    pass
            candidates.append((width, url))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [c[1] for c in candidates]

    @staticmethod
    def parse_srcset_with_width(srcset_str: str) -> list:
        if not srcset_str:
            return []
        pairs = []
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
        for attempt_idx, ref in enumerate(refs):
            try:
                headers, _ = await fetch_with_retry(
                    session, url, 'headers', referer=ref, extra_headers=image_headers,
                    non_retry_statuses={400,401,403,404,451}, max_retries=IMG_MAX_RETRIES,
                    backoff=IMG_RETRY_DELAY, timeout=IMAGE_TIMEOUT
                )
                if headers:
                    data, _ = await fetch_with_retry(
                        session, url, 'bytes', referer=ref, extra_headers=image_headers,
                        non_retry_statuses={400,401,403,404,451}, max_retries=IMG_MAX_RETRIES,
                        backoff=IMG_RETRY_DELAY, timeout=IMAGE_TIMEOUT
                    )
                else:
                    data = None

                if headers and data:
                    return headers, data, None
                last_err = "No headers" if not headers else "No data"
                if attempt_idx == 0 and (not headers or not data):
                    aiohttp_fail_reason = "Failed on first aiohttp attempt (403/Timeout/Error)"
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
        elif not (headers and data):
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

        cap_text = caption_text
        cap_p = wrapper.find("p", class_="caption")
        if cap_text is None and cap_p:
            cap_text = cap_p.get_text(strip=True) or None

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
    async def _seed_images_from_nextjs_data(raw_html: str, body_soup: BeautifulSoup, base_url: str, book_assets: list, session, profile: Optional[SiteProfile] = None) -> None:
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
                mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(origin, headers, data_bytes)
                if val_err or not final_data:
                    log.debug(f"WaPo __NEXT_DATA__ validate failed for {origin}: {val_err}")
                    continue
                fname_base = sanitize_filename(os.path.splitext(os.path.basename(urlparse(origin).path))[0]) or f"img_{abs(hash(origin))}"
                count = 0
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                while any(a.filename == fname for a in book_assets):
                    count += 1
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"
                uid = f"img_{abs(hash(fname))}"
                asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=origin, alt_urls=[origin])
                book_assets.append(asset)
                
                target_tag = body_soup.find(id=el.get("_id"))
                if not target_tag:
                    target_tag = body_soup.find(attrs={"data-id": el.get("_id")})
                if not target_tag:
                    target_tag = body_soup.find(attrs={"data-uuid": el.get("_id")})
                
                if target_tag:
                    img_block_wrapper = body_soup.new_tag("div", attrs={"class": "img-block"})
                    img_tag = body_soup.new_tag("img", attrs={"src": fname, "class": "epub-image"})
                    img_block_wrapper.append(img_tag)
                    if cap_text:
                        cap = body_soup.new_tag("p", attrs={"class": "caption"})
                        cap.string = cap_text
                        img_block_wrapper.append(cap)
                    target_tag.replace_with(img_block_wrapper)
                    log.debug(f"Injected WaPo image {origin} into placeholder {el.get('_id')}")
                    added += 1
                else:
                    img_tag = body_soup.new_tag("img", attrs={"src": fname, "class": "epub-image"})
                    
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
    async def process_images(session, soup, base_url, book_assets: list, profile: Optional[SiteProfile] = None):
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
                for source in pic.find_all('source'):
                    source.decompose()
                pic.replace_with(img)
            else:
                pic.decompose()

        img_tags = soup.find_all('img')
        tasks = []

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

            if src and not ImageProcessor.is_junk(src):
                final_src = src
            else:
                for c in candidates:
                    if not ImageProcessor.is_junk(c):
                        final_src = c
                        break
                if not final_src and src and not ImageProcessor.is_junk(src):
                    final_src = src

            if not final_src or final_src.startswith(('data:', 'mailto:', 'javascript:')):
                if src and ImageProcessor.is_junk(src) and not any(not ImageProcessor.is_junk(c) for c in candidates):
                    img_tag.decompose()
                return

            try:
                full_url = urljoin(base_url, final_src.strip())
                if "web.archive.org" in base_url and full_url.startswith("http://"):
                    full_url = full_url.replace("http://", "https://", 1)

                started = asyncio.get_event_loop().time()
                existing = next((a for a in book_assets if a.original_url == full_url), None)
                if existing:
                    img_tag['src'] = existing.filename
                    for attr in ['srcset', 'data-src', 'data-srcset', 'loading', 'decoding', 'style', 'class', 'width', 'height']:
                        if img_tag.has_attr(attr):
                            del img_tag[attr]
                    img_tag['class'] = 'epub-image'
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ImageProcessor.wrap_in_img_block(soup, img_tag, caption_text)
                    ImageProcessor._cleanup_generic_wrapper(img_tag, caption_text)
                    return

                candidate_urls = []
                def _add_candidate(u: Optional[str], prepend: bool = False):
                    if not u or ImageProcessor.is_junk(u):
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
                    m2, e2, d2, val_err = ImageProcessor.optimize_and_get_details(cand, headers, data)
                    if val_err:
                        log.debug(f"Skipped image {cand}: {val_err}")
                        continue
                    mime, ext, final_data, effective_url = m2, e2, d2, cand
                    break

                if not final_data:
                    log.debug(f"Failed to fetch/validate image after candidates: {candidate_urls}")
                    return

                alt_urls = []
                for u in candidate_urls:
                    if u:
                        alt_urls.append(u)

                fname_base = sanitize_filename(os.path.splitext(os.path.basename(urlparse(effective_url).path))[0])
                if len(fname_base) < 3:
                    fname_base = f"img_{abs(hash(effective_url))}"

                count = 0
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                while any(a.filename == fname for a in book_assets):
                    count += 1
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"

                uid = f"img_{abs(hash(fname))}"
                asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=effective_url or full_url, alt_urls=alt_urls or None)
                book_assets.append(asset)

                img_tag['src'] = fname
                for attr in ['srcset', 'data-src', 'data-srcset', 'loading', 'decoding', 'style', 'class', 'width', 'height']:
                    if img_tag.has_attr(attr):
                        del img_tag[attr]
                img_tag['class'] = 'epub-image'
                caption_text = ImageProcessor.find_caption(img_tag)
                ImageProcessor.wrap_in_img_block(soup, img_tag, caption_text)
                ImageProcessor._cleanup_generic_wrapper(img_tag, caption_text)

            except Exception as e:
                log.debug(f"Image process error {src}: {e}")

        for img in img_tags:
            tasks.append(_process_tag(img))

        if tasks:
            await tqdm_asyncio.gather(*tasks, desc="Optimizing Images", unit="img", leave=False)

        for fc in list(soup.find_all("figcaption")):
            fc.decompose()

class ForumImageProcessor:
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
            log.warning(f"Forum requests fetch failed: {e}")
        return None, None, None

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
            targets = []
            if viewer_url:
                targets.append(viewer_url)
            targets.append(url)
            if "/attachments/" in url and "?" in url:
                targets.append(url.split("?", 1)[0])

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
                    headers, resp = await fetch_with_retry(session, target, 'bytes', referer=referer, non_retry_statuses=non_retry, extra_headers=img_headers)
                    if headers and resp:
                        ctype = str(headers.get('Content-Type', ''))
                        if not ctype.startswith('text/html'):
                            return headers, resp, None
                        viewer_img = ForumImageProcessor._parse_viewer_for_image(resp, target)
                        if viewer_img:
                            h3, d3, _ = await ForumImageProcessor._requests_fetch(session, viewer_img, img_headers, referer or target)
                            if d3 and not str(h3.get('Content-Type','')).startswith('text/html'):
                                return h3 or {}, d3, None

                if is_attachment:
                    headers_fallback, data_fallback, _ = await ForumImageProcessor._requests_fetch(session, target, img_headers, referer)
                    if data_fallback:
                        return headers_fallback or {}, data_fallback, None

            return None, None, "No data"
        except Exception as e:
            return None, None, str(e)

    @staticmethod
    async def process_images(session, soup, base_url, book_assets: list, preloaded_assets: Optional[List[Dict[str, Any]]] = None):
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
            log.info(f"Forum preload map size={len(preload_map)} sample={sample_keys}")

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

                if "/avatar" in full_url or "/avatars/" in full_url:
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
                        log.info(f"✓ Exact match found for {check_url[:80]}")
                        break
                    norm_url = normalize_url_for_matching(check_url)
                    if norm_url and norm_url in preload_map:
                        matched_asset = preload_map[norm_url]
                        log.info(f"✓ Normalized match found for {check_url[:80]}")
                        break

                if matched_asset:
                    img_tag['src'] = matched_asset.filename
                    caption_text = ImageProcessor.find_caption(img_tag)
                    ForumImageProcessor._finalize_image_tag(soup, img_tag, caption_text)
                    return
                else:
                    log.warning(f"✗ No preload match for {full_url[:100]}")

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
                        return

                log.debug(f"Forum fetch image {full_url} (viewer={viewer_url}) not found in preload_map")
                headers, data, err = await ForumImageProcessor.fetch_image_data(session, attach_target, referer=base_url, viewer_url=viewer_url)
                if err:
                    return

                mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(full_url, headers, data)
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

            except Exception as e:
                log.debug(f"Image process error {src}: {e}")

        for img in img_tags:
            tasks.append(_process_tag(img))

        if tasks:
            await tqdm_asyncio.gather(*tasks, desc="Optimizing Images", unit="img", leave=False)

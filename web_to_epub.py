#!/usr/bin/env python
# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "requests>=2.30.0",
#   "aiohttp[speedups]>=3.9.0",
#   "beautifulsoup4>=4.11.0",
#   "EbookLib>=0.18",
#   "trafilatura>=1.6.0",
#   "lxml[html_clean]>=4.9.0",
#   "pygments>=2.14.0",
#   "tqdm>=4.65.0",
#   "Pillow>=9.0.0",
#   "PyYAML>=6.0",
#   "youtube-transcript-api>=0.6.0",
#   "python-dotenv>=1.0.0",
# ]
# ///

import argparse
import sys
import re
import os
import mimetypes
import asyncio
import logging
import random
import json
import html
import hashlib
from urllib.parse import urlparse, parse_qs, urljoin, quote
from datetime import datetime
import time
from contextlib import asynccontextmanager
from bs4 import BeautifulSoup, Tag, Comment
import io
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from abc import ABC, abstractmethod
from yarl import URL
from itertools import islice

# --- Dependency Imports ---
try:
    import requests # Added for fallback image fetching
    import aiohttp
    import yaml
    import socket
    from aiohttp.resolver import ThreadedResolver
    from ebooklib import epub
    import trafilatura
    from pygments import highlight
    from pygments.lexers import get_lexer_by_name, guess_lexer
    from pygments.formatters import HtmlFormatter
    from pygments.util import ClassNotFound
    from tqdm.asyncio import tqdm_asyncio
    from tqdm import tqdm
    from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Error: Missing dependency. Please run with 'uv run'. Details: {e}", file=sys.stderr)
    sys.exit(1)

# Load environment variables from .env file
load_dotenv()

try:
    from PIL import Image as PillowImage
    PillowImage.MAX_IMAGE_PIXELS = None
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
    print("Warning: Pillow not found. Image optimization disabled.", file=sys.stderr)

# --- Constants ---
HN_API_BASE_URL = "https://hacker-news.firebaseio.com/v0/"
HN_ITEM_URL_BASE = "https://news.ycombinator.com/item?id="
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=2)
MAX_RETRIES = 5
IMG_MAX_RETRIES = 1
RETRY_DELAY = 2.0
IMG_RETRY_DELAY = 1.5
IMG_MAX_CANDIDATES = 2
IMG_MAX_PER_IMAGE_SEC = 6
IMAGE_DIR_IN_EPUB = "images"
ALLOWED_IMAGE_MIMES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
ARCHIVE_ORG_API_BASE = "https://archive.org/wayback/available"

# Image Optimization Settings
MAX_IMAGE_DIMENSION = 1000
JPEG_QUALITY = 65

# Concurrency Control
GLOBAL_SEMAPHORE = asyncio.Semaphore(2)

def normalize_url_for_matching(url: str) -> str:
    """Create a canonical form for URL matching."""
    if not url or not isinstance(url, str):
        return ""
    cleaned = url.replace("https://", "").replace("http://", "")
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0]
    cleaned = cleaned.rstrip("/")
    return cleaned.lower()

def urls_match(url1: str, url2: str) -> bool:
    """Check if two URLs represent the same resource."""
    if not url1 or not url2:
        return False
    return normalize_url_for_matching(url1) == normalize_url_for_matching(url2)

# --- Logging ---
_LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _LOGLEVEL, logging.INFO),
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# --- Data Structures ---

@dataclass
class ConversionOptions:
    """Configuration passed from CLI or Server to Drivers."""
    no_article: bool = False
    no_comments: bool = False
    no_images: bool = False
    archive: bool = False
    compact_comments: bool = False
    max_depth: Optional[int] = None
    max_pages: Optional[int] = None
    max_posts: Optional[int] = None
    page_spec: Optional[List[int]] = None
    llm_format: bool = False
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    summary: bool = False

@dataclass
class Source:
    """Represents an input source: URL and optional pre-fetched HTML."""
    url: str
    html: Optional[str] = None
    cookies: Optional[Dict[str, str]] = None
    assets: Optional[List[Dict[str, Any]]] = None
    is_forum: bool = False

@dataclass
class SiteProfile:
    name: str
    domain_patterns: List[str]
    driver_alias: Optional[str] = None
    content_selector: Optional[str] = None
    remove_selectors: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    image_proxy_pattern: Optional[str] = None

class ProfileManager:
    _instance = None
    def __init__(self, config_paths: List[str] = None):
        self.profiles: List[SiteProfile] = []
        if config_paths:
            for path in config_paths:
                self.load_config(path)
    @classmethod
    def get_instance(cls):
        if not cls._instance:
            paths = ["sites.yaml", os.path.expanduser("~/.config/epub_downloader/sites.yaml")]
            cls._instance = cls(paths)
        return cls._instance
    def load_config(self, path: str):
        if not os.path.exists(path): return
        try:
            with open(path, 'r') as f:
                data = yaml.safe_load(f)
                if not data or not isinstance(data, list): return
                for item in data:
                    self.profiles.append(SiteProfile(
                        name=item.get("name", "Unknown"),
                        domain_patterns=item.get("domains", []),
                        driver_alias=item.get("driver"),
                        content_selector=item.get("content_selector"),
                        remove_selectors=item.get("remove", []),
                        headers=item.get("headers", {}),
                        image_proxy_pattern=item.get("image_proxy_pattern")
                    ))
            log.info(f"Loaded {len(data)} profiles from {path}")
        except Exception as e:
            log.warning(f"Failed to load config {path}: {e}")
    def get_profile(self, url: str) -> Optional[SiteProfile]:
        for p in self.profiles:
            for pattern in p.domain_patterns:
                try:
                    if re.search(pattern, url): return p
                except: pass
        return None

@dataclass
class ConversionContext:
    """Context object holding state for the conversion process."""
    session: aiohttp.ClientSession
    options: ConversionOptions
    profile: Optional[SiteProfile] = None

@dataclass
class ImageAsset:
    uid: str
    filename: str
    media_type: str
    content: bytes
    original_url: str
    alt_urls: Optional[List[str]] = None

@dataclass
class Chapter:
    title: str
    filename: str
    content_html: str
    uid: str
    is_article: bool = False
    is_comments: bool = False

@dataclass
class BookData:
    title: str
    author: str
    uid: str
    language: str
    description: str
    source_url: str
    chapters: List[Chapter] = field(default_factory=list)
    images: List[ImageAsset] = field(default_factory=list)
    toc_structure: List[Any] = field(default_factory=list)
    extra_metadata: Dict[str, str] = field(default_factory=dict)

# --- Helper Functions ---

def sanitize_filename(filename):
    if not filename: return "untitled"
    filename = re.sub(r'[\x00-\x1f]', '', filename)
    sanitized = re.sub(r'[<>:"/\\|?*]', '', filename)
    sanitized = re.sub(r'\s+', '_', sanitized).strip('_')
    return sanitized[:150]

def parse_page_spec(spec: str) -> Optional[List[int]]:
    if not spec: return None
    pages = set()
    for part in spec.split(','):
        part = part.strip()
        if not part: continue
        if '-' in part:
            try:
                start, end = part.split('-')
                start, end = int(start), int(end)
                if start > end: start, end = end, start
                pages.update(range(start, end + 1))
            except: continue
        else:
            try:
                pages.add(int(part))
            except: continue
    if not pages: return None
    return sorted(p for p in pages if p > 0)

def load_cookie_file(path: str) -> List[Dict[str, str]]:
    """Parse Netscape cookie file format into a list of dict entries."""
    cookies = []
    if not path or not os.path.exists(path):
        return cookies
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if not line or line.startswith('#'): continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    domain, _, _, _, _, name, value = parts[:7]
                    cookies.append({"domain": domain.lstrip('.'), "name": name, "value": value})
    except Exception as e:
        log.warning(f"Failed to parse cookies file {path}: {e}")
    return cookies

@asynccontextmanager
async def get_session():
    # Use threaded DNS to avoid pycares issues on Termux/Android and force IPv4 where needed
    connector = aiohttp.TCPConnector(
        resolver=ThreadedResolver(),
        ttl_dns_cache=300,
        family=socket.AF_INET
    )
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, connector=connector) as session:
        yield session

async def fetch_with_retry(
    session,
    url,
    response_type='json',
    allow_redirects=True,
    referer=None,
    non_retry_statuses: Optional[set] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    max_retries: int = MAX_RETRIES,
    backoff: float = RETRY_DELAY,
    timeout=None
):
    final_url = url
    for attempt in range(max_retries):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            if referer:
                headers['Referer'] = referer
            if extra_headers:
                headers.update(extra_headers)

            async with session.get(url, allow_redirects=allow_redirects, headers=headers, timeout=timeout or REQUEST_TIMEOUT) as response:
                final_url = str(response.url)

                if response.status == 429:
                    retry_after = int(response.headers.get("Retry-After", 10))
                    wait_time = max(retry_after, backoff * (2 ** attempt))
                    log.warning(f"Rate limit hit (429). Cooling down for {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue

                if non_retry_statuses and response.status in non_retry_statuses:
                    log.warning(f"Non-retryable HTTP {response.status} for {url}")
                    return None, final_url

                if response.status >= 400:
                    if response.status == 404: return None, final_url
                    log.warning(f"HTTP {response.status} for {url}")

                response.raise_for_status()

                if response_type == 'json': return await response.json(), final_url
                elif response_type == 'bytes': return await response.read(), final_url
                elif response_type == 'text': return await response.text(encoding='utf-8', errors='replace'), final_url
                elif response_type == 'headers': return response.headers, final_url
                else: return response, final_url

        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as e:
            wait = backoff * (2 ** attempt)
            log.warning(f"Attempt {attempt + 1}/{max_retries} failed for {url}: {e}. Retrying in {wait}s.")
            if attempt + 1 == max_retries: return None, url
            await asyncio.sleep(wait)
        except Exception as e:
            log.error(f"Unexpected error for {url}: {e}")
            if attempt + 1 == max_retries: return None, url
            await asyncio.sleep(backoff * (2 ** attempt))
    return None, url

# --- Article Extraction Logic ---

class ArticleExtractor:
    @staticmethod
    def build_meta_block(url: str, data: dict, context: Optional[str] = None, summary_html: Optional[str] = None) -> str:
        """Shared article metadata block with source, author, date, site, archive info."""
        author = data.get('author') or 'Unknown'
        date = data.get('date') or 'Unknown'
        site = data.get('sitename') or urlparse(url).netloc or 'Unknown'
        rows = [
            f"<p><strong>Article Source:</strong> <a href=\"{url}\">{url}</a></p>",
            f"<p><strong>Article Author:</strong> {author} | <strong>Article Date:</strong> {date} | <strong>Site:</strong> {site}</p>",
        ]
        if context:
            rows.append(context)
        if data.get('was_archived') and data.get('archive_url'):
            rows.append(f"<p class=\"archive-notice\">Archived: <a href=\"{data['archive_url']}\">{data['archive_url']}</a></p>")
        
        if summary_html:
            rows.append(f"<div class='ai-summary'><h3>AI Summary</h3>{summary_html}</div>")

        return "<div class=\"post-meta\">" + "".join(rows) + "</div>"

    @staticmethod
    async def _requests_fetch(session, url):
        try:
            import requests
            cookie_dict = {}
            try:
                jar = session.cookie_jar.filter_cookies(URL(url))
                cookie_dict = {k: v.value for k, v in jar.items()}
            except Exception:
                pass
            extra = getattr(session, "_extra_cookies", None)
            if isinstance(extra, dict):
                cookie_dict.update(extra)
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }

            loop = asyncio.get_running_loop()
            def _do_req():
                return requests.get(url, headers=headers, cookies=cookie_dict, timeout=20, allow_redirects=True)
            
            resp = await loop.run_in_executor(None, _do_req)
            if resp.status_code == 200 and resp.text:
                return resp.text, resp.url
        except Exception as e:
            log.debug(f"Article requests fetch failed for {url}: {e}")
        return None, None

    @staticmethod
    def extract_from_html(html_content, url, profile: Optional[SiteProfile] = None):
        try:
            metadata = trafilatura.extract_metadata(html_content)
            soup = BeautifulSoup(html_content, 'lxml')
            
            # Use profile-specific remove selectors if provided
            if profile and profile.remove_selectors:
                for selector in profile.remove_selectors:
                    for element in soup.select(selector):
                        element.decompose()

            # Prefer profile-specific content selector
            content_soup = None
            if profile and profile.content_selector:
                content_soup = soup.select_one(profile.content_selector)
                if content_soup:
                    log.info(f"Found content using profile selector: '{profile.content_selector}'")
            
            if not content_soup:
                content_soup = ArticleExtractor._smart_selector_extract(soup)

            extracted_html = None
            if content_soup:
                ArticleExtractor._clean_soup(content_soup)
                extracted_html = content_soup.prettify()
            else:
                log.info("Selectors failed, falling back to Trafilatura extraction.")
                extracted_html = trafilatura.extract(html_content, include_images=True, include_tables=True, output_format='html')

            if not extracted_html or len(extracted_html) < 50:
                if soup.body and len(soup.body.get_text()) > 100:
                     log.warning("Extraction returned empty. Using full body as fallback.")
                     ArticleExtractor._clean_soup(soup.body)
                     extracted_html = soup.body.prettify()
                else:
                    raise ValueError("Content too short")

            return {
                'success': True,
                'title': metadata.title if metadata else None,
                'author': metadata.author if metadata else None,
                'date': metadata.date if metadata else None,
                'sitename': metadata.sitename if metadata else None,
                'html': extracted_html,
                'error': None
            }
        except Exception as e:
            return {'success': False, 'html': None, 'error': str(e)}

    @staticmethod
    def _smart_selector_extract(soup):
        selectors = ['article', '[data-qa="article-body"]', '[role="main"]', '.main-content', '.post-content', '.entry-content', '#main', '#content', '.article-body', '.storycontent']
        for selector in selectors:
            found = soup.select_one(selector)
            if found and len(found.get_text(strip=True)) > 200:
                log.info(f"Found content using selector: '{selector}'")
                return found
        return None

    @staticmethod
    def _clean_soup(soup):
        # FIX: Removed 'header' from the kill list. NYTimes puts the main image inside a <header> within the <article>.
        for tag in soup(['script', 'style', 'noscript', 'iframe', 'footer', 'nav', 'aside', 'form', 'button', 'svg']):
            tag.decompose()
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Allow data-* and size attributes
        allowed_attrs = {'src', 'href', 'alt', 'title', 'id', 'colspan', 'rowspan', 'srcset', 'width', 'height', 'loading'}

        for tag in soup.find_all(True):
            if not hasattr(tag, 'attrs') or not tag.attrs: continue
            attrs = list(tag.attrs.keys())
            for attr in attrs:
                if attr not in allowed_attrs and not attr.startswith('data-'):
                    del tag[attr]
            if tag.name == 'div' and not tag.get_text(strip=True) and not tag.find(['img', 'figure']):
                if tag.has_attr('id'): continue # Preserve potential placeholders
                tag.decompose()

    @staticmethod
    async def get_wayback_url(session, target_url):
        api_url = f"{ARCHIVE_ORG_API_BASE}?url={quote(target_url)}"
        try:
            data, _ = await fetch_with_retry(session, api_url, 'json')
            if data and data.get('archived_snapshots', {}).get('closest', {}).get('available'):
                snap = data['archived_snapshots']['closest']['url']
                if snap.startswith('http:'): snap = snap.replace('http:', 'https:', 1)
                return snap
        except Exception: pass
        return None

    @staticmethod
    async def get_article_content(session, url, force_archive=False, raw_html=None, profile: Optional[SiteProfile] = None):
        result = {'success': False, 'html': None, 'title': None, 'author': None, 'date': None, 'sitename': None, 'was_archived': False, 'archive_url': None}
        loop = asyncio.get_running_loop()

        if raw_html:
            log.info("Using pre-fetched HTML content.")
            extracted = await loop.run_in_executor(None, ArticleExtractor.extract_from_html, raw_html, url, profile)
            if extracted['success']:
                result.update(extracted)
                result['raw_html_for_metadata'] = raw_html
                result['source_url'] = url
                return result

        if force_archive:
            snap_url = await ArticleExtractor.get_wayback_url(session, url)
            if snap_url:
                raw_html, final_url = await fetch_with_retry(session, snap_url, 'text')
                if raw_html:
                    extracted = await loop.run_in_executor(None, ArticleExtractor.extract_from_html, raw_html, url, profile)
                    result.update(extracted)
                    result['was_archived'] = True
                    result['archive_url'] = final_url
                    if not result['success']: result['html'] = raw_html
            return result

        # Treat 403 as non-retryable to fast-fail to archive fallback
        raw_html, final_url = await fetch_with_retry(session, url, 'text', non_retry_statuses={403}, max_retries=1)

        if not raw_html:
             log.warning(f"aiohttp failed for {url}, trying requests fallback...")
             req_html, req_url = await ArticleExtractor._requests_fetch(session, url)
             if req_html:
                 raw_html = req_html
                 final_url = req_url

        if raw_html:
             extracted = await loop.run_in_executor(None, ArticleExtractor.extract_from_html, raw_html, url, profile)
             if extracted['success']:
                 result.update(extracted)
                 result['raw_html_for_metadata'] = raw_html
                 return result

        log.warning(f"Live fetch failed. Trying archive...")
        return await ArticleExtractor.get_article_content(session, url, force_archive=True, profile=profile)

# --- Image Processing ---

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

        parent = wrapper.parent
        while parent and parent.name in ("div", "section"):
            for fc in list(parent.find_all("figcaption", recursive=False)):
                fc.decompose()
            children = [c for c in parent.contents if not (isinstance(c, str) and not c.strip())]
            dataid = (parent.get("data-testid") or "").lower()
            if len(children) == 1 and children[0] is wrapper and (dataid.startswith("imageblock") or dataid.startswith("photoviewer")):
                parent.unwrap()
                parent = wrapper.parent
                continue
            break

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
                # If we get here, it implies fetch_with_retry returned None (failed after retries or hit non_retry_status)
                # Mark specific reasons for immediate fallback after first ref attempt
                if attempt_idx == 0 and (not headers or not data):
                    aiohttp_fail_reason = "Failed on first aiohttp attempt (403/Timeout/Error)"
                    break # Break out of referer loop
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                if attempt_idx == 0: # If the first attempt fails with ClientError/Timeout
                    aiohttp_fail_reason = "ClientError or Timeout on first aiohttp attempt"
                    break # Break out of referer loop
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
            
            # Profile-based proxy detection
            if profile and profile.image_proxy_pattern and profile.image_proxy_pattern in (parsed.path or ""):
                return qs.get("src") or qs.get("url") or qs.get("original")

            # Common proxy patterns
            if "imrs.php" in (parsed.path or "") or "resizer" in (parsed.path or parsed.netloc) or "proxy" in (parsed.path or parsed.netloc):
                return qs.get("src") or qs.get("url") or qs.get("original")
            # Next.js images often use query params for sizing but actual src is direct
            if parsed.netloc and qs.keys() & {"w", "q", "fit", "h", "fm"}: # common Next.js image optimizer params
                # If the path looks like a direct image and not a generic proxy path
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
            
            # 1. Try standard Arc XP / WaPo path first (Most reliable)
            props = data.get("props", {}).get("pageProps", {})
            elems = props.get("globalContent", {}).get("content_elements", [])

            # 2. If not found, look for *any* key named 'content_elements' that is a list
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
                # Pick the longest list found (heuristic: main article is longer than sidebars)
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
                
                # Prefer direct URL (el.get("url"))
                origin = el.get("url")
                if not origin: continue

                # Try to extract a cleaner origin from the found URL first, then use it
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
                
                # Try to find a placeholder in the body_soup by content ID (relaxed search)
                target_tag = body_soup.find(id=el.get("_id"))
                if not target_tag:
                    target_tag = body_soup.find(attrs={"data-id": el.get("_id")})
                if not target_tag:
                    target_tag = body_soup.find(attrs={"data-uuid": el.get("_id")})
                
                if target_tag:
                    # Construct the image block according to guidelines
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
                    # Fallback
                    img_tag = body_soup.new_tag("img", attrs={"src": fname, "class": "epub-image"})
                    
                    if is_first_image and not lede_candidate:
                        # Defer lede insertion
                        lede_candidate = (img_tag, cap_text, origin)
                        added += 1
                    else:
                        # Append subsequent unmatched images
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
                # remove any leftover figcaptions after injection
                for fc in list(body_soup.find_all("figcaption")):
                    fc.decompose()
                log.info(f"Seeded {added} Next.js images from __NEXT_DATA__")
        except Exception as e:
            log.debug(f"WaPo __NEXT_DATA__ seed failed: {e}")

    @staticmethod
    async def process_images(session, soup, base_url, book_assets: list, profile: Optional[SiteProfile] = None):
        # Flatten/remove known wrapper containers and stray labels (NYT et al.) before processing
        for wrapper in list(soup.find_all("div")):
            dataid = (wrapper.get("data-testid") or "").lower()
            if dataid.startswith(("imageblock", "photoviewer")):
                meaningful = [c for c in wrapper.contents if not (isinstance(c, str) and not c.strip())]
                if len(meaningful) == 1:
                    wrapper.unwrap()
        for label in soup.find_all("span"):
            if (label.get_text(strip=True) or "").lower() == "image":
                # drop decorative "Image" labels around photos
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
            # Skip if already processed (e.g. by seeding)
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

            # Prefer widest WaPo imrs srcset entry, and collect origin src for proxies
            wapo_origin_seed = None
            for srcset_candidate in [data_srcset, srcset]:
                if srcset_candidate and "washingtonpost.com/wp-apps/imrs.php" in srcset_candidate:
                    parsed_set = ImageProcessor.parse_srcset(srcset_candidate)
                    if parsed_set:
                        final_src = parsed_set[0] if not final_src else final_src
                        # extract origin from widest entry
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

                # Build fetch candidates from src and any srcset entries (plus queryless variants)
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

                # Seed lede image candidates if available
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

                # Always prefer direct origin if found from proxy
                if origin_src:
                    _add_candidate(origin_src, prepend=True)
                
                # Add the full URL, and its query-stripped version if it contains a query string
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

                # Final pass: ensure origin versions of any known proxies are prepended
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

        # Remove any remaining figcaptions after images are wrapped/captioned
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





                resp = requests.get(target, headers={**img_headers, "Referer": referer or ""}, cookies=cookie_dict, timeout=20, allow_redirects=True)





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


        # Map existing assets (pre-seeded in driver) to URLs for quick rewrites


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


            # Secondary: query-stripped attachment mapping


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





        # Add viewer/canonical hints from preloaded metadata to existing assets


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





        # Remove iframe/video wrappers; keep a link instead


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

# --- Drivers ---

class BaseDriver(ABC):
    @abstractmethod
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        pass

class SubstackDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        log.info(f"Substack Driver processing: {url}")

        data = await ArticleExtractor.get_article_content(session, url, force_archive=options.archive, raw_html=source.html, profile=context.profile)
        if not data['success']:
            log.error(f"Failed to fetch Substack content: {url}")
            return None

        raw_html = data.get('raw_html_for_metadata') or data.get('html', '')
        soup = BeautifulSoup(data['html'], 'html.parser')
        post_id, pub_id, subdomain = self._extract_all_metadata(soup, raw_html)

        if not post_id:
            log.info("Metadata extraction incomplete. Trying API slug lookup...")
            base_url = self._extract_base_url(url)
            api_post, api_pub, api_sub = await self._fetch_ids_from_slug(url, base_url, session)
            if api_post: post_id = api_post
            if api_pub: pub_id = api_pub
            if api_sub and not subdomain: subdomain = api_sub

        if not post_id:
            log.error(f"CRITICAL: Could not find Post ID for {url}. Comments cannot be fetched.")
        else:
            log.info(f"Targeting Post ID: {post_id} (Pub ID: {pub_id}, Subdomain: {subdomain})")

        body_soup = soup.body if soup.body else soup
        assets = []
        if not options.no_images:
            base = data.get('archive_url') if data.get('was_archived') else data.get('source_url', url)
            await ImageProcessor.process_images(session, body_soup, base, assets)

        summary_html = None
        if options.summary:
            log.info("Generating AI summary for Substack...")
            text_content = body_soup.get_text(separator=" ", strip=True)
            summary_html = await LLMHelper.generate_summary(text_content, options.llm_model, options.llm_api_key)

        title = data['title'] or "Substack Article"
        chapter_html = body_soup.prettify()
        meta_html = ArticleExtractor.build_meta_block(url, data, summary_html=summary_html)

        chapters = []
        final_art_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head>
        <body><h1>{title}</h1>{meta_html}<hr/>{chapter_html}</body></html>"""

        art_chap = Chapter(title=title, filename=f"article_{post_id}.xhtml", content_html=final_art_html, uid=f"art_{post_id}", is_article=True)
        chapters.append(art_chap)

        comments_html = ""
        if post_id and not options.no_comments:
            primary_domain = self._extract_base_url(url)
            raw_comments = await self._fetch_comments(primary_domain, post_id, pub_id, session)

            if (not raw_comments) and subdomain and subdomain not in primary_domain:
                fallback_domain = f"https://{subdomain}.substack.com"
                log.info(f"Primary API empty. Retrying on native domain: {fallback_domain}")
                raw_comments = await self._fetch_comments(fallback_domain, post_id, pub_id, session, force_referer=fallback_domain)

            if not raw_comments:
                 global_domain = "https://substack.com"
                 log.info(f"Native API empty. Retrying on GLOBAL domain: {global_domain}")
                 raw_comments = await self._fetch_comments(global_domain, post_id, pub_id, session, force_referer=primary_domain)

            if raw_comments:
                raw_nodes = self._normalize_substack_tree(raw_comments)
                enriched_roots = _enrich_comment_tree(raw_nodes)
                fmt = HtmlFormatter(style='default', cssclass='codehilite', noclasses=False)

                chunks = []
                for i, comment in enumerate(enriched_roots):
                    chunks.append(f"<div class='thread-container'>")
                    chunks.append(format_comment_html(comment, fmt))
                    chunks.append("</div>")

                comments_html = "".join(chunks)
            else:
                if not options.no_comments:
                    log.warning("Comment list is empty (all fallback methods failed).")

        com_chap = None
        if comments_html:
             full_com_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Comments</title><link rel="stylesheet" href="style/default.css"/></head><body>
             <h1>Comments</h1>{comments_html}</body></html>"""
             com_chap = Chapter(title="Comments", filename=f"comments_{post_id}.xhtml", content_html=full_com_html, uid=f"com_{post_id}", is_comments=True)
             chapters.append(com_chap)

        toc_structure = []
        if art_chap and com_chap:
             toc_structure.append((epub.Link(art_chap.filename, "Article", art_chap.uid), [epub.Link(com_chap.filename, "Comments", com_chap.uid)]))
        elif art_chap:
             toc_structure.append(epub.Link(art_chap.filename, "Article", art_chap.uid))

        return BookData(title=title, author=data['author'] or "Substack", uid=f"urn:substack:{post_id or abs(hash(url))}", language='en', description=f"Substack Post {url}", source_url=url, chapters=chapters, images=assets, toc_structure=toc_structure)

    def _extract_base_url(self, url):
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _extract_all_metadata(self, soup, html) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        post_id, pub_id, subdomain = None, None, None
        try:
            pattern = r'window\._preloads\s*=\s*JSON\.parse\((["\'].*?["\'])\)'
            match = re.search(pattern, html, re.DOTALL)
            if match:
                import json
                inner = json.loads(match.group(1))
                data = json.loads(inner)
                if 'post' in data: post_id = str(data['post'].get('id'))
                if 'pub' in data:
                    pub_id = str(data['pub'].get('id'))
                    subdomain = data['pub'].get('subdomain')
                elif 'publication' in data:
                    pub_id = str(data['publication'].get('id'))
                    subdomain = data['publication'].get('subdomain')
        except Exception: pass
        if not post_id:
            m = soup.find("meta", attrs={"name": "substack:post_id"})
            if m: post_id = m.get("content")
        if not pub_id:
            m = soup.find("meta", attrs={"name": "substack:publication_id"})
            if m: pub_id = m.get("content")
        if not subdomain:
             og = soup.find("meta", attrs={"property": "og:url"})
             if og and "substack.com" in str(og.get("content")):
                 p = urlparse(og.get("content"))
                 parts = p.netloc.split('.')
                 if len(parts) >= 3: subdomain = parts[0]
        return post_id, pub_id, subdomain

    async def _fetch_ids_from_slug(self, url, base_url, session) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        path = urlparse(url).path
        match = re.search(r'/(?:p|in)/([^/]+)', path)
        if not match: return None, None, None
        slug = match.group(1)
        api_url = f"{base_url}/api/v1/posts/{slug}"
        try:
            data, _ = await fetch_with_retry(session, api_url, 'json')
            if data:
                pub_data = data.get('publication', {})
                return str(data.get('id')), str(data.get('publication_id')), pub_data.get('subdomain')
        except Exception: pass
        return None, None, None

    async def _fetch_comments(self, base_url, post_id, pub_id, session, force_referer=None):
        comments = []
        offset = 0
        ref_base = force_referer if force_referer else base_url
        headers = {
            "Accept": "application/json",
            "Referer": f"{ref_base}/p/{post_id}",
        }
        if pub_id: headers["x-pub-context"] = str(pub_id)

        active_endpoint = None
        candidates = [f"/api/v1/posts/{post_id}/comments", f"/api/v1/post/{post_id}/comments"]
        for ep in candidates:
             try:
                test_url = f"{base_url}{ep}?limit=1&sort=new"
                async with session.get(test_url, headers=headers) as resp:
                    if resp.status == 200:
                        active_endpoint = ep
                        log.info(f"Found valid endpoint: {ep}")
                        break
             except: pass

        if not active_endpoint: return None
        api_url = f"{base_url}{active_endpoint}"

        while True:
            try:
                await asyncio.sleep(random.uniform(0.5, 1.0))
                full_url = f"{api_url}?limit=50&offset={offset}&sort=new"
                async with session.get(full_url, headers=headers) as response:
                    if response.status == 404: return None
                    if response.status != 200:
                        log.debug(f"API Status {response.status}")
                        break
                    ctype = response.headers.get("Content-Type", "")
                    if "application/json" not in ctype: break
                    data = await response.json()
                if not data or 'comments' not in data: break
                batch = data['comments']
                if not batch: break
                comments.extend(batch)
                if not data.get('has_more'): break
                offset += len(batch)
            except Exception: break
        return comments

    def _normalize_substack_tree(self, raw_roots):
        normalized = []
        count = 0
        def recurse(node):
            nonlocal count
            count += 1
            text = node.get('body_html') or node.get('body') or ""
            author = node.get('name')
            if not author and 'user' in node:
                author = node['user'].get('name')
            if not author: author = 'Anonymous'

            norm_node = {
                'id': str(node.get('id')),
                'by': author,
                'text': text,
                'time': self._iso_to_unix(node.get('date')),
                'children_data': []
            }
            if 'children' in node and isinstance(node['children'], list):
                for child in node['children']:
                    norm_node['children_data'].append(recurse(child))
            return norm_node

        for root in raw_roots:
            normalized.append(recurse(root))
        log.info(f"Deep search found {count} total comments (including replies).")
        return normalized

    def _iso_to_unix(self, iso_str):
        if not iso_str: return 0
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except: return 0

class WordPressDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        log.info(f"WordPress Driver processing: {url}")

        data = await ArticleExtractor.get_article_content(session, url, force_archive=options.archive, raw_html=source.html, profile=context.profile)
        if not data['success']:
            log.error(f"Failed to fetch content: {url}")
            return None

        title = data['title'] or "WordPress Article"
        soup = BeautifulSoup(data['html'], 'html.parser')
        body_soup = soup.body if soup.body else soup

        assets = []
        if not options.no_images:
            base = data.get('archive_url') if data.get('was_archived') else data.get('source_url', url)
            await ImageProcessor.process_images(session, body_soup, base, assets)

        summary_html = None
        if options.summary:
            log.info("Generating AI summary for WordPress...")
            text_content = body_soup.get_text(separator=" ", strip=True)
            summary_html = await LLMHelper.generate_summary(text_content, options.llm_model, options.llm_api_key)

        chapter_html = body_soup.prettify()
        meta_html = ArticleExtractor.build_meta_block(url, data, summary_html=summary_html)
        final_art_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head>
        <body><h1>{title}</h1>{meta_html}<hr/>{chapter_html}</body></html>"""
        
        uid = f"urn:wordpress:{abs(hash(url))}"
        art_chap = Chapter(title=title, filename="article.xhtml", content_html=final_art_html, uid="article", is_article=True)
        chapters = [art_chap]

        comments_html = ""
        if not options.no_comments:
            raw = data.get('raw_html_for_metadata') or source.html
            if raw:
                full_soup = BeautifulSoup(raw, 'html.parser')
                comment_list = full_soup.select_one('ol.comment-list, ul.comment-list, .commentlist')
                if comment_list:
                    comments = self._parse_comments(comment_list)
                    enriched = _enrich_comment_tree(comments)
                    fmt = HtmlFormatter(style='default', cssclass='codehilite', noclasses=False)
                    chunks = []
                    for c in enriched:
                        chunks.append("<div class='thread-container'>")
                        chunks.append(format_comment_html(c, fmt))
                        chunks.append("</div>")
                    comments_html = "".join(chunks)

        com_chap = None
        if comments_html:
             full_com_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Comments</title><link rel="stylesheet" href="style/default.css"/></head><body>
             <h1>Comments</h1>{comments_html}</body></html>"""
             com_chap = Chapter(title="Comments", filename="comments.xhtml", content_html=full_com_html, uid="comments", is_comments=True)
             chapters.append(com_chap)

        toc = [epub.Link("article.xhtml", "Article", "article")]
        if com_chap:
            toc.append(epub.Link("comments.xhtml", "Comments", "comments"))

        return BookData(title=title, author=data['author'] or "WordPress", uid=uid, language='en', description=f"Source: {url}", source_url=url, chapters=chapters, images=assets, toc_structure=toc)

    def _parse_comments(self, element):
        results = []
        for li in element.find_all('li', recursive=False):
            classes = li.get("class", [])
            if "comment" not in classes and "pingback" not in classes: continue
            
            # Author: Try standard, then relaxed selectors
            author_tag = li.select_one('.comment-author .fn, .comment-author cite, .comment-meta .fn, cite.fn')
            
            # Date: Try time tag, then fallback to link text in metadata
            date_tag = li.select_one('.comment-metadata time, .comment-meta time')
            
            author = author_tag.get_text(strip=True) if author_tag else "Anonymous"
            
            time_val = 0
            if date_tag and date_tag.has_attr('datetime'):
                try:
                    dt_str = date_tag['datetime'].replace("Z", "+00:00")
                    dt = datetime.fromisoformat(dt_str)
                    time_val = dt.timestamp()
                except: pass
            elif not time_val:
                # Fallback: look for a date link text (common in older WP themes)
                date_link = li.select_one('.comment-metadata a, .comment-meta a')
                if date_link:
                    dtext = date_link.get_text(strip=True)
                    try:
                        # Try parsing common WP date formats "November 27, 2025 at 12:38 am"
                        # This is a bit brittle but better than 0
                        # Remove "at" if present
                        clean_dtext = dtext.replace(" at ", " ")
                        dt = datetime.strptime(clean_dtext, "%B %d, %Y %I:%M %p")
                        time_val = dt.timestamp()
                    except:
                        pass

            # Prefer content only to avoid duplicating metadata (avatars)
            body = li.select_one('.comment-content')
            if not body:
                body = li.select_one('.comment-body')
                if body:
                    # If falling back to wrapper, remove metadata elements
                    for meta in body.select('footer, .comment-meta, .comment-author'):
                        meta.decompose()

            text = ""
            if body:
                for junk in body.select('.reply, .comment-likes, .comment-like-link, .comment-like-feedback, .wd-public-likes, .like-button'):
                    junk.decompose()
                text = str(body)
            
            node = {
                'id': li.get('id', f"c_{abs(hash(text))}"),
                'by': author,
                'text': text,
                'time': time_val,
                'children_data': []
            }
            
            children_ol = li.select_one('ol.children, ul.children')
            if children_ol:
                node['children_data'] = self._parse_comments(children_ol)
            
            results.append(node)
        return results

class GenericDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        log.info(f"Generic Driver processing: {url}")
        data = await ArticleExtractor.get_article_content(session, url, force_archive=options.archive, raw_html=source.html, profile=context.profile)
        if not data['success']:
            log.error(f"Failed to fetch content for {url}")
            return None

        raw_html = data.get('raw_html_for_metadata') or data.get('html', '')
        
        if 'substack:post_id' in raw_html:
             log.info("Detected Substack metadata after fetch. Switching to SubstackDriver.")
             return await SubstackDriver().prepare_book_data(context, source)

        if 'data-template="thread_view"' in raw_html or 'xenforo' in raw_html.lower():
             log.info("Detected Forum metadata after fetch. Switching to ForumDriver.")
             return await ForumDriver().prepare_book_data(context, source)

        title = data['title'] or "Untitled Webpage"
        soup = BeautifulSoup(data['html'], 'html.parser')
        body_soup = soup.body if soup.body else soup

        assets = []
        if not options.no_images:
            base = data.get('archive_url') if data.get('was_archived') else data.get('source_url', url)
            
            # Try to seed from __NEXT_DATA__ first (Fastest/Highest Quality for Next.js)
            if raw_html and "__NEXT_DATA__" in raw_html:
                log.info("Attempting to seed from __NEXT_DATA__ first.")
                await ImageProcessor._seed_images_from_nextjs_data(raw_html, body_soup, base, assets, session, profile=context.profile)

            await ImageProcessor.process_images(session, body_soup, base, assets, profile=context.profile)
            
            # If we downloaded assets but no <img> tags survived extraction, inject them now at bottom
            if assets and not body_soup.find('img'):
                for asset in assets:
                    wrapper = body_soup.new_tag("div", attrs={"class": "img-block"})
                    img_tag = body_soup.new_tag("img", attrs={"src": asset.filename, "class": "epub-image"})
                    wrapper.append(img_tag)
                    body_soup.append(wrapper)
                # remove any figcaptions after injection
                for fc in list(body_soup.find_all("figcaption")):
                    fc.decompose()

        # Final cleanup: remove any remaining empty divs (unused placeholders)
        for tag in body_soup.find_all('div'):
            if not tag.get_text(strip=True) and not tag.find(['img', 'figure']):
                tag.decompose()

        summary_html = None
        if options.summary:
            log.info("Generating AI summary...")
            text_content = body_soup.get_text(separator=" ", strip=True)
            summary_html = await LLMHelper.generate_summary(text_content, options.llm_model, options.llm_api_key)

        chapter_html = body_soup.prettify()
        meta_html = ArticleExtractor.build_meta_block(url, data, summary_html=summary_html)

        final_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head>
        <body><h1>{title}</h1>{meta_html}<hr/>{chapter_html}</body></html>"""

        chapter = Chapter(title=title, filename="index.xhtml", content_html=final_html, uid="chap_index", is_article=True)

        return BookData(
            title=title, author=data['author'] or "Webpage", uid=f"urn:web:{abs(hash(url))}",
            language='en', description=f"Content from {url}", source_url=url,
            chapters=[chapter], images=assets, toc_structure=[epub.Link("index.xhtml", title, "chap_index")]
        )

class HackerNewsDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        try:
            q = parse_qs(urlparse(url).query)
            item_id = q['id'][0]
        except:
            log.error("Invalid HN URL"); return None

        log.info(f"Fetching HN Item {item_id}")
        api_url = f"{HN_API_BASE_URL}item/{item_id}.json"
        post_data, _ = await fetch_with_retry(session, api_url)
        if not post_data: return None

        title = post_data.get('title', f"HN Post {item_id}")
        author = post_data.get('by', 'Hacker News')

        chapters, assets, toc_links = [], [], []
        article_url = post_data.get('url')
        post_text = post_data.get('text')

        comments_html = ""
        art_chap = None

        if (article_url and not options.no_article) or post_text:
            art_title = title
            summary_html = None
            
            if article_url and not options.no_article:
                art_data = await ArticleExtractor.get_article_content(session, article_url, force_archive=options.archive, raw_html=source.html if not article_url else None, profile=context.profile)
                if art_data['success']:
                    if art_data['title']: art_title = art_data['title']
                    soup = BeautifulSoup(art_data['html'], 'html.parser')
                    body = soup.body if soup.body else soup
                    
                    if options.summary:
                        log.info("Generating AI summary for HN Link...")
                        txt = body.get_text(separator=" ", strip=True)
                        summary_html = await LLMHelper.generate_summary(txt, options.llm_model, options.llm_api_key)

                    if not options.no_images:
                        base = art_data.get('archive_url') if art_data.get('was_archived') else article_url
                        await ImageProcessor.process_images(session, body, base, assets)
                    art_html = body.prettify()
                    context = f"<p><strong>HN Source:</strong> <a href=\"{url}\">{title}</a></p>"
                    meta_html = ArticleExtractor.build_meta_block(article_url, art_data, context=context, summary_html=summary_html)
                    art_html = f"{meta_html}<hr/>{art_html}"
                else: art_html = f"<p>Could not fetch article: <a href='{article_url}'>{article_url}</a></p>"
            elif post_text:
                if options.summary:
                    log.info("Generating AI summary for HN Self Text...")
                    summary_html = await LLMHelper.generate_summary(post_text, options.llm_model, options.llm_api_key)
                
                sum_div = f"<div class='ai-summary'><h3>AI Summary</h3>{summary_html}</div><hr/>" if summary_html else ""
                art_html = f"{sum_div}<div>{post_text}</div>"
            else: art_html = ""

            final_art_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{art_title}</title><link rel="stylesheet" href="style/default.css"/></head><body>
            <h1>{art_title}</h1>{art_html}</body></html>"""

            art_chap = Chapter(title=art_title, filename="article.xhtml", content_html=final_art_html, uid="article", is_article=True)
            chapters.append(art_chap)

        if post_data.get('kids') and not options.no_comments:
            kids = post_data['kids']
            fetched_comments = {}
            raw_comments = await fetch_comments_recursive(session, kids, fetched_comments, options.max_depth)
            top_comments = sorted([c for c in raw_comments if c], key=lambda c: c.get('time', 0))
            enriched_roots = _enrich_comment_tree(top_comments)

            fmt = HtmlFormatter(style='default', cssclass='codehilite', noclasses=False)
            chunks = []
            for i, comment in enumerate(enriched_roots):
                chunks.append(f"<div class='thread-container'>")
                chunks.append(format_comment_html(comment, fmt))
                chunks.append("</div>")
            comments_html = "".join(chunks)

        com_chap = None
        if comments_html:
             full_com_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Comments</title><link rel="stylesheet" href="style/default.css"/></head><body>
             <h1>Comments</h1>{comments_html}</body></html>"""
             com_chap = Chapter(title="Comments", filename="comments.xhtml", content_html=full_com_html, uid="comments", is_comments=True)
             chapters.append(com_chap)

        toc_structure = []
        if art_chap and com_chap:
            toc_structure.append((epub.Link(art_chap.filename, "Article", art_chap.uid), [epub.Link(com_chap.filename, "Comments", com_chap.uid)]))
        elif art_chap:
            toc_structure.append(epub.Link(art_chap.filename, "Article", art_chap.uid))
        elif com_chap:
            toc_structure.append(epub.Link(com_chap.filename, "Comments", com_chap.uid))

        return BookData(title=title, author=author, uid=f"urn:hn:{item_id}", language='en', description=f"HN Thread {item_id}", source_url=url, chapters=chapters, images=assets, toc_structure=toc_structure)

class RedditDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        api_url = self._build_api_url(source.url)
        log.info(f"Reddit Driver processing: {api_url}")

        payload, final_url = await fetch_with_retry(session, api_url, 'json')
        if not payload or not isinstance(payload, list) or len(payload) < 2:
            log.error("Failed to fetch Reddit thread JSON")
            return None

        post_listing = payload[0].get("data", {}).get("children", [])
        if not post_listing:
            log.error("No post data in Reddit response")
            return None
        post_data = post_listing[0].get("data", {})

        # Handle crossposts: if it's a crosspost, use the original post's data
        if post_data.get("crosspost_parent_list"):
            log.info(f"Detected crosspost for {source.url}. Using original post data.")
            # Reddit API returns a list of crosspost parents, usually just one
            post_data = post_data["crosspost_parent_list"][0]

        post_id = post_data.get("id") or abs(hash(source.url))
        title = post_data.get("title") or "Reddit Thread"
        author = f"u/{post_data.get('author')}" if post_data.get("author") else "Reddit"
        subreddit = post_data.get("subreddit")

        chapters, assets = [], []
        art_chap = None

        if not options.no_article:
            selftext_html = post_data.get("selftext_html")
            link_url = post_data.get("url")
            article_html = ""
            chapter_title = title
            is_image_link = link_url and re.search(r'\.(jpe?g|png|webp|gif)(\?|$)', link_url, re.IGNORECASE)
            summary_html = None

            if selftext_html:
                decoded = html.unescape(selftext_html)
                soup = BeautifulSoup(decoded, 'html.parser')
                
                if options.summary:
                    log.info("Generating AI summary for Reddit Selftext...")
                    summary_html = await LLMHelper.generate_summary(soup.get_text(separator=" ", strip=True), options.llm_model, options.llm_api_key)

                if not options.no_images:
                    await ImageProcessor.process_images(session, soup, source.url, assets)
                article_html = soup.prettify()
                if summary_html:
                    article_html = f"<div class='ai-summary'><h3>AI Summary</h3>{summary_html}</div><hr/>{article_html}"

            elif is_image_link:
                img_html = f"""<div class="img-block"><img class="epub-image" src="{link_url}" alt="{title}"/></div>"""
                soup = BeautifulSoup(img_html, 'html.parser')
                if not options.no_images:
                    await ImageProcessor.process_images(session, soup, link_url, assets)
                article_html = soup.prettify()
                context = f"<p><strong>Reddit Link:</strong> <a href=\"{source.url}\">{source.url}</a></p>"
                meta_html = ArticleExtractor.build_meta_block(link_url, {"author": None, "date": None, "sitename": urlparse(link_url).netloc}, context=context)
                article_html = f"{meta_html}<hr/>{article_html}"
            elif link_url and not link_url.startswith(("https://www.reddit.com", "https://old.reddit.com", "https://redd.it")):
                art_data = await ArticleExtractor.get_article_content(session, link_url, force_archive=options.archive, profile=context.profile)
                if art_data['success']:
                    chapter_title = art_data.get('title') or chapter_title
                    soup = BeautifulSoup(art_data['html'], 'html.parser')
                    body = soup.body if soup.body else soup
                    
                    if options.summary:
                        log.info("Generating AI summary for Reddit Link...")
                        summary_html = await LLMHelper.generate_summary(body.get_text(separator=" ", strip=True), options.llm_model, options.llm_api_key)

                    if not options.no_images:
                        base = art_data.get('archive_url') if art_data.get('was_archived') else link_url
                        await ImageProcessor.process_images(session, body, base, assets)
                    article_html = body.prettify()
                    context = f"<p><strong>Reddit Link:</strong> <a href=\"{source.url}\">{source.url}</a></p>"
                    meta_html = ArticleExtractor.build_meta_block(link_url, art_data, context=context, summary_html=summary_html)
                    article_html = f"{meta_html}<hr/>{article_html}"
                else:
                    article_html = f"<p>Original link: <a href=\"{link_url}\">{link_url}</a></p>"
            else:
                article_html = f"<p>Original thread: <a href=\"{source.url}\">{source.url}</a></p>"

            final_art_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{chapter_title}</title><link rel="stylesheet" href="style/default.css"/></head>
            <body><h1>{chapter_title}</h1>{article_html}</body></html>"""

            art_chap = Chapter(title=chapter_title, filename="article.xhtml", content_html=final_art_html, uid=f"reddit_art_{post_id}", is_article=True)
            chapters.append(art_chap)

        com_chap = None
        if not options.no_comments:
            comments_listing = payload[1].get("data", {}).get("children", [])
            normalized = self._normalize_comments(comments_listing, options.max_depth)
            enriched_roots = _enrich_comment_tree(normalized)

            fmt = HtmlFormatter(style='default', cssclass='codehilite', noclasses=False)
            chunks = []
            for comment in enriched_roots:
                chunks.append("<div class='thread-container'>")
                chunks.append(format_comment_html(comment, fmt))
                chunks.append("</div>")
            comments_html = "".join(chunks)

            if comments_html and not options.no_images:
                try:
                    com_soup = BeautifulSoup(comments_html, 'html.parser')
                    for a in com_soup.find_all('a'):
                        href = a.get('href')
                        if href and re.search(r'\.(jpe?g|png|webp|gif)(\?|$)', href, re.IGNORECASE):
                            # Skip non-file wiki pages masquerading with extensions
                            if "://commons.wikimedia.org/wiki/" in href:
                                continue
                            img = com_soup.new_tag('img', src=href, alt=a.get_text(strip=True) or "Image")
                            a.replace_with(img)
                    await ImageProcessor.process_images(session, com_soup, source.url, assets)
                    comments_html = com_soup.prettify()
                except Exception as e:
                    log.debug(f"Reddit comment image embed failed: {e}")

            if comments_html:
                full_com_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Comments</title><link rel="stylesheet" href="style/default.css"/></head><body>
                <h1>Comments</h1>{comments_html}</body></html>"""
                com_chap = Chapter(title="Comments", filename="comments.xhtml", content_html=full_com_html, uid=f"reddit_com_{post_id}", is_comments=True)
                chapters.append(com_chap)

        toc_links = []
        if art_chap: toc_links.append(epub.Link(art_chap.filename, "Post", art_chap.uid))
        if art_chap and com_chap:
            toc_links = [(epub.Link(art_chap.filename, "Post", art_chap.uid), [epub.Link(com_chap.filename, "Comments", com_chap.uid)])]
        elif art_chap:
            toc_links = [epub.Link(art_chap.filename, "Post", art_chap.uid)]
        elif com_chap:
            toc_links = [epub.Link(com_chap.filename, "Comments", com_chap.uid)]

        desc = f"Reddit thread r/{subreddit}" if subreddit else "Reddit thread"
        return BookData(title=title, author=author, uid=f"urn:reddit:{post_id}", language='en', description=desc, source_url=source.url, chapters=chapters, images=assets, toc_structure=toc_links)

    def _build_api_url(self, url: str) -> str:
        cleaned = url.rstrip('/')
        if cleaned.endswith(".json"):
            if "raw_json=1" in cleaned: return cleaned
            joiner = "&" if "?" in cleaned else "?"
            return f"{cleaned}{joiner}raw_json=1"
        joiner = "&" if "?" in cleaned else "?"
        return f"{cleaned}.json{joiner}raw_json=1"

    def _normalize_comments(self, children, max_depth, depth=0):
        if not children: return []
        results = []
        for child in children:
            if child.get("kind") != "t1": continue
            data = child.get("data", {})
            if max_depth is not None and depth >= max_depth: continue

            body_html = data.get("body_html") or ""
            text = html.unescape(body_html) if body_html else "<p>[deleted]</p>"
            author = data.get("author")
            timestamp = data.get("created_utc") or 0
            comment_id = data.get('id') or f"c_{abs(hash(text))}"

            norm = {
                'id': str(comment_id),
                'by': f"u/{author}" if author else "[deleted]",
                'text': text,
                'time': timestamp,
                'children_data': []
            }
            replies = data.get("replies")
            if isinstance(replies, dict):
                rep_children = replies.get("data", {}).get("children", [])
                norm['children_data'] = self._normalize_comments(rep_children, max_depth, depth + 1)
            results.append(norm)
        return results

# --- Forum Driver ---

class ForumDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        base_url = self._normalize_url(source.url)
        log.info(f"Forum Driver processing: {base_url}")

        assets: List[ImageAsset] = []
        page_blocks: List[Tuple[int, List[Dict[str, Any]]]] = []
        title = None

        target_pages = options.page_spec or []
        pages_sequence = list(target_pages) if target_pages else []
        max_pages = options.max_pages
        max_posts = options.max_posts

        page = 1
        seen_pages = set()
        seen_urls = set()

        # Seed preloaded assets once so they are available for every page
        if source.assets:
            seeded = 0
            for a in source.assets:
                raw = a.get("content")
                if isinstance(raw, str):
                    import base64
                    try:
                        raw = base64.b64decode(raw)
                    except Exception:
                        raw = None
                if not raw:
                    continue
                url_like = a.get("canonical_url") or a.get("original_url") or a.get("viewer_url") or ""
                if not isinstance(url_like, str) or not url_like.startswith("http"):
                    continue
                parsed = urlparse(url_like)
                path_val = parsed.path if parsed else ""
                mime = a.get("media_type") or a.get("content_type") or "image/jpeg"
                fname_base = sanitize_filename(os.path.splitext(os.path.basename(path_val))[0])
                if len(fname_base) < 3:
                    fname_base = f"img_{abs(hash(a.get('original_url') or a.get('viewer_url') or seeded))}"
                ext = os.path.splitext(path_val)[1]
                if not ext or not ext.startswith("."):
                    ext = mimetypes.guess_extension(mime) or ".img"
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                count = 0
                while any(existing.filename == fname for existing in assets):
                    count += 1
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"
                uid = f"img_{abs(hash(fname))}"
                alt_urls = []
                viewer = a.get("viewer_url")
                canonical = a.get("canonical_url")
                orig = a.get("original_url")
                for u in (orig, viewer, canonical):
                    if u and isinstance(u, str):
                        alt_urls.append(u)
                        if "?" in u:
                            alt_urls.append(u.split("?", 1)[0])
                assets.append(ImageAsset(uid=uid, filename=fname, media_type=mime, content=raw, original_url=url_like, alt_urls=alt_urls))
                seeded += 1
            log.info(f"Seeded {seeded} preloaded assets into EPUB.")

        while True:
            if target_pages:
                if not pages_sequence:
                    break
                page = pages_sequence.pop(0)
            else:
                if max_pages and page > max_pages:
                    break

            if page in seen_pages:
                page += 1
                continue

            page_url = self._build_page_url(base_url, page)
            html_content, final_url = await fetch_with_retry(session, page_url, 'text')
            if not html_content:
                if target_pages:
                    log.warning(f"Page {page} missing.")
                    continue
                else:
                    break
            if final_url in seen_urls:
                log.info(f"Final URL for page {page} already seen. Stopping to avoid loop: {final_url}")
                break
            seen_urls.add(final_url)

            soup = BeautifulSoup(html_content, 'lxml')
            if not title:
                title = self._extract_title(soup, base_url)

            if source.assets and page == 1:
                log.info(f"Preloaded assets received: {len(source.assets)}")
                for a in source.assets[:3]:
                    log.info(f"Asset sample original={a.get('original_url')} viewer={a.get('viewer_url')} canonical={a.get('canonical_url')} type={a.get('content_type')}")

            if not options.no_images:
                base_for_imgs = final_url or base_url
                await ForumImageProcessor.process_images(session, soup, base_for_imgs, assets, preloaded_assets=source.assets)

            posts = self._extract_posts(soup)
            if posts:
                if max_posts:
                    remaining = max_posts - sum(len(b[1]) for b in page_blocks)
                    if remaining <= 0:
                        break
                    posts = list(islice(posts, remaining))
                page_blocks.append((page, posts))
            if max_posts and sum(len(b[1]) for b in page_blocks) >= max_posts:
                break

            seen_pages.add(page)
            if target_pages:
                continue

            has_next = self._has_next_page(soup, page, final_url)
            if not has_next:
                break
            page += 1

        if not page_blocks:
            log.error("Forum extraction produced no posts.")
            return None

        summary_html = None
        if options.summary:
            log.info("Generating AI summary for Forum Thread...")
            sample_text = []
            count = 0
            for _, posts in page_blocks:
                for p in posts:
                    clean = BeautifulSoup(p.get("html", ""), "html.parser").get_text(separator=" ", strip=True)
                    sample_text.append(f"Post by {p.get('author')}: {clean}")
                    count += 1
                    if count >= 5: break
                if count >= 5: break
            
            if sample_text:
                summary_html = await LLMHelper.generate_summary("\n\n".join(sample_text), options.llm_model, options.llm_api_key)

        chapter_html = self._render_thread_html(title or "Forum Thread", source.url, page_blocks, summary_html=summary_html)
        assets, chapter_html = self._dedupe_assets(assets, chapter_html)
        chapter = Chapter(
            title=title or "Forum Thread",
            filename="thread.xhtml",
            content_html=chapter_html,
            uid="forum_thread",
            is_article=True
        )

        toc_links = [epub.Link("thread.xhtml", "Thread", "forum_thread")]
        return BookData(
            title=title or "Forum Thread",
            author="Forum",
            uid=f"urn:forum:{abs(hash(base_url))}",
            language="en",
            description=f"Forum thread from {base_url}",
            source_url=source.url,
            chapters=[chapter],
            images=assets,
            toc_structure=toc_links
        )

    def _normalize_url(self, url: str) -> str:
        cleaned = url.rstrip('/')
        if "page-" in cleaned:
            cleaned = re.sub(r'/page-\d+', '', cleaned)
        cleaned = re.sub(r'([?&])page=\d+', r'\1', cleaned)
        cleaned = cleaned.rstrip('?&')
        return cleaned

    def _build_page_url(self, base_url: str, page: int) -> str:
        if page <= 1:
            return base_url
        parsed = urlparse(base_url)
        path = parsed.path or ""
        query = parsed.query or ""
        # Handle XenForo style index.php?threads/slug[/page-N]
        if query and query.startswith("threads/"):
            q = query
            if re.search(r'page-\d+', q):
                q = re.sub(r'page-\d+', f"page-{page}", q)
            else:
                q = q.rstrip('/') + f"/page-{page}"
            return parsed._replace(query=q).geturl()
        if re.search(r'page-\d+', path):
            new_path = re.sub(r'page-\d+', f"page-{page}", path)
        elif path.endswith('/'):
            new_path = f"{path}page-{page}"
        else:
            new_path = f"{path}/page-{page}"
        rebuilt = parsed._replace(path=new_path)
        return rebuilt.geturl()

    def _extract_title(self, soup, url):
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"): return og.get("content")
        if soup.title and soup.title.string: return soup.title.string.strip()
        return url

    def _extract_posts(self, soup):
        posts = []
        seen_ids = set()
        # XenForo style messages
        containers = soup.select("article.message.message--post, li.message, article.message")
        if not containers:
            containers = soup.find_all(lambda tag: tag.get("class") and any("message" in c for c in tag.get("class")))
        for c in containers:
            pid_raw = c.get("id") or c.get("data-content") or ""
            if pid_raw in ("messageList",):
                continue
            anchor_id = sanitize_filename(pid_raw) if pid_raw else f"post_{len(posts)+1}"
            num_id = None
            try:
                m = re.search(r'(\d+)', pid_raw)
                if m: num_id = m.group(1)
            except Exception:
                pass
            key = anchor_id or num_id
            if key and key in seen_ids:
                continue
            seen_ids.add(key)

            author = None
            author_tag = c.find(lambda t: t.get("class") and any("username" in x or "author" in x for x in t.get("class")))
            if not author and c.has_attr("data-author"):
                author = c.get("data-author")
            if author_tag:
                author = author_tag.get_text(strip=True)
            if author and author.endswith(","):
                author = author.rstrip(",").strip()

            content_tag = c.select_one(".message-body") or c.select_one(".message-content") or c.select_one(".messageContent")
            if not content_tag:
                content_tag = c.find(lambda t: t.get("class") and any("messageContent" in x or "bbWrapper" in x or "content" == x for x in t.get("class")))
            if not content_tag:
                continue
            time_val = None
            time_tag = c.find("time")
            if time_tag:
                time_val = time_tag.get("datetime") or time_tag.get("title") or time_tag.get_text(strip=True)
            posts.append({
                "id": pid_raw or anchor_id,
                "anchor_id": anchor_id,
                "numeric_id": num_id,
                "author": author or "Anonymous",
                "html": str(content_tag),
                "time": time_val
            })
        return posts

    def _extract_page_number(self, href: str) -> Optional[int]:
        if not href: return None
        m = re.search(r'page[-=](\d+)', href, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        return None

    def _has_next_page(self, soup, current_page: int, current_url: str) -> bool:
        if current_page > 500:
            return False
        link = soup.find("link", rel="next")
        if link and link.get("href"):
            maybe = self._extract_page_number(link.get("href"))
            if maybe and maybe <= current_page:
                return False
            return True
        numeric_pages = []
        for el in soup.select("li.pageNav-page, a.pageNav-page"):
            try:
                val = int(el.get_text(strip=True))
                numeric_pages.append(val)
            except Exception:
                continue
        max_num = max(numeric_pages) if numeric_pages else None
        if max_num and current_page < max_num:
            return True
        jump_next = soup.select_one("a.pageNav-jump--next, a.pageNavSimple-el--next, a[rel='next']")
        if jump_next and jump_next.get("href"):
            maybe = self._extract_page_number(jump_next.get("href"))
            if maybe and maybe <= current_page:
                return False
            return True
        # Some themes only expose "Next" text; avoid infinite climb by capping at known max or a hard guard.
        anchors = soup.find_all("a")
        for a in anchors:
            txt = a.get_text(strip=True).lower()
            if txt in ("next", "next >", "next>"):
                return True
            if txt == str(current_page + 1):
                return True
        return False

    def _render_thread_html(self, title, url, page_blocks: List[Tuple[int, List[Dict[str, Any]]]], summary_html: Optional[str] = None):
        anchor_map: Dict[str, str] = {}
        for _, posts in page_blocks:
            for p in posts:
                anchor = p.get("anchor_id") or sanitize_filename(p.get("id") or "")
                pid_raw = p.get("id")
                num = p.get("numeric_id")
                if pid_raw:
                    anchor_map[pid_raw] = anchor
                if num:
                    anchor_map[num] = anchor
                    anchor_map[f"post-{num}"] = anchor

        def rewrite_quote_links(html_snippet: str) -> str:
            if not html_snippet:
                return html_snippet
            try:
                soup = BeautifulSoup(html_snippet, 'html.parser')
                links = soup.find_all("a")
                for a in links:
                    cls = " ".join(a.get("class", [])) if a.get("class") else ""
                    target = None
                    if "bbCodeBlock-sourceJump" in cls or "AttributionLink" in cls:
                        sel = a.get("data-content-selector")
                        if sel and isinstance(sel, str):
                            sel = sel.lstrip("#")
                            if sel:
                                target = sel
                        if not target:
                            href = a.get("href")
                            if href:
                                m = re.search(r'id=(\d+)', href)
                                if m:
                                    target = m.group(1)
                                else:
                                    m2 = re.search(r'post-(\d+)', href)
                                    if m2:
                                        target = m2.group(1)
                    if target and target in anchor_map:
                        anchor = anchor_map[target]
                        a['href'] = f"#p_{anchor}"
                        if a.has_attr("data-xf-click"):
                            del a["data-xf-click"]
                        if a.has_attr("data-content-selector"):
                            del a["data-content-selector"]
                return str(soup)
            except Exception as e:
                return html_snippet

        chunks = [f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head><body>"""]
        chunks.append(f"<h1>{title}</h1><div class='post-meta'><p><strong>Source:</strong> <a href=\"{url}\">{url}</a></p></div>")
        
        if summary_html:
            chunks.append(f"<div class='ai-summary'><h3>AI Summary</h3>{summary_html}</div><hr/>")

        post_counter = 1
        for page_no, posts in page_blocks:
            chunks.append(f"<div class='page-label' id='page_{page_no}'>Page {page_no}</div>")
            for post in posts:
                anchor_id = post.get("anchor_id") or sanitize_filename(post.get("id") or f"post_{post_counter}")
                pid = anchor_id
                author = html.escape(post.get("author") or "Anonymous")
                when = html.escape(post.get("time") or "")
                chunks.append(f"<div class='forum-post' id='p_{pid}'>")
                chunks.append(f"<div class='forum-post-header'><span class='forum-author'>{author}</span>")
                if when: chunks.append(f"<span class='forum-time'>{when}</span>")
                chunks.append("</div>")
                body_html = rewrite_quote_links(post.get('html',''))
                chunks.append(f"<div class='forum-post-body'>{body_html}</div>")
                chunks.append("</div>")
                post_counter += 1
        chunks.append("</body></html>")
        return "".join(chunks)

    def _dedupe_assets(self, assets: List[ImageAsset], html: str) -> Tuple[List[ImageAsset], str]:
        """Remove duplicate image assets by content hash and rewrite HTML src to canonical filenames."""
        seen: Dict[str, ImageAsset] = {}
        keep: List[ImageAsset] = []
        replace_map: Dict[str, str] = {}

        for a in assets:
            if not a.content:
                keep.append(a)
                continue
            try:
                h = hashlib.sha1(a.content).hexdigest()
            except Exception:
                keep.append(a)
                continue
            if h in seen:
                replace_map[a.filename] = seen[h].filename
            else:
                seen[h] = a
                keep.append(a)

        if replace_map and html:
            for old, new in replace_map.items():
                html = html.replace(old, new)

        return keep, html

# --- Global Logic: Tree Enrichment ---

def _enrich_comment_tree(roots: List[Dict]) -> List[Dict]:
    for i in range(len(roots) - 1):
        roots[i]['next_root_id'] = str(roots[i+1].get('id'))
    def recurse(nodes, parent_id, root_id, next_root_id):
        for i, node in enumerate(nodes):
            node['parent_id'] = parent_id
            node['root_id'] = root_id
            node['next_root_id'] = next_root_id
            if i < len(nodes) - 1:
                node['next_sibling_id'] = str(nodes[i+1].get('id'))
            if node.get('children_data'):
                recurse(node['children_data'], str(node.get('id')), root_id or str(node.get('id')), next_root_id)
    for root in roots:
        next_r = root.get('next_root_id')
        if root.get('children_data'):
             recurse(root['children_data'], str(root.get('id')), str(root.get('id')), next_r)
    return roots

async def fetch_comments_recursive(session, comment_ids, fetched_data, max_depth, current_depth=0):
    if not comment_ids or (max_depth is not None and current_depth >= max_depth): return []
    tasks = []
    valid_ids = [cid for cid in comment_ids if cid not in fetched_data]
    for cid in valid_ids:
        url = f"{HN_API_BASE_URL}item/{cid}.json"
        tasks.append(fetch_with_retry(session, url))
    if not tasks: return []
    results = await asyncio.gather(*tasks)
    child_tasks = []
    comments = []
    for i, (data, _) in enumerate(results):
        if not data: continue
        cid = valid_ids[i]
        fetched_data[cid] = data
        if not data.get('deleted') and not data.get('dead'):
            data['children_data'] = []
            data['id'] = str(data.get('id'))
            comments.append(data)
            if data.get('kids'):
                t = fetch_comments_recursive(session, data['kids'], fetched_data, max_depth, current_depth + 1)
                child_tasks.append((data, t))
    if child_tasks:
        res = await asyncio.gather(*(t[1] for t in child_tasks))
        for i, (parent, _) in enumerate(child_tasks):
            parent['children_data'] = res[i]
    return comments

def format_comment_html(comment_data, formatter, depth=0):
    auth = comment_data.get('by', '[deleted]')
    text = comment_data.get('text', '')
    cid = comment_data.get('id')
    pid = comment_data.get('parent_id')
    nsid = comment_data.get('next_sibling_id')
    rid = comment_data.get('root_id')
    nrid = comment_data.get('next_root_id')

    def make_btn(target_id, symbol, title):
        if target_id: return f'<a href="#c_{target_id}" class="nav-btn" title="{title}">{symbol}</a>'
        else: return f'<span class="nav-btn ghost">{symbol}</span>'

    btns = [make_btn(pid, "↑", "Parent"), make_btn(nsid, "→", "Next Sibling"), make_btn(rid if depth > 1 else None, "⏮", "Thread Root"), make_btn(nrid, "⏭", "Next Thread")]
    nav_bar = f'<div class="nav-bar">{"".join(btns)}</div>'

    if '<pre>' in text:
        soup = BeautifulSoup(text, 'html.parser')
        for pre in soup.find_all('pre'):
            try:
                code = pre.get_text()
                lexer = guess_lexer(code)
                hl = highlight(code, lexer, formatter)
                pre.replace_with(BeautifulSoup(hl, 'html.parser'))
            except: pass
        text = str(soup)

    capped_depth = min(depth, 5)
    margin = capped_depth * 10
    border_style = f"border-left: 2px solid #ccc;" if depth > 0 else ""
    padding = 10 if depth < 6 else 2
    style = f"{border_style} padding-left: {padding}px; margin-left: {margin}px; margin-bottom: 15px;"
    if depth == 0: style = "margin-bottom: 20px;"

    header = f'<div class="comment-header"><div class="comment-author"><div class="comment-author-inner">{auth}</div></div><div class="nav-bar">{nav_bar}</div></div>'
    html = f'<div id="c_{cid}" style="{style}">{header}<div class="comment-body">{text}</div>'
    if comment_data.get('children_data'):
        for child in comment_data['children_data']:
            html += format_comment_html(child, formatter, depth + 1)
    html += '</div>'
    return html

# --- EPUB Writer ---
class EpubWriter:
    @staticmethod
    def write(book_data: BookData, output_path: str, custom_css: str = None):
        book = epub.EpubBook()
        book.set_identifier(book_data.uid)
        book.set_title(book_data.title)
        book.set_language(book_data.language)
        book.add_author(book_data.author)

        pygments_style = HtmlFormatter(style='default').get_style_defs('.codehilite')
        base_css = """
            body { font-family: sans-serif; margin: 0.5em; background-color: #fdfdfd; line-height: 1.5; }
            .img-block { margin: 0.5em 0; page-break-inside: avoid; break-inside: avoid; -webkit-column-break-inside: avoid; text-align: center; }
            .img-block img { max-width: 100%; max-height: 70vh; height: auto; display: block; margin: 0 auto; object-fit: contain; }
            .img-block .caption { margin: 0.25em 0 0; font-size: 0.9em; color: #555; }
            .epub-image { max-width: 100%; height: auto; display: block; }
            figure { margin: 0; text-align: center; }
            figcaption { font-size: 0.8em; color: #666; font-style: italic; margin-top: 0; }
            .post-meta { background: #f5f5f5; padding: 10px; margin-bottom: 20px; border-radius: 5px; font-size: 0.9em; }
            .thread-container { margin-top: 25px; padding-top: 15px; border-top: 1px solid #ddd; }
            .comment-header { display: table; width: 100%; table-layout: auto; border-bottom: 1px solid #eee; margin-bottom: 4px; background-color: #f9f9f9; border-radius: 4px; }
            .comment-author { display: table-cell; width: auto; vertical-align: middle; padding: 4px 6px; }
            .comment-author-inner { display: block; font-weight: bold; color: #333; font-size: 0.95em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 45vw; }
            .nav-bar { display: table-cell; width: 1%; vertical-align: middle; white-space: nowrap; padding-right: 4px; }
            .nav-btn { display: inline-block; text-decoration: none; color: #666; font-weight: bold; font-size: 1.1em; padding: 0px 12px; height: 1.6em; line-height: 1.6em; border-left: 1px solid #ddd; text-align: center; margin-left: 14px; }
            .nav-btn:hover { background-color: #eee; color: #000; }
            .nav-btn.ghost { visibility: hidden; }
            .comment-body { margin-top: 2px; }
            pre { background: #f0f0f0; padding: 10px; overflow-x: auto; font-size: 0.9em; }
            p { margin-top: 0; margin-bottom: 0.4em; }
            .forum-post { border: 1px solid #e0e0e0; border-radius: 6px; padding: 6px; margin-bottom: 8px; background: #fff; }
            .forum-post-header { display: flex; justify-content: space-between; font-weight: 600; margin-bottom: 6px; font-size: 0.95em; color: #333; }
            .forum-author { color: #222; }
            .forum-time { color: #777; font-weight: 400; font-size: 0.9em; }
            .forum-post-body { font-size: 0.97em; color: #222; }
            .page-label { margin: 14px 0 8px 0; padding: 6px 8px; background: #eef5ff; border-left: 3px solid #4a7bd4; font-weight: 600; border-radius: 4px; }
        """ + pygments_style
        if custom_css: base_css += f"\n{custom_css}"

        css_item = epub.EpubItem(uid="style_default", file_name="style/default.css", media_type="text/css", content=base_css)
        book.add_item(css_item)

        for asset in book_data.images:
            img = epub.EpubImage(uid=asset.uid, file_name=asset.filename, media_type=asset.media_type, content=asset.content)
            book.add_item(img)

        epub_chapters = []
        for chap in book_data.chapters:
            c = epub.EpubHtml(title=chap.title, file_name=chap.filename, lang='en')
            c.content = chap.content_html
            c.add_item(css_item)
            book.add_item(c)
            epub_chapters.append(c)

        if book_data.toc_structure:
            book.toc = tuple(book_data.toc_structure)
        else:
            book.toc = tuple(epub_chapters)

        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ['nav'] + epub_chapters

        epub.write_epub(output_path, book)
        log.info(f"Wrote EPUB: {output_path}")

class LLMHelper:
    @staticmethod
    async def _call_llm(prompt: str, model: Optional[str], api_key: Optional[str]) -> Optional[str]:
        gemini_key = api_key or os.getenv("GEMINI_API_KEY")
        openrouter_key = api_key or os.getenv("OPENROUTER_API_KEY")
        openai_key = api_key or os.getenv("OPENAI_API_KEY")

        # Heuristic to detect key type if passed generically
        passed_key_is_gemini = api_key and ("AIza" in api_key)
        
        if api_key:
            if passed_key_is_gemini:
                gemini_key = api_key
                openrouter_key = None
                openai_key = None
            else:
                gemini_key = None
                openrouter_key = api_key 
                openai_key = api_key

        if not (gemini_key or openrouter_key or openai_key):
            log.warning("No API keys found. Skipping LLM task.")
            return None

        # Determine Model
        model = model or os.getenv("LLM_MODEL")

        try:
            # Google Gemini (REST)
            if gemini_key:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model or 'gemini-1.5-flash'}:generateContent?key={gemini_key}"
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if 'candidates' in data and data['candidates']:
                                return data['candidates'][0]['content']['parts'][0]['text']
                            else:
                                log.warning(f"Gemini API returned no candidates: {data}")
                                return None
                        else:
                            log.error(f"Gemini API Error: {resp.status} {await resp.text()}")
                            if not (openrouter_key or openai_key): return None

            # OpenAI Compatible (OpenRouter / OpenAI)
            active_key = openrouter_key or openai_key
            base_url = "https://openrouter.ai/api/v1" if openrouter_key and not openai_key else "https://api.openai.com/v1"
            if "openrouter" in (model or ""): base_url = "https://openrouter.ai/api/v1"
            
            if api_key and not passed_key_is_gemini:
                active_key = api_key
                if model and ("/" in model): base_url = "https://openrouter.ai/api/v1"

            target_model = model or ("google/gemini-flash-1.5" if "openrouter" in base_url else "gpt-3.5-turbo")
            
            headers = {
                "Authorization": f"Bearer {active_key}",
                "Content-Type": "application/json"
            }
            if "openrouter" in base_url:
                headers["HTTP-Referer"] = "https://github.com/loki/epub_downloader"
                headers["X-Title"] = "EPUB Downloader"

            payload = {
                "model": target_model,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt}
                ]
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                    else:
                        log.error(f"LLM API Error ({base_url}): {resp.status} {await resp.text()}")
                        return None

        except Exception as e:
            log.error(f"LLM call failed: {e}")
            return None

    @staticmethod
    async def format_transcript(text: str, model: Optional[str] = None, api_key: Optional[str] = None) -> str:
        # Prepare Prompt
        custom_prompt = os.getenv("LLM_PROMPT")
        if custom_prompt:
            prompt = custom_prompt.replace("{text}", text)
        else:
            prompt = (
                "You are an expert editor. Please format the following YouTube transcript into a readable article. "
                "Fix punctuation, capitalization, and paragraph breaks. "
                "Do not summarize; keep the full content but make it flow like a written piece. "
                "Remove filler words like 'um', 'uh', 'like' where appropriate.\\n\\n"
                f"{text}"
            )
        
        result = await LLMHelper._call_llm(prompt, model, api_key)
        return result if result else text

    @staticmethod
    async def generate_summary(text: str, model: Optional[str] = None, api_key: Optional[str] = None) -> Optional[str]:
        custom_prompt = os.getenv("LLM_SUMMARY_PROMPT")
        if custom_prompt:
            prompt = custom_prompt.replace("{text}", text[:15000]) # truncate to avoid huge context costs?
        else:
            prompt = (
                "Please provide a concise executive summary (3-5 paragraphs) of the following text. "
                "Capture the main arguments, key takeaways, and conclusion. "
                "Format with <b>bold</b> for key terms if helpful.\\n\\n"
                f"{text[:25000]}" # Limit context window usage for summary
            )
        
        return await LLMHelper._call_llm(prompt, model, api_key)

class YouTubeDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        log.info(f"YouTube Driver processing: {url}")

        video_id = self._extract_video_id(url)
        if not video_id:
            log.error("Could not extract video ID")
            return None

        # 1. Fetch Page for Metadata (Title, Author, Thumbnail)
        try:
            html_content, _ = await fetch_with_retry(session, url, 'text')
            soup = BeautifulSoup(html_content, 'html.parser')
            title = soup.find("meta", property="og:title")
            title = title["content"] if title else f"YouTube Video {video_id}"
            
            desc = soup.find("meta", property="og:description")
            description = desc["content"] if desc else ""

            author = "YouTube"
            # Try to find channel name
            channel = soup.find("link", itemprop="name")
            if channel: author = channel.get("content")
            
            # Thumbnail
            thumb_url = None
            og_img = soup.find("meta", property="og:image")
            if og_img: thumb_url = og_img["content"]
            
        except Exception as e:
            log.warning(f"Metadata fetch failed: {e}")
            title = f"YouTube Video {video_id}"
            author = "YouTube"
            description = ""
            thumb_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

        # 2. Fetch Transcript
        loop = asyncio.get_running_loop()
        transcript_list = []
        try:
            # Using instance method fetch() which returns a FetchedTranscript object, then converting to raw dicts
            transcript_list = await loop.run_in_executor(
                None, 
                lambda: YouTubeTranscriptApi().fetch(video_id).to_raw_data()
            )
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            log.warning(f"No transcript available: {e}")
        except Exception as e:
            log.error(f"Transcript fetch error: {e}")

        if not transcript_list:
            log.error("Aborting: No transcript found.")
            return None

        # 3. Process Text
        if options.llm_format:
            full_text = " ".join([t['text'] for t in transcript_list])
            full_text = html.unescape(full_text)
            log.info("Formatting transcript with LLM...")
            final_text = await LLMHelper.format_transcript(full_text, options.llm_model, options.llm_api_key)
            # Wrap in paragraphs if LLM returned plain text block (LLMs usually add newlines)
            if "<p>" not in final_text:
                final_text = "".join(f"<p>{p.strip()}</p>" for p in final_text.split('\n\n') if p.strip())
        else:
            # Basic cleanup using timestamps
            final_text = self._basic_transcript_cleanup(transcript_list)

        # 4. Build Chapter
        assets = []
        cover_image_html = ""
        if not options.no_images and thumb_url:
            headers, data, err = await ImageProcessor.fetch_image_data(session, thumb_url)
            if data:
                mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(thumb_url, headers, data)
                if final_data:
                    fname = f"{IMAGE_DIR_IN_EPUB}/cover{ext}"
                    uid = "cover_img"
                    asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=thumb_url)
                    assets.append(asset)
                    cover_image_html = f'<div class="img-block"><img src="{fname}" alt="Thumbnail" class="epub-image"/></div><hr/>'

        summary_html = ""
        if options.summary:
            log.info("Generating AI summary for YouTube...")
            raw_transcript_text = " ".join([t['text'] for t in transcript_list])
            sum_res = await LLMHelper.generate_summary(raw_transcript_text, options.llm_model, options.llm_api_key)
            if sum_res:
                summary_html = f"<div class='ai-summary'><h3>AI Summary</h3>{sum_res}</div><hr/>"

        content_html = f"{summary_html}{cover_image_html}<div class='transcript-body'>{final_text}</div>"
        
        final_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head>
        <body><h1>{title}</h1><div class="post-meta"><p><strong>Source:</strong> <a href="{url}">{url}</a></p><p><strong>Channel:</strong> {author}</p></div>{content_html}</body></html>"""

        chapter = Chapter(title=title, filename="transcript.xhtml", content_html=final_html, uid="transcript", is_article=True)

        return BookData(
            title=title, 
            author=author, 
            uid=f"urn:youtube:{video_id}", 
            language='en', 
            description=description, 
            source_url=url, 
            chapters=[chapter], 
            images=assets, 
            toc_structure=[epub.Link("transcript.xhtml", title, "transcript")]
        )

    def _basic_transcript_cleanup(self, transcript_list: List[Dict]) -> str:
        """Merges lines and creates paragraphs based on silence gaps (>2s)."""
        paragraphs = []
        current_para = []
        last_end = 0

        for item in transcript_list:
            text = html.unescape(item['text']).replace('\n', ' ').strip()
            if not text: continue
            
            start = item['start']
            
            # If gap > 2 seconds, start new paragraph
            if current_para and (start - last_end > 2.0):
                # Join sentences, try to capitalize first letter
                joined = " ".join(current_para)
                if joined:
                    joined = joined[0].upper() + joined[1:]
                paragraphs.append(f"<p>{joined}</p>")
                current_para = []

            current_para.append(text)
            last_end = start + item['duration']

        if current_para:
            joined = " ".join(current_para)
            if joined:
                joined = joined[0].upper() + joined[1:]
            paragraphs.append(f"<p>{joined}</p>")
        
        return "".join(paragraphs)

    def _extract_video_id(self, url):
        """Extracts video ID from various YouTube URL formats."""
        parsed = urlparse(url)
        if parsed.netloc == "youtu.be":
            return parsed.path[1:]
        if parsed.netloc in ("www.youtube.com", "youtube.com"):
            if "/watch" in parsed.path:
                return parse_qs(parsed.query).get("v", [None])[0]
            if "/embed/" in parsed.path:
                return parsed.path.split("/embed/")[1]
            if "/v/" in parsed.path:
                return parsed.path.split("/v/")[1]
        return None

# --- Public API (for Server) ---

class DriverDispatcher:
    @staticmethod
    def get_driver(source: Source, profile: Optional[SiteProfile] = None) -> BaseDriver:
        if profile and profile.driver_alias:
            alias = profile.driver_alias.lower()
            if alias in ("forum", "xenforo"): return ForumDriver()
            if alias == "wordpress": return WordPressDriver()
            if alias == "substack": return SubstackDriver()
            if alias in ("hn", "hackernews"): return HackerNewsDriver()
            if alias == "reddit": return RedditDriver()
            if alias == "youtube": return YouTubeDriver()
            if alias == "generic": return GenericDriver()

        url = source.url
        parsed = urlparse(url)
        
        # 1. Explicit Flags
        if source.is_forum:
            return ForumDriver()
            
        # 2. Domain Matching
        if "news.ycombinator.com" in url:
            return HackerNewsDriver()
        if "reddit.com" in parsed.netloc or parsed.netloc.endswith("redd.it"):
            return RedditDriver()
        if "substack.com" in url or "/p/" in parsed.path:
            return SubstackDriver()
        if "wordpress.com" in url:
            return WordPressDriver()
        if parsed.netloc in ("www.youtube.com", "youtube.com", "youtu.be"):
            return YouTubeDriver()
            
        # 3. Content Sniffing
        if source.html:
            if 'substack:post_id' in source.html:
                 return SubstackDriver()
            if 'data-template="thread_view"' in source.html or 'xenforo' in source.html.lower():
                 return ForumDriver()
            if 'name="generator" content="WordPress"' in source.html or 'class="comment-list"' in source.html:
                 return WordPressDriver()
                 
        return GenericDriver()

async def process_urls(sources: List[Source], options: ConversionOptions, session) -> List[BookData]:
    processed_books = []

    async def safe_process(source):
        async with GLOBAL_SEMAPHORE:
            profile = ProfileManager.get_instance().get_profile(source.url)
            driver = DriverDispatcher.get_driver(source, profile)

            local_session = session
            if source.cookies:
                connector = aiohttp.TCPConnector(
                    resolver=ThreadedResolver(),
                    ttl_dns_cache=300,
                    family=socket.AF_INET
                )
                local_session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, cookies=source.cookies, connector=connector)
                setattr(local_session, "_extra_cookies", source.cookies)

            try:
                context = ConversionContext(session=local_session, options=options, profile=profile)
                return await driver.prepare_book_data(context, source)
            except Exception as e:
                log.exception(f"Failed to process {source.url}: {e}")
                return None
            finally:
                if local_session is not session:
                    await local_session.close()

    results = await tqdm_asyncio.gather(*[safe_process(s) for s in sources], desc="Processing URLs")
    return [b for b in results if b]

def create_bundle(books: List[BookData], title: str, author: str) -> BookData:
    master_uid = f"urn:bundle:{abs(hash(title))}"
    master_chapters = []
    master_images = []
    master_toc = []

    for book in books:
        for img in book.images:
            if img not in master_images: master_images.append(img)

        article_chap = None
        comments_chap = None

        for chap in book.chapters:
            chap.filename = f"doc_{abs(hash(book.source_url))}_{chap.filename}"
            master_chapters.append(chap)
            if chap.is_article: article_chap = chap
            elif chap.is_comments: comments_chap = chap

        if article_chap:
             if comments_chap:
                  master_toc.append( (epub.Link(article_chap.filename, book.title, article_chap.uid), [epub.Link(comments_chap.filename, "Comments", comments_chap.uid)]) )
             else:
                  master_toc.append(epub.Link(article_chap.filename, book.title, article_chap.uid))

    return BookData(title=title, author=author, uid=master_uid, language="en", description=f"Bundle of {len(books)} articles.", source_url="", chapters=master_chapters, images=master_images, toc_structure=master_toc)

# --- CLI Entry Point ---

async def async_main():
    parser = argparse.ArgumentParser(description="Universal Web to EPUB Downloader")
    parser.add_argument("input", nargs='?', help="URL")
    parser.add_argument("-i", "--input-file", help="File with URLs")
    parser.add_argument("-o", "--output", help="Output filename")
    parser.add_argument("--no-article", action="store_true")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("-a", "--archive", action="store_true")
    parser.add_argument("--no-comments", action="store_true", help="Skip downloading comments entirely")
    parser.add_argument("--bundle", action="store_true", help="Combine all URLs into a single EPUB")
    parser.add_argument("--bundle-title", help="Custom title for the bundle")
    parser.add_argument("--bundle-author", help="Custom author for the bundle")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=None, help="Max forum pages to fetch")
    parser.add_argument("--max-posts", type=int, default=None, help="Max forum posts to fetch")
    parser.add_argument("--pages", help="Specific pages to fetch (e.g. '1,3-5')")
    parser.add_argument("--css", help="Custom CSS file to inject")
    parser.add_argument("--llm", action="store_true", dest="llm_format", help="Format transcript using LLM (requires GEMINI_API_KEY)")
    parser.add_argument("--llm-model", help="Specify LLM model (default: gemini-1.5-flash)")
    parser.add_argument("--api-key", help="API Key for LLM (overrides env vars)")
    parser.add_argument("--summary", action="store_true", help="Generate AI summary at start of article")
    return parser.parse_args()

async def main():
    args = parse_args()
    
    urls = []
    if args.input_file:
        with open(args.input_file, 'r') as f:
            urls.extend([line.strip() for line in f if line.strip() and not line.startswith('#')])
    urls.extend(args.url)
    
    if not urls:
        print("No URLs provided.")
        return

    options = ConversionOptions(
        no_article=args.no_article,
        no_comments=args.no_comments,
        no_images=args.no_images,
        archive=args.archive,
        compact_comments=args.compact_comments,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        max_posts=args.max_posts,
        page_spec=parse_page_spec(args.pages),
        llm_format=args.llm_format,
        llm_model=args.llm_model,
        llm_api_key=args.api_key,
        summary=args.summary
    )

    # Load cookies once if provided
    cookie_entries = load_cookie_file(args.cookie_file) if (args.cookie_file and args.forum) else []

    def cookies_for_url(u: str) -> Optional[Dict[str, str]]:
        if not cookie_entries: return None
        host = urlparse(u).netloc.lower()
        jar = {}
        for c in cookie_entries:
            dom = c.get("domain", "").lower()
            if dom and (host == dom or host.endswith(f".{dom}")):
                jar[c["name"]] = c["value"]
        return jar or None

    # Convert URLs to Sources (CLI doesn't support HTML injection)
    sources = []
    for u in urls:
        sources.append(Source(
            url=u,
            html=None,
            cookies=cookies_for_url(u) if args.forum else None,
            assets=None,
            is_forum=args.forum
        ))

    async with get_session() as session:
        processed_books = await process_urls(sources, options, session)

    if not processed_books:
        log.error("No content was successfully fetched.")
        sys.exit(1)

    css_content = None
    if args.css:
        with open(args.css) as f: css_content = f.read()

    if args.bundle:
        master_title = args.bundle_title
        if not master_title:
            date_str = datetime.now().strftime("%b_%d_%Y")
            domains = {urlparse(b.source_url).netloc.replace("www.", "") for b in processed_books}
            if len(domains) == 1: master_title = f"{list(domains)[0]}_{date_str}"
            else: master_title = f"Anthology_{date_str}"

        master_author = args.bundle_author or "Various Authors"
        master_book = create_bundle(processed_books, master_title, master_author)
        fname = args.output or f"{sanitize_filename(master_title)}.epub"
        EpubWriter.write(master_book, fname, css_content)
    else:
        for book in processed_books:
            fname = f"{sanitize_filename(book.title)}.epub"
            if args.output:
                if len(urls) == 1: fname = args.output
                else: fname = f"{sanitize_filename(book.title)}_{args.output}"
            EpubWriter.write(book, fname, css_content)

if __name__ == "__main__":
    asyncio.run(async_main())

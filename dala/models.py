import os
import re
import asyncio
import aiohttp
import logging
import mimetypes
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple
from bs4 import BeautifulSoup

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
    thumbnails: bool = False

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

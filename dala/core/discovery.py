import json
import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from ..models import ConversionOptions, Source, log
from .extractor import ArticleExtractor
from .session import fetch_with_retry


class DiscoveryError(RuntimeError):
    """Raised when date-range discovery cannot produce usable post URLs."""


@dataclass
class DiscoveredPost:
    url: str
    published_date: Optional[date]
    title: Optional[str] = None


MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def parse_bound(value: Optional[str], *, is_end: bool = False) -> Optional[date]:
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        return date(year, 12, 31) if is_end else date(year, 1, 1)
    month_match = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if month_match:
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        if not 1 <= month <= 12:
            raise DiscoveryError(f"Invalid date '{value}'. Use YYYY, YYYY-MM, or YYYY-MM-DD.")
        day = calendar.monthrange(year, month)[1] if is_end else 1
        return date(year, month, day)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise DiscoveryError(f"Invalid date '{value}'. Use YYYY, YYYY-MM, or YYYY-MM-DD.") from exc


def parse_date_value(value: Optional[str], default_year: Optional[int] = None) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    iso_match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso_match:
        try:
            return date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
        except ValueError:
            pass

    numeric_match = re.search(r"\b(\d{4})[/.](\d{1,2})[/.](\d{1,2})\b", text)
    if numeric_match:
        try:
            return date(int(numeric_match.group(1)), int(numeric_match.group(2)), int(numeric_match.group(3)))
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    try:
        return parsedate_to_datetime(text).date()
    except Exception:
        pass

    long_match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(january|february|march|april|may|june|july|august|september|october|november|december)"
        r"\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if long_match:
        try:
            return date(
                int(long_match.group(3)),
                MONTHS[long_match.group(2).lower()],
                int(long_match.group(1)),
            )
        except ValueError:
            pass

    month_first_match = re.search(
        r"\b(?:mon|tue|wed|thu|fri|sat|sun)?(?:day)?[,]?\s*"
        r"(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:,\s*|\s+)(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if month_first_match:
        try:
            return date(
                int(month_first_match.group(3)),
                MONTHS[month_first_match.group(1).lower().rstrip(".")],
                int(month_first_match.group(2)),
            )
        except ValueError:
            pass

    day_first_short_match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(\d{4})\b",
        text,
        re.IGNORECASE,
    )
    if day_first_short_match:
        try:
            return date(
                int(day_first_short_match.group(3)),
                MONTHS[day_first_short_match.group(2).lower().rstrip(".")],
                int(day_first_short_match.group(1)),
            )
        except ValueError:
            pass

    substack_match = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
        text,
        re.IGNORECASE,
    )
    if substack_match:
        year = int(substack_match.group(3)) if substack_match.group(3) else default_year
        if year:
            try:
                return date(year, MONTHS[substack_match.group(1).lower().rstrip(".")], int(substack_match.group(2)))
            except ValueError:
                pass
    return None


def date_from_url_path(url: str) -> Optional[date]:
    path = urlparse(url).path
    patterns = (
        r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|\.html?$|$)",
        r"/(\d{4})-(\d{1,2})-(\d{1,2})(?:[-_/][^/]*)?(?:/|\.html?$|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, path)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None

    query = parse_qs(urlparse(url).query)
    if query.get("date"):
        parsed = parse_date_value(query["date"][0])
        if parsed:
            return parsed
    if query.get("year") and query.get("month") and query.get("day"):
        try:
            return date(int(query["year"][0]), int(query["month"][0]), int(query["day"][0]))
        except ValueError:
            return None
    return None


def archive_context_from_url(url: str) -> Tuple[Optional[int], Optional[int]]:
    path = urlparse(url).path
    match = re.search(r"/(\d{4})/(\d{1,2})(?:/|$)", path)
    if match:
        try:
            year = int(match.group(1))
            month = int(match.group(2))
            if 1 <= month <= 12:
                return year, month
        except ValueError:
            pass
    return None, None


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = ""
    if parsed.netloc.lower().endswith("crazyguyonabike.com") and path == "/doc":
        params = parse_qs(parsed.query, keep_blank_values=False)
        kept = []
        for key in ("doc_id", "v"):
            if params.get(key):
                kept.append((key, params[key][0]))
        query = urlencode(kept)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def is_internal_url(candidate: str, base_url: str) -> bool:
    base_host = urlparse(base_url).hostname or ""
    host = urlparse(candidate).hostname or ""
    return host == base_host or host.endswith(f".{base_host}")


def looks_like_article_url(url: str, base_url: str) -> bool:
    if not is_internal_url(url, base_url):
        return False
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)
    if parsed.netloc.lower().endswith("crazyguyonabike.com") and path == "doc" and query.get("doc_id"):
        return True
    if not path:
        return False
    lower = f"/{path.lower()}/"
    blocked = (
        "/about/",
        "/authors/",
        "/author/",
        "/tag/",
        "/category/",
        "/archive/",
        "/recommendations/",
        "/account/",
        "/subscribe/",
        "/privacy",
        "/terms",
        "/films/",
        "/books-in-progress/",
    )
    if any(part in lower for part in blocked):
        return False
    if re.search(r"/\d{4}/\d{1,2}/\d{1,2}/[^/]+", lower):
        return True
    if "/p/" in lower:
        return True
    if lower.startswith("/issue/") and len(path.split("/")) >= 2:
        return not re.fullmatch(r"issue(?:-\d+)?|issue/\d+|issue-[\w-]+", path.lower())
    return len(path.split("/")) == 1 and "-" in path


def extract_jsonld_dates(soup: BeautifulSoup) -> Optional[date]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _walk_json(data):
            if isinstance(obj, dict):
                for key in ("datePublished", "dateCreated", "uploadDate"):
                    parsed = parse_date_value(obj.get(key))
                    if parsed:
                        return parsed
    return None


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def extract_metadata_date(html: str, url: str, default_year: Optional[int] = None) -> Optional[date]:
    soup = BeautifulSoup(html or "", "html.parser")
    parsed = date_from_url_path(url)
    if parsed:
        return parsed

    selectors = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "article:published_time"}),
        ("meta", {"name": "date"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publishdate"}),
        ("meta", {"name": "timestamp"}),
        ("meta", {"name": "DC.date"}),
        ("meta", {"name": "dc.date"}),
        ("meta", {"name": "dc.date.issued"}),
        ("meta", {"property": "og:published_time"}),
        ("meta", {"itemprop": "datePublished"}),
    ]
    for name, attrs in selectors:
        tag = soup.find(name, attrs=attrs)
        if tag:
            parsed = parse_date_value(tag.get("content"), default_year=default_year)
            if parsed:
                return parsed

    for name, attrs in [
        ("meta", {"property": "article:modified_time"}),
        ("meta", {"itemprop": "dateModified"}),
    ]:
        tag = soup.find(name, attrs=attrs)
        if tag:
            parsed = parse_date_value(tag.get("content"), default_year=default_year)
            if parsed:
                return parsed

    for time_tag in soup.find_all("time"):
        parsed = parse_date_value(time_tag.get("datetime") or time_tag.get_text(" ", strip=True), default_year=default_year)
        if parsed:
            return parsed

    parsed = extract_jsonld_dates(soup)
    if parsed:
        return parsed

    return parse_date_value(soup.get_text(" ", strip=True)[:5000], default_year=default_year)


def nearby_listing_date(anchor, default_year: Optional[int], default_month: Optional[int] = None) -> Optional[date]:
    for attr in ("datetime", "title", "aria-label"):
        parsed = parse_date_value(anchor.get(attr), default_year=default_year)
        if parsed:
            return parsed
    for parent in [anchor, *list(anchor.parents)[:5]]:
        time_tag = parent.find("time") if hasattr(parent, "find") else None
        if time_tag:
            parsed = parse_date_value(time_tag.get("datetime") or time_tag.get_text(" ", strip=True), default_year=default_year)
            if parsed:
                return parsed
        text = parent.get_text(" ", strip=True) if hasattr(parent, "get_text") else ""
        parsed = parse_date_value(text[:1000], default_year=default_year)
        if parsed:
            return parsed
        if default_year and default_month:
            day_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text[:160], re.IGNORECASE)
            if day_match:
                try:
                    return date(default_year, default_month, int(day_match.group(1)))
                except ValueError:
                    pass
    return None


def extract_candidate_posts(html: str, page_url: str, default_year: Optional[int]) -> List[DiscoveredPost]:
    soup = BeautifulSoup(html or "", "html.parser")
    posts: List[DiscoveredPost] = []
    seen = set()
    context_year, context_month = archive_context_from_url(page_url)
    effective_year = default_year or context_year
    for anchor in soup.find_all("a", href=True):
        url = canonical_url(urljoin(page_url, anchor["href"]))
        if url in seen or not looks_like_article_url(url, page_url):
            continue
        seen.add(url)
        title = anchor.get_text(" ", strip=True) or None
        posts.append(DiscoveredPost(
            url=url,
            published_date=date_from_url_path(url) or nearby_listing_date(anchor, effective_year, context_month),
            title=title,
        ))
    return posts


def extract_feed_posts(feed_xml: str, feed_url: str) -> List[DiscoveredPost]:
    try:
        root = ET.fromstring(feed_xml or "")
    except ET.ParseError:
        return []

    posts: List[DiscoveredPost] = []
    seen = set()

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def child_text(node, names) -> Optional[str]:
        wanted = {name.lower() for name in names}
        for child in list(node):
            if local_name(child.tag) in wanted and child.text:
                return child.text.strip()
        return None

    def item_link(node) -> Optional[str]:
        text = child_text(node, {"link"})
        if text:
            return text
        for child in list(node):
            if local_name(child.tag) == "link" and child.get("href"):
                return child.get("href")
        return None

    for node in root.iter():
        if local_name(node.tag) not in {"item", "entry"}:
            continue
        link = item_link(node)
        if not link:
            continue
        url = canonical_url(urljoin(feed_url, link))
        if url in seen or not looks_like_article_url(url, feed_url):
            continue
        seen.add(url)
        date_text = child_text(node, {"pubDate", "published", "updated", "date"})
        title = child_text(node, {"title"})
        posts.append(DiscoveredPost(url=url, published_date=date_from_url_path(url) or parse_date_value(date_text), title=title))
    return posts


def extract_next_page(html: str, page_url: str) -> Optional[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    link = soup.find("link", attrs={"rel": lambda value: value and "next" in value})
    if link and link.get("href"):
        return canonical_url(urljoin(page_url, link["href"]))
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True).lower()
        rel = " ".join(anchor.get("rel", [])).lower() if anchor.get("rel") else ""
        if text in {"next", "next page", "older", "older posts", "more"} or "next" in rel:
            return canonical_url(urljoin(page_url, anchor["href"]))
    return None


async def discover_feed_posts(session, source_url: str) -> List[DiscoveredPost]:
    candidates = []
    parsed = urlparse(source_url)
    base = urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
    for path in ("feed", "rss", "atom.xml", "index.xml"):
        candidates.append(urljoin(base, path))
    if parsed.path and parsed.path != "/":
        for path in ("feed", "rss", "atom.xml", "index.xml"):
            candidates.append(urljoin(source_url.rstrip("/") + "/", path))

    seen_feeds = set()
    for feed_url in candidates:
        if feed_url in seen_feeds:
            continue
        seen_feeds.add(feed_url)
        feed_xml, final_url = await fetch_with_retry(session, feed_url, "text", max_retries=1)
        if not feed_xml:
            continue
        posts = extract_feed_posts(feed_xml, final_url or feed_url)
        if posts:
            return posts
    return []


async def hydrate_candidate_date(session, post: DiscoveredPost, options: ConversionOptions, default_year: Optional[int]) -> DiscoveredPost:
    fallback = (options.date_fallback or "auto").lower()
    if post.published_date or fallback == "shallow":
        return post
    html, final_url = await fetch_with_retry(session, post.url, "text", max_retries=1)
    if not html:
        return post
    parsed = extract_metadata_date(html, final_url or post.url, default_year=default_year)
    if not parsed and fallback in {"auto", "full"}:
        data = await ArticleExtractor.get_article_content(session, final_url or post.url, raw_html=html)
        parsed = parse_date_value(data.get("date"), default_year=default_year)
    return DiscoveredPost(url=canonical_url(final_url or post.url), published_date=parsed, title=post.title)


def in_range(value: Optional[date], start: Optional[date], end: Optional[date], include_undated: bool) -> bool:
    if value is None:
        return include_undated
    if start and value < start:
        return False
    if end and value > end:
        return False
    return True


def sorted_posts_for_output(posts: List[DiscoveredPost], options: ConversionOptions) -> List[DiscoveredPost]:
    descending = (getattr(options, "date_sort", "asc") or "asc").lower() == "desc"

    def key(post: DiscoveredPost):
        if post.published_date is None:
            return (1, 0)
        ordinal = post.published_date.toordinal()
        return (0, -ordinal if descending else ordinal)

    return sorted(posts, key=key)


def default_year_from_bounds(start: Optional[date], end: Optional[date]) -> Optional[int]:
    if start and end and start.year == end.year:
        return start.year
    if start:
        return start.year
    if end:
        return end.year
    return None


async def discover_posts_for_sources(session, sources: Iterable[Source], options: ConversionOptions) -> List[Source]:
    start = parse_bound(options.start_date, is_end=False)
    end = parse_bound(options.end_date, is_end=True)
    if start and end and start > end:
        raise DiscoveryError("start_date must be on or before end_date.")

    default_year = default_year_from_bounds(start, end)
    by_url: Dict[str, DiscoveredPost] = {}

    for source in sources:
        page_url = source.url
        source_had_candidates = False
        canonical_source_url = canonical_url(source.url)
        if looks_like_article_url(canonical_source_url, source.url):
            by_url[canonical_source_url] = DiscoveredPost(
                url=canonical_source_url,
                published_date=date_from_url_path(canonical_source_url),
            )
            source_had_candidates = True
        visited = set()
        for _ in range(max(1, options.max_discovery_pages)):
            if page_url in visited:
                break
            visited.add(page_url)
            html = source.html if page_url == source.url and source.html else None
            if html is None:
                html, final_url = await fetch_with_retry(session, page_url, "text", max_retries=1)
                page_url = final_url or page_url
            if not html:
                break

            for post in extract_candidate_posts(html, page_url, default_year):
                source_had_candidates = True
                if post.url not in by_url:
                    by_url[post.url] = post
                if len(by_url) >= options.max_discovered_posts:
                    break
            if len(by_url) >= options.max_discovered_posts:
                break

            next_page = extract_next_page(html, page_url)
            if not next_page:
                break
            page_url = next_page

        if not source_had_candidates and len(by_url) < options.max_discovered_posts:
            for post in await discover_feed_posts(session, source.url):
                if post.url not in by_url:
                    by_url[post.url] = post
                if len(by_url) >= options.max_discovered_posts:
                    break

    hydrated: List[DiscoveredPost] = []
    for post in list(by_url.values())[: options.max_discovered_posts]:
        hydrated.append(await hydrate_candidate_date(session, post, options, default_year))

    filtered = [
        post for post in hydrated
        if in_range(post.published_date, start, end, options.include_undated)
    ]
    deduped = []
    seen = set()
    for post in sorted_posts_for_output(filtered, options):
        key = canonical_url(post.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(Source(
            url=post.url,
            published_date=post.published_date.isoformat() if post.published_date else None,
        ))

    if not deduped:
        raise DiscoveryError("No posts found in date range.")
    log.info(f"Discovered {len(deduped)} posts in date range.")
    return deduped

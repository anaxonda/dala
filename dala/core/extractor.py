import asyncio
import base64
import html as html_lib
import os
import re
import trafilatura
from bs4 import BeautifulSoup, Comment, Tag
from datetime import datetime, timezone
from urllib.parse import urlparse, quote
from typing import Optional
from yarl import URL

from ..models import log, ARCHIVE_ORG_API_BASE, SiteProfile
from .browser import DEFAULT_BROWSER_PROFILE_DIR, BrowserChallengeError, BrowserFetchError, BrowserFetchOptions, detect_browser_challenge, fetch_rendered_source, resolve_browser_extension_path
from .session import fetch_with_retry

class ArticleExtractor:
    @staticmethod
    def _store_browser_cookies(session, url: str, cookies: dict) -> None:
        if not cookies:
            return
        try:
            session.cookie_jar.update_cookies(cookies, response_url=URL(url))
        except Exception as exc:
            log.debug(f"Could not store browser cookies in aiohttp jar for {url}: {exc}")
        try:
            extra = dict(getattr(session, "_extra_cookies", {}) or {})
            extra.update(cookies)
            setattr(session, "_extra_cookies", extra)
        except Exception as exc:
            log.debug(f"Could not store browser cookies for requests fallback for {url}: {exc}")

    @staticmethod
    def _should_raise_browser_challenge(browser_options: Optional[BrowserFetchOptions]) -> bool:
        return bool(
            browser_options
            and browser_options.challenge_action in {"user_browser", "warm", "error"}
        )

    @staticmethod
    def _log_browser_challenge_archive_fallback(url: str, marker: str) -> None:
        log.warning(
            "Browser challenge detected for %s (%s). Falling back to archive.",
            url,
            marker,
        )

    @staticmethod
    def _paywall_fallback_has_content(extracted: dict, min_chars: int = 1800) -> bool:
        if not extracted.get("is_paywall") or not extracted.get("html"):
            return False
        text = BeautifulSoup(extracted["html"], "html.parser").get_text(" ", strip=True)
        return len(text) >= min_chars

    @staticmethod
    def browser_options_from_conversion_options(options) -> Optional[BrowserFetchOptions]:
        if not getattr(options, "browser_fallback", False):
            return None
        extension_path = resolve_browser_extension_path(getattr(options, "browser_extension_path", None))
        return BrowserFetchOptions(
            extension_path=extension_path,
            profile_dir=getattr(options, "browser_profile_dir", None) or os.getenv("DALA_BROWSER_PROFILE_DIR") or str(DEFAULT_BROWSER_PROFILE_DIR),
            executable_path=getattr(options, "browser_executable", None) or os.getenv("DALA_BROWSER_EXECUTABLE"),
            timeout_ms=getattr(options, "browser_timeout_ms", 30000) or 30000,
            wait_until=getattr(options, "browser_wait_until", "load") or "load",
            settle_ms=getattr(options, "browser_settle_ms", 1000),
            challenge_action=getattr(options, "browser_challenge_action", "archive"),
        )

    @staticmethod
    def _normalize_wayback_snapshot_url(snapshot_url: str) -> str:
        if not snapshot_url:
            return snapshot_url
        if snapshot_url.startswith("http:"):
            return snapshot_url.replace("http:", "https:", 1)
        if snapshot_url.startswith("//"):
            return f"https:{snapshot_url}"
        return snapshot_url

    @staticmethod
    def _extract_wayback_snapshot_from_available(data: dict) -> Optional[str]:
        try:
            closest = data.get("archived_snapshots", {}).get("closest", {})
            if closest.get("available") and closest.get("url"):
                return ArticleExtractor._normalize_wayback_snapshot_url(closest["url"])
        except Exception:
            return None
        return None

    @staticmethod
    def build_meta_block(url: str, data: dict, context: Optional[str] = None, summary_html: Optional[str] = None) -> str:
        """Shared article metadata block with source, author, date, site, archive info."""
        author = data.get('author') or 'Unknown'
        date = data.get('date') or 'Unknown'
        site = data.get('sitename') or urlparse(url).netloc or 'Unknown'
        source_label = site if site != "Unknown" else (urlparse(url).netloc or "Source")
        detail_parts = []
        if author != "Unknown":
            detail_parts.append(f"<strong>Author:</strong> {html_lib.escape(str(author))}")
        if date != "Unknown":
            detail_parts.append(f"<strong>Date:</strong> {html_lib.escape(str(date))}")
        if site != "Unknown" and site != source_label:
            detail_parts.append(f"<strong>Site:</strong> {html_lib.escape(str(site))}")
        rows = [
            (
                f"<p><strong>Source:</strong> "
                f"<a href=\"{html_lib.escape(url, quote=True)}\">{html_lib.escape(source_label)}</a></p>"
            ),
        ]
        if detail_parts:
            rows.append(f"<p>{' | '.join(detail_parts)}</p>")
        if context:
            rows.append(context)
        if data.get('was_archived') and data.get('archive_url'):
            archive_url = str(data["archive_url"])
            rows.append(
                f"<p class=\"archive-notice\">Archived: "
                f"<a href=\"{html_lib.escape(archive_url, quote=True)}\">Wayback snapshot</a></p>"
            )
        
        if summary_html:
            rows.append(f"<div class='ai-summary'><h3>AI Summary</h3>{summary_html}</div>")

        return "<div class=\"post-meta\">" + "".join(rows) + "</div>"

    @staticmethod
    def build_article_html(title: str, body_html: str, meta_html: str = "", lang: str = "en", include_hr: bool = False) -> str:
        """Build a consistent, escaped XHTML article wrapper."""
        safe_title = html_lib.escape(str(title or "Untitled"))
        safe_lang = html_lib.escape(str(lang or "en"), quote=True)
        divider = "<hr/>" if include_hr and meta_html else ""
        return (
            f'<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="{safe_lang}">'
            f'<head><title>{safe_title}</title><link rel="stylesheet" href="style/default.css"/></head>'
            f"<body><h1>{safe_title}</h1>{meta_html}{divider}{body_html}</body></html>"
        )

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

            # Check for paywalls/truncation markers
            paywall_selectors = [
                '[data-testid="optimistic-truncator-message"]',
                '#gateway-content',
                '.paywall-content',
                '.subscribe-promo',
                '#reg-wall-message'
            ]
            for ps in paywall_selectors:
                if soup.select_one(ps):
                    log.warning(f"Paywall/Truncation detected using selector: {ps}")
                    # Return extracted HTML (truncated) but keep success=False to trigger archive
                    truncated_html = None
                    if content_soup:
                         ArticleExtractor._clean_soup(content_soup)
                         truncated_html = content_soup.prettify()
                    return {
                        'success': False, 
                        'html': truncated_html, 
                        'error': 'Paywall detected', 
                        'is_paywall': True,
                        'title': metadata.title if metadata else None,
                        'author': metadata.author if metadata else None,
                        'date': metadata.date if metadata else None,
                        'sitename': metadata.sitename if metadata else None
                    }

            extracted_html = None
            if content_soup:
                ArticleExtractor._clean_soup(content_soup)
                extracted_html = content_soup.prettify()
            else:
                log.info("Selectors failed, falling back to Trafilatura extraction.")
                extracted_html = trafilatura.extract(html_content, include_images=True, include_tables=True, output_format='html')

            if not extracted_html or len(extracted_html) < 50:
                best_fallback = ArticleExtractor._best_readable_container(soup)
                if best_fallback and len(best_fallback.get_text()) > 100:
                     log.warning("Extraction returned empty. Using best readable container fallback.")
                     ArticleExtractor._clean_soup(best_fallback)
                     extracted_html = best_fallback.prettify()
                elif soup.body and len(soup.body.get_text()) > 100:
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
    def _best_readable_container(soup):
        selectors = [
            'article',
            'main',
            '[role="main"]',
            '.entry-content',
            '.post-content',
            '.article-content',
            '.article-body',
            '.storycontent',
            '.post',
            '.hentry',
            '#content',
            '#main',
        ]
        candidates = []
        seen = set()
        for selector in selectors:
            candidates.extend(soup.select(selector))
        if soup.body:
            candidates.extend(soup.body.find_all(['section', 'div'], recursive=True))

        best = None
        best_score = 0
        for candidate in candidates:
            ident = id(candidate)
            if ident in seen:
                continue
            seen.add(ident)
            text = candidate.get_text(" ", strip=True)
            text_len = len(text)
            if text_len < 200:
                continue
            link_text_len = sum(len(a.get_text(" ", strip=True)) for a in candidate.find_all("a"))
            paragraph_count = len(candidate.find_all(["p", "li", "blockquote", "table", "figure"]))
            score = text_len - int(link_text_len * 1.5) + (paragraph_count * 25)
            if score > best_score:
                best = candidate
                best_score = score

        return best

    @staticmethod
    def _compact_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _remove_boilerplate_block(tag: Tag, max_chars: int = 2400) -> None:
        candidate = tag
        for ancestor in tag.parents:
            if not isinstance(ancestor, Tag) or ancestor.name in {"body", "html", "article", "main"}:
                break
            text = ArticleExtractor._compact_text(ancestor.get_text(" ", strip=True))
            if text and len(text) <= max_chars:
                candidate = ancestor
            else:
                break
        candidate.decompose()

    @staticmethod
    def _remove_publisher_boilerplate(soup) -> None:
        heading_re = re.compile(
            r"^(?:"
            r"social sharing|popular now(?:\s+in .*)?|trending videos?|discover more from .+|"
            r"recommended(?: for you)?|related stories?|related articles?|related podcast|"
            r"table of contents|download pdf|advertisement|listen to this article|watch\s*\|.*"
            r")$",
            re.IGNORECASE,
        )
        short_widget_re = re.compile(
            r"^(?:"
            r"progress|volume|mute|unmute|play|pause|0:00|"
            r"ai-generated audio|report an issue|give feedback"
            r")$",
            re.IGNORECASE,
        )

        for selector in [
            "[aria-label*='share' i]",
            "[data-testid*='share' i]",
            "[data-cy*='share' i]",
            "[data-cy*='author-image' i]",
            "[data-cy*='player' i]",
            "[data-testid*='player' i]",
            "[data-testid*='video' i]",
            "[class*='share' i]",
            "[class*='social' i]",
            "[class*='recommended' i]",
            "[class*='related' i]",
            "[class*='trending' i]",
            "[class*='popular' i]",
            "[class*='podcast' i]",
            "[class*='video-player' i]",
            "[class*='advertisement' i]",
            "[class*='ad-' i]",
        ]:
            try:
                for tag in list(soup.select(selector)):
                    text = ArticleExtractor._compact_text(tag.get_text(" ", strip=True))
                    if not text or len(text) <= 2400:
                        tag.decompose()
            except Exception:
                continue

        for tag in list(soup.find_all(["h1", "h2", "h3", "h4", "h5", "p", "div", "span", "button", "a", "strong", "em"])):
            if tag.name is None:
                continue
            text = ArticleExtractor._compact_text(tag.get_text(" ", strip=True))
            if not text or len(text) > 120:
                continue
            if heading_re.match(text):
                ArticleExtractor._remove_boilerplate_block(tag)
            elif short_widget_re.match(text):
                tag.decompose()

        for marker in list(soup.find_all(string=lambda value: isinstance(value, str) and "This audio was generated" in value)):
            parent = marker.find_parent(["div", "section", "aside", "p"])
            if parent:
                ArticleExtractor._remove_boilerplate_block(parent)

    @staticmethod
    def _clean_soup(soup):
        ArticleExtractor._remove_publisher_boilerplate(soup)
        ArticleExtractor._convert_injected_svg_images(soup)
        # FIX: Removed 'header' from the kill list. NYTimes puts the main image inside a <header> within the <article>.
        for tag in soup(['script', 'style', 'noscript', 'iframe', 'footer', 'nav', 'aside', 'form', 'button', 'svg']):
            tag.decompose()
        for tag in soup.find_all(True):
            classes = " ".join(tag.get("class") or []).casefold()
            if "upper-caption" in classes:
                tag["data-dala-upper-caption"] = "1"
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            text = str(comment or "")
            if "<img" in text.lower():
                fragment = BeautifulSoup(text, "html.parser")
                imgs = [
                    img
                    for img in fragment.find_all("img")
                    if img.get("src") or img.get("data-src") or img.get("srcset") or img.get("data-srcset")
                ]
                for img in imgs:
                    comment.insert_before(img)
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
    def _convert_injected_svg_images(soup):
        for svg in list(soup.find_all("svg")):
            src = (
                svg.get("data-inject-url")
                or svg.get("data-src")
                or svg.get("src")
                or svg.get("href")
            )
            if src:
                lower = src.lower()
            else:
                classes = " ".join(svg.get("class") or []).casefold()
                data_name = str(svg.get("data-name") or "").casefold()
                figure = svg.find_parent("figure")
                figure_text = ArticleExtractor._compact_text(figure.get_text(" ", strip=True)) if figure else ""
                looks_like_article_svg = bool(
                    figure
                    and (
                        len(str(svg)) > 1000
                        or "graphic" in figure_text.casefold()
                        or "data" in figure_text.casefold()
                        or "map" in figure_text.casefold()
                        or "locator" in data_name
                    )
                )
                looks_like_icon = any(marker in classes or marker in data_name for marker in ["icon", "logo", "sprite", "avatar"])
                if not looks_like_article_svg or looks_like_icon:
                    continue
                svg_markup = str(svg)
                src = "data:image/svg+xml;base64," + base64.b64encode(svg_markup.encode("utf-8")).decode("ascii")
                lower = src.lower()

            basename = os.path.basename(urlparse(lower).path)
            if any(marker in lower for marker in ["/sprite", "sprite.", "/logo", "logo.", "/avatar", "favicon"]):
                continue

            caption = ""
            figure = svg.find_parent("figure")
            if figure:
                lower_caption = None
                for candidate in figure.find_all(["figcaption", "div", "p"], recursive=True):
                    classes = " ".join(candidate.get("class") or []).casefold()
                    text = ArticleExtractor._compact_text(candidate.get_text(" ", strip=True))
                    if not text:
                        continue
                    if "upper-caption" not in classes and ("graphic" in text.casefold() or "data" in text.casefold()):
                        lower_caption = text
                        break
                caption = lower_caption or ArticleExtractor._compact_text(figure.get_text(" ", strip=True))
            if not caption:
                caption = svg.get("aria-label") or svg.get("title") or svg.get("data-name") or basename or "Article graphic"

            img = BeautifulSoup("", "html.parser").new_tag(
                "img",
                src=src,
                alt=caption[:240],
            )
            if svg.get("data-name"):
                img["data-name"] = svg.get("data-name")
            if svg.get("width"):
                img["width"] = svg.get("width")
            if svg.get("height"):
                img["height"] = svg.get("height")
            svg.replace_with(img)

    @staticmethod
    async def get_wayback_url(session, target_url):
        encoded_target = quote(target_url, safe='')
        current_yyyymm = datetime.now(timezone.utc).strftime("%Y%m")
        available_queries = [
            f"{ARCHIVE_ORG_API_BASE}?url={encoded_target}",
            f"{ARCHIVE_ORG_API_BASE}?timestamp={current_yyyymm}&url={encoded_target}",
        ]

        for api_url in available_queries:
            try:
                log.info(f"Checking Wayback Machine: {api_url}")
                data, _ = await fetch_with_retry(session, api_url, 'json', max_retries=2, backoff=0.75)
                log.debug(f"Wayback API response: {data}")
                snap = ArticleExtractor._extract_wayback_snapshot_from_available(data or {})
                if snap:
                    log.info(f"Found archive snapshot: {snap}")
                    return snap
            except Exception as e:
                log.warning(f"Wayback availability lookup failed for {api_url}: {e}")

        # `wayback/available` can intermittently miss snapshots. CDX is a stronger fallback.
        cdx_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url={encoded_target}"
            "&output=json"
            "&fl=timestamp,original,statuscode,mimetype"
            "&filter=statuscode:200"
            "&filter=mimetype:text/html"
            "&limit=1"
            "&sort=reverse"
        )
        try:
            log.info(f"Wayback availability empty; checking CDX index: {cdx_url}")
            cdx_data, _ = await fetch_with_retry(session, cdx_url, 'json', max_retries=2, backoff=0.75)
            if isinstance(cdx_data, list) and len(cdx_data) > 1:
                row = cdx_data[1]
                if isinstance(row, list) and len(row) >= 2 and row[0] and row[1]:
                    timestamp = row[0]
                    original = row[1]
                    snap = ArticleExtractor._normalize_wayback_snapshot_url(
                        f"https://web.archive.org/web/{timestamp}/{original}"
                    )
                    log.info(f"Found archive snapshot via CDX: {snap}")
                    return snap
        except Exception as e:
            log.warning(f"Wayback CDX lookup failed: {e}")

        log.warning("No archive snapshot found after availability and CDX checks.")
        return None

    @staticmethod
    async def get_article_content(
        session,
        url,
        force_archive=False,
        raw_html=None,
        profile: Optional[SiteProfile] = None,
        browser_options: Optional[BrowserFetchOptions] = None,
    ):
        result = {'success': False, 'html': None, 'title': None, 'author': None, 'date': None, 'sitename': None, 'was_archived': False, 'archive_url': None}
        loop = asyncio.get_running_loop()

        if raw_html:
            challenge_marker = detect_browser_challenge(raw_html)
            if challenge_marker and ArticleExtractor._should_raise_browser_challenge(browser_options):
                raise BrowserChallengeError(url, challenge_marker)
            if challenge_marker:
                ArticleExtractor._log_browser_challenge_archive_fallback(url, challenge_marker)
            log.info("Using pre-fetched HTML content.")
            extracted = await loop.run_in_executor(None, ArticleExtractor.extract_from_html, raw_html, url, profile)
            if extracted['success']:
                result.update(extracted)
                result['raw_html_for_metadata'] = raw_html
                result['source_url'] = url
                return result
            if ArticleExtractor._paywall_fallback_has_content(extracted):
                log.warning("Pre-fetched HTML is paywall-marked but has substantial content; using it without archive fallback.")
                result.update(extracted)
                result['success'] = True
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

        browser_attempted = False
        fallback_extracted = None
        if browser_options and not raw_html:
            browser_attempted = True
            try:
                log.info(f"Fetching rendered source with browser for {url}")
                rendered = await fetch_rendered_source(url, browser_options)
                extracted = await loop.run_in_executor(None, ArticleExtractor.extract_from_html, rendered.html, rendered.url or url, profile)
                if extracted['success']:
                    ArticleExtractor._store_browser_cookies(session, rendered.url or url, rendered.cookies)
                    result.update(extracted)
                    result['raw_html_for_metadata'] = rendered.html
                    result['source_url'] = rendered.url or url
                    return result
                if extracted.get('is_paywall'):
                    fallback_extracted = extracted
                    if ArticleExtractor._paywall_fallback_has_content(extracted):
                        log.warning("Browser content is paywall-marked but substantial; using it without archive fallback.")
                        ArticleExtractor._store_browser_cookies(session, rendered.url or url, rendered.cookies)
                        result.update(extracted)
                        result['success'] = True
                        result['raw_html_for_metadata'] = rendered.html
                        result['source_url'] = rendered.url or url
                        return result
            except BrowserChallengeError as e:
                if ArticleExtractor._should_raise_browser_challenge(browser_options):
                    raise
                ArticleExtractor._log_browser_challenge_archive_fallback(url, e.marker)
            except BrowserFetchError as e:
                log.warning(f"Browser fetch failed for {url}: {e}")

        # Treat 403 as non-retryable to fast-fail to archive fallback
        raw_html, final_url = await fetch_with_retry(session, url, 'text', non_retry_statuses={403}, max_retries=1)

        if not raw_html:
             log.warning(f"aiohttp failed for {url}, trying requests fallback...")
             req_html, req_url = await ArticleExtractor._requests_fetch(session, url)
             if req_html:
                 raw_html = req_html
                 final_url = req_url

        if raw_html:
             challenge_marker = detect_browser_challenge(raw_html)
             if challenge_marker and ArticleExtractor._should_raise_browser_challenge(browser_options):
                 raise BrowserChallengeError(url, challenge_marker)
             if challenge_marker:
                 ArticleExtractor._log_browser_challenge_archive_fallback(url, challenge_marker)
             extracted = await loop.run_in_executor(None, ArticleExtractor.extract_from_html, raw_html, url, profile)
             if extracted['success']:
                 result.update(extracted)
                 result['raw_html_for_metadata'] = raw_html
                 return result
             
             if extracted.get('is_paywall'):
                 log.info("Paywall detected. Storing truncated content as fallback.")
                 fallback_extracted = extracted

        if browser_options and not browser_attempted and not raw_html and not force_archive:
            try:
                log.info(f"Live extraction failed. Trying browser fallback for {url}")
                rendered = await fetch_rendered_source(url, browser_options)
                extracted = await loop.run_in_executor(None, ArticleExtractor.extract_from_html, rendered.html, rendered.url or url, profile)
                if extracted['success']:
                    ArticleExtractor._store_browser_cookies(session, rendered.url or url, rendered.cookies)
                    result.update(extracted)
                    result['raw_html_for_metadata'] = rendered.html
                    result['source_url'] = rendered.url or url
                    return result
                if extracted.get('is_paywall') and not fallback_extracted:
                    fallback_extracted = extracted
                    if ArticleExtractor._paywall_fallback_has_content(extracted):
                        log.warning("Browser fallback is paywall-marked but has substantial content; using it without archive fallback.")
                        result.update(extracted)
                        result['success'] = True
                        result['raw_html_for_metadata'] = rendered.html
                        result['source_url'] = rendered.url or url
                        return result
            except BrowserChallengeError as e:
                if ArticleExtractor._should_raise_browser_challenge(browser_options):
                    raise
                ArticleExtractor._log_browser_challenge_archive_fallback(url, e.marker)
            except BrowserFetchError as e:
                log.warning(f"Browser fallback failed for {url}: {e}")

        log.warning(f"Live fetch failed. Trying archive...")
        archive_result = await ArticleExtractor.get_article_content(session, url, force_archive=True, profile=profile)

        if archive_result['success']:
            return archive_result

        if fallback_extracted:
            log.warning("Archive fallback failed. Using truncated live content.")
            result.update(fallback_extracted)
            result['success'] = True
            result['raw_html_for_metadata'] = raw_html
            return result
        
        return archive_result

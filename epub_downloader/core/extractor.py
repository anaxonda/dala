import asyncio
import trafilatura
from bs4 import BeautifulSoup, Comment
from urllib.parse import urlparse, quote
from typing import Optional
from yarl import URL

from . .models import log, ARCHIVE_ORG_API_BASE, SiteProfile
from .session import fetch_with_retry

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

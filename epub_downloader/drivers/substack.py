import json
import re
import asyncio
import random
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from pygments.formatters import HtmlFormatter
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter
)
from . .core.extractor import ArticleExtractor
from . .core.image_processor import ImageProcessor
from . .core.session import fetch_with_retry
from . .utils.llm import LLMHelper
from . .utils.formatting import _enrich_comment_tree, format_comment_html

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

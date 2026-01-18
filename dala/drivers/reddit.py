import html
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from ebooklib import epub
from pygments.formatters import HtmlFormatter
from typing import List, Dict, Optional, Any

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter
)
from . .core.extractor import ArticleExtractor
from . .core.image_processor import ImageProcessor
from . .core.session import fetch_with_retry
from . .utils.llm import LLMHelper
from . .utils.formatting import _enrich_comment_tree, format_comment_html

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
                context_html = f"<p><strong>Reddit Link:</strong> <a href=\"{source.url}\">{source.url}</a></p>"
                meta_html = ArticleExtractor.build_meta_block(link_url, {"author": None, "date": None, "sitename": urlparse(link_url).netloc}, context=context_html)
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
                    context_html = f"<p><strong>Reddit Link:</strong> <a href=\"{source.url}\">{source.url}</a></p>"
                    meta_html = ArticleExtractor.build_meta_block(link_url, art_data, context=context_html, summary_html=summary_html)
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
                full_com_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>Reddit Comments</title><link rel="stylesheet" href="style/default.css"/></head><body>
                <h1>Reddit Comments</h1>{comments_html}</body></html>"""
                com_chap = Chapter(title="Reddit Comments", filename="comments.xhtml", content_html=full_com_html, uid=f"reddit_com_{post_id}", is_comments=True)
                chapters.append(com_chap)

        toc_links = []
        if art_chap: toc_links.append(epub.Link(art_chap.filename, "Post", art_chap.uid))
        if art_chap and com_chap:
            toc_links = [(epub.Link(art_chap.filename, "Post", art_chap.uid), [epub.Link(com_chap.filename, "Reddit Comments", com_chap.uid)])]
        elif art_chap:
            toc_links = [epub.Link(art_chap.filename, "Post", art_chap.uid)]
        elif com_chap:
            toc_links = [epub.Link(com_chap.filename, "Reddit Comments", com_chap.uid)]

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

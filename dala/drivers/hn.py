import asyncio
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
from ebooklib import epub
from pygments.formatters import HtmlFormatter
from typing import List, Dict, Optional

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter, HN_API_BASE_URL
)
from . .core.extractor import ArticleExtractor
from . .core.image_processor import ImageProcessor
from . .core.session import fetch_with_retry
from . .core.profiles import ProfileManager
from . .utils.llm import LLMHelper
from . .utils.formatting import _enrich_comment_tree, format_comment_html, fetch_comments_recursive

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
        sub_com_chap = None
        site_label = "Source"

        if (article_url and not options.no_article) or post_text:
            art_title = title
            summary_html = None
            
            sub_book = None
            if article_url and not options.no_article:
                try:
                    # Circular import avoidance: we'll need a way to get the dispatcher or drivers here.
                    # For now, let's assume we can import GenericDriver here to check.
                    from .generic import GenericDriver
                    from . .core.dispatcher import DriverDispatcher
                    
                    temp_source = Source(url=article_url, cookies=source.cookies) 
                    temp_profile = ProfileManager.get_instance().get_profile(article_url)
                    temp_driver = DriverDispatcher.get_driver(temp_source, temp_profile)
                    
                    # If it's not Generic and not HN (recursive), use it
                    if not isinstance(temp_driver, (GenericDriver, HackerNewsDriver)):
                        site_label = temp_driver.__class__.__name__.replace("Driver", "")
                        log.info(f"Delegating linked article to {temp_driver.__class__.__name__} (Site: {site_label})")
                        sub_book = await temp_driver.prepare_book_data(context, temp_source)
                except Exception as e:
                    log.warning(f"Failed to delegate to specialized driver: {e}")

            if sub_book:
                # Merge the sub-driver's chapters (Article + potentially Comments)
                for chap in sub_book.chapters:
                    # Prefix uid/filename to ensure uniqueness
                    chap.uid = f"linked_{chap.uid}" 
                    chap.filename = f"linked_{chap.filename}"
                    chapters.append(chap)
                    if chap.is_article: 
                        art_chap = chap
                        if sub_book.title and sub_book.title != "Unknown":
                            art_title = sub_book.title
                    if chap.is_comments:
                        sub_com_chap = chap
                
                if sub_book.images:
                    assets.extend(sub_book.images)

            elif article_url and not options.no_article:
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
                    context_html = f"<p><strong>HN Source:</strong> <a href=\"{url}\">{title}</a></p>"
                    meta_html = ArticleExtractor.build_meta_block(article_url, art_data, context=context_html, summary_html=summary_html)
                    art_html = f"{meta_html}<hr/>{art_html}"
                else: art_html = f"<p>Could not fetch article: <a href='{article_url}'>{article_url}</a></p>"
                
                final_art_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{art_title}</title><link rel="stylesheet" href="style/default.css"/></head><body>
                <h1>{art_title}</h1>{art_html}</body></html>"""
                art_chap = Chapter(title=art_title, filename="article.xhtml", content_html=final_art_html, uid="article", is_article=True)
                chapters.append(art_chap)

            elif post_text:
                if options.summary:
                    log.info("Generating AI summary for HN Self Text...")
                    summary_html = await LLMHelper.generate_summary(post_text, options.llm_model, options.llm_api_key)
                
                sum_div = f"<div class='ai-summary'><h3>AI Summary</h3>{summary_html}</div><hr/>" if summary_html else ""
                art_html = f"{sum_div}<div>{post_text}</div>"
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
             com_chap = Chapter(title="HN Comments", filename="hn_comments.xhtml", content_html=full_com_html, uid="hn_comments", is_comments=True)
             chapters.append(com_chap)

        toc_structure = []
        if art_chap:
            children = []
            if sub_com_chap:
                children.append(epub.Link(sub_com_chap.filename, f"{site_label} Comments", sub_com_chap.uid))
            if com_chap:
                children.append(epub.Link(com_chap.filename, "HN Comments", com_chap.uid))
            if children:
                toc_structure.append((epub.Link(art_chap.filename, "Article", art_chap.uid), children))
            else:
                toc_structure.append(epub.Link(art_chap.filename, "Article", art_chap.uid))
        else:
            if sub_com_chap:
                toc_structure.append(epub.Link(sub_com_chap.filename, f"{site_label} Comments", sub_com_chap.uid))
            if com_chap:
                toc_structure.append(epub.Link(com_chap.filename, "HN Comments", com_chap.uid))

        return BookData(title=title, author=author, uid=f"urn:hn:{item_id}", language='en', description=f"HN Thread {item_id}", source_url=url, chapters=chapters, images=assets, toc_structure=toc_structure)

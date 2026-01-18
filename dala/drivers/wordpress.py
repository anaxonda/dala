from bs4 import BeautifulSoup
from ebooklib import epub
from pygments.formatters import HtmlFormatter
from typing import List, Dict, Optional

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter
)
from . .core.extractor import ArticleExtractor
from . .core.image_processor import ImageProcessor
from . .utils.llm import LLMHelper
from . .utils.formatting import _enrich_comment_tree, format_comment_html

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
            
            author_tag = li.select_one('.comment-author .fn, .comment-author cite')
            author = author_tag.get_text(strip=True) if author_tag else "Anonymous"
            
            text_tag = li.select_one('.comment-content, .comment-body > p')
            text = str(text_tag) if text_tag else ""
            
            time_tag = li.select_one('.comment-metadata time, .comment-meta time')
            timestamp = time_tag.get('datetime') if time_tag else ""
            
            comment_id = li.get('id') or f"c_{abs(hash(text))}"
            
            norm = {
                'id': str(comment_id),
                'by': author,
                'text': text,
                'time': timestamp,
                'children_data': []
            }
            
            children_list = li.select_one('ol.children, ul.children')
            if children_list:
                norm['children_data'] = self._parse_comments(children_list)
            
            results.append(norm)
        return results

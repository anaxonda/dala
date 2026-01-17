from bs4 import BeautifulSoup
from ebooklib import epub
from typing import Optional

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter
)
from . .core.extractor import ArticleExtractor
from . .core.image_processor import ImageProcessor
from . .utils.llm import LLMHelper

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
             from .substack import SubstackDriver
             return await SubstackDriver().prepare_book_data(context, source)

        if 'data-template="thread_view"' in raw_html or 'xenforo' in raw_html.lower():
             log.info("Detected Forum metadata after fetch. Switching to ForumDriver.")
             from .forum import ForumDriver
             return await ForumDriver().prepare_book_data(context, source)

        title = data['title'] or "Untitled Webpage"
        soup = BeautifulSoup(data['html'], 'html.parser')
        body_soup = soup.body if soup.body else soup

        assets = []
        if not options.no_images:
            base = data.get('archive_url') if data.get('was_archived') else data.get('source_url', url)
            
            if raw_html and "__NEXT_DATA__" in raw_html:
                log.info("Attempting to seed from __NEXT_DATA__ first.")
                await ImageProcessor._seed_images_from_nextjs_data(raw_html, body_soup, base, assets, session, profile=context.profile)

            await ImageProcessor.process_images(session, body_soup, base, assets, profile=context.profile)
            
            if assets and not body_soup.find('img'):
                for asset in assets:
                    wrapper = body_soup.new_tag("div", attrs={"class": "img-block"})
                    img_tag = body_soup.new_tag("img", attrs={"src": asset.filename, "class": "epub-image"})
                    wrapper.append(img_tag)
                    body_soup.append(wrapper)
                for fc in list(body_soup.find_all("figcaption")):
                    fc.decompose()

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

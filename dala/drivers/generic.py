import base64
import mimetypes
import re
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup
from ebooklib import epub
from typing import Optional

from .base import BaseDriver
from ..models import (
    log, BookData, ConversionContext, Source, Chapter, ImageAsset, IMAGE_DIR_IN_EPUB
)
from ..core.extractor import ArticleExtractor
from ..core.image_processor import ImageProcessor
from ..core.session import fetch_with_retry
from ..utils.llm import LLMHelper

class GenericDriver(BaseDriver):
    LINKED_TABLE_HINTS = (
        "table",
        "tables",
        "standings",
        "ranking",
        "schedule",
        "fixtures",
        "results",
        "knockout",
    )

    @staticmethod
    def _seed_preloaded_assets(source: Source, assets: list, options=None) -> int:
        seeded = 0
        max_dim, quality, color_mode, output_pref = ImageProcessor.image_optimize_params(options)
        for item in source.assets or []:
            raw = item.get("content")
            if isinstance(raw, str):
                try:
                    raw = base64.b64decode(raw)
                except Exception:
                    raw = None
            if not raw:
                continue
            url_like = item.get("canonical_url") or item.get("original_url") or item.get("viewer_url") or ""
            if not isinstance(url_like, str) or not url_like.startswith(("http://", "https://")):
                continue
            header_mime = item.get("media_type") or item.get("content_type") or mimetypes.guess_type(url_like)[0] or "image/jpeg"
            headers = {"Content-Type": header_mime}
            mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(
                url_like,
                headers,
                raw,
                max_dimension=max_dim,
                jpeg_quality=quality,
                color_mode=color_mode,
                output_preference=output_pref,
            )
            if val_err or not final_data:
                log.debug(f"Skipping preloaded image {url_like}: {val_err}")
                continue
            fname_base = ImageProcessor._image_filename_base(url_like, fallback_seed=url_like)
            if not ext or not ext.startswith("."):
                ext = mimetypes.guess_extension(mime) or ".img"
            fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
            count = 0
            while any(existing.filename == fname for existing in assets):
                count += 1
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"
            alt_urls = []
            for u in (item.get("original_url"), item.get("viewer_url"), item.get("canonical_url")):
                if u and isinstance(u, str):
                    alt_urls.append(u)
                    if "?" in u:
                        alt_urls.append(u.split("?", 1)[0])
            assets.append(ImageAsset(
                uid=f"img_{ImageProcessor._short_stable_hash(fname)}",
                filename=fname,
                media_type=mime,
                content=final_data,
                original_url=url_like,
                alt_urls=list(dict.fromkeys(alt_urls)) or None,
            ))
            seeded += 1
        return seeded

    @staticmethod
    def _compact_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _linked_table_urls(raw_html: str, body_soup: BeautifulSoup, base_url: str) -> list[str]:
        candidates = []
        origins = {urlparse(base_url).netloc.lower().removeprefix("www.")}
        soups = [body_soup]
        if raw_html:
            try:
                soups.append(BeautifulSoup(raw_html, "html.parser"))
            except Exception:
                pass

        for soup in soups:
            for link in soup.find_all("a", href=True):
                text = GenericDriver._compact_text(link.get_text(" ", strip=True)).casefold()
                href = str(link.get("href") or "")
                full_url = urljoin(base_url, href)
                parsed = urlparse(full_url)
                if parsed.scheme not in {"http", "https"}:
                    continue
                origin = parsed.netloc.lower().removeprefix("www.")
                if origin not in origins:
                    continue
                no_fragment, _ = urldefrag(full_url)
                if normalize_url := no_fragment.rstrip("/"):
                    base_normalized = urldefrag(base_url)[0].rstrip("/")
                    if normalize_url == base_normalized:
                        continue
                hint_text = f"{text} {parsed.path} {parsed.fragment}".casefold()
                if not any(hint in hint_text for hint in GenericDriver.LINKED_TABLE_HINTS):
                    continue
                if no_fragment not in candidates:
                    candidates.append(no_fragment)
                if len(candidates) >= 2:
                    return candidates
        return candidates

    @staticmethod
    def _nearest_table_heading(table: BeautifulSoup) -> Optional[str]:
        for previous in table.find_all_previous(["h2", "h3", "h4"], limit=6):
            text = GenericDriver._compact_text(previous.get_text(" ", strip=True))
            if text and len(text) <= 120:
                return text
        wrapper = table.find_parent(["section", "article", "div"])
        if wrapper:
            heading = wrapper.find(["h2", "h3", "h4"])
            if heading:
                text = GenericDriver._compact_text(heading.get_text(" ", strip=True))
                if text and len(text) <= 120:
                    return text
        return None

    @staticmethod
    def _simplified_table(table: BeautifulSoup, factory: BeautifulSoup):
        clone = BeautifulSoup(str(table), "html.parser").find("table")
        if not clone:
            return None
        for tag in list(clone.find_all(["script", "style", "svg", "img", "button", "form"])):
            tag.decompose()
        for tag in list(clone.find_all(True)):
            if tag.parent is None:
                continue
            if tag.name in {"th", "td"}:
                kept = {key: tag[key] for key in ("colspan", "rowspan", "scope") if tag.has_attr(key)}
                text = GenericDriver._compact_text(tag.get_text(" ", strip=True))
                tag.clear()
                tag.string = text
                tag.attrs = kept
            elif tag.name not in {"table", "thead", "tbody", "tfoot", "tr", "caption", "colgroup", "col"}:
                tag.unwrap()
            else:
                tag.attrs = {}
        header_row = clone.find("tr")
        if header_row:
            header_cells = header_row.find_all(["th", "td"], recursive=False)
            drop_indexes = [
                idx
                for idx, cell in enumerate(header_cells)
                if any(
                    token in GenericDriver._compact_text(cell.get_text(" ", strip=True)).casefold()
                    for token in ("form", "last 6", "last six")
                )
            ]
            for row in clone.find_all("tr"):
                row_cells = row.find_all(["th", "td"], recursive=False)
                for idx in sorted(drop_indexes, reverse=True):
                    if idx < len(row_cells):
                        row_cells[idx].decompose()
        rows = clone.find_all("tr")
        useful_rows = [row for row in rows if GenericDriver._compact_text(row.get_text(" ", strip=True))]
        if len(useful_rows) < 2:
            return None
        clone["class"] = "linked-table"
        return BeautifulSoup(str(clone), "html.parser").find("table")

    @staticmethod
    async def _append_linked_reference_tables(session, raw_html: str, body_soup: BeautifulSoup, base_url: str) -> int:
        if body_soup.find("table"):
            return 0
        links = GenericDriver._linked_table_urls(raw_html, body_soup, base_url)
        if not links:
            return 0

        factory = ImageProcessor._tag_factory(body_soup)
        appended = 0
        section = factory.new_tag("section", attrs={"class": "linked-reference-tables"})
        heading = factory.new_tag("h2")
        heading.string = "Linked Tables"
        section.append(heading)
        seen_tables = set()

        for linked_url in links:
            linked_html, final_url = await fetch_with_retry(
                session,
                linked_url,
                response_type="text",
                referer=base_url,
                max_retries=1,
            )
            if not linked_html:
                continue
            linked_soup = BeautifulSoup(linked_html, "html.parser")
            for table in linked_soup.find_all("table")[:20]:
                simplified = GenericDriver._simplified_table(table, factory)
                if not simplified:
                    continue
                signature = GenericDriver._compact_text(simplified.get_text(" ", strip=True))[:500]
                if signature in seen_tables:
                    continue
                seen_tables.add(signature)
                table_heading = GenericDriver._nearest_table_heading(table)
                if table_heading:
                    h3 = factory.new_tag("h3")
                    h3.string = table_heading
                    section.append(h3)
                section.append(BeautifulSoup(str(simplified), "html.parser").find("table"))
                appended += 1
                if appended >= 12:
                    break
            if appended >= 12:
                break

        if appended:
            body_soup.append(section)
            log.info(f"Appended {appended} linked reference tables from article links.")
        return appended

    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        log.info(f"Generic Driver processing: {url}")
        if source.cookies:
            ArticleExtractor._store_browser_cookies(session, url, source.cookies)
        browser_options = ArticleExtractor.browser_options_from_conversion_options(options)
        data = await ArticleExtractor.get_article_content(
            session,
            url,
            force_archive=options.archive,
            raw_html=source.html,
            profile=context.profile,
            browser_options=browser_options,
        )
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
        base_for_links = data.get('archive_url') if data.get('was_archived') else data.get('source_url', url)
        await self._append_linked_reference_tables(session, raw_html, body_soup, base_for_links)

        assets = []
        if not options.no_images:
            base = base_for_links
            seeded = self._seed_preloaded_assets(source, assets, options=options)
            if seeded:
                log.info(f"Seeded {seeded} browser/preloaded assets into EPUB.")
            
            if raw_html and "__NEXT_DATA__" in raw_html:
                log.info("Attempting to seed from __NEXT_DATA__ first.")
                await ImageProcessor._seed_images_from_nextjs_data(raw_html, body_soup, base, assets, session, profile=context.profile, options=options)

            await ImageProcessor.process_images(session, body_soup, base, assets, profile=context.profile, options=options)

            if raw_html:
                await ImageProcessor._seed_images_from_metadata(raw_html, body_soup, base, assets, session, options=options)
            
            if assets and not body_soup.find('img'):
                for asset in assets:
                    wrapper = soup.new_tag("div", attrs={"class": "img-block"})
                    img_tag = soup.new_tag("img", attrs={"src": asset.filename, "class": "epub-image"})
                    wrapper.append(img_tag)
                    body_soup.append(wrapper)
                for fc in list(body_soup.find_all("figcaption")):
                    fc.decompose()

            attached = ImageProcessor.attach_contextual_preloaded_assets(body_soup, assets)
            if attached:
                log.info(f"Attached {attached} contextual preloaded image assets.")

            pruned = ImageProcessor.retain_referenced_assets(body_soup, assets)
            if pruned:
                log.info(f"Pruned {pruned} unreferenced image assets from EPUB.")

            if (
                not assets
                and source.html
                and not data.get("was_archived")
                and not options.archive
                and body_soup.find("img")
            ):
                log.warning("Prefetched article HTML yielded no embedded images; retrying archive fallback for media.")
                archive_data = await ArticleExtractor.get_article_content(
                    session,
                    url,
                    force_archive=True,
                    profile=context.profile,
                    browser_options=browser_options,
                )
                if archive_data.get("success"):
                    data = archive_data
                    raw_html = data.get('raw_html_for_metadata') or data.get('html', '')
                    title = data['title'] or title
                    soup = BeautifulSoup(data['html'], 'html.parser')
                    body_soup = soup.body if soup.body else soup
                    assets = []
                    archive_base = data.get('archive_url') or data.get('source_url', url)
                    if raw_html and "__NEXT_DATA__" in raw_html:
                        await ImageProcessor._seed_images_from_nextjs_data(raw_html, body_soup, archive_base, assets, session, profile=context.profile, options=options)
                    await ImageProcessor.process_images(session, body_soup, archive_base, assets, profile=context.profile, options=options)
                    if raw_html:
                        await ImageProcessor._seed_images_from_metadata(raw_html, body_soup, archive_base, assets, session, options=options)
                    pruned = ImageProcessor.retain_referenced_assets(body_soup, assets)
                    if pruned:
                        log.info(f"Pruned {pruned} unreferenced archive image assets from EPUB.")
                    log.info(f"Archive media fallback embedded {len(assets)} images.")

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

        final_html = ArticleExtractor.build_article_html(title, chapter_html, meta_html=meta_html, include_hr=True)

        chapter = Chapter(title=title, filename="index.xhtml", content_html=final_html, uid="chap_index", is_article=True)

        return BookData(
            title=title, author=data['author'] or "Webpage", uid=f"urn:web:{abs(hash(url))}",
            language='en', description=f"Content from {url}", source_url=url,
            chapters=[chapter], images=assets, toc_structure=[epub.Link("index.xhtml", title, "chap_index")]
        )

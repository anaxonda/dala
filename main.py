import argparse
import sys
import asyncio
import aiohttp
import socket
from datetime import datetime
from urllib.parse import urlparse
from typing import List, Dict, Optional
from tqdm.asyncio import tqdm_asyncio
from ebooklib import epub

from dala.models import (
    log, Source, ConversionOptions, BookData, ConversionContext, 
    GLOBAL_SEMAPHORE, REQUEST_TIMEOUT, sanitize_filename, parse_page_spec
)
from dala.core.profiles import ProfileManager
from dala.core.dispatcher import DriverDispatcher
from dala.core.session import get_session, load_cookie_file
from dala.core.writer import EpubWriter

async def process_urls(sources: List[Source], options: ConversionOptions, session) -> List[BookData]:
    processed_books = []

    async def safe_process(source):
        async with GLOBAL_SEMAPHORE:
            profile = ProfileManager.get_instance().get_profile(source.url)
            driver = DriverDispatcher.get_driver(source, profile)

            local_session = session
            if source.cookies:
                connector = aiohttp.TCPConnector(
                    resolver=aiohttp.resolver.ThreadedResolver(),
                    ttl_dns_cache=300,
                    family=socket.AF_INET
                )
                local_session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, cookies=source.cookies, connector=connector)
                setattr(local_session, "_extra_cookies", source.cookies)

            try:
                context = ConversionContext(session=local_session, options=options, profile=profile)
                return await driver.prepare_book_data(context, source)
            except Exception as e:
                log.exception(f"Failed to process {source.url}: {e}")
                return None
            finally:
                if local_session is not session:
                    await local_session.close()

    results = await tqdm_asyncio.gather(*[safe_process(s) for s in sources], desc="Processing URLs")
    return [b for b in results if b]

def create_bundle(books: List[BookData], title: str, author: str) -> BookData:
    master_uid = f"urn:bundle:{abs(hash(title))}"
    master_chapters = []
    master_images = []
    master_toc = []

    for book in books:
        for img in book.images:
            if img not in master_images: master_images.append(img)

        article_chap = None
        comments_chap = None

        for chap in book.chapters:
            chap.filename = f"doc_{abs(hash(book.source_url))}_{chap.filename}"
            master_chapters.append(chap)
            if chap.is_article: article_chap = chap
            elif chap.is_comments: comments_chap = chap

        if article_chap:
             if comments_chap:
                  master_toc.append( (epub.Link(article_chap.filename, book.title, article_chap.uid), [epub.Link(comments_chap.filename, "Comments", comments_chap.uid)]) )
             else:
                  master_toc.append(epub.Link(article_chap.filename, book.title, article_chap.uid))

    return BookData(title=title, author=author, uid=master_uid, language="en", description=f"Bundle of {len(books)} articles.", source_url="", chapters=master_chapters, images=master_images, toc_structure=master_toc)

def parse_args():
    parser = argparse.ArgumentParser(description="Universal Web to EPUB Downloader")
    parser.add_argument("url", nargs='*', help="URL(s) to process")
    parser.add_argument("-i", "--input-file", help="File with URLs")
    parser.add_argument("-o", "--output", help="Output filename")
    parser.add_argument("--no-article", action="store_true")
    parser.add_argument("--no-images", action="store_true")
    parser.add_argument("-a", "--archive", action="store_true")
    parser.add_argument("--no-comments", action="store_true", help="Skip downloading comments entirely")
    parser.add_argument("--bundle", action="store_true", help="Combine all URLs into a single EPUB")
    parser.add_argument("--bundle-title", help="Custom title for the bundle")
    parser.add_argument("--bundle-author", help="Custom author for the bundle")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=None, help="Max forum pages to fetch")
    parser.add_argument("--max-posts", type=int, default=None, help="Max forum posts to fetch")
    parser.add_argument("--pages", help="Specific pages to fetch (e.g. '1,3-5')")
    parser.add_argument("--css", help="Custom CSS file to inject")
    parser.add_argument("--llm", action="store_true", dest="llm_format", help="Format transcript using LLM (requires GEMINI_API_KEY)")
    parser.add_argument("--llm-model", help="Specify LLM model (default: gemini-1.5-flash)")
    parser.add_argument("--api-key", help="API Key for LLM (overrides env vars)")
    parser.add_argument("--summary", action="store_true", help="Generate AI summary at start of article")
    parser.add_argument("--forum", action="store_true", help="Enable forum driver for URLs (autodetected usually)")
    parser.add_argument("--cookie-file", help="Netscape cookie file for gated content")
    parser.add_argument("--compact-comments", action="store_true", help="Use compact comment layout")
    return parser.parse_args()

async def async_main():
    args = parse_args()
    
    urls = []
    if args.input_file:
        with open(args.input_file, 'r') as f:
            urls.extend([line.strip() for line in f if line.strip() and not line.startswith('#')])
    urls.extend(args.url)
    
    if not urls:
        print("No URLs provided.")
        return

    options = ConversionOptions(
        no_article=args.no_article,
        no_comments=args.no_comments,
        no_images=args.no_images,
        archive=args.archive,
        compact_comments=args.compact_comments,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        max_posts=args.max_posts,
        page_spec=parse_page_spec(args.pages),
        llm_format=args.llm_format,
        llm_model=args.llm_model,
        llm_api_key=args.api_key,
        summary=args.summary
    )

    cookie_entries = load_cookie_file(args.cookie_file) if (args.cookie_file and args.forum) else []

    def cookies_for_url(u: str) -> Optional[Dict[str, str]]:
        if not cookie_entries: return None
        host = urlparse(u).netloc.lower()
        jar = {}
        for c in cookie_entries:
            dom = c.get("domain", "").lower()
            if dom and (host == dom or host.endswith(f".{dom}")):
                jar[c["name"]] = c["value"]
        return jar or None

    sources = []
    for u in urls:
        sources.append(Source(
            url=u,
            html=None,
            cookies=cookies_for_url(u) if args.forum else None,
            assets=None,
            is_forum=args.forum
        ))

    async with get_session() as session:
        processed_books = await process_urls(sources, options, session)

    if not processed_books:
        log.error("No content was successfully fetched.")
        sys.exit(1)

    css_content = None
    if args.css:
        with open(args.css) as f: css_content = f.read()

    if args.bundle:
        master_title = args.bundle_title
        if not master_title:
            date_str = datetime.now().strftime("%b_%d_%Y")
            domains = {urlparse(b.source_url).netloc.replace("www.", "") for b in processed_books if b.source_url}
            if len(domains) == 1: master_title = f"{list(domains)[0]}_{date_str}"
            else: master_title = f"Anthology_{date_str}"

        master_author = args.bundle_author or "Various Authors"
        master_book = create_bundle(processed_books, master_title, master_author)
        fname = args.output or f"{sanitize_filename(master_title)}.epub"
        EpubWriter.write(master_book, fname, css_content)
    else:
        for book in processed_books:
            fname = f"{sanitize_filename(book.title)}.epub"
            if args.output:
                if len(urls) == 1: fname = args.output
                else: fname = f"{sanitize_filename(book.title)}_{args.output}"
            EpubWriter.write(book, fname, css_content)

if __name__ == "__main__":
    asyncio.run(async_main())

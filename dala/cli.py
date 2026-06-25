import argparse
import sys
import os
import asyncio
import aiohttp
import socket
import time
from datetime import datetime
from urllib.parse import urlparse
from inspect import isawaitable
from typing import Any, Callable, Dict, List, Optional
from tqdm.asyncio import tqdm_asyncio
from ebooklib import epub
from dotenv import load_dotenv

load_dotenv()

from dala.models import (
    log, Source, ConversionOptions, BookData, ConversionContext, 
    GLOBAL_SEMAPHORE, REQUEST_TIMEOUT, normalize_image_preset, sanitize_filename, parse_page_spec
)
from dala.core.profiles import ProfileManager
from dala.core.dispatcher import DriverDispatcher
from dala.core.session import get_session, load_cookie_file
from dala.core.writer import OutputWriteError, default_output_filename, ensure_output_extension, write_output_book
from dala.core.browser import BrowserChallengeError, BrowserFetchError, BrowserFetchOptions, fetch_rendered_source, validate_browser_options
from dala.core.image_budget import ImageBudgetExceeded, assert_image_budget, prepare_books_for_bundle
from dala.core.discovery import DiscoveryError, discover_posts_for_sources
from dala.core.translation import TranslationCache, TranslationProcessor, TranslationError

async def process_urls(
    sources: List[Source],
    options: ConversionOptions,
    session,
    progress_callback: Optional[Callable[[int, int, Optional[str]], Any]] = None,
    source_timing_callback: Optional[Callable[[str, int, bool, Optional[str]], Any]] = None,
) -> List[BookData]:
    processed_books = []
    progress_lock = asyncio.Lock()
    completed = 0
    total = len(sources)

    async def emit_progress(processed: int, current_url: Optional[str]) -> None:
        if not progress_callback:
            return
        maybe = progress_callback(processed, total, current_url)
        if isawaitable(maybe):
            await maybe

    async def emit_source_timing(
        url: str,
        duration_ms: int,
        success: bool,
        error_type: Optional[str],
    ) -> None:
        if not source_timing_callback:
            return
        maybe = source_timing_callback(url, duration_ms, success, error_type)
        if isawaitable(maybe):
            await maybe

    async def safe_process(source):
        nonlocal completed
        started_at = time.perf_counter()
        success = False
        error_type = None
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
                book = await driver.prepare_book_data(context, source)
                if book is None and source.is_forum:
                    from dala.drivers.forum import ForumDriver
                    from dala.drivers.generic import GenericDriver

                    if isinstance(driver, ForumDriver):
                        log.warning(f"Forum driver found no posts for {source.url}; retrying generic extraction.")
                        generic_source = Source(
                            url=source.url,
                            html=source.html,
                            page_htmls=source.page_htmls,
                            cookies=source.cookies,
                            assets=source.assets,
                            is_forum=False,
                            published_date=source.published_date,
                        )
                        book = await GenericDriver().prepare_book_data(context, generic_source)
                success = book is not None
                if book is not None and source.published_date:
                    book.extra_metadata["published_date"] = source.published_date
                return book
            except BrowserChallengeError:
                raise
            except Exception as e:
                error_type = type(e).__name__
                log.exception(f"Failed to process {source.url}: {e}")
                return None
            finally:
                if local_session is not session:
                    await local_session.close()
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                await emit_source_timing(source.url, duration_ms, success, error_type)
                async with progress_lock:
                    completed += 1
                    await emit_progress(completed, source.url)

    results = await tqdm_asyncio.gather(*[safe_process(s) for s in sources], desc="Processing URLs")
    return [b for b in results if b]

def create_bundle(books: List[BookData], title: str, author: str) -> BookData:
    master_uid = f"urn:bundle:{abs(hash(title))}"
    master_chapters = []
    master_images = []
    master_toc = []
    prepared_books, image_stats = prepare_books_for_bundle(books)
    if image_stats.duplicate_count or image_stats.remapped_count:
        log.info(
            f"Bundle image prep: deduped={image_stats.duplicate_count}, "
            f"renamed={image_stats.remapped_count}"
        )

    for book in prepared_books:
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
            toc_title = book.title
            published_date = (book.extra_metadata or {}).get("published_date")
            if published_date:
                toc_title = f"{published_date} - {toc_title}"
            article_chap.toc_title = toc_title
            if comments_chap:
                master_toc.append((
                    epub.Link(article_chap.filename, toc_title, article_chap.uid),
                    [epub.Link(comments_chap.filename, "Comments", comments_chap.uid)],
                ))
            else:
                master_toc.append(epub.Link(article_chap.filename, toc_title, article_chap.uid))

    return BookData(title=title, author=author, uid=master_uid, language="en", description=f"Bundle of {len(books)} articles.", source_url="", chapters=master_chapters, images=master_images, toc_structure=master_toc)


def bundle_filename_title(title: str, options: ConversionOptions) -> str:
    if options.date_range_active:
        if options.start_date and options.end_date:
            suffix = f"{options.start_date}_to_{options.end_date}"
        elif options.start_date:
            suffix = f"from_{options.start_date}"
        else:
            suffix = f"until_{options.end_date}"
        return title if suffix in title else f"{title}_{suffix}"
    today = datetime.now().strftime("%Y-%m-%d")
    if title.endswith(f"_{today}") or title.endswith(f"-{today}") or today in title:
        return title
    return f"{title}_{today}"

def parse_args():
    parser = argparse.ArgumentParser(description="Web to ebook downloader")
    parser.add_argument("url", nargs='*', help="URL(s) to process")
    parser.add_argument("-i", "--input-file", help="File with URLs")
    parser.add_argument("-o", "--output", help="Output filename")
    parser.add_argument("--format", choices=["epub", "pdf"], default="epub", dest="output_format", help="Output file format")
    parser.add_argument("--pdf-preset", choices=["document", "ereader"], default="document", help="PDF layout preset")
    parser.add_argument("--pdf-page-size", choices=["letter", "a4", "kobo_clara"], default="letter", help="PDF page size")
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
    parser.add_argument("--llm-provider", choices=["auto", "gemini", "openrouter", "openai"], default=os.getenv("LLM_PROVIDER", "auto"), help="LLM provider selection")
    parser.add_argument("--llm-model", help="Specify LLM model (default: gemini-3.1-flash-lite for Gemini or deepseek/deepseek-v4-flash for OpenRouter)")
    parser.add_argument("--api-key", help="API Key for LLM (overrides env vars)")
    parser.add_argument("--summary", action="store_true", help="Generate AI summary at start of article")
    parser.add_argument("--translate", dest="translation_target_lang", help="Translate article text to this target language")
    parser.add_argument("--translation-provider", choices=["llm", "google"], default="llm", help="Translation provider")
    parser.add_argument("--translation-source", default="auto", help="Source language for translation (default: auto)")
    parser.add_argument(
        "--translation-display",
        choices=["underneath", "side-by-side", "popup-footnote", "replace"],
        default="underneath",
        help="How to display translated text",
    )
    parser.add_argument(
        "--translation-scope",
        choices=["article", "article-captions", "all-readable"],
        default="article-captions",
        help="Text blocks to translate",
    )
    parser.add_argument("--translation-glossary", help="Glossary file with source=target lines")
    parser.add_argument("--no-translation-cache", action="store_true", help="Disable persistent translation cache")
    parser.add_argument("--clear-translation-cache", action="store_true", help="Remove the persistent translation cache and exit if no URL is provided")
    parser.add_argument("--test-translation-provider", metavar="TEXT", help="Translate a short text and print the result without downloading")
    parser.add_argument("--forum", action="store_true", help="Enable forum driver for URLs (autodetected usually)")
    parser.add_argument("--cookie-file", help="Netscape cookie file for gated content")
    parser.add_argument("--compact-comments", action="store_true", help="Use compact comment layout")
    parser.add_argument("--yt-lang", default="en", help="Languages for YouTube transcripts (comma-separated, default: en)")
    parser.add_argument("--yt-auto", action="store_true", help="Prefer auto-generated YouTube captions")
    parser.add_argument("--thumbnails", action="store_true", help="Embed periodic thumbnails (YouTube only)")
    parser.add_argument("--yt-max-comments", type=int, default=25, help="Max YouTube comments to fetch")
    parser.add_argument("--yt-sort", choices=["top", "new"], default="top", help="YouTube comment sort order")
    parser.add_argument("--browser", action="store_true", help="Fetch URLs with a headless Playwright Chromium browser")
    parser.add_argument("--browser-extension", help="Unpacked Chromium extension directory to load with --browser")
    parser.add_argument("--browser-profile", help="Chromium user data directory to reuse with --browser")
    parser.add_argument("--browser-executable", help="Chromium-compatible executable path for --browser")
    parser.add_argument("--headed", action="store_true", help="Show the browser window for --browser")
    parser.add_argument("--browser-timeout-ms", type=int, default=30000, help="Browser navigation timeout in milliseconds")
    parser.add_argument(
        "--browser-wait-until",
        choices=["domcontentloaded", "load", "networkidle"],
        default="load",
        help="Playwright page.goto wait condition for --browser",
    )
    parser.add_argument("--browser-settle-ms", type=int, default=1000, help="Extra delay after navigation before capturing HTML")
    parser.add_argument(
        "--browser-challenge-action",
        choices=["archive", "user_browser", "warm", "error"],
        default="archive",
        help="What to do when browser fallback hits a challenge",
    )
    parser.add_argument("--image-preset", choices=["compact", "balanced", "full"], default="balanced", help="Image optimization/budget preset")
    parser.add_argument("--image-color", choices=["color", "grayscale"], default="color", help="Image color mode")
    parser.add_argument("--max-bundle-images", type=int, default=None, help="Override maximum images allowed before write")
    parser.add_argument("--max-image-bytes-mb", type=int, default=None, help="Override maximum optimized image MB allowed before write")
    parser.add_argument("--start-date", help="Discover and include posts on/after this date (YYYY, YYYY-MM, or YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Discover and include posts on/before this date (YYYY, YYYY-MM, or YYYY-MM-DD)")
    parser.add_argument("--date-sort", choices=["asc", "desc"], default="asc", help="Sort discovered date-range posts earliest-to-latest or latest-to-earliest")
    parser.add_argument("--date-fallback", choices=["auto", "shallow", "metadata", "full"], default="auto", help="How hard to work to find post dates during discovery")
    parser.add_argument("--include-undated", action="store_true", help="Include discovered posts with no date")
    parser.add_argument("--max-discovery-pages", type=int, default=20, help="Maximum listing/archive pages to scan")
    parser.add_argument("--max-discovered-posts", type=int, default=200, help="Maximum post candidates to discover")
    return parser.parse_args()

async def acquire_browser_sources(sources: List[Source], options: BrowserFetchOptions) -> List[Source]:
    validate_browser_options(options)
    captured = []
    for source in sources:
        log.info(f"Fetching rendered page with browser: {source.url}")
        try:
            result = await fetch_rendered_source(source.url, options)
        except BrowserChallengeError:
            if options.challenge_action in {"user_browser", "warm", "error"}:
                raise
            log.warning(f"Browser challenge for {source.url}; continuing with normal fetch/archive fallback.")
            captured.append(source)
            continue
        except BrowserFetchError as e:
            log.warning(f"Browser fetch failed for {source.url}; continuing with normal fetch/archive fallback: {e}")
            captured.append(source)
            continue
        cookies = dict(source.cookies or {})
        cookies.update(result.cookies)
        assets = list(source.assets or [])
        for asset in result.assets or []:
            asset_url = asset.get("original_url")
            if asset_url and not any(existing.get("original_url") == asset_url for existing in assets):
                assets.append(asset)
        captured.append(Source(
            url=result.url or source.url,
            html=result.html,
            cookies=cookies or None,
            assets=assets or None,
            is_forum=source.is_forum,
        ))
    return captured

def cookies_for_source_url(cookie_entries: List[Dict[str, str]], u: str) -> Optional[Dict[str, str]]:
    if not cookie_entries:
        return None
    host = (urlparse(u).hostname or "").lower()
    jar = {}
    for c in cookie_entries:
        dom = c.get("domain", "").lower()
        if dom and (host == dom or host.endswith(f".{dom}")):
            jar[c["name"]] = c["value"]
    return jar or None

def discovery_bundle_title(sources: List[Source], options: ConversionOptions) -> str:
    domains = {urlparse(s.url).netloc.replace("www.", "") for s in sources if s.url}
    domain = sorted(domains)[0] if len(domains) == 1 else "Discovered_Posts"
    if options.start_date and options.end_date:
        return f"{domain}_{options.start_date}_to_{options.end_date}"
    if options.start_date:
        return f"{domain}_from_{options.start_date}"
    if options.end_date:
        return f"{domain}_until_{options.end_date}"
    return f"{domain}_discovered"

async def async_main():
    args = parse_args()
    translation_glossary = None
    if args.translation_glossary:
        with open(args.translation_glossary, "r", encoding="utf-8") as f:
            translation_glossary = f.read()

    if args.clear_translation_cache:
        cleared = TranslationCache().clear()
        print("Translation cache cleared." if cleared else "Translation cache was already empty.")

    if args.test_translation_provider:
        if not args.translation_target_lang:
            print("--test-translation-provider requires --translate LANG.", file=sys.stderr)
            sys.exit(2)
        test_options = ConversionOptions(
            llm_model=args.llm_model,
            llm_provider=args.llm_provider,
            llm_api_key=args.api_key,
            translation_enabled=True,
            translation_provider=args.translation_provider,
            translation_target_lang=args.translation_target_lang,
            translation_source_lang=args.translation_source,
            translation_display=args.translation_display,
            translation_scope=args.translation_scope,
            translation_glossary=translation_glossary,
            translation_cache=not args.no_translation_cache,
        )
        try:
            print(await TranslationProcessor.test_provider(args.test_translation_provider, test_options))
        except TranslationError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        if not args.url and not args.input_file:
            return
    elif args.clear_translation_cache and not args.url and not args.input_file:
        return
     
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
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_api_key=args.api_key,
        summary=args.summary,
        translation_enabled=bool(args.translation_target_lang),
        translation_provider=args.translation_provider,
        translation_target_lang=args.translation_target_lang,
        translation_source_lang=args.translation_source,
        translation_display=args.translation_display,
        translation_scope=args.translation_scope,
        translation_glossary=translation_glossary,
        translation_cache=not args.no_translation_cache,
        youtube_lang=args.yt_lang,
        youtube_prefer_auto=args.yt_auto,
        thumbnails=args.thumbnails,
        youtube_max_comments=args.yt_max_comments,
        youtube_comment_sort=args.yt_sort,
        image_preset=normalize_image_preset(args.image_preset),
        image_color=args.image_color,
        max_bundle_images=args.max_bundle_images,
        max_image_bytes_mb=args.max_image_bytes_mb,
        output_format=args.output_format,
        pdf_preset=args.pdf_preset,
        pdf_page_size=args.pdf_page_size,
        start_date=args.start_date,
        end_date=args.end_date,
        date_sort=args.date_sort,
        date_fallback=args.date_fallback,
        include_undated=args.include_undated,
        max_discovery_pages=args.max_discovery_pages,
        max_discovered_posts=args.max_discovered_posts,
        browser_challenge_action=args.browser_challenge_action,
    )

    cookie_entries = load_cookie_file(args.cookie_file) if args.cookie_file else []

    sources = []
    for u in urls:
        sources.append(Source(
            url=u,
            html=None,
            cookies=cookies_for_source_url(cookie_entries, u),
            assets=None,
            is_forum=args.forum
        ))

    if args.browser:
        browser_options = BrowserFetchOptions(
            extension_path=args.browser_extension,
            profile_dir=args.browser_profile,
            executable_path=args.browser_executable,
            headed=args.headed,
            timeout_ms=args.browser_timeout_ms,
            wait_until=args.browser_wait_until,
            settle_ms=args.browser_settle_ms,
            challenge_action=args.browser_challenge_action,
        )
        try:
            sources = await acquire_browser_sources(sources, browser_options)
        except BrowserChallengeError as e:
            if args.browser_challenge_action == "user_browser":
                log.error(
                    "Browser challenge detected. Open this URL in your browser and rerun Dala from that tab: %s",
                    e.url,
                )
            else:
                log.error(str(e))
            sys.exit(1)
        except BrowserFetchError as e:
            log.error(str(e))
            sys.exit(1)
    elif args.browser_extension or args.browser_profile or args.browser_executable or args.headed:
        log.error("--browser-extension, --browser-profile, --browser-executable, and --headed require --browser")
        sys.exit(1)

    discovery_title = None
    async with get_session() as session:
        if options.date_range_active:
            try:
                sources = await discover_posts_for_sources(session, sources, options)
                discovery_title = discovery_bundle_title(sources, options)
            except DiscoveryError as e:
                log.error(str(e))
                sys.exit(1)
        try:
            processed_books = await process_urls(sources, options, session)
        except BrowserChallengeError as e:
            if options.browser_challenge_action == "user_browser":
                log.error(
                    "Browser challenge detected. Open this URL in your browser and rerun Dala from that tab: %s",
                    e.url,
                )
            else:
                log.error(str(e))
            sys.exit(1)

    if not processed_books:
        log.error("No content was successfully fetched.")
        sys.exit(1)

    css_content = None
    if args.css:
        with open(args.css) as f: css_content = f.read()

    should_bundle = args.bundle or (options.date_range_active and len(processed_books) > 1)
    if should_bundle:
        master_title = args.bundle_title
        if not master_title:
            if discovery_title:
                master_title = discovery_title
            else:
                date_str = datetime.now().strftime("%b_%d_%Y")
                domains = {urlparse(b.source_url).netloc.replace("www.", "") for b in processed_books if b.source_url}
                if len(domains) == 1: master_title = f"{list(domains)[0]}_{date_str}"
                else: master_title = f"Anthology_{date_str}"

        master_author = args.bundle_author or "Various Authors"
        master_book = create_bundle(processed_books, master_title, master_author)
        fname = ensure_output_extension(args.output, options.output_format) if args.output else default_output_filename(bundle_filename_title(master_title, options), options.output_format)
        try:
            assert_image_budget(master_book, options)
        except ImageBudgetExceeded as e:
            log.error(str(e))
            sys.exit(1)
        try:
            await write_output_book(master_book, fname, options, css_content)
        except OutputWriteError as e:
            log.error(str(e))
            sys.exit(1)
    else:
        for book in processed_books:
            fname = default_output_filename(book.title, options.output_format)
            if args.output:
                if len(urls) == 1:
                    fname = ensure_output_extension(args.output, options.output_format)
                else:
                    fname = ensure_output_extension(f"{sanitize_filename(book.title)}_{args.output}", options.output_format)
            try:
                assert_image_budget(book, options)
            except ImageBudgetExceeded as e:
                log.error(str(e))
                sys.exit(1)
            try:
                await write_output_book(book, fname, options, css_content)
            except OutputWriteError as e:
                log.error(str(e))
                sys.exit(1)

def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()

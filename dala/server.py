# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "aiohttp[speedups]",
#   "EbookLib",
#   "beautifulsoup4",
#   "trafilatura",
#   "Pillow",
#   "lxml",
#   "pygments",
#   "tqdm",
#   "requests",
#   "PyYAML",
#   "youtube-transcript-api>=0.6.0",
#   "python-dotenv>=1.0.0",
# ]
# ///

import asyncio
import argparse
import uvicorn
import html
import json
import os
import tempfile
import shutil
import time
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from inspect import isawaitable
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from . import cli as core_main
from dala.models import ConversionOptions, Source, log, normalize_image_preset
from dala.core.session import get_session
from dala.core.writer import (
    OutputWriteError,
    default_output_filename,
    output_format_info,
    write_output_book,
)
from dala.core.translation import TranslationCache, TranslationError, TranslationProcessor
from dala.utils.llm import DEFAULT_GEMINI_MODEL, DEFAULT_OPENAI_MODEL, DEFAULT_OPENROUTER_MODEL
from dala.core.image_budget import assert_image_budget
from dala.core.discovery import DiscoveryError, discover_posts_for_sources
from dala.core.browser import (
    DEFAULT_BPC_EXTENSION_PATH,
    DEFAULT_BROWSER_PROFILE_DIR,
    BrowserChallengeError,
    BrowserFetchOptions,
    browser_executable_exists,
    detect_browser_challenge,
    is_playwright_available,
    resolve_browser_executable,
    resolve_browser_extension_path,
)
from dala.core import server_jobs as job_state

JobRecord = job_state.JobRecord
JOBS = job_state.JOBS
JOBS_LOCK = job_state.JOBS_LOCK
JOB_RUN_SEMAPHORE = job_state.JOB_RUN_SEMAPHORE
JOB_RETENTION_SECONDS = job_state.JOB_RETENTION_SECONDS
_create_job = job_state.create_job
_get_job = job_state.get_job
_update_job = job_state.update_job
_set_job_task = job_state.set_job_task
cleanup_finished_jobs = job_state.cleanup_finished_jobs
_job_cleanup_loop = job_state.job_cleanup_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_job_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def log_requests(request, call_next):
    log.debug(f"Incoming request: {request.method} {request.url}")
    response = await call_next(request)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Dala-Server-Saved"],
)

class SourceItem(BaseModel):
    url: str
    html: Optional[str] = None
    page_htmls: Optional[list] = None
    cookies: Optional[dict] = None
    assets: Optional[list] = None
    asset_debug: Optional[dict] = None
    is_forum: Optional[bool] = False

class ConversionRequest(BaseModel):
    sources: List[SourceItem] # Renamed from urls
    bundle_title: Optional[str] = None
    bundle_author: Optional[str] = None
    request_token: Optional[str] = None
    no_comments: bool = False
    no_images: bool = False
    no_article: bool = False
    archive: bool = False
    max_depth: Optional[int] = None
    max_pages: Optional[int] = None
    max_posts: Optional[int] = None
    page_spec: Optional[List[int]] = None
    server_save_dir: Optional[str] = None
    termux_copy_dir: Optional[str] = None # Alias for backward compatibility
    archive_server: bool = False
    llm_format: bool = False
    llm_provider: str = "auto"
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    summary: bool = False
    translation_enabled: bool = False
    translation_provider: str = "llm"
    translation_target_lang: Optional[str] = None
    translation_source_lang: str = "auto"
    translation_display: str = "underneath"
    translation_scope: str = "article-captions"
    translation_glossary: Optional[str] = None
    translation_cache: bool = True
    thumbnails: bool = False
    youtube_lang: Optional[str] = "en"
    youtube_prefer_auto: bool = False
    youtube_max_comments: int = 25
    youtube_comment_sort: str = "top"
    image_preset: str = "balanced"
    image_color: str = "color"
    max_bundle_images: Optional[int] = None
    max_image_bytes_mb: Optional[int] = None
    output_format: str = "epub"
    pdf_preset: str = "document"
    pdf_page_size: str = "letter"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    date_fallback: str = "auto"
    include_undated: bool = False
    max_discovery_pages: int = 20
    max_discovered_posts: int = 200
    browser_fallback: bool = False
    browser_extension_path: Optional[str] = None
    browser_profile_dir: Optional[str] = None
    browser_executable: Optional[str] = None
    browser_timeout_ms: int = 30000
    browser_wait_until: str = "load"
    browser_settle_ms: int = 1000
    browser_challenge_action: str = "archive"

class ScanRequest(BaseModel):
    html: str
    url: str


class TranslationTestRequest(BaseModel):
    text: str = "Hello world."
    translation_provider: str = "llm"
    translation_target_lang: Optional[str] = None
    translation_source_lang: str = "auto"
    translation_glossary: Optional[str] = None
    translation_cache: bool = False
    llm_provider: str = "auto"
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str


class WarmStartRequest(BaseModel):
    url: str
    job_id: Optional[str] = None
    browser_extension_path: Optional[str] = None
    browser_profile_dir: Optional[str] = None
    browser_executable: Optional[str] = None
    mobile: bool = False
    startup_timeout_ms: int = 60000


class WarmEventRequest(BaseModel):
    type: str
    x: Optional[float] = None
    y: Optional[float] = None
    x2: Optional[float] = None
    y2: Optional[float] = None
    delta_y: Optional[float] = None
    text: Optional[str] = None
    key: Optional[str] = None


@dataclass
class ConversionResult:
    tmp_path: str
    filename: str
    media_type: str
    saved_user_copy: bool
    total_sources: int
    processed_sources: int


@dataclass
class WarmSession:
    warm_id: str
    url: str
    created_at: str
    expires_at: float
    job_id: Optional[str] = None
    marker: Optional[str] = None
    page: Any = None
    context: Any = None
    playwright: Any = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserWarmManager:
    def __init__(self) -> None:
        self.sessions: Dict[str, WarmSession] = {}
        self.lock = asyncio.Lock()

    def default_options(self, req: Optional[WarmStartRequest] = None) -> BrowserFetchOptions:
        extension_path = resolve_browser_extension_path(req.browser_extension_path if req else None)
        return BrowserFetchOptions(
            extension_path=extension_path,
            profile_dir=(req.browser_profile_dir if req else None) or DEFAULT_BROWSER_PROFILE_DIR,
            executable_path=(req.browser_executable if req else None) or os.getenv("DALA_BROWSER_EXECUTABLE"),
            headed=False,
            timeout_ms=45000,
            wait_until="load",
            settle_ms=1000,
        )

    async def start_session(self, req: WarmStartRequest, marker: Optional[str] = None) -> WarmSession:
        await self.cleanup_expired()
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise HTTPException(
                status_code=500,
                detail="Browser warm-up requires Playwright. Install with `uv sync --extra browser`.",
            ) from exc

        options = self.default_options(req)
        profile_dir = options.profile_dir or DEFAULT_BROWSER_PROFILE_DIR
        os.makedirs(profile_dir, exist_ok=True)

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--lang=en-US",
        ]
        if options.extension_path:
            ext_path = str(Path(options.extension_path).resolve())
            launch_args.extend([
                f"--disable-extensions-except={ext_path}",
                f"--load-extension={ext_path}",
            ])

        viewport = {"width": 390, "height": 844} if req.mobile else {"width": 1365, "height": 900}
        playwright = await async_playwright().start()
        kwargs = {
            "headless": True,
            "args": launch_args,
            "locale": os.getenv("DALA_BROWSER_LOCALE", "en-US"),
            "timezone_id": os.getenv("DALA_BROWSER_TIMEZONE", "America/New_York"),
            "user_agent": os.getenv(
                "DALA_BROWSER_USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            ),
            "viewport": viewport,
            "is_mobile": bool(req.mobile),
            "has_touch": bool(req.mobile),
        }
        if options.executable_path:
            kwargs["executable_path"] = options.executable_path
        else:
            kwargs["channel"] = "chromium"

        async def launch_context(executable_path: Optional[str] = options.executable_path):
            launch_kwargs = dict(kwargs)
            if executable_path:
                launch_kwargs.pop("channel", None)
                launch_kwargs["executable_path"] = executable_path
            return await playwright.chromium.launch_persistent_context(profile_dir, **launch_kwargs)

        async def start_context():
            try:
                return await launch_context(options.executable_path)
            except PlaywrightError as exc:
                fallback = None
                if not options.executable_path:
                    fallback = resolve_browser_executable()
                if not fallback:
                    raise exc
                try:
                    return await launch_context(fallback)
                except PlaywrightError as fallback_exc:
                    raise RuntimeError(
                        "Failed to launch verification browser with Playwright-managed Chromium "
                        f"or detected Chromium-compatible browser at {fallback}."
                    ) from fallback_exc

        context = None
        try:
            context = await asyncio.wait_for(
                start_context(),
                timeout=max(5, req.startup_timeout_ms / 1000),
            )
            await context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                """
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(options.timeout_ms)
            await asyncio.wait_for(
                page.goto(req.url, wait_until=options.wait_until, timeout=options.timeout_ms),
                timeout=max(5, req.startup_timeout_ms / 1000),
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Timed out starting verification browser.") from exc
        except Exception:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            await playwright.stop()
            raise

        warm_id = uuid4().hex
        session = WarmSession(
            warm_id=warm_id,
            url=req.url,
            created_at=_utc_now(),
            expires_at=time.time() + int(os.getenv("DALA_BROWSER_WARM_TTL_SECONDS", "900")),
            job_id=req.job_id,
            marker=marker,
            page=page,
            context=context,
            playwright=playwright,
        )
        async with self.lock:
            self.sessions[warm_id] = session
        return session

    async def get_session(self, warm_id: str) -> WarmSession:
        async with self.lock:
            session = self.sessions.get(warm_id)
        if not session:
            raise HTTPException(status_code=404, detail="Warm session not found.")
        if session.expires_at < time.time():
            await self.close_session(warm_id)
            raise HTTPException(status_code=410, detail="Warm session expired.")
        return session

    async def close_session(self, warm_id: str) -> None:
        async with self.lock:
            session = self.sessions.pop(warm_id, None)
        if not session:
            return
        try:
            await session.context.close()
        finally:
            await session.playwright.stop()

    async def cleanup_expired(self) -> None:
        async with self.lock:
            expired = [sid for sid, session in self.sessions.items() if session.expires_at < time.time()]
        for sid in expired:
            await self.close_session(sid)


BROWSER_WARM_MANAGER = BrowserWarmManager()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _server_version() -> str:
    try:
        return version("dala")
    except PackageNotFoundError:
        pass

    try:
        import tomllib
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        return str(data.get("project", {}).get("version") or "unknown")
    except Exception:
        return "unknown"


def _timing_log(run_id: str, event: str, **fields: Any) -> None:
    payload = {
        "run_id": run_id,
        "event": event,
        "ts": _utc_now(),
    }
    payload.update(fields)
    log.info(f"TIMING {json.dumps(payload, separators=(',', ':'), ensure_ascii=True)}")


def _set_last_conversion_state(**fields: Any) -> None:
    job_state.set_last_conversion_state(**fields)


def _browser_config_status(explicit_extension_path: Optional[str] = None) -> Dict[str, Any]:
    extension_path = resolve_browser_extension_path(explicit_extension_path)
    extension = Path(extension_path).expanduser() if extension_path else None
    configured_executable = os.getenv("DALA_BROWSER_EXECUTABLE")
    configured_profile_dir = os.getenv("DALA_BROWSER_PROFILE_DIR") or DEFAULT_BROWSER_PROFILE_DIR
    executable = resolve_browser_executable(configured_executable)
    executable_display = executable or configured_executable
    executable_found = browser_executable_exists(executable_display)
    playwright_available = is_playwright_available()
    extension_valid = bool(extension and extension.is_dir() and (extension / "manifest.json").is_file())
    return {
        "playwright_available": playwright_available,
        "browser_executable_found": executable_found,
        "browser_fallback_available": playwright_available,
        "pdf_available": playwright_available,
        "browser_executable": executable_display,
        "browser_profile_dir": configured_profile_dir,
        "bpc_default_path": str(DEFAULT_BPC_EXTENSION_PATH),
        "bpc_extension_path": str(extension) if extension else None,
        "bpc_extension_configured": bool(extension_path),
        "bpc_extension_valid": extension_valid,
    }


def _public_warm_url(warm_id: str) -> str:
    return f"/browser/warm/{warm_id}"


def _warm_request_from_job(req: ConversionRequest, challenge: BrowserChallengeError, job_id: str) -> WarmStartRequest:
    return WarmStartRequest(
        url=challenge.url,
        job_id=job_id,
        browser_extension_path=req.browser_extension_path,
        browser_profile_dir=req.browser_profile_dir or os.getenv("DALA_BROWSER_PROFILE_DIR") or DEFAULT_BROWSER_PROFILE_DIR,
        browser_executable=req.browser_executable or os.getenv("DALA_BROWSER_EXECUTABLE"),
    )


def _is_cancelled_factory(record: JobRecord) -> Callable[[], bool]:
    return record.cancel_event.is_set


def _raise_if_cancelled(is_cancelled: Optional[Callable[[], bool]]) -> None:
    if is_cancelled and is_cancelled():
        raise asyncio.CancelledError("Job cancelled.")


async def _emit_progress(
    callback: Optional[Callable[[int, int, Optional[str]], Any]],
    processed_sources: int,
    total_sources: int,
    current_url: Optional[str],
) -> None:
    if not callback:
        return
    maybe = callback(processed_sources, total_sources, current_url)
    if isawaitable(maybe):
        await maybe


def _build_options_and_sources(req: ConversionRequest) -> Tuple[ConversionOptions, List[Source]]:
    log.info(f"Received request: {len(req.sources)} sources")
    log.info(
        f"Options: NoComments={req.no_comments}, NoImages={req.no_images}, "
        f"Thumbnails={req.thumbnails}, YTLang={req.youtube_lang}, YTSort={req.youtube_comment_sort}"
    )
    if req.sources:
        for idx, s in enumerate(req.sources):
            count_assets = len(s.assets) if s.assets else 0
            cookie_count = len(s.cookies) if isinstance(s.cookies, dict) else 0
            page_html_count = len(s.page_htmls) if s.page_htmls else 0
            log.info(f"Source[{idx}] assets: {count_assets}")
            log.info(f"Source[{idx}] cookies: {cookie_count}, page_htmls: {page_html_count}")
            if s.asset_debug:
                log.info(f"Source[{idx}] asset_debug: {s.asset_debug}")
            if s.assets:
                original_count = len(s.assets)
                s.assets = [
                    a for a in s.assets
                    if a.get("original_url")
                    and a.get("original_url") != s.url
                    and (
                        "image" in str(a.get("content_type", "")).lower()
                        or "/attachments/" in str(a.get("original_url"))
                    )
                ]
                log.info(f"Source[{idx}] assets: {original_count} -> {len(s.assets)} (after filtering)")

    options = ConversionOptions(
        no_comments=req.no_comments,
        no_images=req.no_images,
        no_article=req.no_article,
        archive=req.archive,
        compact_comments=True,
        max_depth=req.max_depth,
        max_pages=req.max_pages,
        max_posts=req.max_posts,
        page_spec=req.page_spec,
        llm_format=req.llm_format,
        llm_provider=req.llm_provider or "auto",
        llm_model=req.llm_model,
        llm_api_key=req.llm_api_key,
        summary=req.summary,
        translation_enabled=req.translation_enabled,
        translation_provider=req.translation_provider or "llm",
        translation_target_lang=req.translation_target_lang,
        translation_source_lang=req.translation_source_lang or "auto",
        translation_display=req.translation_display or "underneath",
        translation_scope=req.translation_scope or "article-captions",
        translation_glossary=req.translation_glossary,
        translation_cache=req.translation_cache,
        thumbnails=req.thumbnails,
        youtube_lang=req.youtube_lang or "en",
        youtube_prefer_auto=req.youtube_prefer_auto,
        youtube_max_comments=req.youtube_max_comments,
        youtube_comment_sort=req.youtube_comment_sort,
        image_preset=normalize_image_preset(req.image_preset),
        image_color=req.image_color or "color",
        max_bundle_images=req.max_bundle_images,
        max_image_bytes_mb=req.max_image_bytes_mb,
        output_format=req.output_format or "epub",
        pdf_preset=req.pdf_preset or "document",
        pdf_page_size=req.pdf_page_size or "letter",
        start_date=req.start_date,
        end_date=req.end_date,
        date_fallback=req.date_fallback or "auto",
        include_undated=req.include_undated,
        max_discovery_pages=req.max_discovery_pages,
        max_discovered_posts=req.max_discovered_posts,
        browser_fallback=req.browser_fallback,
        browser_extension_path=resolve_browser_extension_path(req.browser_extension_path),
        browser_profile_dir=req.browser_profile_dir or os.getenv("DALA_BROWSER_PROFILE_DIR") or DEFAULT_BROWSER_PROFILE_DIR,
        browser_executable=req.browser_executable or os.getenv("DALA_BROWSER_EXECUTABLE"),
        browser_timeout_ms=req.browser_timeout_ms,
        browser_wait_until=req.browser_wait_until,
        browser_settle_ms=req.browser_settle_ms,
        browser_challenge_action=req.browser_challenge_action or "archive",
    )

    core_sources = []
    for s in req.sources:
        core_sources.append(Source(
            url=s.url,
            html=s.html,
            page_htmls=s.page_htmls,
            cookies=s.cookies,
            assets=s.assets,
            is_forum=bool(s.is_forum)
        ))
    return options, core_sources


def _save_server_copy(req: ConversionRequest, tmp_path: str, filename: str) -> bool:
    candidates = []
    user_input = (req.server_save_dir or req.termux_copy_dir or "").strip()

    sys_downloads = None
    try:
        termux_path = "/data/data/com.termux/files/home/storage/downloads"
        if os.path.isdir(termux_path):
            sys_downloads = termux_path
        elif os.name == 'posix':
            import subprocess
            res = subprocess.run(['xdg-user-dir', 'DOWNLOAD'], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                sys_downloads = res.stdout.strip()
    except Exception:
        pass

    if not sys_downloads:
        possible_dl = os.path.join(os.path.expanduser("~"), "Downloads")
        if os.path.isdir(possible_dl):
            sys_downloads = possible_dl

    if user_input:
        is_absolute = os.path.isabs(user_input) or (os.name == 'nt' and len(user_input) > 1 and user_input[1] == ':')
        if is_absolute:
            candidates.append(user_input)
        elif sys_downloads:
            candidates.append(os.path.join(sys_downloads, user_input))
        else:
            candidates.append(user_input)

    if sys_downloads and sys_downloads not in candidates:
        candidates.append(sys_downloads)

    project_root = os.path.dirname(os.path.abspath(__file__))
    exports_dir = os.path.join(project_root, "exports")

    if req.archive_server:
        try:
            os.makedirs(exports_dir, exist_ok=True)
            archive_path = os.path.join(exports_dir, filename)
            shutil.copy2(tmp_path, archive_path)
            log.info(f"Archived to project exports: {archive_path}")
        except Exception as e:
            log.warning(f"Archive failed: {e}")

    candidates.append(exports_dir)

    saved_user_copy = False
    for dest in candidates:
        if not dest:
            continue
        if dest == exports_dir and req.archive_server:
            saved_user_copy = True
            break
        try:
            os.makedirs(dest, exist_ok=True)
            if os.path.isdir(dest):
                final_path = os.path.join(dest, filename)
                shutil.copy2(tmp_path, final_path)
                saved_user_copy = True
                log.info(f"Saved local copy to: {final_path}")
                break
        except Exception:
            continue

    if not saved_user_copy:
        log.warning("Could not save a server-side copy to any location.")
    return saved_user_copy


def should_apply_single_title_override(request_title: Optional[str], extracted_title: Optional[str], source_url: str) -> bool:
    title = (request_title or "").strip()
    if not title:
        return False

    host = (urlparse(source_url).hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        if title.casefold() in {"youtube", "youtube video"} and (extracted_title or "").strip():
            return False

    return True


async def run_conversion_job(
    req: ConversionRequest,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[int, int, Optional[str]], Any]] = None,
    job_id: Optional[str] = None,
) -> ConversionResult:
    run_id = job_id or uuid4().hex
    run_started_at = time.perf_counter()
    options, core_sources = _build_options_and_sources(req)
    total_sources = len(core_sources)
    discovery_title = None
    source_timings: List[Tuple[str, int, bool, Optional[str]]] = []

    _timing_log(
        run_id,
        "job_start",
        mode="jobs" if job_id else "convert",
        total_sources=total_sources,
    )

    async def source_timing_cb(url: str, duration_ms: int, success: bool, error_type: Optional[str]) -> None:
        source_timings.append((url, duration_ms, success, error_type))
        _timing_log(
            run_id,
            "source_done",
            url=url,
            duration_ms=duration_ms,
            success=1 if success else 0,
            error_type=error_type,
        )

    try:
        await _emit_progress(progress_callback, 0, total_sources, core_sources[0].url if core_sources else None)
        _raise_if_cancelled(is_cancelled)

        stage_started_at = time.perf_counter()
        async with get_session() as session:
            if options.date_range_active:
                core_sources = await discover_posts_for_sources(session, core_sources, options)
                discovery_title = core_main.discovery_bundle_title(core_sources, options)
                total_sources = len(core_sources)
                await _emit_progress(progress_callback, 0, total_sources, core_sources[0].url if core_sources else None)
                _timing_log(
                    run_id,
                    "stage_done",
                    stage="discover_posts",
                    discovered_sources=total_sources,
                    duration_ms=int((time.perf_counter() - stage_started_at) * 1000),
                )
                stage_started_at = time.perf_counter()
            processed_books = await core_main.process_urls(
                core_sources,
                options,
                session,
                progress_callback=progress_callback,
                source_timing_callback=source_timing_cb,
            )
        _timing_log(
            run_id,
            "stage_done",
            stage="process_urls",
            duration_ms=int((time.perf_counter() - stage_started_at) * 1000),
        )

        _raise_if_cancelled(is_cancelled)
        processed_count = len(processed_books)
        await _emit_progress(progress_callback, processed_count, total_sources, None)

        if not processed_books:
            raise ValueError("No content could be extracted.")

        stage_started_at = time.perf_counter()
        if len(processed_books) > 1:
            title = req.bundle_title or discovery_title or f"Bundle_{len(processed_books)}_Articles"
            author = req.bundle_author or "Web to EPUB"
            final_book = core_main.create_bundle(processed_books, title, author)
            bundle_mode = "bundle"
        else:
            final_book = processed_books[0]
            if should_apply_single_title_override(req.bundle_title, final_book.title, final_book.source_url):
                final_book.title = req.bundle_title
            bundle_mode = "single"
        _timing_log(
            run_id,
            "stage_done",
            stage="prepare_output_book",
            mode=bundle_mode,
            duration_ms=int((time.perf_counter() - stage_started_at) * 1000),
        )

        _raise_if_cancelled(is_cancelled)

        stage_started_at = time.perf_counter()
        image_stats = assert_image_budget(final_book, options)
        _timing_log(
            run_id,
            "stage_done",
            stage="image_budget",
            image_count=image_stats.image_count,
            image_bytes=image_stats.image_bytes,
            duration_ms=int((time.perf_counter() - stage_started_at) * 1000),
        )

        _raise_if_cancelled(is_cancelled)

        format_info = output_format_info(options.output_format)
        with tempfile.NamedTemporaryFile(delete=False, suffix=format_info.extension) as tmp:
            tmp_path = tmp.name

        stage_started_at = time.perf_counter()
        await write_output_book(final_book, tmp_path, options)
        _timing_log(
            run_id,
            "stage_done",
            stage=f"write_{options.output_format}",
            duration_ms=int((time.perf_counter() - stage_started_at) * 1000),
        )
        filename = default_output_filename(final_book.title, options.output_format)
        log.info(f"Generated {format_info.label} at: {tmp_path}")
        log.info(f"Sending as: {filename}")

        stage_started_at = time.perf_counter()
        saved_user_copy = _save_server_copy(req, tmp_path, filename)
        _timing_log(
            run_id,
            "stage_done",
            stage="save_server_copy",
            duration_ms=int((time.perf_counter() - stage_started_at) * 1000),
            server_saved=1 if saved_user_copy else 0,
        )

        failed_sources = sum(1 for _, _, success, _ in source_timings if not success)
        failed_source_details = [
            {"url": url, "ms": duration_ms, "error_type": error_type or "Unknown"}
            for url, duration_ms, success, error_type in source_timings
            if not success
        ]
        slowest_sources = [
            {"url": url, "ms": duration_ms, "success": 1 if success else 0}
            for url, duration_ms, success, _ in sorted(
                source_timings,
                key=lambda item: item[1],
                reverse=True
            )[:5]
        ]

        _timing_log(
            run_id,
            "job_summary",
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
            total_sources=total_sources,
            processed_sources=processed_count,
            failed_sources=failed_sources,
            output_filename=filename,
            server_saved=1 if saved_user_copy else 0,
            slowest_sources=slowest_sources,
            failed_source_details=failed_source_details,
        )
        _set_last_conversion_state(
            status="completed",
            run_id=run_id,
            request_token=req.request_token,
            mode="jobs" if job_id else "convert",
            finished_at=_utc_now(),
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
            total_sources=total_sources,
            processed_sources=processed_count,
            failed_sources=failed_sources,
            failed_source_details=failed_source_details,
            output_filename=filename,
            server_saved=bool(saved_user_copy),
        )

        return ConversionResult(
            tmp_path=tmp_path,
            filename=filename,
            media_type=format_info.media_type,
            saved_user_copy=saved_user_copy,
            total_sources=total_sources,
            processed_sources=processed_count,
        )
    except asyncio.CancelledError:
        _set_last_conversion_state(
            status="cancelled",
            run_id=run_id,
            request_token=req.request_token,
            mode="jobs" if job_id else "convert",
            finished_at=_utc_now(),
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
            total_sources=total_sources,
        )
        _timing_log(
            run_id,
            "job_cancelled",
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
        )
        raise
    except Exception as exc:
        failed_source_details = [
            {"url": url, "ms": duration_ms, "error_type": error_type or "Unknown"}
            for url, duration_ms, success, error_type in source_timings
            if not success
        ]
        _set_last_conversion_state(
            status="failed",
            run_id=run_id,
            request_token=req.request_token,
            mode="jobs" if job_id else "convert",
            finished_at=_utc_now(),
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
            total_sources=total_sources,
            failed_source_details=failed_source_details,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _timing_log(
            run_id,
            "job_error",
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
            failed_source_details=failed_source_details,
        )
        raise

@app.post("/helper/extract-links")
async def extract_links(req: ScanRequest):
    """
    Helper for Chrome Extension (MV3) which cannot use DOMParser.
    Extracts potential image assets and next-page links from HTML.
    """
    from bs4 import BeautifulSoup, Comment
    from urllib.parse import urljoin, urlparse
    import re

    soup = BeautifulSoup(req.html, 'html.parser')
    base_url = req.url
    
    # 1. Extract Images (matching background.js logic)
    images = []
    seen = set()
    
    def _srcset_candidates(srcset: str) -> list[tuple[int, str]]:
        candidates = []
        if not srcset:
            return candidates
        for part in srcset.split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split()
            url = bits[0]
            width = 0
            if len(bits) > 1:
                m = re.match(r"(\d+)[wx]$", bits[1])
                if m:
                    try:
                        width = int(m.group(1))
                    except Exception:
                        width = 0
            candidates.append((width, url))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates

    def _best_img_url(img):
        for attr in ("data-url", "data-original", "data-src"):
            value = img.get(attr)
            if value:
                return value
        for attr in ("data-srcset", "srcset"):
            for _, candidate in _srcset_candidates(img.get(attr) or ""):
                if candidate:
                    return candidate
        return img.get("src")

    def _is_low_value_img(src: str, img) -> bool:
        if not src:
            return True
        lower = src.lower()
        alt = (img.get("alt") or "").lower()
        if lower.startswith("data:"):
            return True
        if any(x in lower for x in ["/avatar", "/reaction", "/smilies", "/emoji", "data:image/gif", "logo", "sprite"]):
            return True
        if alt in {"logo", "avatar"} or (len(alt) <= 24 and any(x in alt for x in ["logo", "avatar"])):
            return True
        return False

    def _append_image_url(src: str, img=None, viewer=None) -> None:
        if _is_low_value_img(src, img or {}):
            return
        primary = urljoin(base_url, src)
        key = primary.split("?", 1)[0]
        if key in seen:
            return
        seen.add(key)
        images.append({
            "url": primary,
            "viewer_url": viewer,
            "filename_hint": src.split("/")[-1],
        })

    # Selectors for forum posts
    img_tags = soup.select(".message-body img, .messageContent img, .bbWrapper img, .bbImage img")
    # Containers fallback
    if not img_tags:
        containers = soup.select("article.message, .message--post, [data-lb-id]")
        for c in containers:
            img_tags.extend(c.find_all("img"))
    if not img_tags:
        root = soup.select_one("article, main, [role='main'], .article-body, .article-content, .entry-content, #content") or soup.body or soup
        img_tags = root.select("picture img, figure img, img")

    for img in img_tags:
        src = _best_img_url(img)
        parent = img.find_parent("a")
        viewer = urljoin(base_url, parent["href"]) if (parent and parent.get("href")) else None
        _append_image_url(src, img=img, viewer=viewer)

    for svg in soup.select("article svg[data-inject-url], article svg[data-src], article svg[src], main svg[data-inject-url], main svg[data-src], main svg[src], figure svg[data-inject-url], figure svg[data-src], figure svg[src]"):
        src = svg.get("data-inject-url") or svg.get("data-src") or svg.get("src")
        if not src:
            continue
        figure = svg.find_parent("figure")
        alt = ""
        if figure:
            alt = " ".join(figure.get_text(" ", strip=True).split())[:240]
        _append_image_url(src, img={"alt": alt or svg.get("aria-label") or svg.get("data-name") or "Article graphic"})

    for selector in [
        'meta[property="og:image"]',
        'meta[property="og:image:url"]',
        'meta[name="twitter:image"]',
        'meta[name="twitter:image:src"]',
    ]:
        for meta in soup.select(selector):
            _append_image_url(meta.get("content"))

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        text = str(comment or "")
        if "<img" not in text.lower():
            continue
        fragment = BeautifulSoup(text, "html.parser")
        for img in fragment.find_all("img"):
            _append_image_url(_best_img_url(img), img=img)

    # External images (non-attachment)
    externals = []
    for img in img_tags:
        src = _best_img_url(img)
        if _is_low_value_img(src, img):
            continue
        full = urljoin(base_url, src)
        if full not in seen:
            externals.append(full)
            seen.add(full)

    # 2. Find Next Page
    next_page = None

    # <link rel="next">
    link_next = soup.find("link", attrs={"rel": lambda value: value and "next" in value})
    if link_next and link_next.get("href"):
        next_page = urljoin(base_url, link_next.get("href"))

    if not next_page:
        jump_next = soup.select_one("a.pageNav-jump--next[href], a.pageNavSimple-el--next[href], a[rel='next'][href]")
        if jump_next:
            next_page = urljoin(base_url, jump_next.get("href"))

    if not next_page:
        current_num = None
        current_match = re.search(r'(?:page[-=/_]|[?&]page=)(\d+)', base_url, re.IGNORECASE)
        if current_match:
            try:
                current_num = int(current_match.group(1))
            except Exception:
                current_num = None
        else:
            current_num = 1
        for a in soup.find_all("a"):
            txt = a.get_text(strip=True).lower()
            if txt in ("next", "next >", "next>"):
                if a.get("href"):
                    next_page = urljoin(base_url, a.get("href"))
                    break
            if current_num and txt == str(current_num + 1) and a.get("href"):
                next_page = urljoin(base_url, a.get("href"))
                break
    
    # Parse page number from next_url to return int if possible? 
    # The extension logic expects a URL to fetch, or logic to build it.
    # The extension builds it: `buildForumPageUrl`.
    # But `findNextPage` in JS returned an integer page number.
    # Let's return the integer if we can extract it.
    
    next_page_num = None
    if next_page:
        m = re.search(r'(?:page[-=/_]|[?&]page=)(\d+)', next_page, re.IGNORECASE)
        if m:
            try:
                next_page_num = int(m.group(1))
            except: pass

    return {
        "assets": images,
        "externals": externals,
        "next_page_url": next_page,
        "next_page_num": next_page_num
    }


@app.get("/helper/last-conversion")
async def last_conversion_state():
    if not job_state.LAST_CONVERSION_STATE:
        raise HTTPException(status_code=404, detail="No conversion state available.")
    return job_state.LAST_CONVERSION_STATE


@app.post("/helper/translation/test")
async def test_translation_provider(req: TranslationTestRequest):
    if not req.translation_target_lang:
        raise HTTPException(status_code=400, detail="translation_target_lang is required.")
    options = ConversionOptions(
        llm_model=req.llm_model,
        llm_provider=req.llm_provider or "auto",
        llm_api_key=req.llm_api_key,
        translation_enabled=True,
        translation_provider=req.translation_provider or "llm",
        translation_target_lang=req.translation_target_lang,
        translation_source_lang=req.translation_source_lang or "auto",
        translation_glossary=req.translation_glossary,
        translation_cache=req.translation_cache,
    )
    try:
        translated = await TranslationProcessor.test_provider(req.text or "Hello world.", options)
    except TranslationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"translated_text": translated}


@app.get("/helper/translation/status")
async def translation_status():
    keys = {
        "gemini": bool(os.getenv("GEMINI_API_KEY")),
        "openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
    }
    models = {
        "gemini": [
            {"id": DEFAULT_GEMINI_MODEL, "label": "Gemini 3.1 Flash Lite (fast)"},
            {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite"},
            {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
            {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash"},
        ],
        "openrouter": [
            {"id": DEFAULT_OPENROUTER_MODEL, "label": "DeepSeek V4 Flash (cheap)"},
            {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
            {"id": "minimax/minimax-m3", "label": "MiniMax M3"},
            {"id": "x-ai/grok-4.3", "label": "Grok 4.3"},
        ],
        "openai": [
            {"id": DEFAULT_OPENAI_MODEL, "label": "GPT-4o mini"},
        ],
    }
    if keys["gemini"]:
        recommended_provider = "gemini"
        recommended_model = DEFAULT_GEMINI_MODEL
    elif keys["openrouter"]:
        recommended_provider = "openrouter"
        recommended_model = DEFAULT_OPENROUTER_MODEL
    elif keys["openai"]:
        recommended_provider = "openai"
        recommended_model = DEFAULT_OPENAI_MODEL
    else:
        recommended_provider = None
        recommended_model = None
    return {
        "keys": keys,
        "models": models,
        "recommended_provider": recommended_provider,
        "recommended_model": recommended_model,
        "server_default_provider": os.getenv("LLM_PROVIDER"),
        "server_default_model": os.getenv("LLM_MODEL"),
    }


@app.post("/helper/translation/cache/clear")
async def clear_translation_cache():
    cache = TranslationCache()
    cleared = cache.clear()
    return {"cleared": cleared, "cache_path": str(cache.path)}


@app.get("/", response_class=HTMLResponse)
async def status_page(request: Request):
    status = _browser_config_status()
    browser_support = "available" if status["browser_fallback_available"] else "needs setup"
    pdf_support = "available" if status["pdf_available"] else "needs setup"
    browser_executable = html.escape(status["browser_executable"] or "not detected")
    server_url = html.escape(str(request.base_url).rstrip("/"))
    return f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Dala Server</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.45; max-width: 760px; }}
      code {{ background: #f1f1f1; padding: 0.15rem 0.3rem; border-radius: 4px; }}
      table {{ border-collapse: collapse; margin-top: 1rem; }}
      td {{ border-bottom: 1px solid #ddd; padding: 0.45rem 0.75rem 0.45rem 0; }}
    </style>
  </head>
  <body>
    <h1>Dala Server</h1>
    <p>The local Dala backend is running.</p>
    <table>
      <tr><td>Extension URL</td><td><code>{server_url}</code></td></tr>
      <tr><td>Headless browser support</td><td>{browser_support}</td></tr>
      <tr><td>PDF output</td><td>{pdf_support}</td></tr>
      <tr><td>Detected browser</td><td><code>{browser_executable}</code></td></tr>
      <tr><td>Playwright package</td><td>{"installed" if status["playwright_available"] else "not installed"}</td></tr>
    </table>
    <p>For headless browser setup, run <code>dala-setup-browser</code>.</p>
  </body>
</html>
"""


@app.get("/ping")
async def ping():
    return {
        "status": "ok",
        "server_version": _server_version(),
        "job_count": len(JOBS),
        "job_retention_seconds": JOB_RETENTION_SECONDS,
        **_browser_config_status(),
    }


def _warm_page_html(warm_id: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dala Verification Browser</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #202124; color: #fff; }}
    .bar {{ position: sticky; top: 0; z-index: 2; display: flex; gap: 8px; align-items: center; padding: 10px; background: #111; }}
    button {{ font: inherit; padding: 10px 14px; border: 0; border-radius: 6px; background: #e8eaed; color: #111; }}
    button.primary {{ background: #8ab4f8; }}
    #status {{ font-size: 13px; color: #c7c7c7; }}
    #screen {{ display: block; width: 100%; max-width: 1365px; margin: 0 auto; touch-action: none; background: #fff; }}
  </style>
</head>
<body>
  <div class="bar">
    <button id="reload">Reload</button>
    <button id="done" class="primary">Done</button>
    <button id="cancel">Cancel</button>
    <span id="status">Complete the site verification in the browser below.</span>
  </div>
  <img id="screen" alt="Remote browser screenshot">
  <script>
    const warmId = {json.dumps(warm_id)};
    const screen = document.getElementById('screen');
    const statusEl = document.getElementById('status');
    let down = null;
    let refreshing = true;

    function setStatus(text) {{ statusEl.textContent = text; }}
    function screenshotUrl() {{ return `/browser/warm/${{warmId}}/screenshot?t=${{Date.now()}}`; }}
    async function refresh() {{
      if (!refreshing) return;
      screen.src = screenshotUrl();
    }}
    setInterval(refresh, 1200);
    refresh();

    function point(evt) {{
      const rect = screen.getBoundingClientRect();
      const scaleX = screen.naturalWidth / rect.width;
      const scaleY = screen.naturalHeight / rect.height;
      return {{
        x: Math.max(0, Math.min(screen.naturalWidth, (evt.clientX - rect.left) * scaleX)),
        y: Math.max(0, Math.min(screen.naturalHeight, (evt.clientY - rect.top) * scaleY)),
      }};
    }}
    async function send(payload) {{
      await fetch(`/browser/warm/${{warmId}}/event`, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }});
      await refresh();
    }}
    screen.addEventListener('pointerdown', evt => {{
      evt.preventDefault();
      screen.setPointerCapture(evt.pointerId);
      down = {{...point(evt), id: evt.pointerId}};
    }});
    screen.addEventListener('pointerup', async evt => {{
      evt.preventDefault();
      if (!down) return;
      const up = point(evt);
      const moved = Math.hypot(up.x - down.x, up.y - down.y);
      if (moved > 8) await send({{type: 'drag', x: down.x, y: down.y, x2: up.x, y2: up.y}});
      else await send({{type: 'click', x: up.x, y: up.y}});
      down = null;
    }});
    screen.addEventListener('wheel', async evt => {{
      evt.preventDefault();
      await send({{type: 'scroll', delta_y: evt.deltaY}});
    }}, {{passive: false}});
    document.addEventListener('keydown', async evt => {{
      if (evt.key.length === 1) await send({{type: 'text', text: evt.key}});
      else await send({{type: 'key', key: evt.key}});
    }});
    document.getElementById('reload').onclick = async () => send({{type: 'reload'}});
    document.getElementById('cancel').onclick = async () => {{
      refreshing = false;
      await fetch(`/browser/warm/${{warmId}}/cancel`, {{method: 'POST'}});
      setStatus('Verification cancelled.');
    }};
    document.getElementById('done').onclick = async () => {{
      setStatus('Checking verification...');
      const response = await fetch(`/browser/warm/${{warmId}}/done`, {{method: 'POST'}});
      const body = await response.json();
      if (body.ready) {{
        refreshing = false;
        setStatus('Ready. Return to Dala; the download will resume.');
      }} else {{
        setStatus(body.message || 'Verification still appears to be required.');
        await refresh();
      }}
    }};
  </script>
</body>
</html>"""


@app.post("/browser/warm/start")
async def browser_warm_start(req: WarmStartRequest):
    warm = await BROWSER_WARM_MANAGER.start_session(req)
    return {
        "warm_id": warm.warm_id,
        "warm_url": _public_warm_url(warm.warm_id),
        "expires_at": warm.expires_at,
    }


@app.get("/browser/warm/{warm_id}", response_class=HTMLResponse)
async def browser_warm_page(warm_id: str):
    await BROWSER_WARM_MANAGER.get_session(warm_id)
    return HTMLResponse(_warm_page_html(warm_id))


@app.get("/browser/warm/{warm_id}/screenshot")
async def browser_warm_screenshot(warm_id: str):
    warm = await BROWSER_WARM_MANAGER.get_session(warm_id)
    async with warm.lock:
        data = await warm.page.screenshot(full_page=False, type="png")
    return Response(content=data, media_type="image/png")


@app.post("/browser/warm/{warm_id}/event")
async def browser_warm_event(warm_id: str, req: WarmEventRequest):
    warm = await BROWSER_WARM_MANAGER.get_session(warm_id)
    async with warm.lock:
        page = warm.page
        if req.type == "click" and req.x is not None and req.y is not None:
            await page.mouse.click(req.x, req.y)
        elif req.type == "drag" and None not in (req.x, req.y, req.x2, req.y2):
            await page.mouse.move(req.x, req.y)
            await page.mouse.down()
            await page.mouse.move(req.x2, req.y2, steps=12)
            await page.mouse.up()
        elif req.type == "scroll":
            await page.mouse.wheel(0, req.delta_y or 0)
        elif req.type == "text" and req.text:
            await page.keyboard.type(req.text)
        elif req.type == "key" and req.key:
            await page.keyboard.press(req.key)
        elif req.type == "reload":
            await page.reload(wait_until="load")
        else:
            raise HTTPException(status_code=400, detail="Unsupported warm browser event.")
        await page.wait_for_timeout(500)
    return {"ok": True}


@app.post("/browser/warm/{warm_id}/cancel")
async def browser_warm_cancel(warm_id: str):
    warm = await BROWSER_WARM_MANAGER.get_session(warm_id)
    job_id = warm.job_id
    await BROWSER_WARM_MANAGER.close_session(warm_id)
    if job_id:
        await _update_job(job_id, status="failed", error="Verification cancelled.", task=None)
    return {"ok": True}


@app.post("/browser/warm/{warm_id}/done")
async def browser_warm_done(warm_id: str):
    warm = await BROWSER_WARM_MANAGER.get_session(warm_id)
    async with warm.lock:
        html = await warm.page.content()
        marker = detect_browser_challenge(html)
        current_url = warm.page.url
    if marker:
        return {
            "ready": False,
            "message": f"Verification still appears to be required ({marker}).",
            "marker": marker,
        }

    job_id = warm.job_id
    await BROWSER_WARM_MANAGER.close_session(warm_id)
    if job_id:
        job = await _get_job(job_id)
        if job and job.request and job.status == "verification_required":
            job.cancel_event = asyncio.Event()
            task = asyncio.create_task(_run_job_task(job_id, job.request))
            await _update_job(
                job_id,
                status="queued",
                error=None,
                verification_url=None,
                verification_token=None,
                verification_source_url=None,
                verification_marker=None,
                task=task,
                current_url=current_url,
            )
    return {"ready": True}

async def _run_job_task(job_id: str, req: ConversionRequest) -> None:
    async with JOB_RUN_SEMAPHORE:
        record = await _get_job(job_id)
        if not record:
            return

        if record.cancel_event.is_set():
            await _update_job(job_id, status="cancelled", error="Job cancelled before start.")
            return

        await _update_job(
            job_id,
            status="running",
            current_url=req.sources[0].url if req.sources else None,
            total_sources=len(req.sources),
        )

        async def progress_cb(processed: int, total: int, current_url: Optional[str]) -> None:
            await _update_job(
                job_id,
                processed_sources=processed,
                total_sources=total,
                current_url=current_url,
            )

        try:
            result = await run_conversion_job(
                req,
                is_cancelled=_is_cancelled_factory(record),
                progress_callback=progress_cb,
                job_id=job_id,
            )
            await _update_job(
                job_id,
                status="completed",
                processed_sources=result.processed_sources,
                total_sources=result.total_sources,
                current_url=None,
                output_path=result.tmp_path,
                output_filename=result.filename,
                output_media_type=result.media_type,
                server_saved=result.saved_user_copy,
                failed_source_details=(
                    job_state.LAST_CONVERSION_STATE.get("failed_source_details", [])
                    if isinstance(job_state.LAST_CONVERSION_STATE, dict) and job_state.LAST_CONVERSION_STATE.get("run_id") == job_id
                    else []
                ),
                error=None,
            )
        except asyncio.CancelledError:
            await _update_job(job_id, status="cancelled", current_url=None, error="Job cancelled.")
        except BrowserChallengeError as e:
            if req.browser_challenge_action == "user_browser":
                await _update_job(
                    job_id,
                    status="user_browser_required",
                    current_url=e.url,
                    error=(
                        "The server browser hit an interactive challenge. "
                        "Open this article in your browser, then run Dala from that tab."
                    ),
                    user_browser_url=e.url,
                    verification_source_url=e.url,
                    verification_marker=e.marker,
                    task=None,
                )
                return
            if req.browser_challenge_action != "warm":
                log.warning(f"Job {job_id} reached browser challenge unexpectedly; marking failed: {e}")
                await _update_job(job_id, status="failed", current_url=None, error=str(e), task=None)
                return
            await _update_job(
                job_id,
                status="verification_starting",
                current_url=e.url,
                error=str(e),
                verification_source_url=e.url,
                verification_marker=e.marker,
            )
            try:
                warm = await BROWSER_WARM_MANAGER.start_session(_warm_request_from_job(req, e, job_id), marker=e.marker)
            except Exception as warm_exc:
                log.exception(f"Job {job_id} could not start verification browser: {warm_exc}")
                await _update_job(
                    job_id,
                    status="failed",
                    current_url=None,
                    error=f"Could not start verification browser: {warm_exc}",
                    task=None,
                )
            else:
                await _update_job(
                    job_id,
                    status="verification_required",
                    current_url=e.url,
                    error=str(e),
                    verification_url=_public_warm_url(warm.warm_id),
                    verification_token=warm.warm_id,
                    verification_source_url=e.url,
                    verification_marker=e.marker,
                    task=None,
                )
        except Exception as e:
            log.exception(f"Job {job_id} failed: {e}")
            await _update_job(job_id, status="failed", current_url=None, error=str(e))


@app.post("/jobs", response_model=JobSubmitResponse)
async def create_job(req: ConversionRequest):
    job = await _create_job(total_sources=len(req.sources))
    job.request = req
    task = asyncio.create_task(_run_job_task(job.job_id, req))
    await _set_job_task(job.job_id, task)
    return JobSubmitResponse(job_id=job.job_id, status=job.status)


@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job.to_public()


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status in {"completed", "failed", "cancelled"}:
        return job.to_public()

    job.cancel_requested = True
    job.cancel_event.set()
    if job.status == "queued":
        await _update_job(job_id, status="cancelled", error="Job cancelled.")
    elif job.task and not job.task.done():
        job.task.cancel()
    await _update_job(job_id, cancel_requested=True)
    return job.to_public()


@app.get("/jobs/{job_id}/download")
async def download_job_result(job_id: str):
    job = await _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "completed":
        raise HTTPException(status_code=409, detail=f"Job is {job.status}.")
    if not job.output_path or not os.path.exists(job.output_path):
        raise HTTPException(status_code=404, detail="Output file not available.")

    return FileResponse(
        path=job.output_path,
        filename=job.output_filename or "download.epub",
        media_type=job.output_media_type,
        headers={"X-Dala-Server-Saved": "1" if job.server_saved else "0"},
    )


@app.post("/convert")
async def convert(req: ConversionRequest):
    try:
        result = await run_conversion_job(req)
        return FileResponse(
            path=result.tmp_path,
            filename=result.filename,
            media_type=result.media_type,
            headers={"X-Dala-Server-Saved": "1" if result.saved_user_copy else "0"},
        )
    except asyncio.CancelledError:
        raise HTTPException(status_code=499, detail="Conversion cancelled.")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except TranslationError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except OutputWriteError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

def parse_server_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the Dala local server")
    parser.add_argument("--host", default=os.getenv("DALA_SERVER_HOST", "127.0.0.1"), help="Host/interface to bind")
    parser.add_argument("--port", type=int, default=int(os.getenv("DALA_SERVER_PORT", "8000")), help="Port to bind")
    parser.add_argument("--open", action="store_true", help="Open the local status page in your browser")
    return parser.parse_args(argv)


def start(argv: Optional[List[str]] = None):
    args = parse_server_args(argv)
    if args.open:
        open_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        webbrowser.open(f"http://{open_host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    start()

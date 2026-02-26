# /// script
# requires-python = ">=3.8"
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
import uvicorn
import json
import os
import tempfile
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

import main as core_main
from dala.models import ConversionOptions, Source, log
from dala.core.session import get_session
from dala.core.writer import EpubWriter
from dala.models import sanitize_filename

app = FastAPI()

@app.middleware("http")
async def log_requests(request, call_next):
    print(f"🔹 Incoming request: {request.method} {request.url}")
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
    cookies: Optional[dict] = None
    assets: Optional[list] = None
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
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    summary: bool = False
    thumbnails: bool = False
    youtube_lang: Optional[str] = "en"
    youtube_prefer_auto: bool = False
    youtube_max_comments: int = 25
    youtube_comment_sort: str = "top"

class ScanRequest(BaseModel):
    html: str
    url: str


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str


@dataclass
class ConversionResult:
    tmp_path: str
    filename: str
    saved_user_copy: bool
    total_sources: int
    processed_sources: int


@dataclass
class JobRecord:
    job_id: str
    status: str
    created_at: str
    updated_at: str
    total_sources: int = 0
    processed_sources: int = 0
    current_url: Optional[str] = None
    error: Optional[str] = None
    output_path: Optional[str] = None
    output_filename: Optional[str] = None
    server_saved: bool = False
    cancel_requested: bool = False
    task: Optional[asyncio.Task] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_public(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "total_sources": self.total_sources,
            "processed_sources": self.processed_sources,
            "current_url": self.current_url,
            "error": self.error,
            "server_saved": self.server_saved,
            "output_filename": self.output_filename,
            "download_ready": self.status == "completed" and bool(self.output_path),
            "cancel_requested": self.cancel_requested,
        }


JOBS: Dict[str, JobRecord] = {}
JOBS_LOCK = asyncio.Lock()
JOB_RUN_SEMAPHORE = asyncio.Semaphore(int(os.getenv("DALA_JOB_CONCURRENCY", "1")))
LAST_CONVERSION_STATE: Optional[Dict[str, Any]] = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timing_log(run_id: str, event: str, **fields: Any) -> None:
    payload = {
        "run_id": run_id,
        "event": event,
        "ts": _utc_now(),
    }
    payload.update(fields)
    print(f"TIMING {json.dumps(payload, separators=(',', ':'), ensure_ascii=True)}")


def _set_last_conversion_state(**fields: Any) -> None:
    global LAST_CONVERSION_STATE
    state = {
        "updated_at": _utc_now(),
    }
    state.update(fields)
    LAST_CONVERSION_STATE = state


async def _create_job(total_sources: int) -> JobRecord:
    now = _utc_now()
    record = JobRecord(
        job_id=uuid4().hex,
        status="queued",
        created_at=now,
        updated_at=now,
        total_sources=total_sources,
        processed_sources=0,
    )
    async with JOBS_LOCK:
        JOBS[record.job_id] = record
    return record


async def _get_job(job_id: str) -> Optional[JobRecord]:
    async with JOBS_LOCK:
        return JOBS.get(job_id)


async def _update_job(job_id: str, **fields: Any) -> Optional[JobRecord]:
    async with JOBS_LOCK:
        record = JOBS.get(job_id)
        if not record:
            return None
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        record.updated_at = _utc_now()
        return record


async def _set_job_task(job_id: str, task: asyncio.Task) -> None:
    await _update_job(job_id, task=task)


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
    print(f"📥 Received request: {len(req.sources)} sources")
    print(
        f"🔧 Options: NoComments={req.no_comments}, NoImages={req.no_images}, "
        f"Thumbnails={req.thumbnails}, YTLang={req.youtube_lang}, YTSort={req.youtube_comment_sort}"
    )
    if req.sources:
        for idx, s in enumerate(req.sources):
            count_assets = len(s.assets) if s.assets else 0
            print(f"Source[{idx}] assets: {count_assets}")
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
                print(f"Source[{idx}] assets: {original_count} -> {len(s.assets)} (after filtering)")

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
        llm_model=req.llm_model,
        llm_api_key=req.llm_api_key,
        summary=req.summary,
        thumbnails=req.thumbnails,
        youtube_lang=req.youtube_lang or "en",
        youtube_prefer_auto=req.youtube_prefer_auto,
        youtube_max_comments=req.youtube_max_comments,
        youtube_comment_sort=req.youtube_comment_sort
    )

    core_sources = []
    for s in req.sources:
        core_sources.append(Source(
            url=s.url,
            html=s.html,
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
            print(f"✅ Archived to project exports: {archive_path}")
        except Exception as e:
            print(f"⚠️  Archive failed: {e}")

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
                print(f"📥 Saved local copy to: {final_path}")
                break
        except Exception:
            continue

    if not saved_user_copy:
        print("⚠️  Could not save a server-side copy to any location.")
    return saved_user_copy


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
            title = req.bundle_title or f"Bundle_{len(processed_books)}_Articles"
            author = req.bundle_author or "Web to EPUB"
            final_book = core_main.create_bundle(processed_books, title, author)
            bundle_mode = "bundle"
        else:
            final_book = processed_books[0]
            if req.bundle_title:
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

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp_path = tmp.name

        stage_started_at = time.perf_counter()
        EpubWriter.write(final_book, tmp_path)
        _timing_log(
            run_id,
            "stage_done",
            stage="write_epub",
            duration_ms=int((time.perf_counter() - stage_started_at) * 1000),
        )
        filename = f"{sanitize_filename(final_book.title)}.epub"
        print(f"✅ Generated EPUB at: {tmp_path}")
        print(f"✅ Sending as: {filename}")

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
            output_filename=filename,
            server_saved=bool(saved_user_copy),
        )

        return ConversionResult(
            tmp_path=tmp_path,
            filename=filename,
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
        _set_last_conversion_state(
            status="failed",
            run_id=run_id,
            request_token=req.request_token,
            mode="jobs" if job_id else "convert",
            finished_at=_utc_now(),
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
            total_sources=total_sources,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _timing_log(
            run_id,
            "job_error",
            duration_ms=int((time.perf_counter() - run_started_at) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise

@app.post("/helper/extract-links")
async def extract_links(req: ScanRequest):
    """
    Helper for Chrome Extension (MV3) which cannot use DOMParser.
    Extracts potential image assets and next-page links from HTML.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse
    import re

    soup = BeautifulSoup(req.html, 'html.parser')
    base_url = req.url
    
    # 1. Extract Images (matching background.js logic)
    images = []
    seen = set()
    
    # Selectors for forum posts
    img_tags = soup.select(".message-body img, .messageContent img, .bbWrapper img, .bbImage img")
    # Containers fallback
    if not img_tags:
        containers = soup.select("article.message, .message--post, [data-lb-id]")
        for c in containers:
            img_tags.extend(c.find_all("img"))

    for img in img_tags:
        src = img.get("src") or img.get("data-src")
        if not src: continue
        
        # Skip junk
        src_lower = src.lower()
        if any(x in src_lower for x in ["/avatar", "/reaction", "/smilies", "/emoji", "data:image/gif"]):
            continue
            
        parent = img.find_parent("a")
        viewer = urljoin(base_url, parent["href"]) if (parent and parent.get("href")) else None
        
        # Collect URLs
        primary = urljoin(base_url, src)
        
        if "/attachments/" not in primary and not viewer: 
            # If it's not an attachment, maybe it's external?
            pass
        
        key = primary.split("?")[0]
        if key in seen: continue
        seen.add(key)
        
        images.append({
            "url": primary,
            "viewer_url": viewer,
            "filename_hint": src.split("/")[-1]
        })

    # External images (non-attachment)
    externals = []
    for img in img_tags:
        src = img.get("src") or img.get("data-src")
        if not src or src.startswith("data:"): continue
        full = urljoin(base_url, src)
        if full not in seen:
            externals.append(full)
            seen.add(full)

    # 2. Find Next Page
    next_page = None
    
    # <link rel="next">
    link_next = soup.find("link", attrs={"rel": "next"})
    if link_next and link_next.get("href"):
        next_page = urljoin(base_url, link_next.get("href"))
    
    if not next_page:
        # <a>Next</a>
        for a in soup.find_all("a"):
            txt = a.get_text(strip=True).lower()
            if txt in ("next", "next >", "next>"):
                if a.get("href"):
                    next_page = urljoin(base_url, a.get("href"))
                    break
    
    # Parse page number from next_url to return int if possible? 
    # The extension logic expects a URL to fetch, or logic to build it.
    # The extension builds it: `buildForumPageUrl`.
    # But `findNextPage` in JS returned an integer page number.
    # Let's return the integer if we can extract it.
    
    next_page_num = None
    if next_page:
        m = re.search(r'page[-=_/](\d+)', next_page)
        if m:
            try:
                next_page_num = int(m.group(1))
            except: pass

    return {
        "assets": images,
        "externals": externals,
        "next_page_num": next_page_num
    }


@app.get("/helper/last-conversion")
async def last_conversion_state():
    if not LAST_CONVERSION_STATE:
        raise HTTPException(status_code=404, detail="No conversion state available.")
    return LAST_CONVERSION_STATE


@app.get("/ping")
async def ping(): return {"status": "ok"}

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
                server_saved=result.saved_user_copy,
                error=None,
            )
        except asyncio.CancelledError:
            await _update_job(job_id, status="cancelled", current_url=None, error="Job cancelled.")
        except Exception as e:
            log.exception(f"Job {job_id} failed: {e}")
            await _update_job(job_id, status="failed", current_url=None, error=str(e))


@app.post("/jobs", response_model=JobSubmitResponse)
async def create_job(req: ConversionRequest):
    job = await _create_job(total_sources=len(req.sources))
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
        media_type='application/epub+zip',
        headers={"X-Dala-Server-Saved": "1" if job.server_saved else "0"},
    )


@app.post("/convert")
async def convert(req: ConversionRequest):
    try:
        result = await run_conversion_job(req)
        return FileResponse(
            path=result.tmp_path,
            filename=result.filename,
            media_type='application/epub+zip',
            headers={"X-Dala-Server-Saved": "1" if result.saved_user_copy else "0"},
        )
    except asyncio.CancelledError:
        raise HTTPException(status_code=499, detail="Conversion cancelled.")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

def start():
    uvicorn.run(app, host="127.0.0.1", port=8000)

if __name__ == "__main__":
    start()

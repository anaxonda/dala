import asyncio
import os
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models import log
from . import job_store


JOB_RETENTION_SECONDS = int(os.getenv("DALA_JOB_RETENTION_SECONDS", str(2 * 60 * 60)))
JOB_CLEANUP_INTERVAL_SECONDS = int(os.getenv("DALA_JOB_CLEANUP_INTERVAL_SECONDS", "300"))
JOB_RUN_SEMAPHORE = asyncio.Semaphore(int(os.getenv("DALA_JOB_CONCURRENCY", "1")))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class SourceRecovery:
    recovery_id: str
    url: str
    expires_at: float
    marker: str
    future: asyncio.Future
    captured: bool = False
    include_cookies: bool = False

    def to_public(self) -> Dict[str, Any]:
        return {
            "recovery_id": self.recovery_id,
            "url": self.url,
            "expires_at": self.expires_at,
            "marker": self.marker,
            "captured": self.captured,
            "include_cookies": self.include_cookies,
        }


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
    output_media_type: str = "application/epub+zip"
    server_saved: bool = False
    cancel_requested: bool = False
    failed_source_details: List[Dict[str, Any]] = field(default_factory=list)
    request: Optional[Any] = None
    verification_url: Optional[str] = None
    verification_token: Optional[str] = None
    verification_source_url: Optional[str] = None
    verification_marker: Optional[str] = None
    user_browser_url: Optional[str] = None
    task: Optional[asyncio.Task] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    source_recoveries: Dict[str, SourceRecovery] = field(default_factory=dict)
    source_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    cached_books: Dict[str, Any] = field(default_factory=dict)
    finish_event: asyncio.Event = field(default_factory=asyncio.Event)
    persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
            "output_media_type": self.output_media_type,
            "download_ready": self.status == "completed" and bool(self.output_path),
            "cancel_requested": self.cancel_requested,
            "failed_source_details": self.failed_source_details,
            "verification_url": self.verification_url,
            "verification_token": self.verification_token,
            "verification_source_url": self.verification_source_url,
            "verification_marker": self.verification_marker,
            "user_browser_url": self.user_browser_url,
            "source_recoveries": [item.to_public() for item in self.source_recoveries.values()],
            "source_states": [{**item, "url": url, "elapsed_seconds": max(0, item.get("finished_at", time.time()) - item["started_at"])} for url, item in self.source_states.items()],
            "successful_sources": len(self.cached_books),
            "skipped_sources": len(self.failed_source_details),
            "finish_requested": self.finish_event.is_set(),
            "can_finish": self.status in {"running", "discovering"} and bool(self.cached_books),
            "can_retry": self.status in {"completed", "failed", "cancelled", "interrupted"} and bool(self.request) and (self.status != "completed" or bool(self.failed_source_details) or len(self.cached_books) < self.total_sources),
        }


JOBS: Dict[str, JobRecord] = {}
JOBS_LOCK = asyncio.Lock()
LAST_CONVERSION_STATE: Optional[Dict[str, Any]] = None


async def create_job(total_sources: int) -> JobRecord:
    now = utc_now()
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


async def get_job(job_id: str) -> Optional[JobRecord]:
    async with JOBS_LOCK:
        return JOBS.get(job_id)


async def update_job(job_id: str, **fields: Any) -> Optional[JobRecord]:
    async with JOBS_LOCK:
        record = JOBS.get(job_id)
        if not record:
            return None
        for key, value in fields.items():
            if hasattr(record, key):
                setattr(record, key, value)
        record.updated_at = utc_now()
        return record


async def set_job_task(job_id: str, task: asyncio.Task) -> None:
    await update_job(job_id, task=task)


async def persist_job(record: JobRecord) -> None:
    if not record.request:
        return
    async with record.persist_lock:
        request = record.request.model_dump(exclude={"llm_api_key"})
        for source in request["sources"]:
            for key in ("cookies", "html", "assets", "page_htmls", "asset_debug"):
                source.pop(key, None)
        state = record.to_public()
        state.update(request=request, output_path=record.output_path)
        try:
            await asyncio.to_thread(job_store.write_json, job_store.job_dir(record.job_id) / 'manifest.json', state)
        except OSError as exc:
            log.warning(f'Could not persist job {record.job_id}: {exc}')


async def restore_jobs(request_factory) -> None:
    for manifest in job_store.root_dir().glob('*/manifest.json'):
        try:
            data = await asyncio.to_thread(lambda p=manifest: json.loads(p.read_text()))
            job_id = data['job_id']
            if job_id in JOBS:
                continue
            record = JobRecord(job_id=job_id, status=data['status'], created_at=data['created_at'], updated_at=data['updated_at'])
            record.request = request_factory(data['request'])
            record.cached_books = await asyncio.to_thread(job_store.load_books, job_id)
            record.total_sources = len(record.request.sources)
            record.processed_sources = data.get('processed_sources', len(record.cached_books))
            record.failed_source_details = data.get('failed_source_details', [])
            record.output_path = data.get('output_path')
            record.output_filename = data.get('output_filename')
            record.output_media_type = data.get('output_media_type', 'application/epub+zip')
            record.server_saved = data.get('server_saved', False)
            if record.status not in {'completed', 'failed', 'cancelled', 'user_browser_required'}:
                record.status = 'interrupted'
                record.error = 'Server restarted. Retry to continue from saved articles.'
            if record.status == 'completed' and (not record.output_path or not os.path.isfile(record.output_path)):
                record.status = 'interrupted'
                record.error = 'Output file expired. Retry to rebuild from saved articles.'
            JOBS[job_id] = record
        except Exception as exc:
            log.warning(f'Could not restore saved job {manifest.parent.name}: {exc}')


def set_last_conversion_state(**fields: Any) -> None:
    global LAST_CONVERSION_STATE
    state = {
        "updated_at": utc_now(),
    }
    state.update(fields)
    LAST_CONVERSION_STATE = state


async def cleanup_finished_jobs(retention_seconds: int = JOB_RETENTION_SECONDS) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - max(0, retention_seconds)
    removed = 0
    async with JOBS_LOCK:
        for job_id, record in list(JOBS.items()):
            if record.status not in {"completed", "failed", "cancelled", "user_browser_required", "interrupted"}:
                continue
            try:
                updated = parse_utc(record.updated_at).timestamp()
            except Exception:
                updated = 0
            if updated > cutoff:
                continue

            output_path = record.output_path
            if output_path and os.path.exists(output_path):
                try:
                    os.unlink(output_path)
                except OSError as exc:
                    log.warning(f"Could not remove old job output {output_path}: {exc}")
            del JOBS[job_id]
            await asyncio.to_thread(job_store.remove_job, job_id)
            removed += 1
    return removed


async def job_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(max(30, JOB_CLEANUP_INTERVAL_SECONDS))
        try:
            removed = await cleanup_finished_jobs()
            if removed:
                log.info(f"Cleaned up {removed} finished job records.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"Job cleanup failed: {exc}")

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models import log


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
            if record.status not in {"completed", "failed", "cancelled", "user_browser_required"}:
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

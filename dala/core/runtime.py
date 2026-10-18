"""Per-source scheduling shared by drivers and browser acquisition."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Callable, Optional


@dataclass
class SourceRuntime:
    url: str
    semaphore: asyncio.Semaphore
    callback: Optional[Callable[..., Any]] = None
    acquired: bool = False

    async def report(self, stage: str) -> None:
        if self.callback:
            result = self.callback(self.url, stage)
            if isawaitable(result):
                await result

    async def acquire(self) -> None:
        await self.report("waiting_processing")
        await self.semaphore.acquire()
        self.acquired = True
        await self.report("processing")

    def release(self) -> None:
        if self.acquired:
            self.acquired = False
            self.semaphore.release()


CURRENT_SOURCE: ContextVar[Optional[SourceRuntime]] = ContextVar("dala_source_runtime", default=None)


async def report_source_stage(stage: str) -> None:
    runtime = CURRENT_SOURCE.get()
    if runtime:
        await runtime.report(stage)


@asynccontextmanager
async def browser_work():
    """Browser queueing and capture never occupy an article processing slot."""
    runtime = CURRENT_SOURCE.get()
    resume = bool(runtime and runtime.acquired)
    if resume:
        runtime.release()
    try:
        yield
    except asyncio.CancelledError:
        raise
    except Exception:
        if resume:
            await runtime.acquire()
        raise
    else:
        if resume:
            await runtime.acquire()

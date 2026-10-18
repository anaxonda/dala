import asyncio

import pytest
from fastapi import HTTPException

from dala import cli, server
from dala.core.browser import BrowserChallengeError
from dala.core.dispatcher import DriverDispatcher
from dala.core.extractor import ArticleExtractor
from dala.models import BookData, Chapter, ConversionOptions, Source


def make_book(url):
    return BookData(
        title=url.rsplit('/', 1)[-1], author="Author", uid=url, language="en",
        description="", source_url=url,
        chapters=[Chapter(title="Article", filename="article.xhtml", content_html="<p>Content</p>", uid=url, is_article=True)],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('recovery_fails', [False, True])
async def test_bundle_keeps_good_sources_and_reports_challenge(monkeypatch, recovery_fails):
    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/challenge'):
                raise BrowserChallengeError(source.url, "verification required")
            if source.url.endswith('/error'):
                raise RuntimeError("broken source")
            return make_book(source.url)

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    async def failed_recovery(source, challenge):
        raise BrowserChallengeError(source.url, "still blocked after capture")

    timings = []
    urls = ['https://example.com/first', 'https://example.com/challenge', 'https://example.com/error', 'https://example.com/last']
    books = await cli.process_urls(
        [Source(url=url) for url in urls], ConversionOptions(), object(),
        source_timing_callback=lambda *args: timings.append(args),
        challenge_callback=failed_recovery if recovery_fails else None,
    )
    assert [book.source_url for book in books] == [urls[0], urls[3]]
    errors = {url: error for url, _, success, error in timings if not success}
    assert errors == {urls[1]: 'BrowserChallengeError', urls[2]: 'RuntimeError'}


@pytest.mark.asyncio
async def test_single_source_challenge_preserves_browser_handoff(monkeypatch):
    class Driver:
        async def prepare_book_data(self, context, source):
            raise BrowserChallengeError(source.url, "verification required")

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    with pytest.raises(BrowserChallengeError):
        await cli.process_urls([Source(url='https://example.com/challenge')], ConversionOptions(), object())


@pytest.mark.asyncio
async def test_recovery_releases_source_slot_and_keeps_order(monkeypatch):
    other_done = asyncio.Event()
    monkeypatch.setattr(cli, 'GLOBAL_SEMAPHORE', asyncio.Semaphore(1))

    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/challenge'):
                raise BrowserChallengeError(source.url, "verification required")
            other_done.set()
            return make_book(source.url)

    async def recover(source, challenge):
        await asyncio.wait_for(other_done.wait(), 1)
        return make_book(source.url)

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    urls = ['https://example.com/challenge', 'https://example.com/other']
    books = await cli.process_urls(
        [Source(url=url) for url in urls], ConversionOptions(), object(), challenge_callback=recover,
    )
    assert [book.source_url for book in books] == urls


@pytest.mark.asyncio
@pytest.mark.parametrize('recover', [True, False])
async def test_server_bundle_capture_or_timeout(monkeypatch, recover):
    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/challenge') and not source.html:
                raise BrowserChallengeError(source.url, "verification required")
            return make_book(source.url)

    written = []

    async def write(book, path, options):
        written.append(book)

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    monkeypatch.setattr(server, 'write_output_book', write)
    monkeypatch.setattr(server, '_save_server_copy', lambda *args: False)
    monkeypatch.setattr(ArticleExtractor, 'extract_from_html', lambda *args: {'success': True})
    job = await server._create_job(2)
    job.status = 'running'
    request = server.ConversionRequest(
        sources=[server.SourceItem(url='https://example.com/challenge'), server.SourceItem(url='https://example.com/other')],
        browser_challenge_action='user_browser', browser_recovery_timeout_seconds=1 if recover else 0.05,
    )
    task = asyncio.create_task(server.run_conversion_job(request, job_id=job.job_id))
    result = None
    try:
        for _ in range(100):
            if job.source_recoveries:
                break
            await asyncio.sleep(0.001)
        assert job.source_recoveries
        pending = next(iter(job.source_recoveries.values()))
        if recover:
            with pytest.raises(HTTPException) as missing_html:
                await server.capture_recovered_source(job.job_id, pending.recovery_id, server.SourceItem(url=pending.url))
            assert missing_html.value.status_code == 422
            with pytest.raises(HTTPException) as mismatch:
                await server.capture_recovered_source(job.job_id, pending.recovery_id, server.SourceItem(url='https://example.com/wrong', html='<article>Content</article>'))
            assert mismatch.value.status_code == 400
            accepted = await server.capture_recovered_source(
                job.job_id, pending.recovery_id, server.SourceItem(url=pending.url, html='<article>Content</article>'),
            )
            assert accepted['accepted']
        result = await asyncio.wait_for(task, 2)
        assert result.processed_sources == (2 if recover else 1)
        assert not job.source_recoveries
        state = server.job_state.LAST_CONVERSION_STATE
        assert state['failed_sources'] == (0 if recover else 1)
        if not recover:
            assert state['failed_source_details'][0]['error_type'] == 'BrowserChallengeError'
        with pytest.raises(HTTPException) as expired:
            await server.capture_recovered_source(job.job_id, pending.recovery_id, server.SourceItem(url=pending.url, html='<article>Content</article>'))
        assert expired.value.status_code == 409
        assert written
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.JOBS.pop(job.job_id, None)
        if result:
            from pathlib import Path
            Path(result.tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_bundle_cancellation_stops_recovery_and_other_sources(monkeypatch):
    waiting = asyncio.Event()
    stopped = asyncio.Event()

    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/challenge'):
                raise BrowserChallengeError(source.url, "verification required")
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    async def recover(*args):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    task = asyncio.create_task(cli.process_urls(
        [Source(url='https://example.com/challenge'), Source(url='https://example.com/other')],
        ConversionOptions(), object(), challenge_callback=recover,
    ))
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.parametrize('directory', ['extension_chrome', 'firefox_extension'])
def test_extension_reuses_tabs_and_recaptures_after_challenge(directory):
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for extension recovery tests')
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, str(root / 'tests/bundle_recovery_extension.cjs'), directory], cwd=root, check=True, timeout=10)


@pytest.mark.asyncio
async def test_saturated_browser_queue_does_not_block_recovered_capture(monkeypatch):
    from dala.core import browser

    monkeypatch.setattr(cli, 'GLOBAL_SEMAPHORE', asyncio.Semaphore(2))
    browser_started = asyncio.Event()
    recovery_done = asyncio.Event()
    finish = asyncio.Event()
    states = []

    async def stalled_fetch(url, options):
        browser_started.set()
        await asyncio.Event().wait()

    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/challenge') and not source.html:
                raise BrowserChallengeError(source.url, 'verification required')
            if source.html:
                recovery_done.set()
                return make_book(source.url)
            await browser.fetch_rendered_source(source.url, browser.BrowserFetchOptions())
            return make_book(source.url)

    async def recover(source, challenge):
        await browser_started.wait()
        books = await cli.process_urls(
            [Source(url=source.url, html='<article>Recovered</article>')], ConversionOptions(), object(),
        )
        return books[0]

    monkeypatch.setattr(browser, '_fetch_rendered_source', stalled_fetch)
    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    sources = [Source(url='https://example.com/challenge')] + [Source(url=f'https://example.com/browser-{i}') for i in range(8)]
    task = asyncio.create_task(cli.process_urls(
        sources, ConversionOptions(), object(), challenge_callback=recover,
        source_state_callback=lambda url, stage: states.append((url, stage)), finish_event=finish,
    ))
    try:
        await asyncio.wait_for(recovery_done.wait(), 1)
        # Let the recovered task return before requesting a partial result.
        for _ in range(100):
            if ('https://example.com/challenge', 'completed') in states:
                break
            await asyncio.sleep(0.001)
        finish.set()
        books = await asyncio.wait_for(task, 1)
        assert [book.source_url for book in books] == ['https://example.com/challenge']
        assert any(stage == 'waiting_browser' for _, stage in states)
        assert cli.GLOBAL_SEMAPHORE._value == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_source_deadline_keeps_other_results(monkeypatch):
    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/stuck'):
                await asyncio.Event().wait()
            return make_book(source.url)

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    timings = []
    books = await cli.process_urls(
        [Source(url='https://example.com/stuck'), Source(url='https://example.com/good')],
        ConversionOptions(source_timeout_seconds=0.02), object(),
        source_timing_callback=lambda *args: timings.append(args),
    )
    assert [book.title for book in books] == ['good']
    assert any(not success and error == 'TimeoutError' for _, _, success, error in timings)


@pytest.mark.asyncio
async def test_checkpoints_restore_and_retry_without_refetching_successes(monkeypatch, tmp_path):
    from dala.core import job_store
    from dala.models import ImageAsset
    from ebooklib import epub

    old = await server._create_job(2)
    old.request = server.ConversionRequest(
        sources=[server.SourceItem(url='https://example.com/good', cookies={'session': 'secret-cookie'}, html='secret-html'), server.SourceItem(url='https://example.com/retry')],
        llm_api_key='secret-api-key',
    )
    book = make_book('https://example.com/good')
    book.images = [ImageAsset(uid='image', filename='image.jpg', media_type='image/jpeg', content=b'image-bytes', original_url='https://example.com/image.jpg')]
    book.toc_structure = [(epub.Link('article.xhtml', 'Article', 'article'), [epub.Link('comments.xhtml', 'Comments', 'comments')])]
    old.cached_books[book.source_url] = book
    await asyncio.to_thread(job_store.save_book, old.job_id, book.source_url, book)
    await server.job_state.persist_job(old)
    manifest = (job_store.job_dir(old.job_id) / 'manifest.json').read_text()
    assert all(secret not in manifest for secret in ['secret-cookie', 'secret-html', 'secret-api-key'])
    server.JOBS.pop(old.job_id)
    await server.job_state.restore_jobs(server.ConversionRequest.model_validate)
    restored = server.JOBS[old.job_id]
    assert restored.status == 'interrupted'
    assert restored.to_public()['can_retry']
    assert restored.cached_books[book.source_url].images[0].content == b'image-bytes'
    assert restored.cached_books[book.source_url].toc_structure[0][0].title == 'Article'
    called = []

    class Driver:
        async def prepare_book_data(self, context, source):
            called.append(source.url)
            return make_book(source.url)

    async def write(book, path, options):
        from pathlib import Path
        Path(path).write_bytes(b'epub')

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    monkeypatch.setattr(server, 'write_output_book', write)
    monkeypatch.setattr(server, '_save_server_copy', lambda *args: False)
    result = await server.retry_job_sources(old.job_id)
    retried = server.JOBS[result.job_id]
    try:
        await asyncio.wait_for(retried.task, 2)
        assert retried.status == 'completed'
        assert set(retried.cached_books) == {book.source_url, 'https://example.com/retry'}
        assert called == ['https://example.com/retry']
    finally:
        from pathlib import Path
        if retried.output_path:
            Path(retried.output_path).unlink(missing_ok=True)
        server.JOBS.pop(old.job_id, None)
        server.JOBS.pop(retried.job_id, None)


@pytest.mark.asyncio
async def test_capture_gets_conversion_deadline_after_verification(monkeypatch):
    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/challenge'):
                if not source.html:
                    raise BrowserChallengeError(source.url, 'verification required')
                await asyncio.sleep(0.08)
            return make_book(source.url)

    async def write(*args):
        pass

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    monkeypatch.setattr(server, 'write_output_book', write)
    monkeypatch.setattr(server, '_save_server_copy', lambda *args: False)
    monkeypatch.setattr(ArticleExtractor, 'extract_from_html', lambda *args: {'success': True})
    job = await server._create_job(2)
    job.status = 'running'
    req = server.ConversionRequest(
        sources=[server.SourceItem(url='https://example.com/challenge'), server.SourceItem(url='https://example.com/good')],
        browser_challenge_action='user_browser', browser_recovery_timeout_seconds=0.05,
        source_timeout_seconds=1,
    )
    task = asyncio.create_task(server.run_conversion_job(req, job_id=job.job_id))
    result = None
    try:
        for _ in range(100):
            if job.source_recoveries:
                break
            await asyncio.sleep(0.001)
        recovery = next(iter(job.source_recoveries.values()))
        await server.capture_recovered_source(job.job_id, recovery.recovery_id, server.SourceItem(url=recovery.url, html='<article>Content</article>'))
        result = await asyncio.wait_for(task, 1)
        assert result.processed_sources == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.JOBS.pop(job.job_id, None)
        if result:
            from pathlib import Path
            Path(result.tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_finish_endpoint_builds_saved_articles_and_marks_pending_skipped(monkeypatch):
    good_ready = asyncio.Event()
    class Driver:
        async def prepare_book_data(self, context, source):
            if source.url.endswith('/slow'):
                await asyncio.Event().wait()
            good_ready.set()
            return make_book(source.url)

    async def write(*args):
        pass

    monkeypatch.setattr(DriverDispatcher, 'get_driver', lambda *args: Driver())
    monkeypatch.setattr(server, 'write_output_book', write)
    monkeypatch.setattr(server, '_save_server_copy', lambda *args: False)
    req = server.ConversionRequest(sources=[server.SourceItem(url='https://example.com/good'), server.SourceItem(url='https://example.com/slow')])
    job = await server._create_job(2)
    job.request = req
    task = asyncio.create_task(server._run_job_task(job.job_id, req))
    try:
        await asyncio.wait_for(good_ready.wait(), 1)
        for _ in range(100):
            if job.cached_books:
                break
            await asyncio.sleep(0.001)
        assert job.to_public()['can_finish']
        await server.finish_job_available(job.job_id)
        await asyncio.wait_for(task, 1)
        assert job.status == 'completed'
        assert len(job.cached_books) == 1
        assert job.failed_source_details[0]['error_type'] == 'FinishedEarly'
        assert job.to_public()['can_retry']
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.JOBS.pop(job.job_id, None)
        if job.output_path:
            from pathlib import Path
            Path(job.output_path).unlink(missing_ok=True)

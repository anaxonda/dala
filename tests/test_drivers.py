import pytest
import aiohttp
from unittest.mock import patch, MagicMock
from aioresponses import aioresponses
from web_to_epub import GenericDriver, Source, ConversionContext, ConversionOptions, ARCHIVE_ORG_API_BASE

@pytest.mark.asyncio
async def test_generic_driver_fetch_success():
    url = "https://example.com/article"
    html = """<html><head><title>Test Article</title></head>
              <body><h1>Test Article</h1><p>Some content.</p></body></html>"""
    
    with aioresponses() as m:
        m.get(url, status=200, body=html)
        
        async with aiohttp.ClientSession() as session:
            options = ConversionOptions()
            context = ConversionContext(session=session, options=options)
            source = Source(url=url)
            driver = GenericDriver()
            
            book = await driver.prepare_book_data(context, source)
            
            assert book is not None
            assert book.title == "Test Article"
            assert "Some content" in book.chapters[0].content_html
            assert book.source_url == url

@pytest.mark.asyncio
async def test_generic_driver_404():
    url = "https://example.com/404"
    
    # Mock requests response
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = ""

    with aioresponses() as m:
        # 1. Main fetch fails (aiohttp)
        m.get(url, status=404)
        
        # 2. Archive fetch fails (mocking the API call)
        # We use a regex to match the archive URL loosely
        import re
        archive_pattern = re.compile(f"^{re.escape(ARCHIVE_ORG_API_BASE)}.*")
        m.get(archive_pattern, status=200, payload={})
        
        # Patch requests.get for the synchronous fallback
        with patch("requests.get", return_value=mock_resp):
            # Patch asyncio.sleep to skip retry delays
            with patch("asyncio.sleep", return_value=None):
                async with aiohttp.ClientSession() as session:
                    options = ConversionOptions()
                    context = ConversionContext(session=session, options=options)
                    source = Source(url=url)
                    driver = GenericDriver()
                    
                    book = await driver.prepare_book_data(context, source)
                    assert book is None

@pytest.mark.asyncio
async def test_generic_driver_switches_to_forum():
    url = "https://example.com/unknown-forum/thread"
    # HTML that triggers the switch AND contains valid posts for ForumDriver
    forum_html = """
    <html>
    <body>
        <div data-template="thread_view">
            <article class="message message--post" id="post-1">
                <div class="message-inner">
                    <div class="message-cell message-cell--user">
                        <div class="message-user">
                            <h4 class="message-name"><a href="#" class="username">TestUser</a></h4>
                        </div>
                    </div>
                    <div class="message-cell message-cell--main">
                        <div class="message-content">
                            <div class="message-body">Hello Forum</div>
                        </div>
                    </div>
                </div>
            </article>
        </div>
    </body>
    </html>
    """
    
    with aioresponses() as m:
        # 1. GenericDriver fetch
        m.get(url, status=200, body=forum_html)
        # 2. ForumDriver fetch (it starts over)
        m.get(url, status=200, body=forum_html)
        
        async with aiohttp.ClientSession() as session:
            options = ConversionOptions()
            context = ConversionContext(session=session, options=options)
            source = Source(url=url)
            driver = GenericDriver()
            
            # This should return a BookData object from ForumDriver
            book = await driver.prepare_book_data(context, source)
            
            assert book is not None
            # ForumDriver sets author="Forum" usually
            assert book.author == "Forum" 
            assert "urn:forum:" in book.uid

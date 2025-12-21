import pytest
import aiohttp
from aioresponses import aioresponses
from web_to_epub import GenericDriver, Source, ConversionContext, ConversionOptions

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
    
    with aioresponses() as m:
        m.get(url, status=404)
        
        async with aiohttp.ClientSession() as session:
            options = ConversionOptions()
            context = ConversionContext(session=session, options=options)
            source = Source(url=url)
            driver = GenericDriver()
            
            book = await driver.prepare_book_data(context, source)
            assert book is None

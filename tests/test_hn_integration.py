import pytest
import aiohttp
from unittest.mock import patch, MagicMock, AsyncMock
from aioresponses import aioresponses
from web_to_epub import HackerNewsDriver, Source, ConversionContext, ConversionOptions, BookData, Chapter, HN_API_BASE_URL, DriverDispatcher

@pytest.mark.asyncio
async def test_hn_driver_delegates_to_substack():
    hn_item_id = "12345"
    hn_url = f"https://news.ycombinator.com/item?id={hn_item_id}"
    target_url = "https://example.substack.com/p/test-article"
    
    # Mock HN API response
    hn_data = {
        "id": 12345,
        "title": "HN Title",
        "by": "user1",
        "url": target_url,
        "kids": [67890]
    }
    
    # Mock Substack Book Data
    sub_chapters = [
        Chapter(title="Sub Title", filename="sub_art.xhtml", content_html="<p>Sub Content</p>", uid="sub_art", is_article=True),
        Chapter(title="Sub Comments", filename="sub_com.xhtml", content_html="<p>Sub Com</p>", uid="sub_com", is_comments=True)
    ]
    sub_book = BookData(
        title="Sub Title", 
        author="Sub Author", 
        uid="urn:sub:1", 
        language="en", 
        description="desc", 
        source_url=target_url, 
        chapters=sub_chapters, 
        images=[]
    )
    
    # Mock DriverDispatcher
    mock_sub_driver = AsyncMock()
    mock_sub_driver.prepare_book_data.return_value = sub_book
    mock_sub_driver.__class__.__name__ = "SubstackDriver" # Fake the name
    
    # We need to simulate fetching the HN comment
    
    with aioresponses() as m:
        m.get(f"{HN_API_BASE_URL}item/{hn_item_id}.json", payload=hn_data)
        m.get(f"{HN_API_BASE_URL}item/67890.json", payload={"id": 67890, "text": "HN Comment", "by": "user2"})
        
        with patch("web_to_epub.DriverDispatcher.get_driver") as mock_get_driver:
            # Setup the mock to return our sub driver when called with target_url
            def side_effect(source, profile=None):
                if source.url == target_url:
                    return mock_sub_driver
                # For any other URL (shouldn't be any in this flow), return a dummy
                return MagicMock()
            
            mock_get_driver.side_effect = side_effect

            async with aiohttp.ClientSession() as session:
                options = ConversionOptions()
                context = ConversionContext(session=session, options=options)
                source = Source(url=hn_url)
                driver = HackerNewsDriver()
                
                book = await driver.prepare_book_data(context, source)
                
                assert book is not None
                
                # Check Title 
                # The Book title stays as the HN Title (the container)
                assert book.title == "HN Title"
                
                # The Article Chapter should have the Substack title
                assert book.chapters[0].title == "Sub Title"
                
                # Check Chapters
                # 1. Linked Article
                # 2. Linked Comments
                # 3. HN Comments
                assert len(book.chapters) == 3
                
                # Check filenames/UIDs
                assert book.chapters[0].filename == "linked_sub_art.xhtml"
                assert book.chapters[0].uid == "linked_sub_art"
                
                assert book.chapters[1].filename == "linked_sub_com.xhtml"
                assert book.chapters[1].uid == "linked_sub_com"
                
                assert book.chapters[2].filename == "hn_comments.xhtml"
                assert book.chapters[2].uid == "hn_comments"
                
                # Check TOC
                # Expectation: 
                # 1. (Article, [Source Comments, HN Comments])
                
                toc = book.toc_structure
                assert len(toc) == 1
                
                # Item 1: Tuple (Article, [Source Comments, HN Comments])
                assert isinstance(toc[0], tuple)
                assert toc[0][0].href == "linked_sub_art.xhtml"
                
                children = toc[0][1]
                assert len(children) == 2
                assert children[0].href == "linked_sub_com.xhtml"
                assert children[0].title == "Substack Comments"
                
                assert children[1].href == "hn_comments.xhtml"
                assert children[1].title == "HN Comments"

import pytest
from web_to_epub import DriverDispatcher, Source, ForumDriver, SubstackDriver, HackerNewsDriver, GenericDriver, BaseImageProcessor, ForumImageProcessor

def test_driver_dispatch_explicit_forum():
    src = Source(url="http://example.com", is_forum=True)
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, ForumDriver)

def test_driver_dispatch_hn():
    src = Source(url="https://news.ycombinator.com/item?id=123")
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, HackerNewsDriver)

def test_driver_dispatch_substack():
    src = Source(url="https://test.substack.com/p/123")
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, SubstackDriver)

def test_driver_dispatch_generic():
    src = Source(url="https://example.com")
    driver = DriverDispatcher.get_driver(src)
    assert isinstance(driver, GenericDriver)

def test_is_junk_generic():
    assert BaseImageProcessor.is_junk("https://example.com/spacer.gif")
    assert not BaseImageProcessor.is_junk("https://example.com/photo.jpg")

def test_is_junk_forum():
    assert ForumImageProcessor.is_junk("https://example.com/reaction_id=5")
    assert not ForumImageProcessor.is_junk("https://example.com/attachment/123.jpg")

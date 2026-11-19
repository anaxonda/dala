import json
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aioresponses import aioresponses
from bs4 import BeautifulSoup

from dala.core.dispatcher import DriverDispatcher
from dala.core.extractor import ArticleExtractor
from dala.core.profiles import ProfileManager
from dala.drivers.forum import ForumDriver
from dala.drivers.generic import GenericDriver
from dala.drivers.hn import HackerNewsDriver
from dala.drivers.reddit import RedditDriver
from dala.drivers.substack import SubstackDriver
from dala.drivers.wordpress import WordPressDriver
from dala.drivers.youtube import YouTubeDriver
from dala.models import ConversionContext, ConversionOptions, HN_API_BASE_URL, SiteProfile, Source


@pytest.mark.parametrize(("url", "is_forum", "expected"), [
    ("https://www.youtube.com/watch?v=abc123&ref=wordpress.com", True, YouTubeDriver),
    ("https://www.youtube.com/watch?v=abc123&ref=substack.com", False, YouTubeDriver),
    ("https://example.com/p/hiking-boots", False, GenericDriver),
    ("https://notreddit.com/article", False, GenericDriver),
    ("https://example.com/wordpress.com", False, GenericDriver),
    ("https://i.redd.it/image.png", False, GenericDriver),
    ("https://WWW.REDDIT.COM:443/r/test/comments/abc/post/", False, RedditDriver),
    ("https://news.ycombinator.com/newest", False, GenericDriver),
    ("https://news.ycombinator.com/item?id=123", True, HackerNewsDriver),
])
def test_driver_dispatch_respects_host_and_resource(url, is_forum, expected):
    assert isinstance(DriverDispatcher.get_driver(Source(url=url, is_forum=is_forum)), expected)


@pytest.mark.parametrize(("url", "expected"), [
    (
        "https://www.reddit.com/r/test/comments/abc/post/?share_id=xyz&context=3#comment",
        "https://www.reddit.com/r/test/comments/abc/post.json?share_id=xyz&context=3&raw_json=1",
    ),
    (
        "https://www.reddit.com/r/test/comments/abc/post.json?sort=top&raw_json=0",
        "https://www.reddit.com/r/test/comments/abc/post.json?sort=top&raw_json=1",
    ),
    ("https://redd.it/abc", "https://www.reddit.com/comments/abc.json?raw_json=1"),
    ("https://www.reddit.com/gallery/abc", "https://www.reddit.com/comments/abc.json?raw_json=1"),
])
def test_reddit_builds_json_url_from_parsed_path(url, expected):
    assert RedditDriver()._build_api_url(url) == expected


@pytest.mark.asyncio
async def test_reddit_resolves_share_url_before_fetching_thread_json():
    share = "https://www.reddit.com/r/test/s/token?share_id=xyz"
    resolved = "https://www.reddit.com/r/test/comments/abc/post/?sort=new"
    api = "https://www.reddit.com/r/test/comments/abc/post.json?sort=new&raw_json=1"
    post = {"id": "abc", "title": "Reddit post", "selftext_html": "<p>Post body.</p>"}
    with aioresponses() as mock:
        mock.get(share, status=302, headers={"Location": resolved})
        mock.get(resolved, body="<html></html>")
        mock.get(api, payload=[{"data": {"children": [{"data": post}]}}, {"data": {"children": []}}])
        async with aiohttp.ClientSession() as session:
            context = ConversionContext(session, ConversionOptions(no_images=True))
            book = await DriverDispatcher.get_driver(Source(url=share)).prepare_book_data(context, Source(url=share))
    assert book.source_url == share
    assert "Post body." in book.chapters[0].content_html


def article_data(raw_html, url):
    return {
        "success": True, "title": "Example article", "author": "Author",
        "html": "<article><p>Article body.</p></article>",
        "raw_html_for_metadata": raw_html, "source_url": url,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("captured", [True, False])
async def test_article_mentioning_xenforo_stays_generic(monkeypatch, captured):
    url = "https://example.com/article"
    html = "<article>This article discusses XenForo.</article>"
    monkeypatch.setattr(ArticleExtractor, "get_article_content", AsyncMock(return_value=article_data(html, url)))
    source = Source(url=url, html=html if captured else None)
    driver = DriverDispatcher.get_driver(source)
    assert isinstance(driver, GenericDriver)
    book = await driver.prepare_book_data(ConversionContext(None, ConversionOptions(no_images=True)), source)
    assert "Article body." in book.chapters[0].content_html


@pytest.mark.asyncio
@pytest.mark.parametrize("captured", [True, False])
async def test_custom_wordpress_generator_and_legacy_comments(monkeypatch, captured):
    url = "https://example.com/2026/11/example"
    html = """<meta content='WordPress 6.9' name='generator'>
        <ol class='reader-comments commentlist'>
        <li class='comment'><div class='comment-content'>Reader comment.</div></li></ol>"""
    extract = AsyncMock(return_value=article_data(html, url))
    monkeypatch.setattr(ArticleExtractor, "get_article_content", extract)
    source = Source(url=url, html=html if captured else None)
    driver = DriverDispatcher.get_driver(source)
    assert isinstance(driver, WordPressDriver if captured else GenericDriver)
    book = await driver.prepare_book_data(ConversionContext(None, ConversionOptions(no_images=True)), source)
    assert book.chapters[1].is_comments
    assert "Reader comment." in book.chapters[1].content_html
    extract.assert_awaited_once()


def test_substack_uses_metadata_when_preloads_have_no_ids():
    preload = json.dumps(json.dumps({"post": {}, "pub": {}}))
    html = f"""<script>window._preloads = JSON.parse({preload})</script>
        <meta property='substack:post_id' content='123'>
        <meta name='substack:publication_id' content='456'>"""
    assert SubstackDriver()._extract_all_metadata(BeautifulSoup(html, "html.parser"), html) == ("123", "456", None)
    assert isinstance(DriverDispatcher.get_driver(Source(url="https://example.com/p/post", html=html)), SubstackDriver)


@pytest.mark.asyncio
async def test_substack_comment_lookup_uses_resolved_post_url(monkeypatch):
    incoming = "https://substack.com/redirect/example"
    resolved = "https://publication.substack.com/p/example"
    monkeypatch.setattr(ArticleExtractor, "get_article_content", AsyncMock(return_value=article_data("", resolved)))
    slug = AsyncMock(return_value=("123", "456", None))
    comments = AsyncMock(return_value=[{"id": "7", "body": "Reader comment."}])
    monkeypatch.setattr(SubstackDriver, "_fetch_ids_from_slug", slug)
    monkeypatch.setattr(SubstackDriver, "_fetch_comments", comments)

    context = ConversionContext(None, ConversionOptions(no_images=True))
    book = await SubstackDriver().prepare_book_data(context, Source(url=incoming))
    slug.assert_awaited_once_with(resolved, "https://publication.substack.com", context.session)
    comments.assert_awaited_once_with("https://publication.substack.com", "123", "456", context.session)
    assert "Reader comment." in book.chapters[1].content_html


def forum_html(page, next_href=None):
    next_tag = f'<link rel="next" href="{next_href}">' if next_href else ""
    return f"""<html><head><title>Example thread</title>{next_tag}</head>
        <body><article class="message message--post" id="post-{page}" data-author="User">
        <div class="message-content"><div class="bbWrapper">Page {page} body.</div></div>
        </article></body></html>"""


@pytest.mark.asyncio
@pytest.mark.parametrize("next_href", ["?page=2", "/threads/example.42/page/2"])
async def test_forum_follows_advertised_url(next_href):
    base = "https://forum.example.com/threads/example.42"
    next_url = base + "?page=2" if next_href.startswith("?") else "https://forum.example.com" + next_href
    with aioresponses() as mock:
        mock.get(next_url, body=forum_html(2))
        async with aiohttp.ClientSession() as session:
            source = Source(url=base, html=forum_html(1, next_href))
            book = await ForumDriver().prepare_book_data(ConversionContext(session, ConversionOptions(no_images=True)), source)
    assert "Page 2 body." in book.chapters[0].content_html


@pytest.mark.asyncio
async def test_forum_snapshot_of_later_page_keeps_page_number_and_crawl():
    base = "https://forum.example.com/threads/example.42"
    with aioresponses() as mock:
        mock.get(base, body=forum_html(1, "?page=2"))
        mock.get(base + "?page=3", body=forum_html(3))
        async with aiohttp.ClientSession() as session:
            source = Source(url=base + "?page=2#post-2", html=forum_html(2, "?page=3"))
            book = await ForumDriver().prepare_book_data(ConversionContext(session, ConversionOptions(no_images=True)), source)
    text = book.chapters[0].content_html
    assert text.index("Page 1 body.") < text.index("Page 2 body.") < text.index("Page 3 body.")


@pytest.mark.parametrize("url", [
    "https://forum.example.com/threads/example.42?page=2&foo=bar#post-2",
    "https://example.com/index.php?threads/example.42/page-2&foo=bar",
])
def test_forum_normalization_keeps_other_query_parameters(url):
    driver = ForumDriver()
    base = driver._normalize_url(url)
    assert base.endswith("foo=bar")
    assert driver._extract_page_number(base) is None
    next_url = driver._build_page_url(base, 3, url)
    assert driver._extract_page_number(next_url) == 3
    assert "foo=bar" in next_url


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://example.com/article", "https://example.wordpress.com/article"])
async def test_hn_linked_article_extraction_uses_target_profile(monkeypatch, target):
    original_profile = SiteProfile("HN", ["news.ycombinator.com"], content_selector=".hn-only")
    target_profile = SiteProfile("Linked article", ["example"], content_selector=".article-body")
    monkeypatch.setattr(ProfileManager.get_instance(), "get_profile", lambda url: target_profile)
    extract = AsyncMock(return_value=article_data("", target))
    monkeypatch.setattr(ArticleExtractor, "get_article_content", extract)
    with aioresponses() as mock:
        mock.get(f"{HN_API_BASE_URL}item/123.json", payload={"title": "Story", "url": target})
        async with aiohttp.ClientSession() as session:
            context = ConversionContext(session, ConversionOptions(no_images=True), original_profile)
            book = await HackerNewsDriver().prepare_book_data(context, Source(url="https://news.ycombinator.com/item?id=123"))
    assert extract.await_args.kwargs["profile"] is target_profile
    assert book.chapters[0].is_article

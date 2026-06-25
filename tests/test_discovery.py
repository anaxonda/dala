from datetime import date

import pytest

from dala.core import discovery
from dala.core.discovery import (
    DiscoveredPost,
    canonical_url,
    date_from_url_path,
    discover_posts_for_sources,
    extract_candidate_posts,
    extract_feed_posts,
    extract_metadata_date,
    in_range,
    looks_like_article_url,
    parse_bound,
    parse_date_value,
)
from dala.models import ConversionOptions, Source


def test_date_parsing_common_site_shapes():
    assert date_from_url_path("https://example.com/2025/08/15/post/") == date(2025, 8, 15)
    assert parse_date_value("18th June 2026") == date(2026, 6, 18)
    assert parse_date_value("June 18, 2026") == date(2026, 6, 18)
    assert parse_date_value("Thursday, Jun 18, 2026") == date(2026, 6, 18)
    assert parse_date_value("Jun 16", default_year=2026) == date(2026, 6, 16)
    assert parse_date_value("2025-08-15T12:30:00Z") == date(2025, 8, 15)
    assert parse_date_value("2025.08.15") == date(2025, 8, 15)
    assert parse_date_value("2025/8/5") == date(2025, 8, 5)
    assert parse_date_value("15 Aug 2025") == date(2025, 8, 15)
    assert parse_date_value("Aug. 15th, 2025") == date(2025, 8, 15)


def test_parse_bound_accepts_partial_dates():
    assert parse_bound("2025") == date(2025, 1, 1)
    assert parse_bound("2025", is_end=True) == date(2025, 12, 31)
    assert parse_bound("2025-08") == date(2025, 8, 1)
    assert parse_bound("2025-08", is_end=True) == date(2025, 8, 31)
    assert parse_bound("2025-08-15") == date(2025, 8, 15)

    with pytest.raises(discovery.DiscoveryError, match="YYYY, YYYY-MM, or YYYY-MM-DD"):
        parse_bound("2025-13")


def test_crazyguy_doc_urls_preserve_identity_query_params():
    url = "https://www.crazyguyonabike.com/doc/?o=3d2&doc_id=26940&v=12X"

    assert canonical_url(url) == "https://www.crazyguyonabike.com/doc?doc_id=26940&v=12X"
    assert looks_like_article_url(canonical_url(url), url)


def test_date_from_url_path_supports_common_variants():
    assert date_from_url_path("https://example.com/2025/08/15.html") == date(2025, 8, 15)
    assert date_from_url_path("https://example.com/posts/2025/08/15/slug") == date(2025, 8, 15)
    assert date_from_url_path("https://example.com/2025-08-15-slug") == date(2025, 8, 15)
    assert date_from_url_path("https://example.com/post?date=2025-08-15") == date(2025, 8, 15)
    assert date_from_url_path("https://example.com/post?year=2025&month=8&day=15") == date(2025, 8, 15)


def test_extract_metadata_date_from_jsonld_and_time():
    html = """
    <html><head>
    <script type="application/ld+json">{"@type":"Article","datePublished":"2026-06-18T10:00:00Z"}</script>
    </head><body><article>Post</article></body></html>
    """
    assert extract_metadata_date(html, "https://example.com/post") == date(2026, 6, 18)

    html = '<html><body><time datetime="2025-08-15">Aug 15</time></body></html>'
    assert extract_metadata_date(html, "https://example.com/post") == date(2025, 8, 15)

    html = '<html><head><meta name="dc.date.issued" content="2025.08.15"></head><body></body></html>'
    assert extract_metadata_date(html, "https://example.com/post") == date(2025, 8, 15)

    html = '<html><head><meta property="article:modified_time" content="2025-08-16"></head><body></body></html>'
    assert extract_metadata_date(html, "https://example.com/post") == date(2025, 8, 16)


def test_extract_candidate_posts_from_wordpress_and_substack_listing():
    html = """
    <article><a href="/2025/08/15/coye-la-foret-paris-coye-la-foret/">Coye</a></article>
    <div><a href="/p/open-thread">Open Thread</a><span>Jun 16 • Scott</span></div>
    <a href="/about">About</a>
    """
    posts = extract_candidate_posts(html, "https://example.com/2025/08/", default_year=2026)

    by_url = {post.url: post for post in posts}
    assert by_url["https://example.com/2025/08/15/coye-la-foret-paris-coye-la-foret"].published_date == date(2025, 8, 15)
    assert by_url["https://example.com/p/open-thread"].published_date == date(2026, 6, 16)
    assert "https://example.com/about" not in by_url


def test_extract_candidate_posts_uses_archive_month_context_for_day_only_dates():
    html = """
    <div><a href="/a-post-from-archive">Archive Post</a><span>15</span></div>
    """
    posts = extract_candidate_posts(html, "https://example.com/2025/08/", default_year=None)

    assert posts[0].published_date == date(2025, 8, 15)


def test_extract_feed_posts_from_rss_and_atom():
    rss = """
    <rss><channel><item>
        <title>RSS Post</title>
        <link>https://example.com/2025-08-15-rss-post</link>
        <pubDate>Fri, 15 Aug 2025 12:00:00 GMT</pubDate>
    </item></channel></rss>
    """
    atom = """
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Atom Post</title>
        <link href="https://example.com/2025/08/16/atom-post"/>
        <published>2025-08-16T12:00:00Z</published>
    </entry></feed>
    """

    assert extract_feed_posts(rss, "https://example.com/feed")[0].published_date == date(2025, 8, 15)
    assert extract_feed_posts(atom, "https://example.com/atom.xml")[0].published_date == date(2025, 8, 16)


def test_in_range_inclusive_and_undated_handling():
    assert in_range(date(2025, 8, 1), date(2025, 8, 1), date(2025, 8, 31), False)
    assert in_range(date(2025, 8, 31), date(2025, 8, 1), date(2025, 8, 31), False)
    assert not in_range(date(2025, 9, 1), date(2025, 8, 1), date(2025, 8, 31), False)
    assert not in_range(None, None, None, False)
    assert in_range(None, None, None, True)


@pytest.mark.asyncio
async def test_discover_posts_filters_and_hydrates_metadata(monkeypatch):
    listing_html = """
    <a href="/issue/how-abolishing-the-stakeholder-state-caused-the-industrial-revolution/">WIP</a>
    <a href="/issue/older-article/">Older</a>
    """
    pages = {
        "https://worksinprogress.co/archive/": listing_html,
        "https://worksinprogress.co/issue/how-abolishing-the-stakeholder-state-caused-the-industrial-revolution": "<h1>WIP</h1><p>18th June 2026</p>",
        "https://worksinprogress.co/issue/older-article": "<h1>Older</h1><p>1st May 2026</p>",
    }

    async def fake_fetch(session, url, response_type="text", **kwargs):
        return pages.get(url), url

    monkeypatch.setattr(discovery, "fetch_with_retry", fake_fetch)

    sources = await discover_posts_for_sources(
        object(),
        [Source(url="https://worksinprogress.co/archive/")],
        ConversionOptions(start_date="2026-06-01", end_date="2026-06-30"),
    )

    assert [source.url for source in sources] == [
        "https://worksinprogress.co/issue/how-abolishing-the-stakeholder-state-caused-the-industrial-revolution"
    ]


@pytest.mark.asyncio
async def test_discover_posts_sorts_by_date_and_preserves_metadata(monkeypatch):
    listing_html = """
    <a href="/2025/08/20/newer/">Newer</a>
    <a href="/2025/08/10/older/">Older</a>
    <a href="/2025/08/15/middle/">Middle</a>
    """
    pages = {"https://example.com/archive/": listing_html}

    async def fake_fetch(session, url, response_type="text", **kwargs):
        return pages.get(url), url

    monkeypatch.setattr(discovery, "fetch_with_retry", fake_fetch)

    sources = await discover_posts_for_sources(
        object(),
        [Source(url="https://example.com/archive/")],
        ConversionOptions(start_date="2025-08", end_date="2025-08"),
    )

    assert [source.url for source in sources] == [
        "https://example.com/2025/08/10/older",
        "https://example.com/2025/08/15/middle",
        "https://example.com/2025/08/20/newer",
    ]
    assert [source.published_date for source in sources] == ["2025-08-10", "2025-08-15", "2025-08-20"]

    reversed_sources = await discover_posts_for_sources(
        object(),
        [Source(url="https://example.com/archive/")],
        ConversionOptions(start_date="2025-08", end_date="2025-08", date_sort="desc"),
    )

    assert [source.url for source in reversed_sources] == [
        "https://example.com/2025/08/20/newer",
        "https://example.com/2025/08/15/middle",
        "https://example.com/2025/08/10/older",
    ]


@pytest.mark.asyncio
async def test_discover_posts_handles_crazyguy_query_doc_links(monkeypatch):
    listing_html = """
    <div>
        <a href="/doc/?o=3d2&doc_id=26940&v=12X">Tour Journal</a>
        <span>June 18, 2025</span>
    </div>
    """
    pages = {
        "https://www.crazyguyonabike.com/doc/?o=3d2&doc_id=26940&v=12X": listing_html,
        "https://www.crazyguyonabike.com/doc?doc_id=26940&v=12X": "<html><body>Thursday, June 18, 2025</body></html>",
    }

    async def fake_fetch(session, url, response_type="text", **kwargs):
        return pages.get(url), url

    monkeypatch.setattr(discovery, "fetch_with_retry", fake_fetch)

    sources = await discover_posts_for_sources(
        object(),
        [Source(url="https://www.crazyguyonabike.com/doc/?o=3d2&doc_id=26940&v=12X")],
        ConversionOptions(start_date="2025-06", end_date="2025-06"),
    )

    assert [source.url for source in sources] == [
        "https://www.crazyguyonabike.com/doc?doc_id=26940&v=12X"
    ]


@pytest.mark.asyncio
async def test_discover_posts_uses_feed_fallback_when_listing_has_no_candidates(monkeypatch):
    pages = {
        "https://example.com/archive": "<html><body>No links here</body></html>",
        "https://example.com/feed": """
        <rss><channel>
            <item><title>Hit</title><link>https://example.com/2025-08-15-hit</link><pubDate>15 Aug 2025</pubDate></item>
            <item><title>Miss</title><link>https://example.com/2025-09-01-miss</link><pubDate>1 Sep 2025</pubDate></item>
        </channel></rss>
        """,
    }

    async def fake_fetch(session, url, response_type="text", **kwargs):
        return pages.get(url), url

    monkeypatch.setattr(discovery, "fetch_with_retry", fake_fetch)

    sources = await discover_posts_for_sources(
        object(),
        [Source(url="https://example.com/archive")],
        ConversionOptions(start_date="2025-08", end_date="2025-08"),
    )

    assert [source.url for source in sources] == ["https://example.com/2025-08-15-hit"]


@pytest.mark.asyncio
async def test_auto_date_fallback_escalates_to_full_extractor(monkeypatch):
    listing_html = '<a href="/p/undated-post">Undated</a>'
    pages = {
        "https://example.substack.com/archive": listing_html,
        "https://example.substack.com/p/undated-post": "<html><head><title>Undated</title></head><body>Post</body></html>",
    }
    calls = {"extractor": 0}

    async def fake_fetch(session, url, response_type="text", **kwargs):
        return pages.get(url), url

    async def fake_article_content(session, url, raw_html=None, **kwargs):
        calls["extractor"] += 1
        return {"date": "2026-06-18"}

    monkeypatch.setattr(discovery, "fetch_with_retry", fake_fetch)
    monkeypatch.setattr(discovery.ArticleExtractor, "get_article_content", fake_article_content)

    sources = await discover_posts_for_sources(
        object(),
        [Source(url="https://example.substack.com/archive")],
        ConversionOptions(start_date="2026-06-01", end_date="2026-06-30", date_fallback="auto"),
    )

    assert [source.url for source in sources] == ["https://example.substack.com/p/undated-post"]
    assert calls["extractor"] == 1


@pytest.mark.asyncio
async def test_metadata_date_fallback_does_not_escalate_to_full_extractor(monkeypatch):
    async def fake_fetch(session, url, response_type="text", **kwargs):
        if url == "https://example.substack.com/archive":
            return '<a href="/p/undated-post">Undated</a>', url
        return "<html><head><title>Undated</title></head><body>Post</body></html>", url

    async def fake_article_content(session, url, raw_html=None, **kwargs):
        raise AssertionError("metadata fallback should not call full article extraction")

    monkeypatch.setattr(discovery, "fetch_with_retry", fake_fetch)
    monkeypatch.setattr(discovery.ArticleExtractor, "get_article_content", fake_article_content)

    with pytest.raises(discovery.DiscoveryError, match="No posts found"):
        await discover_posts_for_sources(
            object(),
            [Source(url="https://example.substack.com/archive")],
            ConversionOptions(start_date="2026-06-01", end_date="2026-06-30", date_fallback="metadata"),
        )


@pytest.mark.asyncio
async def test_discover_posts_reports_empty_range(monkeypatch):
    async def fake_fetch(session, url, response_type="text", **kwargs):
        return '<a href="/2025/08/15/post/">Post</a>', url

    monkeypatch.setattr(discovery, "fetch_with_retry", fake_fetch)

    with pytest.raises(discovery.DiscoveryError, match="No posts found"):
        await discover_posts_for_sources(
            object(),
            [Source(url="https://example.com/archive/")],
            ConversionOptions(start_date="2025-09-01", end_date="2025-09-30"),
        )

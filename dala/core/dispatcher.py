from urllib.parse import urlparse
from typing import Optional
from bs4 import BeautifulSoup

from ..models import Source, SiteProfile, log
from ..drivers.base import BaseDriver
from ..drivers.forum import ForumDriver
from ..drivers.wordpress import WordPressDriver
from ..drivers.substack import SubstackDriver
from ..drivers.hn import HackerNewsDriver
from ..drivers.reddit import RedditDriver
from ..drivers.youtube import YouTubeDriver
from ..drivers.generic import GenericDriver

class DriverDispatcher:
    @staticmethod
    def get_driver(source: Source, profile: Optional[SiteProfile] = None) -> BaseDriver:
        if profile and profile.driver_alias:
            alias = profile.driver_alias.lower()
            if alias in ("forum", "xenforo"): return ForumDriver()
            if alias == "wordpress": return WordPressDriver()
            if alias == "substack": return SubstackDriver()
            if alias in ("hn", "hackernews"): return HackerNewsDriver()
            if alias == "reddit": return RedditDriver()
            if alias == "youtube": return YouTubeDriver()
            if alias == "generic": return GenericDriver()

        parsed = urlparse(source.url)
        host = parsed.hostname or ""
        if host == "news.ycombinator.com" and parsed.path.rstrip("/") == "/item":
            if source.is_forum:
                log.info(f"Ignoring forum flag for HN source: {source.url}")
            return HackerNewsDriver()
        if host in YouTubeDriver.HOSTS:
            if source.is_forum:
                log.info(f"Ignoring forum flag for YouTube source: {source.url}")
            return YouTubeDriver()

        if source.is_forum:
            return ForumDriver()

        if RedditDriver.is_post_host(host):
            return RedditDriver()
        if host == "substack.com" or host.endswith(".substack.com"):
            return SubstackDriver()
        if host == "wordpress.com" or host.endswith(".wordpress.com"):
            return WordPressDriver()

        driver_type = DriverDispatcher.detect_html_driver(source.html)
        return driver_type() if driver_type else GenericDriver()

    @staticmethod
    def detect_html_driver(raw_html: Optional[str]) -> Optional[type[BaseDriver]]:
        if not raw_html:
            return None
        lower_html = raw_html.lower()
        if not any(marker in lower_html for marker in (
            "substack:post_id", "window._preloads", "thread_view", "message--post", "messagecontent",
            "wordpress", "comment-list", "commentlist",
        )):
            return None
        soup = BeautifulSoup(raw_html, "lxml")
        if soup.select_one('meta[name="substack:post_id"], meta[property="substack:post_id"]'):
            return SubstackDriver
        if "window._preloads" in lower_html and SubstackDriver()._extract_all_metadata(soup, raw_html)[0]:
            return SubstackDriver
        if soup.select_one('[data-template="thread_view"], article.message--post, li.message .messageContent'):
            return ForumDriver
        if soup.select_one('meta[name="generator" i][content^="WordPress" i], ol.comment-list, ul.comment-list, .commentlist'):
            return WordPressDriver
        return None

from urllib.parse import urlparse
from typing import Optional

from . .models import Source, SiteProfile
from . .drivers.base import BaseDriver
from . .drivers.forum import ForumDriver
from . .drivers.wordpress import WordPressDriver
from . .drivers.substack import SubstackDriver
from . .drivers.hn import HackerNewsDriver
from . .drivers.reddit import RedditDriver
from . .drivers.youtube import YouTubeDriver
from . .drivers.generic import GenericDriver

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

        url = source.url
        parsed = urlparse(url)
        
        # 1. Explicit Flags
        if source.is_forum:
            return ForumDriver()
            
        # 2. Domain Matching
        if "news.ycombinator.com" in url:
            return HackerNewsDriver()
        if "reddit.com" in parsed.netloc or parsed.netloc.endswith("redd.it"):
            return RedditDriver()
        if "substack.com" in url or "/p/" in parsed.path:
            return SubstackDriver()
        if "wordpress.com" in url:
            return WordPressDriver()
        if parsed.netloc in ("www.youtube.com", "youtube.com", "youtu.be"):
            return YouTubeDriver()
            
        # 3. Content Sniffing
        if source.html:
            if 'substack:post_id' in source.html:
                 return SubstackDriver()
            if 'data-template="thread_view"' in source.html or 'xenforo' in source.html.lower():
                 return ForumDriver()
            if 'name="generator" content="WordPress"' in source.html or 'class="comment-list"' in source.html:
                 return WordPressDriver()
                 
        return GenericDriver()

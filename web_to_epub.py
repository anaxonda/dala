#!/usr/bin/env python
import asyncio
import sys
import main
from dala.models import *
from dala.core.session import get_session, fetch_with_retry, load_cookie_file
from dala.core.extractor import ArticleExtractor
from dala.core.image_processor import ImageProcessor, ForumImageProcessor, BaseImageProcessor
from dala.core.profiles import ProfileManager
from dala.core.dispatcher import DriverDispatcher
from dala.core.writer import EpubWriter
from dala.drivers.base import BaseDriver
from dala.drivers.generic import GenericDriver
from dala.drivers.substack import SubstackDriver
from dala.drivers.hn import HackerNewsDriver
from dala.drivers.reddit import RedditDriver
from dala.drivers.forum import ForumDriver
from dala.drivers.youtube import YouTubeDriver
from dala.drivers.wordpress import WordPressDriver
from dala.utils.llm import LLMHelper
from dala.utils.formatting import _enrich_comment_tree, format_comment_html, fetch_comments_recursive

# Shim functions from main
process_urls = main.process_urls
create_bundle = main.create_bundle

if __name__ == "__main__":
    asyncio.run(main.async_main())
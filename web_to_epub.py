#!/usr/bin/env python
import asyncio
import sys
import main
from epub_downloader.models import *
from epub_downloader.core.session import get_session, fetch_with_retry, load_cookie_file
from epub_downloader.core.extractor import ArticleExtractor
from epub_downloader.core.image_processor import ImageProcessor, ForumImageProcessor, BaseImageProcessor
from epub_downloader.core.profiles import ProfileManager
from epub_downloader.core.dispatcher import DriverDispatcher
from epub_downloader.core.writer import EpubWriter
from epub_downloader.drivers.base import BaseDriver
from epub_downloader.drivers.generic import GenericDriver
from epub_downloader.drivers.substack import SubstackDriver
from epub_downloader.drivers.hn import HackerNewsDriver
from epub_downloader.drivers.reddit import RedditDriver
from epub_downloader.drivers.forum import ForumDriver
from epub_downloader.drivers.youtube import YouTubeDriver
from epub_downloader.drivers.wordpress import WordPressDriver
from epub_downloader.utils.llm import LLMHelper
from epub_downloader.utils.formatting import _enrich_comment_tree, format_comment_html, fetch_comments_recursive

# Shim functions from main
process_urls = main.process_urls
create_bundle = main.create_bundle

if __name__ == "__main__":
    asyncio.run(main.async_main())
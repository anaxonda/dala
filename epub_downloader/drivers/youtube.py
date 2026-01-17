import html
import asyncio
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
from ebooklib import epub
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from typing import List, Dict, Optional

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter, ImageAsset, IMAGE_DIR_IN_EPUB
)
from . .core.image_processor import ImageProcessor
from . .core.session import fetch_with_retry
from . .utils.llm import LLMHelper

class YouTubeDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        log.info(f"YouTube Driver processing: {url}")

        video_id = self._extract_video_id(url)
        if not video_id:
            log.error("Could not extract video ID")
            return None

        # 1. Fetch Page for Metadata (Title, Author, Thumbnail)
        try:
            html_content, _ = await fetch_with_retry(session, url, 'text')
            soup = BeautifulSoup(html_content, 'html.parser')
            title = soup.find("meta", property="og:title")
            title = title["content"] if title else f"YouTube Video {video_id}"
            
            desc = soup.find("meta", property="og:description")
            description = desc["content"] if desc else ""

            author = "YouTube"
            # Try to find channel name
            channel = soup.find("link", itemprop="name")
            if channel: author = channel.get("content")
            
            # Thumbnail
            thumb_url = None
            og_img = soup.find("meta", property="og:image")
            if og_img: thumb_url = og_img["content"]
            
        except Exception as e:
            log.warning(f"Metadata fetch failed: {e}")
            title = f"YouTube Video {video_id}"
            author = "YouTube"
            description = ""
            thumb_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

        # 2. Fetch Transcript
        loop = asyncio.get_running_loop()
        transcript_list = []
        try:
            # Using instance method fetch() which returns a FetchedTranscript object, then converting to raw dicts
            transcript_list = await loop.run_in_executor(
                None, 
                lambda: YouTubeTranscriptApi().fetch(video_id).to_raw_data()
            )
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            log.warning(f"No transcript available: {e}")
        except Exception as e:
            log.error(f"Transcript fetch error: {e}")

        if not transcript_list:
            log.error("Aborting: No transcript found.")
            return None

        # 3. Process Text
        if options.llm_format:
            full_text = " ".join([t['text'] for t in transcript_list])
            full_text = html.unescape(full_text)
            log.info("Formatting transcript with LLM...")
            final_text = await LLMHelper.format_transcript(full_text, options.llm_model, options.llm_api_key)
            # Wrap in paragraphs if LLM returned plain text block (LLMs usually add newlines)
            if "<p>" not in final_text:
                final_text = "".join(f"<p>{p.strip()}</p>" for p in final_text.split('\n\n') if p.strip())
        else:
            # Basic cleanup using timestamps
            final_text = self._basic_transcript_cleanup(transcript_list)

        # 4. Build Chapter
        assets = []
        cover_image_html = ""
        if not options.no_images and thumb_url:
            headers, data, err = await ImageProcessor.fetch_image_data(session, thumb_url)
            if data:
                mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(thumb_url, headers, data)
                if final_data:
                    fname = f"{IMAGE_DIR_IN_EPUB}/cover{ext}"
                    uid = "cover_img"
                    asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=thumb_url)
                    assets.append(asset)
                    cover_image_html = f'<div class="img-block"><img src="{fname}" alt="Thumbnail" class="epub-image"/></div><hr/>'

        summary_html = ""
        if options.summary:
            log.info("Generating AI summary for YouTube...")
            raw_transcript_text = " ".join([t['text'] for t in transcript_list])
            sum_res = await LLMHelper.generate_summary(raw_transcript_text, options.llm_model, options.llm_api_key)
            if sum_res:
                summary_html = f"<div class='ai-summary'><h3>AI Summary</h3>{sum_res}</div><hr/>"

        content_html = f"{summary_html}{cover_image_html}<div class='transcript-body'>{final_text}</div>"
        
        final_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head>
        <body><h1>{title}</h1><div class="post-meta"><p><strong>Source:</strong> <a href="{url}">{url}</a></p><p><strong>Channel:</strong> {author}</p></div>{content_html}</body></html>"""

        chapter = Chapter(title=title, filename="transcript.xhtml", content_html=final_html, uid="transcript", is_article=True)

        return BookData(
            title=title, 
            author=author, 
            uid=f"urn:youtube:{video_id}", 
            language='en', 
            description=description, 
            source_url=url, 
            chapters=[chapter], 
            images=assets, 
            toc_structure=[epub.Link("transcript.xhtml", title, "transcript")]
        )

    def _basic_transcript_cleanup(self, transcript_list: List[Dict]) -> str:
        """Merges lines and creates paragraphs based on silence gaps (>2s)."""
        paragraphs = []
        current_para = []
        last_end = 0

        for item in transcript_list:
            text = html.unescape(item['text']).replace('\n', ' ').strip()
            if not text: continue
            
            start = item['start']
            
            # If gap > 2 seconds, start new paragraph
            if current_para and (start - last_end > 2.0):
                # Join sentences, try to capitalize first letter
                joined = " ".join(current_para)
                if joined:
                    joined = joined[0].upper() + joined[1:]
                paragraphs.append(f"<p>{joined}</p>")
                current_para = []

            current_para.append(text)
            last_end = start + item['duration']

        if current_para:
            joined = " ".join(current_para)
            if joined:
                joined = joined[0].upper() + joined[1:]
            paragraphs.append(f"<p>{joined}</p>")
        
        return "".join(paragraphs)

    def _extract_video_id(self, url):
        """Extracts video ID from various YouTube URL formats."""
        parsed = urlparse(url)
        if parsed.netloc == "youtu.be":
            return parsed.path[1:]
        if parsed.netloc in ("www.youtube.com", "youtube.com"):
            if "/watch" in parsed.path:
                v_params = parse_qs(parsed.query).get("v")
                return v_params[0] if v_params else None
            if "/embed/" in parsed.path:
                return parsed.path.split("/embed/")[1]
            if "/v/" in parsed.path:
                return parsed.path.split("/v/")[1]
        return None

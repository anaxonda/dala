import html
import asyncio
import json
from itertools import islice
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
from ebooklib import epub
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
try:
    from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_POPULAR, SORT_BY_RECENT
    HAS_COMMENTS = True
except ImportError as e:
    import logging
    logging.getLogger(__name__).warning(f"Failed to import youtube-comment-downloader: {e}")
    HAS_COMMENTS = False
    
from typing import List, Dict, Optional

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter, ImageAsset, IMAGE_DIR_IN_EPUB
)
from . .core.image_processor import ImageProcessor
from . .core.profiles import ProfileManager
from . .core.session import fetch_with_retry
from . .utils.llm import LLMHelper
from . .utils.formatting import _enrich_comment_tree, format_comment_html
from pygments.formatters import HtmlFormatter

class YouTubeDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        url = source.url
        log.info(f"YouTube Driver processing: {url}")

        assets = []
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
            def _fetch_smart_transcript():
                # Adjusted for installed library version which requires instantiation
                transcript_list = YouTubeTranscriptApi().list(video_id)
                
                # Parse user preferences
                target_langs = [l.strip() for l in (options.youtube_lang or "en").split(",") if l.strip()]
                if not target_langs: target_langs = ["en"]
                prefer_auto = options.youtube_prefer_auto

                # Iterate and filter
                candidates = []
                for t in transcript_list:
                    candidates.append(t)
                
                # Sort candidates by preference
                def sort_key(t):
                    lang_score = 999
                    for idx, lang in enumerate(target_langs):
                        if t.language_code.lower().startswith(lang.lower()):
                            lang_score = idx
                            break
                    type_score = 0
                    if prefer_auto:
                        type_score = 0 if t.is_generated else 1
                    else:
                        type_score = 1 if t.is_generated else 0
                    return (lang_score, type_score)

                candidates.sort(key=sort_key)
                best = candidates[0]
                log.info(f"Selected transcript: {best.language_code} ({'Auto' if best.is_generated else 'Manual'})")
                
                is_match = False
                for lang in target_langs:
                    if best.language_code.lower().startswith(lang.lower()):
                        is_match = True
                        break
                
                if not is_match:
                    try:
                        log.info(f"Translating transcript from {best.language_code} to {target_langs[0]}")
                        best = best.translate(target_langs[0])
                    except Exception as trans_err:
                        log.warning(f"Translation failed: {trans_err}")

                return best.fetch().to_raw_data()

            transcript_list = await loop.run_in_executor(None, _fetch_smart_transcript)

        except (TranscriptsDisabled, NoTranscriptFound) as e:
            log.warning(f"No transcript available: {e}")
        except Exception as e:
            log.error(f"Transcript fetch error: {e}")

        if not transcript_list:
            log.error("Aborting: No transcript found.")
            return None
        
        # Calculate duration
        total_duration = 0
        if transcript_list:
            last = transcript_list[-1]
            total_duration = last['start'] + last['duration']
        
        log.info(f"Video duration: {total_duration:.1f}s. Thumbnails: {options.thumbnails}. LLM Format: {options.llm_format}")

        # Fetch Periodic Thumbnails
        thumbnail_map = {}
        if options.thumbnails and not options.no_images and total_duration > 60:
            log.info("Fetching periodic thumbnails...")
            for i, ratio in [(1, 0.25), (2, 0.50), (3, 0.75)]:
                t_url = f"https://img.youtube.com/vi/{video_id}/hq{i}.jpg"
                try:
                    headers, data, err = await ImageProcessor.fetch_image_data(session, t_url)
                    if data:
                        mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(t_url, headers, data)
                        if final_data:
                            fname = f"{IMAGE_DIR_IN_EPUB}/yt_thumb_{i}{ext}"
                            uid = f"yt_thumb_{i}"
                            asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=t_url)
                            assets.append(asset)
                            thumbnail_map[ratio] = fname
                            log.info(f"✓ Fetched thumb {i} ({fname})")
                except Exception as e:
                    log.warning(f"Failed to fetch periodic thumbnail {i}: {e}")

        # 3. Process Text
        if options.llm_format:
            marked_transcript = []
            pending_thumbs = sorted(thumbnail_map.keys()) if thumbnail_map else []
            for item in transcript_list:
                start = item['start']
                if pending_thumbs and total_duration > 0:
                    current_ratio = start / total_duration
                    if current_ratio >= pending_thumbs[0]:
                        ratio = pending_thumbs.pop(0)
                        marked_transcript.append(f"\n\n[[IMAGE_MARKER_{ratio}]]\n\n")
                marked_transcript.append(item['text'])
            full_text = " ".join(marked_transcript)
            full_text = html.unescape(full_text)
            log.info("Formatting transcript with LLM (including markers)...")
            llm_instruction = (
                "IMPORTANT: You will see markers like [[IMAGE_MARKER_0.25]] in the text. "
                "These represent where images should be placed. DO NOT REMOVE THEM. "
                "Ensure they remain on their own line between paragraphs."
            )
            final_text = await LLMHelper.format_transcript(f"{llm_instruction}\n\n{full_text}", options.llm_model, options.llm_api_key)
            if "<p>" not in final_text:
                final_text = "".join(f"<p>{p.strip()}</p>" for p in final_text.split('\n\n') if p.strip())
            if thumbnail_map:
                for ratio, fname in thumbnail_map.items():
                    marker = f"[[IMAGE_MARKER_{ratio}]]"
                    img_html = f'</div><div class="img-block"><img src="{fname}" alt="Timestamp {int(ratio*100)}%" class="epub-image"/></div><div class="transcript-body">'
                    if marker in final_text:
                        final_text = final_text.replace(f"{marker}", f"</p>{img_html}<p>")
                    else:
                        final_text = final_text.replace(marker.strip("[]"), f"</p>{img_html}<p>")
        else:
            final_text = self._basic_transcript_cleanup(transcript_list, thumbnail_map, total_duration)

        # 4. Build Chapter
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
            raw_transcript_text = " ".join([t['text'] for t in transcript_list])
            sum_res = await LLMHelper.generate_summary(raw_transcript_text, options.llm_model, options.llm_api_key)
            if sum_res:
                summary_html = f"<div class='ai-summary'><h3>AI Summary</h3>{sum_res}</div><hr/>"

        content_html = f"{summary_html}{cover_image_html}<div class='transcript-body'>{final_text}</div>"
        final_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head>
        <body><h1>{title}</h1><div class="post-meta"><p><strong>Source:</strong> <a href="{url}">{url}</a></p><p><strong>Channel:</strong> {author}</p></div>{content_html}</body></html>"""

        chapters = [Chapter(title=title, filename="transcript.xhtml", content_html=final_html, uid="transcript", is_article=True)]
        toc_structure = [epub.Link("transcript.xhtml", title, "transcript")]

        # 5. Fetch Comments
        if HAS_COMMENTS and not options.no_comments:
            log.info("Fetching YouTube comments via youtube-comment-downloader...")
            def _fetch_yt_comments():
                downloader = YoutubeCommentDownloader()
                sort_val = SORT_BY_RECENT if options.youtube_comment_sort == "new" else SORT_BY_POPULAR
                try:
                    generator = downloader.get_comments_from_url(url, sort_by=sort_val)
                    wanted_roots, roots_found, limit = set(), 0, options.youtube_max_comments
                    scan_limit, kept_comments = limit * 20, []
                    
                    for i, c in enumerate(generator):
                        if i >= scan_limit: break
                        
                        cid = c.get('cid')
                        is_reply = c.get('reply', False)
                        
                        if not is_reply:
                            if roots_found < limit:
                                wanted_roots.add(cid)
                                roots_found += 1
                                kept_comments.append(c)
                        else:
                            parent_id = cid.split('.')[0] if '.' in cid else None
                            if parent_id in wanted_roots:
                                kept_comments.append(c)
                    
                    log.info(f"Scanned {i+1} items. Kept {len(kept_comments)} comments ({roots_found} roots).")
                    
                    comment_map, roots = {}, []
                    for c in kept_comments:
                        cid = c.get('cid')
                        is_reply = c.get('reply', False)
                        parent_id = cid.split('.')[0] if (is_reply and '.' in cid) else None

                        mapped = {
                            'id': cid,
                            'by': f"{c.get('author', 'Unknown')} ({c.get('votes', '0')} likes, {c.get('time', '')})",
                            'text': c.get('text', ''),
                            'parent_id': parent_id,
                            'children_data': [],
                            'time': c.get('time_parsed', 0) 
                        }
                        comment_map[cid] = mapped
                    
                    for cid, mapped in comment_map.items():
                        pid = mapped['parent_id']
                        if pid and pid in comment_map:
                            comment_map[pid]['children_data'].append(mapped)
                        elif not pid:
                            roots.append(mapped)
                            
                    return roots

                except Exception as e:
                    log.error(f"Comment fetch error: {e}"); return []

            try:
                enriched_roots = await loop.run_in_executor(None, _fetch_yt_comments)
                if enriched_roots:
                    _enrich_comment_tree(enriched_roots)
                    fmt = HtmlFormatter(style='default', cssclass='codehilite', noclasses=False)
                    comment_chunks = []
                    for comment in enriched_roots:
                        comment_chunks.append(f"<div class='thread-container'>")
                        comment_chunks.append(format_comment_html(comment, fmt))
                        comment_chunks.append("</div>")
                    
                    full_com_html = f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>YouTube Comments</title><link rel="stylesheet" href="style/default.css"/></head><body>
                    <h1>YouTube Comments</h1>
                    {"".join(comment_chunks)}
                    </body></html>"""
                    
                    com_chap = Chapter(title="YouTube Comments", filename="comments.xhtml", content_html=full_com_html, uid="comments", is_comments=True)
                    chapters.append(com_chap)
                    if com_chap:
                        toc_structure = [(epub.Link("transcript.xhtml", title, "transcript"), [epub.Link("comments.xhtml", "YouTube Comments", "comments")])]
            except Exception as e: log.error(f"Comment processing failed: {e}")

        return BookData(title=title, author=author, uid=f"urn:youtube:{video_id}", language='en', description=description, source_url=url, chapters=chapters, images=assets, toc_structure=toc_structure)

    def _basic_transcript_cleanup(self, transcript_list: List[Dict], thumbnails: Dict[float, str] = None, total_duration: float = 0) -> str:
        paragraphs, current_para, last_end = [], [], 0
        pending_thumbs = sorted(thumbnails.keys()) if thumbnails else []
        for item in transcript_list:
            text = html.unescape(item['text']).replace('\n', ' ').strip()
            if not text: continue
            start = item['start']
            if pending_thumbs and total_duration > 0:
                if (start / total_duration) >= pending_thumbs[0]:
                    target = pending_thumbs.pop(0)
                    if current_para:
                        joined = " ".join(current_para)
                        paragraphs.append(f"<p>{joined[0].upper() + joined[1:]}</p>")
                        current_para = []
                    paragraphs.append(f'<div class="img-block"><img src="{thumbnails[target]}" alt="Timestamp {int(target*100)}%" class="epub-image"/></div>')
            if current_para and (start - last_end > 2.0):
                joined = " ".join(current_para)
                paragraphs.append(f"<p>{joined[0].upper() + joined[1:]}</p>")
                current_para = []
            current_para.append(text)
            last_end = start + item['duration']
        if current_para:
            joined = " ".join(current_para)
            paragraphs.append(f"<p>{joined[0].upper() + joined[1:]}</p>")
        return "".join(paragraphs)

    def _extract_video_id(self, url):
        parsed = urlparse(url)
        if parsed.netloc == "youtu.be": return parsed.path[1:]
        if parsed.netloc in ("www.youtube.com", "youtube.com"):
            if "/watch" in parsed.path: return parse_qs(parsed.query).get("v", [None])[0]
            if "/embed/" in parsed.path: return parsed.path.split("/embed/")[1]
            if "/v/" in parsed.path: return parsed.path.split("/v/")[1]
        return None
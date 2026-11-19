import html
import asyncio
import json
import re
from itertools import islice
from urllib.parse import urlparse, parse_qs, urlencode
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
from ..models import (
    log, BookData, ConversionContext, Source, Chapter, ImageAsset, IMAGE_DIR_IN_EPUB
)
from ..core.extractor import ArticleExtractor
from ..core.image_processor import ImageProcessor
from ..core.profiles import ProfileManager
from ..core.session import fetch_with_retry
from ..core.translation import comparable_language, normalize_translation_display
from ..utils.llm import LLMHelper
from ..utils.formatting import _enrich_comment_tree, format_comment_html
from pygments.formatters import HtmlFormatter

class YouTubeDriver(BaseDriver):
    HOSTS = {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
        "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be",
    }

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
        watch_url = f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"

        # 1. Fetch Page for Metadata (Title, Author, Thumbnail)
        try:
            html_content, _ = await fetch_with_retry(session, watch_url, 'text')
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
        is_generated = False
        transcript_language = None
        transcript_translation_satisfied = False
        try:
            def _fetch_smart_transcript():
                # Adjusted for installed library version which requires instantiation
                available_transcripts = YouTubeTranscriptApi().list(video_id)
                
                # Parse user preferences
                replace_translation = (
                    getattr(options, "translation_enabled", False)
                    and getattr(options, "translation_target_lang", None)
                    and normalize_translation_display(getattr(options, "translation_display", "underneath")) == "replace"
                )
                if replace_translation:
                    target_langs = [options.translation_target_lang]
                else:
                    target_langs = [l.strip() for l in (options.youtube_lang or "en").split(",") if l.strip()]
                if not target_langs: target_langs = ["en"]
                prefer_auto = options.youtube_prefer_auto

                # Iterate and filter
                candidates = []
                for t in available_transcripts:
                    candidates.append(t)
                if not candidates:
                    raise NoTranscriptFound(video_id, target_langs, available_transcripts)
                
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
                    if comparable_language(best.language_code) == comparable_language(lang):
                        is_match = True
                        break
                
                used_youtube_translation = False
                if not is_match:
                    try:
                        log.info(f"Translating transcript from {best.language_code} to {target_langs[0]}")
                        best = best.translate(target_langs[0])
                        used_youtube_translation = True
                    except Exception as trans_err:
                        log.warning(f"Translation failed: {trans_err}")

                final_language = target_langs[0] if used_youtube_translation else getattr(best, "language_code", None)
                final_is_match = any(
                    comparable_language(final_language) == comparable_language(lang)
                    for lang in target_langs
                )
                satisfied = bool(replace_translation and (is_match or used_youtube_translation) and final_is_match)

                return best.fetch().to_raw_data(), best.is_generated, final_language, satisfied

            transcript_list, is_generated, transcript_language, transcript_translation_satisfied = await loop.run_in_executor(None, _fetch_smart_transcript)

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
        max_dim, quality, color_mode, output_pref = ImageProcessor.image_optimize_params(options)
        if options.thumbnails and not options.no_images and total_duration > 60:
            log.info("Fetching periodic thumbnails...")
            for i, ratio in [(1, 0.25), (2, 0.50), (3, 0.75)]:
                t_url = f"https://img.youtube.com/vi/{video_id}/hq{i}.jpg"
                try:
                    headers, data, err = await ImageProcessor.fetch_image_data(session, t_url)
                    if data:
                        mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(t_url, headers, data, max_dimension=max_dim, jpeg_quality=quality, color_mode=color_mode, output_preference=output_pref)
                        if final_data:
                            fname = f"{IMAGE_DIR_IN_EPUB}/yt_thumb_{i}{ext}"
                            uid = f"yt_thumb_{i}"
                            asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=t_url)
                            assets.append(asset)
                            thumbnail_map[ratio] = fname
                            log.info(f"✓ Fetched thumb {i} ({fname})")
                except Exception as e:
                    log.warning(f"Failed to fetch periodic thumbnail {i}: {e}")

        timestamp_links = self._timestamp_links(transcript_list, watch_url) if options.youtube_timestamps else {}

        # 3. Process Text
        final_text = None
        if options.llm_format:
            marked_transcript = []
            pending_thumbs = sorted(thumbnail_map.keys()) if thumbnail_map else []
            for index, item in enumerate(transcript_list):
                start = item['start']
                if pending_thumbs and total_duration > 0:
                    current_ratio = start / total_duration
                    if current_ratio >= pending_thumbs[0]:
                        ratio = pending_thumbs.pop(0)
                        marked_transcript.append(f"\n\n[[IMAGE_MARKER_{ratio}]]\n\n")
                if index in timestamp_links:
                    marked_transcript.append(f"\n\n[[TIMESTAMP_{index}]]\n\n")
                marked_transcript.append(item['text'])
            full_text = " ".join(marked_transcript)
            full_text = html.unescape(full_text)
            log.info("Formatting transcript with LLM (including markers)...")
            llm_instruction = (
                "IMPORTANT: You will see markers like [[IMAGE_MARKER_0.25]] in the text. "
                "These represent where images should be placed. DO NOT REMOVE THEM. "
                "Ensure they remain on their own line between paragraphs."
            )
            if timestamp_links:
                llm_instruction += " Preserve the TIMESTAMP markers exactly and in order on their own lines."
            llm_input = f"{llm_instruction}\n\n{full_text}"
            final_text = await LLMHelper.format_transcript(llm_input, options.llm_model, options.llm_api_key, options.llm_provider)
            unformatted = final_text == llm_input
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
            if timestamp_links:
                if final_text and not unformatted:
                    final_text = self._restore_timestamp_links(final_text, timestamp_links)
                else:
                    final_text = None
                if final_text is None:
                    log.warning("Formatted transcript lost timestamp markers. Using timed captions.")
        if final_text is None:
            final_text = self._basic_transcript_cleanup(
                transcript_list, thumbnail_map, total_duration, is_generated, timestamp_links,
            )

        # 4. Build Chapter
        cover_image_html = ""
        if not options.no_images and thumb_url:
            headers, data, err = await ImageProcessor.fetch_image_data(session, thumb_url)
            if data:
                mime, ext, final_data, val_err = ImageProcessor.optimize_and_get_details(thumb_url, headers, data, max_dimension=max_dim, jpeg_quality=quality, color_mode=color_mode, output_preference=output_pref)
                if final_data:
                    fname = f"{IMAGE_DIR_IN_EPUB}/cover{ext}"
                    uid = "cover_img"
                    asset = ImageAsset(uid=uid, filename=fname, media_type=mime, content=final_data, original_url=thumb_url)
                    assets.append(asset)
                    cover_image_html = f'<div class="img-block"><img src="{fname}" alt="Thumbnail" class="epub-image"/></div><hr/>'

        summary_html = ""
        if options.summary:
            raw_transcript_text = " ".join([t['text'] for t in transcript_list])
            sum_res = await LLMHelper.generate_summary(raw_transcript_text, options.llm_model, options.llm_api_key, options.llm_provider)
            if sum_res:
                summary_html = f"<div class='ai-summary'><h3>AI Summary</h3>{sum_res}</div><hr/>"

        transcript_body_class = "transcript-body"
        transcript_body_lang = transcript_language or ""
        if transcript_translation_satisfied:
            transcript_body_class += " dala-translation-skip"
        lang_attr = f' lang="{html.escape(transcript_body_lang)}"' if transcript_body_lang else ""
        content_html = f"{summary_html}{cover_image_html}<div class='{transcript_body_class}'{lang_attr}>{final_text}</div>"
        meta_html = ArticleExtractor.build_meta_block(url, {"author": author, "sitename": "YouTube"})
        final_html = ArticleExtractor.build_article_html(title, content_html, meta_html=meta_html)

        chapters = [Chapter(title=title, filename="transcript.xhtml", content_html=final_html, uid="transcript", is_article=True)]
        toc_structure = [epub.Link("transcript.xhtml", title, "transcript")]

        # 5. Fetch Comments
        if HAS_COMMENTS and not options.no_comments:
            log.info("Fetching YouTube comments via youtube-comment-downloader...")
            def _fetch_yt_comments():
                downloader = YoutubeCommentDownloader()
                sort_val = SORT_BY_RECENT if options.youtube_comment_sort == "new" else SORT_BY_POPULAR
                try:
                    generator = downloader.get_comments_from_url(watch_url, sort_by=sort_val)
                    wanted_roots, roots_found, limit = set(), 0, options.youtube_max_comments
                    scan_limit, kept_comments = limit * 20, []
                    scanned = 0
                    
                    for i, c in enumerate(generator):
                        if i >= scan_limit: break
                        scanned += 1
                        
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
                    
                    log.info(f"Scanned {scanned} items. Kept {len(kept_comments)} comments ({roots_found} roots).")
                    
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
                    
                    full_com_html = ArticleExtractor.build_article_html("YouTube Comments", "".join(comment_chunks))
                    
                    com_chap = Chapter(title="YouTube Comments", filename="comments.xhtml", content_html=full_com_html, uid="comments", is_comments=True)
                    chapters.append(com_chap)
                    if com_chap:
                        toc_structure = [(epub.Link("transcript.xhtml", title, "transcript"), [epub.Link("comments.xhtml", "YouTube Comments", "comments")])]
            except Exception as e: log.error(f"Comment processing failed: {e}")

        return BookData(title=title, author=author, uid=f"urn:youtube:{video_id}", language='en', description=description, source_url=url, chapters=chapters, images=assets, toc_structure=toc_structure)

    @staticmethod
    def _timestamp_links(transcript, watch_url: str) -> dict[int, str]:
        links = {}
        previous_minute = -1
        for index, item in enumerate(transcript):
            seconds = int(item["start"])
            minute = seconds // 60
            if minute <= previous_minute or not item["text"].strip():
                continue
            previous_minute = minute
            hours, minutes = divmod(minute, 60)
            label = f"{minutes}:{seconds % 60:02d}"
            if hours:
                label = f"{hours}:{minutes:02d}:{seconds % 60:02d}"
            href = html.escape(f"{watch_url}&t={seconds}s", quote=True)
            links[index] = f'<a class="youtube-timestamp" href="{href}">{label}</a>'
        return links

    @staticmethod
    def _restore_timestamp_links(formatted: str, links: dict[int, str]) -> Optional[str]:
        pattern = re.compile(r"(\[\[TIMESTAMP_(\d+)\]\])")
        soup = BeautifulSoup(formatted, "html.parser")
        text_nodes = soup.find_all(string=pattern)
        markers = [int(match[2]) for node in text_nodes for match in pattern.finditer(node)]
        if markers != list(links) or any(node.find_parent(["a", "script", "style"]) for node in text_nodes):
            return None
        for node in text_nodes:
            parts = pattern.split(node)
            node.insert_before(parts[0])
            for _, index, tail in zip(parts[1::3], parts[2::3], parts[3::3]):
                node.insert_before(BeautifulSoup(links[int(index)], "html.parser").a)
                node.insert_before(tail)
            node.extract()
        return str(soup)

    def _basic_transcript_cleanup(
        self, transcript_list: List[Dict], thumbnails: Dict[float, str] = None,
        total_duration: float = 0, is_auto: bool = False, timestamp_links: Optional[dict[int, str]] = None,
    ) -> str:
        paragraphs, current_para, last_end = [], [], 0
        current_len = 0
        paragraph_timestamp = ""
        pending_thumbs = sorted(thumbnails.keys()) if thumbnails else []
        
        SENTENCE_STARTERS = {"i", "we", "the", "it", "so", "but", "and", "they", "this", "there", "you", "my", "our"}

        def flush_para():
            nonlocal current_para, current_len, paragraph_timestamp
            if current_para:
                text = " ".join(current_para)
                # Clean up whitespace
                text = re.sub(r'\s+', ' ', text).strip()
                if text:
                    # Ensure it starts with uppercase if it's a normal word
                    if text[0].islower():
                        text = text[0].upper() + text[1:]
                    
                    # Add a trailing period if it's auto-generated and lacks punctuation
                    if is_auto and text[-1] not in {'.', '!', '?', ':'}:
                        text += "."
                    
                    paragraphs.append(f"<p>{paragraph_timestamp}{text}</p>")
                current_para = []
                current_len = 0
                paragraph_timestamp = ""

        for index, item in enumerate(transcript_list):
            text = html.unescape(item['text']).replace('\n', ' ').strip()
            if not text: continue
            
            start = item['start']
            duration = item['duration']
            gap = start - last_end
            
            # 1. Check for thumbnails
            if pending_thumbs and total_duration > 0:
                if (start / total_duration) >= pending_thumbs[0]:
                    target = pending_thumbs.pop(0)
                    flush_para()
                    paragraphs.append(f'<div class="img-block"><img src="{thumbnails[target]}" alt="Timestamp {int(target*100)}%" class="epub-image"/></div>')

            # 2. Heuristic for paragraph split
            should_split = False
            if current_para:
                first_word = text.split()[0].lower().strip(">,.!") if text.split() else ""
                
                # Split on speaker change
                if ">>" in text:
                    should_split = True
                # Split on brackets like [Music] or [Laughter]
                elif text.startswith("[") and text.endswith("]"):
                    should_split = True
                # Split on significant pause
                elif gap > 1.5:
                    should_split = True
                # Auto-generated transcript heuristic: split more aggressively on pauses
                elif is_auto:
                    if gap > 0.3 and (first_word in SENTENCE_STARTERS or current_len > 300):
                        should_split = True
                    elif current_len > 600:
                        should_split = True
                # Manual transcript heuristic
                else:
                    if gap > 0.5 and current_len > 500:
                        should_split = True
                    elif current_len > 1000:
                        should_split = True

            if should_split:
                flush_para()
            if timestamp_links and index in timestamp_links:
                flush_para()
                paragraph_timestamp = f"{timestamp_links[index]} "

            # Handle mid-item speaker changes
            if ">>" in text and not text.startswith(">>"):
                parts = text.split(">>")
                current_para.append(parts[0].strip())
                flush_para()
                for p in parts[1:]:
                    current_para.append(">> " + p.strip())
                    if p != parts[-1]:
                        flush_para()
            else:
                current_para.append(text)
                current_len += len(text) + 1
            
            last_end = start + duration

        flush_para()
        return "".join(paragraphs)

    def _extract_video_id(self, url):
        parsed = urlparse(url)
        if parsed.hostname not in self.HOSTS:
            return None
        route, _, remainder = parsed.path.removeprefix("/").partition("/")
        if parsed.hostname == "youtu.be":
            return route or None
        if route == "watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        if route in {"embed", "v", "live", "shorts"}:
            return remainder.partition("/")[0] or None
        return None

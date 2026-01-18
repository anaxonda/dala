import html
import re
import hashlib
import mimetypes
import os
from urllib.parse import urlparse
from itertools import islice
from bs4 import BeautifulSoup
from ebooklib import epub
from typing import List, Dict, Optional, Any, Tuple

from .base import BaseDriver
from . .models import (
    log, BookData, ConversionContext, Source, Chapter, ImageAsset, IMAGE_DIR_IN_EPUB, sanitize_filename
)
from . .core.image_processor import ForumImageProcessor
from . .core.session import fetch_with_retry
from . .utils.llm import LLMHelper

class ForumDriver(BaseDriver):
    async def prepare_book_data(self, context: ConversionContext, source: Source) -> Optional[BookData]:
        session = context.session
        options = context.options
        base_url = self._normalize_url(source.url)
        log.info(f"Forum Driver processing: {base_url}")

        assets: List[ImageAsset] = []
        page_blocks: List[Tuple[int, List[Dict[str, Any]]]] = []
        title = None

        target_pages = options.page_spec or []
        pages_sequence = list(target_pages) if target_pages else []
        max_pages = options.max_pages
        max_posts = options.max_posts

        page = 1
        seen_pages = set()
        seen_urls = set()

        # Seed preloaded assets once so they are available for every page
        if source.assets:
            seeded = 0
            for a in source.assets:
                raw = a.get("content")
                if isinstance(raw, str):
                    import base64
                    try:
                        raw = base64.b64decode(raw)
                    except Exception:
                        raw = None
                if not raw:
                    continue
                url_like = a.get("canonical_url") or a.get("original_url") or a.get("viewer_url") or ""
                if not isinstance(url_like, str) or not url_like.startswith("http"):
                    continue
                parsed = urlparse(url_like)
                path_val = parsed.path if parsed else ""
                mime = a.get("media_type") or a.get("content_type") or "image/jpeg"
                fname_base = sanitize_filename(os.path.splitext(os.path.basename(path_val))[0])
                if len(fname_base) < 3:
                    fname_base = f"img_{abs(hash(a.get('original_url') or a.get('viewer_url') or seeded))}"
                ext = os.path.splitext(path_val)[1]
                if not ext or not ext.startswith("."):
                    ext = mimetypes.guess_extension(mime) or ".img"
                fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}{ext}"
                count = 0
                while any(existing.filename == fname for existing in assets):
                    count += 1
                    fname = f"{IMAGE_DIR_IN_EPUB}/{fname_base}_{count}{ext}"
                uid = f"img_{abs(hash(fname))}"
                alt_urls = []
                viewer = a.get("viewer_url")
                canonical = a.get("canonical_url")
                orig = a.get("original_url")
                for u in (orig, viewer, canonical):
                    if u and isinstance(u, str):
                        alt_urls.append(u)
                        if "?" in u:
                            alt_urls.append(u.split("?", 1)[0])
                assets.append(ImageAsset(uid=uid, filename=fname, media_type=mime, content=raw, original_url=url_like, alt_urls=alt_urls))
                seeded += 1
            log.info(f"Seeded {seeded} preloaded assets into EPUB.")

        while True:
            if target_pages:
                if not pages_sequence:
                    break
                page = pages_sequence.pop(0)
            else:
                if max_pages and page > max_pages:
                    break

            if page in seen_pages:
                page += 1
                continue

            page_url = self._build_page_url(base_url, page)
            html_content, final_url = await fetch_with_retry(session, page_url, 'text')
            if not html_content:
                if target_pages:
                    log.warning(f"Page {page} missing.")
                    continue
                else:
                    break
            if final_url in seen_urls:
                log.info(f"Final URL for page {page} already seen. Stopping to avoid loop: {final_url}")
                break
            seen_urls.add(final_url)

            soup = BeautifulSoup(html_content, 'lxml')
            if not title:
                title = self._extract_title(soup, base_url)

            if source.assets and page == 1:
                log.info(f"Preloaded assets received: {len(source.assets)}")
                for a in source.assets[:3]:
                    log.info(f"Asset sample original={a.get('original_url')} viewer={a.get('viewer_url')} canonical={a.get('canonical_url')} type={a.get('content_type')}")

            if not options.no_images:
                base_for_imgs = final_url or base_url
                await ForumImageProcessor.process_images(session, soup, base_for_imgs, assets, preloaded_assets=source.assets)

            posts = self._extract_posts(soup)
            if posts:
                if max_posts:
                    remaining = max_posts - sum(len(b[1]) for b in page_blocks)
                    if remaining <= 0:
                        break
                    posts = list(islice(posts, remaining))
                page_blocks.append((page, posts))
            if max_posts and sum(len(b[1]) for b in page_blocks) >= max_posts:
                break

            seen_pages.add(page)
            if target_pages:
                continue

            has_next = self._has_next_page(soup, page, final_url)
            if not has_next:
                break
            page += 1

        if not page_blocks:
            log.error("Forum extraction produced no posts.")
            return None

        summary_html = None
        if options.summary:
            log.info("Generating AI summary for Forum Thread...")
            sample_text = []
            count = 0
            for _, posts in page_blocks:
                for p in posts:
                    clean = BeautifulSoup(p.get("html", ""), "html.parser").get_text(separator=" ", strip=True)
                    sample_text.append(f"Post by {p.get('author')}: {clean}")
                    count += 1
                    if count >= 5: break
                if count >= 5: break
            
            if sample_text:
                summary_html = await LLMHelper.generate_summary("\n\n".join(sample_text), options.llm_model, options.llm_api_key)

        chapter_html = self._render_thread_html(title or "Forum Thread", source.url, page_blocks, summary_html=summary_html)
        assets, chapter_html = self._dedupe_assets(assets, chapter_html)
        chapter = Chapter(
            title=title or "Forum Thread",
            filename="thread.xhtml",
            content_html=chapter_html,
            uid="forum_thread",
            is_article=True
        )

        toc_links = [epub.Link("thread.xhtml", "Thread", "forum_thread")]
        return BookData(
            title=title or "Forum Thread",
            author="Forum",
            uid=f"urn:forum:{abs(hash(base_url))}",
            language="en",
            description=f"Forum thread from {base_url}",
            source_url=source.url,
            chapters=[chapter],
            images=assets,
            toc_structure=toc_links
        )

    def _normalize_url(self, url: str) -> str:
        cleaned = url.rstrip('/')
        if "page-" in cleaned:
            cleaned = re.sub(r'/page-\d+', '', cleaned)
        cleaned = re.sub(r'([?&])page=\d+', r'\1', cleaned)
        cleaned = cleaned.rstrip('?&')
        return cleaned

    def _build_page_url(self, base_url: str, page: int) -> str:
        if page <= 1:
            return base_url
        parsed = urlparse(base_url)
        path = parsed.path or ""
        query = parsed.query or ""
        if query and query.startswith("threads/"):
            q = query
            if re.search(r'page-\d+', q):
                q = re.sub(r'page-\d+', f"page-{page}", q)
            else:
                q = q.rstrip('/') + f"/page-{page}"
            return parsed._replace(query=q).geturl()
        if re.search(r'page-\d+', path):
            new_path = re.sub(r'page-\d+', f"page-{page}", path)
        elif path.endswith('/'):
            new_path = f"{path}page-{page}"
        else:
            new_path = f"{path}/page-{page}"
        rebuilt = parsed._replace(path=new_path)
        return rebuilt.geturl()

    def _extract_title(self, soup, url):
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            return og.get("content")
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return url

    def _extract_posts(self, soup):
        posts = []
        seen_ids = set()
        containers = soup.select("article.message.message--post, li.message, article.message")
        if not containers:
            containers = soup.find_all(lambda tag: tag.get("class") and any("message" in c for c in tag.get("class")))
        for c in containers:
            pid_raw = c.get("id") or c.get("data-content") or ""
            if pid_raw in ("messageList",):
                continue
            anchor_id = sanitize_filename(pid_raw) if pid_raw else f"post_{len(posts)+1}"
            num_id = None
            try:
                m = re.search(r'(\d+)', pid_raw)
                if m:
                    num_id = m.group(1)
            except Exception:
                pass
            key = anchor_id or num_id
            if key and key in seen_ids:
                continue
            seen_ids.add(key)

            author = None
            author_tag = c.find(lambda t: t.get("class") and any("username" in x or "author" in x for x in t.get("class")))
            if not author and c.has_attr("data-author"):
                author = c.get("data-author")
            if author_tag:
                author = author_tag.get_text(strip=True)
            if author and author.endswith(","):
                author = author.rstrip(",").strip()

            content_tag = c.select_one(".message-body") or c.select_one(".message-content") or c.select_one(".messageContent")
            if not content_tag:
                content_tag = c.find(lambda t: t.get("class") and any("messageContent" in x or "bbWrapper" in x or "content" == x for x in t.get("class")))
            if not content_tag:
                continue
            time_val = None
            time_tag = c.find("time")
            if time_tag:
                time_val = time_tag.get("datetime") or time_tag.get("title") or time_tag.get_text(strip=True)
            posts.append({
                "id": pid_raw or anchor_id,
                "anchor_id": anchor_id,
                "numeric_id": num_id,
                "author": author or "Anonymous",
                "html": str(content_tag),
                "time": time_val
            })
        return posts

    def _extract_page_number(self, href: str) -> Optional[int]:
        if not href: return None
        m = re.search(r'page[-=](\d+)', href, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        return None

    def _has_next_page(self, soup, current_page: int, current_url: str) -> bool:
        if current_page > 500:
            return False
        link = soup.find("link", rel="next")
        if link and link.get("href"):
            maybe = self._extract_page_number(link.get("href"))
            if maybe and maybe <= current_page:
                return False
            return True
        numeric_pages = []
        for el in soup.select("li.pageNav-page, a.pageNav-page"):
            try:
                val = int(el.get_text(strip=True))
                numeric_pages.append(val)
            except Exception:
                continue
        max_num = max(numeric_pages) if numeric_pages else None
        if max_num and current_page < max_num:
            return True
        jump_next = soup.select_one("a.pageNav-jump--next, a.pageNavSimple-el--next, a[rel='next']")
        if jump_next and jump_next.get("href"):
            maybe = self._extract_page_number(jump_next.get("href"))
            if maybe and maybe <= current_page:
                return False
            return True
        anchors = soup.find_all("a")
        for a in anchors:
            txt = a.get_text(strip=True).lower()
            if txt in ("next", "next >", "next>"):
                return True
            if txt == str(current_page + 1):
                return True
        return False

    def _render_thread_html(self, title, url, page_blocks: List[Tuple[int, List[Dict[str, Any]]]], summary_html: Optional[str] = None):
        anchor_map: Dict[str, str] = {}
        for _, posts in page_blocks:
            for p in posts:
                anchor = p.get("anchor_id") or sanitize_filename(p.get("id") or "")
                pid_raw = p.get("id")
                num = p.get("numeric_id")
                if pid_raw:
                    anchor_map[pid_raw] = anchor
                if num:
                    anchor_map[num] = anchor
                    anchor_map[f"post-{num}"] = anchor

        def rewrite_quote_links(html_snippet: str) -> str:
            if not html_snippet:
                return html_snippet
            try:
                soup = BeautifulSoup(html_snippet, 'html.parser')
                links = soup.find_all("a")
                for a in links:
                    cls = " ".join(a.get("class", [])) if a.get("class") else ""
                    target = None
                    if "bbCodeBlock-sourceJump" in cls or "AttributionLink" in cls:
                        sel = a.get("data-content-selector")
                        if sel and isinstance(sel, str):
                            sel = sel.lstrip("#")
                            if sel:
                                target = sel
                        if not target:
                            href = a.get("href")
                            if href:
                                m = re.search(r'id=(\d+)', href)
                                if m:
                                    target = m.group(1)
                                else:
                                    m2 = re.search(r'post-(\d+)', href)
                                    if m2:
                                        target = m2.group(1)
                    if target and target in anchor_map:
                        anchor = anchor_map[target]
                        a['href'] = f"#p_{anchor}"
                        if a.has_attr("data-xf-click"):
                            del a["data-xf-click"]
                        if a.has_attr("data-content-selector"):
                            del a["data-content-selector"]
                return str(soup)
            except Exception as e:
                return html_snippet

        chunks = [f"""<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head><title>{title}</title><link rel="stylesheet" href="style/default.css"/></head><body>"""]
        chunks.append(f"<h1>{title}</h1><div class='post-meta'><p><strong>Source:</strong> <a href=\"{url}\">{url}</a></p></div>")
        
        if summary_html:
            chunks.append(f"<div class='ai-summary'><h3>AI Summary</h3>{summary_html}</div><hr/>")

        post_counter = 1
        for page_no, posts in page_blocks:
            chunks.append(f"<div class='page-label' id='page_{page_no}'>Page {page_no}</div>")
            for post in posts:
                anchor_id = post.get("anchor_id") or sanitize_filename(post.get("id") or f"post_{post_counter}")
                pid = anchor_id
                author = html.escape(post.get("author") or "Anonymous")
                when = html.escape(post.get("time") or "")
                chunks.append(f"<div class='forum-post' id='p_{pid}'>")
                chunks.append(f"<div class='forum-post-header'><span class='forum-author'>{author}</span>")
                if when: chunks.append(f"<span class='forum-time'>{when}</span>")
                chunks.append("</div>")
                body_html = rewrite_quote_links(post.get('html',''))
                chunks.append(f"<div class='forum-post-body'>{body_html}</div>")
                chunks.append("</div>")
                post_counter += 1
        chunks.append("</body></html>")
        return "".join(chunks)

    def _dedupe_assets(self, assets: List[ImageAsset], html: str) -> Tuple[List[ImageAsset], str]:
        seen: Dict[str, ImageAsset] = {}
        keep: List[ImageAsset] = []
        replace_map: Dict[str, str] = {}

        for a in assets:
            if not a.content:
                keep.append(a)
                continue
            try:
                h = hashlib.sha1(a.content).hexdigest()
            except Exception:
                keep.append(a)
                continue
            if h in seen:
                replace_map[a.filename] = seen[h].filename
            else:
                seen[h] = a
                keep.append(a)

        if replace_map and html:
            for old, new in replace_map.items():
                html = html.replace(old, new)

        return keep, html

# /// script
# requires-python = ">=3.8"
# dependencies = [
#   "fastapi",
#   "uvicorn",
#   "aiohttp[speedups]",
#   "EbookLib",
#   "beautifulsoup4",
#   "trafilatura",
#   "Pillow",
#   "lxml",
#   "pygments",
#   "tqdm",
#   "requests",
#   "PyYAML",
#   "youtube-transcript-api>=0.6.0",
#   "python-dotenv>=1.0.0",
# ]
# ///

import uvicorn
import os
import tempfile
import shutil
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

import main as core_main
from dala.models import ConversionOptions, Source, log
from dala.core.session import get_session
from dala.core.writer import EpubWriter
from dala.models import sanitize_filename

app = FastAPI()

@app.middleware("http")
async def log_requests(request, call_next):
    print(f"🔹 Incoming request: {request.method} {request.url}")
    response = await call_next(request)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

class SourceItem(BaseModel):
    url: str
    html: Optional[str] = None
    cookies: Optional[dict] = None
    assets: Optional[list] = None
    is_forum: Optional[bool] = False

class ConversionRequest(BaseModel):
    sources: List[SourceItem] # Renamed from urls
    bundle_title: Optional[str] = None
    bundle_author: Optional[str] = None
    no_comments: bool = False
    no_images: bool = False
    no_article: bool = False
    archive: bool = False
    max_depth: Optional[int] = None
    max_pages: Optional[int] = None
    max_posts: Optional[int] = None
    page_spec: Optional[List[int]] = None
    termux_copy_dir: Optional[str] = None
    llm_format: bool = False
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    summary: bool = False
    thumbnails: bool = False
    youtube_lang: Optional[str] = "en"
    youtube_prefer_auto: bool = False
    youtube_max_comments: int = 25
    youtube_comment_sort: str = "top"

class ScanRequest(BaseModel):
    html: str
    url: str

@app.post("/helper/extract-links")
async def extract_links(req: ScanRequest):
    """
    Helper for Chrome Extension (MV3) which cannot use DOMParser.
    Extracts potential image assets and next-page links from HTML.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse
    import re

    soup = BeautifulSoup(req.html, 'html.parser')
    base_url = req.url
    
    # 1. Extract Images (matching background.js logic)
    images = []
    seen = set()
    
    # Selectors for forum posts
    img_tags = soup.select(".message-body img, .messageContent img, .bbWrapper img, .bbImage img")
    # Containers fallback
    if not img_tags:
        containers = soup.select("article.message, .message--post, [data-lb-id]")
        for c in containers:
            img_tags.extend(c.find_all("img"))

    for img in img_tags:
        src = img.get("src") or img.get("data-src")
        if not src: continue
        
        # Skip junk
        src_lower = src.lower()
        if any(x in src_lower for x in ["/avatar", "/reaction", "/smilies", "/emoji", "data:image/gif"]):
            continue
            
        parent = img.find_parent("a")
        viewer = urljoin(base_url, parent["href"]) if (parent and parent.get("href")) else None
        
        # Collect URLs
        primary = urljoin(base_url, src)
        
        if "/attachments/" not in primary and not viewer: 
            # If it's not an attachment, maybe it's external?
            pass
        
        key = primary.split("?")[0]
        if key in seen: continue
        seen.add(key)
        
        images.append({
            "url": primary,
            "viewer_url": viewer,
            "filename_hint": src.split("/")[-1]
        })

    # External images (non-attachment)
    externals = []
    for img in img_tags:
        src = img.get("src") or img.get("data-src")
        if not src or src.startswith("data:"): continue
        full = urljoin(base_url, src)
        if full not in seen:
            externals.append(full)
            seen.add(full)

    # 2. Find Next Page
    next_page = None
    
    # <link rel="next">
    link_next = soup.find("link", attrs={"rel": "next"})
    if link_next and link_next.get("href"):
        next_page = urljoin(base_url, link_next.get("href"))
    
    if not next_page:
        # <a>Next</a>
        for a in soup.find_all("a"):
            txt = a.get_text(strip=True).lower()
            if txt in ("next", "next >", "next>"):
                if a.get("href"):
                    next_page = urljoin(base_url, a.get("href"))
                    break
    
    # Parse page number from next_url to return int if possible? 
    # The extension logic expects a URL to fetch, or logic to build it.
    # The extension builds it: `buildForumPageUrl`.
    # But `findNextPage` in JS returned an integer page number.
    # Let's return the integer if we can extract it.
    
    next_page_num = None
    if next_page:
        m = re.search(r'page[-=_/](\d+)', next_page)
        if m:
            try:
                next_page_num = int(m.group(1))
            except: pass

    return {
        "assets": images,
        "externals": externals,
        "next_page_num": next_page_num
    }

@app.get("/ping")
async def ping(): return {"status": "ok"}

@app.post("/convert")
async def convert(req: ConversionRequest):
    print(f"📥 Received request: {len(req.sources)} sources")
    print(f"🔧 Options: NoComments={req.no_comments}, NoImages={req.no_images}, Thumbnails={req.thumbnails}, YTLang={req.youtube_lang}, YTSort={req.youtube_comment_sort}")
    if req.sources:
        for idx, s in enumerate(req.sources):
            count_assets = len(s.assets) if s.assets else 0
            print(f"Source[{idx}] assets: {count_assets}")
            if s.assets:
                original_count = len(s.assets)
                s.assets = [
                    a for a in s.assets
                    if a.get("original_url")
                    and a.get("original_url") != s.url
                    and (
                        "image" in str(a.get("content_type", "")).lower()
                        or "/attachments/" in str(a.get("original_url"))
                    )
                ]
                print(f"Source[{idx}] assets: {original_count} -> {len(s.assets)} (after filtering)")

    options = ConversionOptions(
        no_comments=req.no_comments,
        no_images=req.no_images,
        no_article=req.no_article,
        archive=req.archive,
        compact_comments=True,
        max_depth=req.max_depth,
        max_pages=req.max_pages,
        max_posts=req.max_posts,
        page_spec=req.page_spec,
        llm_format=req.llm_format,
        llm_model=req.llm_model,
        llm_api_key=req.llm_api_key,
        summary=req.summary,
        thumbnails=req.thumbnails,
        youtube_lang=req.youtube_lang or "en",
        youtube_prefer_auto=req.youtube_prefer_auto,
        youtube_max_comments=req.youtube_max_comments,
        youtube_comment_sort=req.youtube_comment_sort
    )

    # Map Pydantic to Core Dataclass
    core_sources = []
    for s in req.sources:
        is_forum = bool(s.is_forum)
        core_sources.append(Source(
            url=s.url,
            html=s.html,
            cookies=s.cookies,
            assets=s.assets,
            is_forum=is_forum
        ))

    async with get_session() as session:
        processed_books = await core_main.process_urls(core_sources, options, session)

    if not processed_books:
        raise HTTPException(status_code=500, detail="No content could be extracted.")

    try:
        if len(processed_books) > 1:
            title = req.bundle_title or f"Bundle_{len(processed_books)}_Articles"
            author = req.bundle_author or "Web to EPUB"
            final_book = core_main.create_bundle(processed_books, title, author)
        else:
            final_book = processed_books[0]
            if req.bundle_title: final_book.title = req.bundle_title

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp_path = tmp.name

        EpubWriter.write(final_book, tmp_path)
        filename = f"{sanitize_filename(final_book.title)}.epub"
        print(f"✅ Generated EPUB at: {tmp_path}")
        print(f"✅ Sending as: {filename}")

        # If running inside Termux with shared storage mounted, drop a copy to Downloads
        termux_dl = (req.termux_copy_dir or "").strip() or "/data/data/com.termux/files/home/storage/downloads"
        if os.path.isdir(termux_dl):
            try:
                dst = os.path.join(termux_dl, filename)
                shutil.copy2(tmp_path, dst)
                print(f"📥 Copied EPUB to Termux downloads: {dst}")
            except Exception as copy_err:
                print(f"⚠️  Could not copy to Termux downloads: {copy_err}")

        return FileResponse(path=tmp_path, filename=filename, media_type='application/epub+zip')

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

def start():
    uvicorn.run(app, host="127.0.0.1", port=8000)

if __name__ == "__main__":
    start()

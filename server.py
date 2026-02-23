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
    expose_headers=["Content-Disposition", "X-Dala-Server-Saved"],
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
    server_save_dir: Optional[str] = None
    termux_copy_dir: Optional[str] = None # Alias for backward compatibility
    archive_server: bool = False
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

        # --- Smart Server-Side Saving (Single Copy) ---
        # We attempt to save a single copy to the best available location.
        # Priority: 1. User Path -> 2. System Downloads -> 3. Project Exports (Fallback)
        
        candidates = []
        user_input = (req.server_save_dir or req.termux_copy_dir or "").strip()
        
        # Determine System Downloads (Cross-Platform)
        sys_downloads = None
        try:
            # Android/Termux check
            termux_path = "/data/data/com.termux/files/home/storage/downloads"
            if os.path.isdir(termux_path):
                sys_downloads = termux_path
            # Linux XDG check
            elif os.name == 'posix':
                import subprocess
                res = subprocess.run(['xdg-user-dir', 'DOWNLOAD'], capture_output=True, text=True)
                if res.returncode == 0 and res.stdout.strip():
                    sys_downloads = res.stdout.strip()
        except Exception: 
            pass
            
        if not sys_downloads:
            # Windows/macOS/Linux Fallback
            possible_dl = os.path.join(os.path.expanduser("~"), "Downloads")
            if os.path.isdir(possible_dl):
                sys_downloads = possible_dl

        # Logic for User Input
        if user_input:
            # Check if Absolute
            is_absolute = os.path.isabs(user_input) or (os.name == 'nt' and len(user_input) > 1 and user_input[1] == ':')
            if is_absolute:
                candidates.append(user_input)
            elif sys_downloads:
                # Relative to system downloads
                candidates.append(os.path.join(sys_downloads, user_input))
            else:
                # If no system downloads, just use the relative path as-is (current dir)
                candidates.append(user_input)
        
        if sys_downloads and sys_downloads not in candidates:
            candidates.append(sys_downloads)

        # 3. Project Fallback (Archive)
        project_root = os.path.dirname(os.path.abspath(__file__))
        exports_dir = os.path.join(project_root, "exports")
        
        # If user EXPLICITLY requested archive, force copy there first
        if req.archive_server:
            try:
                os.makedirs(exports_dir, exist_ok=True)
                archive_path = os.path.join(exports_dir, filename)
                shutil.copy2(tmp_path, archive_path)
                print(f"✅ Archived to project exports: {archive_path}")
            except Exception as e:
                print(f"⚠️  Archive failed: {e}")

        # Now try to save the 'User Copy' (Single Best Location)
        candidates.append(exports_dir) 

        saved_user_copy = False
        for dest in candidates:
            if not dest: continue
            
            # Optimization: Skip if matches archive
            if dest == exports_dir and req.archive_server:
                saved_user_copy = True
                break

            try:
                # Ensure path exists (recursively create if relative)
                os.makedirs(dest, exist_ok=True)
                
                if os.path.isdir(dest):
                    final_path = os.path.join(dest, filename)
                    shutil.copy2(tmp_path, final_path)
                    saved_user_copy = True
                    print(f"📥 Saved local copy to: {final_path}")
                    break # Stop after first success
            except Exception:
                continue
        
        if not saved_user_copy:
             print("⚠️  Could not save a server-side copy to any location.")

        return FileResponse(
            path=tmp_path,
            filename=filename,
            media_type='application/epub+zip',
            headers={"X-Dala-Server-Saved": "1" if saved_user_copy else "0"},
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

def start():
    uvicorn.run(app, host="127.0.0.1", port=8000)

if __name__ == "__main__":
    start()

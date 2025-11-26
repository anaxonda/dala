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
#   "tqdm"
# ]
# ///

import uvicorn
import os
import tempfile
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import web_to_epub as core

app = FastAPI()

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

@app.get("/ping")
async def ping(): return {"status": "ok"}

@app.post("/convert")
async def convert(req: ConversionRequest):
    print(f"📥 Received request: {len(req.sources)} sources")
    if req.sources:
        for idx, s in enumerate(req.sources):
            count_assets = len(s.assets) if s.assets else 0
            print(f"Source[{idx}] assets: {count_assets}")
            if s.assets:
                original_count = len(s.assets)
                s.assets = [
                    a for a in s.assets
                    if a.get("original_url")
                    and not str(a.get("original_url")).endswith("/")
                    and a.get("original_url") != s.url
                    and "image" in str(a.get("content_type", ""))
                ]
                print(f"Source[{idx}] assets: {original_count} -> {len(s.assets)} (after filtering)")

    options = core.ConversionOptions(
        no_comments=req.no_comments,
        no_images=req.no_images,
        no_article=req.no_article,
        archive=req.archive,
        compact_comments=True,
        max_depth=req.max_depth,
        max_pages=req.max_pages,
        max_posts=req.max_posts,
        page_spec=req.page_spec
    )

    # Map Pydantic to Core Dataclass
    core_sources = []
    for s in req.sources:
        is_forum = bool(s.is_forum)
        core_sources.append(core.Source(
            url=s.url,
            html=s.html,
            cookies=s.cookies if is_forum else None,
            assets=s.assets if is_forum else None,
            is_forum=is_forum
        ))

    async with core.get_session() as session:
        processed_books = await core.process_urls(core_sources, options, session)

    if not processed_books:
        raise HTTPException(status_code=500, detail="No content could be extracted.")

    try:
        if len(processed_books) > 1:
            title = req.bundle_title or f"Bundle_{len(processed_books)}_Articles"
            author = req.bundle_author or "Web to EPUB"
            final_book = core.create_bundle(processed_books, title, author)
        else:
            final_book = processed_books[0]
            if req.bundle_title: final_book.title = req.bundle_title

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp_path = tmp.name

        core.EpubWriter.write(final_book, tmp_path)
        filename = f"{core.sanitize_filename(final_book.title)}.epub"
        print(f"✅ Sending: {filename}")

        return FileResponse(path=tmp_path, filename=filename, media_type='application/epub+zip')

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)

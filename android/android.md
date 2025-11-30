# android notes
UV run cache not persisting, so made venv.
    
    python -m venv .venv
    . .venv/bin/activate
    UV_LINK_MODE=copy UV_CACHE_DIR=$HOME/.cache/uv uv pip install \
      fastapi uvicorn aiohttp[speedups] EbookLib beautifulsoup4 trafilatura Pillow lxml pygments tqdm
    # after this, run the server quickly:
    python server.py   # or: uv run --python .venv/bin/python server.py




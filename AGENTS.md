# Repository Guidelines

## Project Structure & Module Organization
- **`dala/` (Package)**:
    - **`drivers/`**: Specialized extractors (HN, Reddit, Substack, YouTube, WordPress, Forum).
    - **`core/`**: Core logic including `ArticleExtractor` (text), `ImageProcessor` (media), and `DriverDispatcher`.
    - **`models.py`**: Shared data structures (`BookData`, `Source`, etc.).
- `main.py`: CLI entry point (replaces monolithic script).
- `web_to_epub.py`: Legacy shim for backward compatibility (imports from `dala`).
- `server.py`: FastAPI backend. Adds `/helper/extract-links` for server-side HTML parsing (Chrome MV3 compat).
- `firefox_extension/`: Firefox add-on (Manifest V2).
- `extension_chrome/`: Chrome/Brave/Edge add-on (Manifest V3). Uses a shim and server-side parsing to bypass Service Worker DOM limits.

## Build, Test, and Development Commands
- `uv run main.py https://example.com/article`: Run the CLI directly.
- `uv run server.py`: Start the FastAPI backend. Required for both Firefox and Chrome extensions.
- **Chrome Extension Dev:** Load `extension_chrome/` unpacked in `chrome://extensions`.
- **Packaging:** Run `./package_extensions.sh` to generate installable ZIPs/XPIs in `dist/`.

## Chrome Extension (Manifest V3) Specifics
- **Service Worker:** Runs in `background.js`. No access to DOM/`DOMParser`.
- **HTML Parsing:** Offloaded to the local server (`/helper/extract-links`) via POST request.
- **Downloads:** Uses a fallback chain:
    1.  `URL.createObjectURL(blob)` (Standard, often blocked in SW).
    2.  `browser.downloads.download` with specific filename.
    3.  **Last Resort:** Converts Blob to **Data URI** (`data:application/epub...`) and downloads with `filename: fallback.epub`. This bypasses most filesystem/permissions issues.
- **Shim:** `chrome-shim.js` polyfills `browser.*` APIs to `chrome.*` APIs, wrapping callbacks in Promises.

## Known Limitations
- **Popup Closure:** The extension popup handles the initial "Grabbing content..." phase (script injection to steal HTML). If the user closes the popup **before** the status changes to "Started in background", the process aborts.
    - *Future Task:* Refactor `preparePayload` logic to run entirely in the background script to fix this.

## Coding Style & Naming Conventions
- Python: 4-space indent, snake_case for functions/vars, CapWords for classes, prefer type hints and dataclasses where shared across API boundaries.
- Prefer async flows (aiohttp sessions) and explicit option objects over ad-hoc kwargs; keep network calls cancellable and timeouts explicit.
- Use f-strings for formatting; keep prints concise and debug-friendly.
- Respect EPUB/e-reader constraints: avoid modern CSS in generated content; keep layouts table-based when needed.

## Testing Guidelines
- No formal test suite is present; rely on targeted manual runs:
  - Single URL sanity: `uv run web_to_epub.py <url>`
  - Bundle path: `uv run web_to_epub.py -i links.txt --bundle`
  - Extension round-trip: `uv run server.py` + load `firefox_extension/` temporary add-on, then trigger “Download Page”.
- When adding drivers/features, exercise problematic cases (lazy-loaded images, deep comment trees, Substack custom domains) and capture failures in the PR notes.

## Commit & Pull Request Guidelines
- Git history is minimal; use short, imperative subjects (e.g., "Add HN fallback driver", "Improve image unwrapping").
- PRs should include: purpose, key changes, manual test commands run, and screenshots/GIFs for extension UX changes.
- Link related issues if tracking; call out breaking changes (API fields, flag renames) explicitly.
- Keep diffs scoped: one concern per PR. If adding deps, justify them in the description and ensure `uv` metadata stays in sync.

## Security & Configuration Tips
- Do not hardcode cookies/API keys; the extension should rely on the user’s logged-in session only.
- CORS is open for the local server by design; if exposing externally, restrict origins/ports and prefer localhost-only binding.
- Clean temporary artifacts when experimenting with EPUB outputs; avoid committing generated `.epub` files.
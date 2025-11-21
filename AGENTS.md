# Repository Guidelines

## Project Structure & Module Organization
- `web_to_epub.py`: Core conversion pipeline, async scraping drivers (generic, Hacker News, Substack), EPUB assembly helpers.
- `server.py`: FastAPI wrapper exposing `/convert` and `/ping` for the browser extension; uses the same conversion options.
- `extension/`: Firefox add-on (background script, popup UI, manifest, icon) that forwards unlocked HTML to the server for EPUB creation.
- Assets live alongside their feature (e.g., CSS/JS in `extension/`); keep new modules co-located with their primary feature to reduce churn.

## Build, Test, and Development Commands
- `uv run web_to_epub.py https://example.com/article`: Run the CLI directly with inline deps resolved by `uv`.
- `uv run web_to_epub.py -i links.txt --bundle`: Batch/bundle multiple URLs from a file.
- `uv run server.py`: Start the FastAPI backend for extension-driven conversions (listens on 127.0.0.1:8000 by default).
- `python -m web_to_epub --help`: Quick flag reference if you add a CLI wrapper; mirror new flags here.

## Coding Style & Naming Conventions
- Python: 4-space indent, snake_case for functions/vars, CapWords for classes, prefer type hints and dataclasses where shared across API boundaries.
- Prefer async flows (aiohttp sessions) and explicit option objects over ad-hoc kwargs; keep network calls cancellable and timeouts explicit.
- Use f-strings for formatting; keep prints concise and debug-friendly.
- Respect EPUB/e-reader constraints: avoid modern CSS in generated content; keep layouts table-based when needed.

## Testing Guidelines
- No formal test suite is present; rely on targeted manual runs:
  - Single URL sanity: `uv run web_to_epub.py <url>`
  - Bundle path: `uv run web_to_epub.py -i links.txt --bundle`
  - Extension round-trip: `uv run server.py` + load `extension/` temporary add-on, then trigger “Download Page”.
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

# Repository Guidelines

## Project Structure & Module Organization
- **`dala/` (Package)**:
    - **`drivers/`**: Specialized extractors (HN, Reddit, Substack, YouTube, WordPress, Forum).
    - **`core/`**: Core logic including `ArticleExtractor` (text), `ImageProcessor` (media), and `DriverDispatcher`.
    - **`models.py`**: Shared data structures (`BookData`, `Source`, etc.).
- `main.py`: CLI entry point (replaces monolithic script).
- `web_to_epub.py`: Legacy shim for backward compatibility (imports from `dala`).
- `server.py`: FastAPI backend. Provides `/convert`, async `/jobs`, `/jobs/{id}/download`, `/helper/extract-links`, and `/helper/last-conversion`.
- `firefox_extension/`: Firefox add-on (Manifest V2).
- `extension_chrome/`: Chrome/Brave/Edge add-on (Manifest V3). Uses a shim and server-side parsing to bypass Service Worker DOM limits.
- `tests/`: Pytest coverage for dispatch, drivers, server conversion, server-side saving, and HN delegation behavior.
- `config/bpc/`: Ignored local checkout/extraction area for Bypass Paywalls Clean. Do not commit fetched third-party extension artifacts.

## Build, Test, and Development Commands
- `uv run main.py https://example.com/article`: Run the CLI directly.
- `uv run dala-server`: Start the FastAPI backend. Required for both Firefox and Chrome extensions. `uv run server.py` still works as a direct script fallback.
- `uv run pytest`: Run the automated test suite.
- `git status --short`: Check the dirty tree before editing and before handing work back.
- **Chrome Extension Dev:** Load `extension_chrome/` unpacked in `chrome://extensions`.
- **Packaging:** Run `./package_extensions.sh` to generate browser extension packages and the installer bundle in `dist/`.

## Chrome Extension (Manifest V3) Specifics
- **Service Worker:** Runs in `background.js`. No access to DOM/`DOMParser`.
- **HTML Parsing:** Offloaded to the local server (`/helper/extract-links`) via POST request.
- **Background Preparation:** `popup.js` sends `init_download`; `background.js` handles tab HTML capture, queue/bundle payload preparation, server job submission, polling, cancellation, and downloads. The popup can close after the background task starts.
- **Downloads:** Uses a fallback chain:
    1.  `URL.createObjectURL(blob)` (Standard, often blocked in SW).
    2.  `browser.downloads.download` with specific filename.
    3.  **Last Resort:** Converts Blob to **Data URI** (`data:application/epub...`) and downloads with `filename: fallback.epub`. This bypasses most filesystem/permissions issues.
- **Shim:** `chrome-shim.js` polyfills `browser.*` APIs to `chrome.*` APIs, wrapping callbacks in Promises.

## Known Limitations
- **Local Server State:** Async jobs are stored in process memory only. Server restart loses job status and temporary download paths.
- **Browser Fallback Policy:** Server browser fallback auto-detects Chrome, Edge, Brave, Chromium, or Playwright's managed Chromium, and uses the dedicated Dala profile at `~/.local/share/dala/browser-profile` by default. Do not auto-use the user's real browser profile. Interactive bot challenges should default to archive fallback; the extension can optionally open the original URL in the user's browser. The screenshot-based warm browser remains an explicit `browser_challenge_action: "warm"` path only. BPC-style extensions can be installed in the user's normal headed browser for extension capture; server-side BPC under `config/bpc/` is an advanced local fallback/testing path.
- **Server Trust Boundary:** CORS is open for extension use. Keep the server bound to `127.0.0.1` unless intentionally exposing it, and document remote/LAN server assumptions when changing extension server URL behavior.
- **Driver Heuristics:** Generic, forum, and image extraction include site-specific heuristics and fallbacks. When touching them, add focused regression tests with representative HTML.
- **Generated Artifacts:** EPUB/PDF outputs, extension packages, installer bundles, `web-ext-artifacts/`, `exports/`, pycache, screenshots, logs, local systemd exports, and `config/bpc/` are generated/local artifacts and should remain uncommitted unless explicitly requested for release staging.
- **PDF Image Assets:** EPUB assets may be optimized WebP, but PDF rendering should feed Chromium temporary JPEG assets derived from the selected image preset/color mode to avoid oversized embedded RGB image streams.

## Coding Style & Naming Conventions
- Python: 4-space indent, snake_case for functions/vars, CapWords for classes, prefer type hints and dataclasses where shared across API boundaries.
- Prefer async flows (aiohttp sessions) and explicit option objects over ad-hoc kwargs; keep network calls cancellable and timeouts explicit.
- Use f-strings for formatting; keep prints concise and debug-friendly.
- Respect EPUB/e-reader constraints: avoid modern CSS in generated content; keep layouts table-based when needed.

## Testing Guidelines
- Run automated tests before and after core/server/driver changes:
  - `uv run pytest`
- Add focused pytest coverage for driver routing, server payload handling, image URL normalization, and representative HTML fixtures when changing extraction behavior.
- Also use targeted manual runs for real-network and extension behavior:
  - Single URL sanity: `uv run web_to_epub.py <url>`
  - Bundle path: `uv run web_to_epub.py -i links.txt --bundle`
  - Extension round-trip: `uv run dala-server` + load `firefox_extension/` or `extension_chrome/`, then trigger “Download Page” and “Download Bundle”.
- When adding drivers/features, exercise problematic cases (lazy-loaded images, gated forum attachments, deep comment trees, Substack custom domains, YouTube transcript/comment options) and capture failures in the PR notes.

## Commit & Pull Request Guidelines
- Git history is minimal; use short, imperative subjects (e.g., "Add HN fallback driver", "Improve image unwrapping").
- PRs should include: purpose, key changes, manual test commands run, and screenshots/GIFs for extension UX changes.
- Link related issues if tracking; call out breaking changes (API fields, flag renames) explicitly.
- Keep diffs scoped: one concern per PR. If adding deps, justify them in the description and ensure `uv` metadata stays in sync.
- Practice tight git hygiene: review `git status --short` before and after changes, separate source/doc edits from generated rebuild outputs, and call out intentionally dirty or ignored local artifacts in handoff notes.
- Commit source/docs/config changes before tagging or publishing. Do not commit `dist/`, `.env`, `.pypirc`, generated ebooks/PDFs, screenshots, logs, or downloaded third-party extension artifacts.
- Use normal push flow only after reviewing the final diff: `git status --short`, `git diff --check`, commit with an imperative subject, then `git push origin <branch>`. Push tags explicitly with `git push origin <tag>` or `git push origin --tags`.
- When extension source changes, rebuild both browser packages with `./package_extensions.sh` and verify `dist/` contains the expected Chrome ZIP and Firefox XPI. Firefox release assets should use the AMO-signed XPI, not the unsigned local build.

## Release & Publishing Guidelines
- Treat Python package and browser extension versions as separate but user-visible. For normal public releases, keep `pyproject.toml`/`uv.lock` and both extension manifests aligned. If the extension version is intentionally unchanged, make that explicit in release notes and asset names.
- For Python package releases:
  - Update the package version in `pyproject.toml` and refresh `uv.lock`.
  - Run `uv run pytest` and `git diff --check`.
  - Build with `uv build --out-dir /tmp/dala-build.<version>` and validate with `uv run --with twine twine check /tmp/dala-build.<version>/*`.
  - For packaging changes, smoke-test the wheel locally with `uv tool install --force /tmp/dala-build.<version>/dala-<version>-py3-none-any.whl`, then verify `dala --help`, `dala-server --help`, and any touched entry points.
  - Publish to TestPyPI before PyPI when metadata, entry points, package data, or installer docs change. Then publish the same checked artifacts to PyPI.
- For extension releases:
  - Update both `firefox_extension/manifest.json` and `extension_chrome/manifest.json` when the extension is part of the release.
  - Run `./package_extensions.sh`; confirm the Chrome ZIP, Firefox XPI, and `dala-installers-v<package-version>.zip` exist in `dist/`.
  - Sign the Firefox package with `web-ext`/AMO credentials from the local environment. Never commit credentials. Attach the signed XPI to GitHub releases.
- For GitHub releases:
  - Tag only after tests, packaging checks, and release notes are ready.
  - Attach `dala-chrome-v<extension-version>.zip`, the AMO-signed `dala-firefox-v<extension-version>.xpi`, and `dala-installers-v<package-version>.zip`.
  - Prefer PyPI as the canonical source for Python wheels/sdists; attach Python artifacts only if there is a specific release reason.
  - Release notes should list user-facing changes, packaging/install changes, known limitations, and whether extension assets were rebuilt or unchanged.
- After publishing, test the public install path when practical:
  - `uv tool install --force dala`
  - `uv tool install --force "dala[browser]"`
  - Termux/Android: run `scripts/install-dala.sh`; do not assume `uv tool install` or `uv pip` works because `uv` may fail to inspect Android Python.
  - `dala-server --open`
  - `dala-setup-browser --check-only`

## Security & Configuration Tips
- Do not hardcode cookies/API keys; the extension should rely on the user’s logged-in session only.
- CORS is open for the local server by design; if exposing externally, restrict origins/ports and prefer localhost-only binding.
- Clean temporary artifacts when experimenting with EPUB outputs; avoid committing generated `.epub` files.

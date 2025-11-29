# Web-to-EPUB Downloader (E-Ink Optimized)

Tool to convert web articles, Hacker News threads, Substack posts, and Reddit threads into EPUBs tuned for e-ink devices (Kindle, KOReader, Kobo). A bundled Firefox extension can capture unlocked page HTML from your browser and send it to the local FastAPI server for EPUB generation. Specialized drivers handle site-specific APIs, anti-bot hurdles, and layouts that survive older reader engines.

<img src="extension/icon.png" alt="Firefox Extension" width="72" />

Allow threaded comment navigation with button links in the epub  to jump to different levels in hierarchy: top level, parent, sibling, next top level.

## Table of Contents
- [Quick Usage](#quick-usage)
- [Options & Flags](#options--flags)
- [Examples](#examples)
- [Updates](#-updates)
- [Installation](#-installation)
- [Firefox Extension Setup](#-firefox-extension-setup)
- [Systemd Setup](#-systemd-setup)
- [Architecture & History](#-architecture--history)

---

## Quick Usage
```bash
uv run web_to_epub.py [URL]
```
- Supports single articles, HN threads, Substack posts (including custom domains), Reddit threads (old/new/redd.it), and forum threads (e.g., XenForo).
- Bundle multiple URLs: `uv run web_to_epub.py -i links.txt --bundle --bundle-title "Morning Read"`
- Run local server for the extension: `uv run server.py`
- Install the signed Firefox add-on (unlisted, current): `extension/web-ext-artifacts/79556425c64b4e2c9b57-2.3.xpi` via “Install Add-on From File…” in `about:addons`.

## Options & Flags

| Flag | Description |
| :--- | :--- |
| `--bundle` | Anthology mode; combines all input into one EPUB with nested TOC. |
| `--bundle-title "Name"` | Custom bundle title; defaults to `Domain - Date`. |
| `--no-comments` | Article text only; skip comment fetch. |
| `--no-article` | Comments only (useful for HN where the link is just context). |
| `--no-images` | Text-only mode to shrink file size. |
| `-a`, `--archive` | Force fetch from the Wayback Machine (dead links). |
| `-i [file.txt]` | File with one URL per line. |
| `--max-depth [N]` | Limit comment recursion depth (HN/Reddit). |
| `--max-pages [N]` | Limit forum pages fetched. |
| `--pages 1,3-5` | Fetch specific forum pages. |
| `--css [file.css]` | Inject custom CSS into output. |
| `-v` | Verbose logging (debug mode). |

## Examples

**Morning Digest Bundle**
```bash
uv run web_to_epub.py -i links.txt --bundle --bundle-title "Morning Read - Nov 20"
```

**Hacker News (Deep Dive)**
```bash
uv run web_to_epub.py "https://news.ycombinator.com/item?id=123456"
```

**Substack (Custom Domain)**
```bash
uv run web_to_epub.py https://www.astralcodexten.com/p/the-bloomers-paradox
```

**Reddit Thread**
```bash
uv run web_to_epub.py https://old.reddit.com/r/AskHistorians/comments/1p2uk19/ken_burns_the_american_revolution_claims_that_the/
```

**XenForo Forum Thread (pages 1–2)**
```bash
uv run web_to_epub.py "https://www.trek-lite.com/index.php?threads/arcdome-1.15243/" --pages 1-2
```

---

## 🔄 Updates
- **UI & queue polish (latest):**
  - Popup queue is now an editable textarea: paste/edit URLs directly; changes auto-save and the queue persists across downloads.
  - Added context-menu action “Download Page to EPUB” for immediate conversion of the current page/link; queue remains untouched.
- **Metadata consistency:**
  - Unified article meta block across drivers (Article Source/Author/Date/Site; archive notice where applicable). HN/Reddit external articles now show consistent metadata alongside thread context.
- **Image cleanup:**
  - Generic image processing strips placeholders (grey-placeholder), flattens wrappers, dedupes captions, and enforces `<div class="img-block"><img class="epub-image">` with a single caption when present.
  - Figures/spans are unwrapped; duplicate captions removed.
- **Reddit media fixes:**
  - Image-link posts (`i.redd.it/...png`) render as the article with embedded images.
  - Comment image links are inlined and fetched (skipping non-file wiki pages).
- **Wikimedia fetch tuning:**
  - Wikimedia images fetched with Commons referer + project UA; targeted fallbacks added and logging when blocked (ongoing).
- **Forum image HTML simplification (latest):**
  - **Problem:** XenForo lightbox markup (`lazyloadPreSize`, `lbContainer*`, zoomer stubs, `data-lb-*`, `data-zoom-target`, empty `title`) leaked into the EPUB, leaving nested wrappers and non-reader-safe attributes.
  - **Fix:** Added forum-specific cleanup in `ForumImageProcessor` to strip lightbox attrs, unwrap XenForo containers, and remove zoomer divs while still running the same asset mapping/dedup logic.
  - **Result:** EPUB now emits minimal image HTML (`<div class="img-block"><img class="epub-image" src="..."></div>` plus caption when found) without breaking filename mapping or preload reuse.
- **Forum attachment reliability (latest):**
  - **What was broken:** Page fragments (`#replies`) kept every fetch on page 1; popup closed before fetch finished; discovery crashed on lightbox nodes without `closest`; “enough assets” skipped re-fetch when only avatars/1x1s were present; 409/redirect fetches aborted; attachments on page 2/3 never entered the preload map, causing misses/dupes.
  - **Fixes applied:** Strip fragments before building `page-N` URLs so pages 2/3 load; move all asset fetching to background after the popup closes; always re-fetch/merge assets and dedupe by URL instead of early-skipping; filter strictly to `/attachments/`, skip avatars/1x1/data GIFs; expand lightbox selectors (`[data-lb-*]`, `.bbImage`, `a.attachment`, `[data-attachment-id]`) with guards for missing `closest`; binary fetch uses `cache: reload` and retries query-stripped URLs on 409/opaque redirects. Result: all post attachments across pages are discovered and reused without duplication.
- **Reddit Driver (new):** Reddit/old.reddit/redd.it links now fetch via the JSON API (`raw_json=1`), render self-posts or linked articles, and include threaded comments with navigation. Works in both CLI and the Firefox extension via the existing FastAPI backend.
- **Forum Driver & Attachments:** Forum threads (e.g., XenForo) now support page ranges, asset preloading from the browser, and external images. The Firefox extension can fetch gated attachments with your session cookies and embed them into EPUBs.

---

## 🚀 Installation

**Prerequisites:** Python 3.8+

### Recommended: Using `uv`
This script contains inline dependency metadata. If you have [uv](https://github.com/astral-sh/uv), no manual install is needed.

```bash
# Run immediately
uv run web_to_epub.py [URL]
```

### Alternative: Standard PIP
```bash
pip install requests aiohttp beautifulsoup4 EbookLib trafilatura lxml pygments tqdm Pillow uvicorn fastapi
```

---

## 🦊 Firefox Extension Setup

The project includes a **Firefox Extension** and a **Local Python Server**.
The extension grabs unlocked HTML from your browser session; for gated forums, it can also fetch attachments and inline images with your cookies. Use the extension path when images/attachments are blocked via CLI (403/409 hotlink rules).

### Step 1: Run the Server
The extension needs a backend to build the EPUB.

1.  Open a terminal in the project folder.
2.  Run:
    ```bash
    uv run server.py
    ```
3.  Leave this terminal open (or see "Systemd Setup" below).

### Step 2: Install the Extension (Temporary/Developer Mode)
1.  Open Firefox and type `about:debugging` in the address bar.
2.  Click **"This Firefox"** on the left.
3.  Click **"Load Temporary Add-on..."**.
4.  Navigate to the `epub-extension/` folder in this project.
5.  Select `manifest.json`.
   - Or install the signed XPI (`extension/web-ext-artifacts/79556425c64b4e2c9b57-2.3.xpi`) via “Install Add-on From File…” in `about:addons` (unlisted AMO-signed).

### Step 3: Using the Extension
*   **Download Page:** Click the extension icon -> "Download Page", or right-click and choose **"Download Page to EPUB"**.
*   **Queue/Bundle:** Right-click to **"Add to EPUB Queue"**, or paste/edit URLs directly in the Queue textarea (one per line), then **Download Bundle**.
*   **Add Tabs:** Use **+ Current / + Selected / + All** in the Queue tab to import open tabs.

---

## ⚙️ Systemd Setup (Linux Auto-Start)

To keep the server running in the background automatically on Linux:

1.  **Create Service File:**
    ```bash
    mkdir -p ~/.config/systemd/user/
    nano ~/.config/systemd/user/epub-server.service
    ```

2.  **Paste Configuration:**
    *Replace `/path/to/project` with your actual path.*
    *Replace `/path/to/uv` with your uv path (run `which uv` to find it).*

    ```ini
    [Unit]
    Description=Web to EPUB Python Server
    After=network.target

    [Service]
    WorkingDirectory=/path/to/project
    ExecStart=/path/to/uv run server.py
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=default.target
    ```

3.  **Enable & Start:**
    ```bash
    systemctl --user daemon-reload
    systemctl --user enable --now epub-server
    ```

4.  **Check Status:**
    ```bash
    systemctl --user status epub-server
    ```

---

## 🏗 Architecture & History
*Why the code looks the way it does.*

### 1. The Driver Pattern
A single heuristic scraper was not enough.
*   **Generic Driver:** Uses heuristics to find the main content on standard websites.
*   **Hacker News Driver:** Uses the Firebase API to recursively fetch comment trees, preserving the discussion structure.
*   **Substack Driver:** Finds hidden JSON in `window._preloads`, handles Cloudflare 403s via headers, and falls back to native `*.substack.com` API when custom domains break.
*   **Reddit Driver:** Calls the Reddit JSON API (`raw_json=1`), renders self-post HTML or linked articles, and normalizes threaded comments for navigation.

### 2. The Battle for Layout (E-Ink Optimization)
Formatting for a 30-inch monitor breaks on a 6-inch Kindle.
*   **"Squashed Text" Problem:** Deep nesting shrank the content column. Border-based indentation keeps hierarchy without collapsing width.
*   **"Flexbox" Failure:** Older e-readers ignore `display: flex`. CSS table layout keeps headers and navigation on one line.

### 3. Navigation (The "Cluster")
Reading threaded conversations linearly is difficult.
*   Solution: A navigation cluster (`↑ → ⏮ ⏭`) in each comment header with internal anchors for Parent / Next Sibling / Thread Root / Next Thread.

### 4. Image Handling & The "Picture" Problem
*   **Lazy Loading:** Detects `data-src`/`srcset` fallbacks to retrieve high-res images.
*   **`<picture>` Trap:** Unwraps `<picture>` and `<source>` so e-readers use the bundled `<img>` instead of remote URLs.

### 5. Forums & Gated Attachments (Browser Assist)
*   **Problem:** XenForo and similar forums gate attachments behind session/anti-hotlink checks, returning 403/409 and serving only thumbnails to direct fetches.
*   **Solution:** The Firefox extension preloads assets using the live browser session:
    * Scrapes post-body images (src/srcset/data-url) from the active tab.
    * Background-fetches forum pages/attachments with cookies; follows viewer pages and parses full-size URLs, and downloads external images (e.g., Flickr).
*   **Latest Fix (multi-page reliability):** Asset fetch now runs entirely in the background after the popup closes, strips URL fragments so pages 2/3 load, filters out avatars/1x1s, guards lazy-load lightbox nodes, and retries attachment fetches when needed so all post images make it into the EPUB without duplication.
    * Sends assets to the server; core matches them and skips re-downloading.
*   **Result:** Full-size forum attachments embed correctly; use the extension path for gated content (CLI alone cannot replicate browser-only tokens/headers).

### Forum Pipeline (Current)
*   **Input:** Extension flags `is_forum`, passes cookies plus preloaded assets (base64 content with original/viewer/canonical URLs, queryless variants allowed), optional page ranges (`pages`, `max_pages`).
*   **Pre-seed:** All preloaded assets are decoded once and added to `book_assets` with URL variants so they are available for every page before HTML processing.
*   **Crawl:** Normalize base thread URL; fetch each page (sequentially or explicit range); parse posts; run the forum image processor.
*   **Rewrite:** Build a map from every pre-seeded asset URL (including query-stripped and viewer/canonical variants) to its filename; swap matching `<img>`/attachment URLs in-place. Skip junk (reaction emoji) and prefer preloaded assets over network fetches.
*   **Fallback fetch:** For unmatched attachment URLs, use requests-only with cookies, including queryless retries; avoid aiohttp 409 loops.
*   **Output:** Single-thread chapter with page labels and all distinct attachments embedded; tightened CSS to reduce whitespace around images/posts.
*   **Challenges solved:** MTBR-style attachments used multiple URL forms and 409-protected CDNs. The mapping of URL variants to preloaded assets prevented collapsing every image to the first, and request-only/queryless fallbacks plus multi-page asset fetch ensured coverage across thread pages.

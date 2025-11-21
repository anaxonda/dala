# Web-to-EPUB Downloader (E-Ink Optimized)

Tool to convert web articles, Hacker News threads, Substack posts, and Reddit threads into EPUBs tuned for e-ink devices (Kindle, KOReader, Kobo). A bundled Firefox extension can capture unlocked page HTML from your browser and send it to the local FastAPI server for EPUB generation. Specialized drivers handle site-specific APIs, anti-bot hurdles, and layouts that survive older reader engines.

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
- Supports single articles, HN threads, Substack posts (including custom domains), and Reddit threads (old/new/redd.it).
- Bundle multiple URLs: `uv run web_to_epub.py -i links.txt --bundle --bundle-title "Morning Read"`

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

---

## 🔄 Updates
- **Reddit Driver (new):** Reddit/old.reddit/redd.it links now fetch via the JSON API (`raw_json=1`), render self-posts or linked articles, and include threaded comments with navigation. Works in both CLI and the Firefox extension via the existing FastAPI backend.

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
This enables **Paywall Bypassing**: The extension grabs the *unlocked* HTML from your browser (where you are logged in) and sends it to the server, ensuring the EPUB matches exactly what you see.

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

### Step 3: Using the Extension
*   **Download Page:** Click the extension icon -> "Download Page".
*   **Queue/Bundle:**
    1.  Right-click any link or page -> **"Add to EPUB Queue"**.
    2.  Open extension popup -> Go to **"Queue"** tab.
    3.  Click **"Download Bundle"**.
*   **Add Tabs:** You can bulk-add all open tabs or currently selected (Shift+Click) tabs via the popup.

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

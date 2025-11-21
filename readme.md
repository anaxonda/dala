Here is the comprehensive `README.md` reflecting the current state of the project, its architectural history, and full usage documentation.

***

# Universal Web-to-EPUB Downloader (E-Ink Optimized)

A sophisticated tool to convert web articles, Hacker News threads, and Substack posts into high-quality EPUB files designed specifically for e-ink devices (Kindle, KOReader, Kobo).

This project goes beyond simple HTML scraping. It includes specialized drivers to handle complex APIs, anti-bot protections, and layout optimizations for small, low-refresh-rate screens.

---

## 🏗 Architecture & History
*Why the code looks the way it does.*

### 1. The Driver Pattern
Early in development, it became clear that a "one size fits all" scraper (like `trafilatura` alone) wasn't enough.
*   **Generic Driver:** Uses heuristics to find the main content on standard websites.
*   **Hacker News Driver:** Uses the Firebase API to recursively fetch comment trees, preserving the discussion structure.
*   **Substack Driver:** Reverse-engineered. It hunts for hidden JSON in `window._preloads` to find IDs that aren't in the HTML, handles Cloudflare 403s via headers, and automatically falls back to the native `*.substack.com` API if a custom domain's API is broken (a common issue with Substack).

### 2. The Battle for Layout (E-Ink Optimization)
Formatting for a 30-inch monitor breaks on a 6-inch Kindle.
*   **The "Squashed Text" Problem:** Initially, we used `margin-left` to indent nested comments. By depth 10, the text column was 1 character wide.
    *   *Fix:* We switched to a **Border-based layout**. Instead of pushing text to the right, we add a vertical line on the left. Deep threads flatten visual indentation after Level 5 but keep the lines so you can track the hierarchy.
*   **The "Flexbox" Failure:** Modern CSS (`display: flex`) is ignored by many older e-readers (Kindle's engine is ancient). This caused navigation buttons to wrap onto new lines, wasting vertical space.
    *   *Fix:* We implemented a **CSS Table Layout**. This forces the username and buttons to stay on a single line, truncating the username if necessary.

### 3. Navigation (The "Cluster")
Reading threaded conversations linearly is difficult.
*   *Solution:* We inject a **Navigation Cluster** (`↑ → ⏮ ⏭`) into every comment header.
    *   These are internal anchor links calculated during a pre-processing pass of the comment tree.
    *   They allow jumping to the **Parent**, the **Next Sibling** (skipping the current argument), or the **Next Root Thread**.

### 4. Image Handling & The "Picture" Problem
*   **Lazy Loading:** Sites like NYTimes use `data-src` or complex `srcset` attributes, leaving `src` as a 1x1 pixel.
    *   *Fix:* The script uses a "Trust but Verify" logic. It checks `src` first; if it's a placeholder (junk), it scans `data-src` and `srcset` for the high-res version.
*   **The `<picture>` Trap:** E-readers often prioritize the `<source>` tag inside a `<picture>` element, which points to a remote URL. Even if we downloaded the image locally, the reader would try (and fail) to load the remote one.
    *   *Fix:* The script "unwraps" images. It deletes the `<picture>` and `<source>` tags, leaving only the standard `<img>` pointing to the local file inside the EPUB.

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

## 💻 CLI Usage

### Basic Command
```bash
uv run web_to_epub.py [URL]
```

### Options & Flags

| Flag | Description |
| :--- | :--- |
| `--bundle` | **Anthology Mode.** Combines all input URLs into a single EPUB file with a nested Table of Contents. |
| `--bundle-title "Name"` | Sets the title for the bundle. If omitted, defaults to "Domain - Date". |
| `--no-comments` | Downloads the article text only. Skips API calls for comments. |
| `--no-article` | Downloads the comments only (useful for HN discussions where the link is just context). |
| `--no-images` | Text-only mode. Significantly reduces file size. |
| `-a`, `--archive` | Force-fetch content from the **Wayback Machine** (useful for dead links). |
| `-i [file.txt]` | Input file containing a list of URLs (one per line). |
| `--max-depth [N]` | (HN Only) Limit comment recursion depth. |
| `--css [file.css]` | Inject custom CSS to override default e-reader styling. |
| `-v` | Verbose logging (debug mode). |

### Examples

**1. The "Morning Digest" Bundle**
Create a text file `links.txt` with 5 URLs. Run:
```bash
uv run web_to_epub.py -i links.txt --bundle --bundle-title "Morning Read - Nov 20"
```
*Result:* A single file `Morning_Read_-_Nov_20.epub` with a TOC listing all articles.

**2. Hacker News (Deep Dive)**
Download a thread, including the linked article and all comments.
```bash
uv run web_to_epub.py "https://news.ycombinator.com/item?id=123456"
```

**3. Substack (Custom Domain)**
```bash
uv run web_to_epub.py https://www.astralcodexten.com/p/the-bloomers-paradox
```
*Note:* The script automatically detects the custom domain, finds the hidden ID, and uses the native API to fetch comments.

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

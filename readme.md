# Web-to-EPUB Downloader (E-Ink Optimized)

A powerful tool to convert web articles, Hacker News threads, Substack posts, Reddit threads, and forums into clean, e-ink optimized EPUBs (Kindle, Kobo, KOReader).

It solves the "read later" problem for complex content:
- **Threaded Discussions:** Recursively fetches comments (HN, Reddit) and nests them with "next/previous" navigation buttons.
- **Paywalls & Forums:** A bundled browser extension (Firefox/Chrome) captures your active session (cookies/HTML) to download gated content or forum attachments.
- **E-Ink Optimization:** Flattens layouts, removes sidebars, and ensures high-contrast readability on small E-Ink screens.

<img src="firefox_extension/icon.png" alt="Extension Icon" width="72" />

## ✨ Features
- **Multi-Site Support:** Specialized drivers for Hacker News, Reddit, Substack, XenForo, WordPress, and a robust Generic driver for everything else.
- **Anthology Mode:** Bundle multiple URLs (e.g., "Morning Reads") into a single EPUB with a nested Table of Contents.
- **Anti-Bot Evasion:** Smart fallbacks to `requests`, "Fail Fast" logic for 403s, and optional Wayback Machine integration for dead links.
- **Deep Content:** Hydrates Next.js apps (like WaPo) to find images hidden in JSON, and fetches full-resolution images from proxy URLs.

---

## 🚀 Installation

### Prerequisites
- Python 3.8+
- [uv](https://github.com/astral-sh/uv) (Recommended) or `pip`

### 1. Backend Setup
Clone the repository and run the server.

**Using `uv` (Fastest, handles venv):**
```bash
# Run the server directly
uv run server.py
```

**Using `pip`:**
```bash
pip install -r requirements.txt # (You may need to generate this or install manually: requests aiohttp beautifulsoup4 EbookLib trafilatura lxml pygments tqdm Pillow uvicorn fastapi)
python server.py
```

### 2. Browser Extension Setup
The extension allows one-click downloading and handles session-based scraping (essential for paywalls or private forums).

**Firefox:**
1.  Type `about:debugging` in the address bar -> "This Firefox".
2.  Click **"Load Temporary Add-on..."**.
3.  Select `manifest.json` inside the `firefox_extension/` folder.
    *   *Or install the signed `.xpi` from the [Releases](https://github.com/yourusername/dala/releases) page.*

**Chrome / Brave / Edge:**
1.  Go to `chrome://extensions` and enable **Developer Mode** (top right).
2.  Click **"Load unpacked"**.
3.  Select the `extension_chrome/` folder.

---

## 📖 Usage

### CLI Usage
The CLI is perfect for batch processing or automation.

```bash
# Single URL
uv run web_to_epub.py "https://news.ycombinator.com/item?id=123456"

# Bundle multiple URLs from a file
uv run web_to_epub.py -i links.txt --bundle --bundle-title "Weekly Digest"
```

### Options & Flags

| Flag | Description |
| :--- | :--- |
| `--bundle` | Combine all inputs into one EPUB. |
| `--bundle-title "Title"` | Title for the bundle (defaults to Domain - Date). |
| `--no-comments` | Skip comments (article text only). |
| `--no-article` | Skip article (comments only). |
| `--no-images` | Text-only mode (smaller file size). |
| `-a`, `--archive` | Force fetch from Wayback Machine. |
| `--max-depth [N]` | Limit comment recursion depth. |
| `--pages 1,3-5` | Fetch specific forum pages. |
| `-v` | Verbose logging (debug mode). |

### Extension Usage
1.  Ensure `uv run server.py` is running.
2.  **Download Page:** Click the extension icon or use the Right-Click menu -> "Download Page to EPUB".
3.  **Queue:** Right-click links to "Add to EPUB Queue". Open the popup to view/edit the queue and "Download Bundle".

---

## ⚙️ Systemd Auto-Start (Linux)

To keep the server running in the background:

1.  Create `~/.config/systemd/user/epub-server.service`:
    ```ini
    [Unit]
    Description=Web to EPUB Server
    After=network.target

    [Service]
    WorkingDirectory=/path/to/dala
    ExecStart=/path/to/uv run server.py
    Restart=always

    [Install]
    WantedBy=default.target
    ```
2.  Enable it:
    ```bash
    systemctl --user enable --now epub-server
    ```

---

## 🏗 Architecture

The project uses a **modular driver pattern**:
- **`dala/drivers/`**: Site-specific logic (HN, Reddit, Forum, etc.).
- **`dala/core/`**: Shared logic for text extraction, image processing, and EPUB generation.
- **Extensions**: act as "Thin Clients", injecting scripts to scrape DOM/Cookies and sending payloads to the Python backend for heavy lifting (EPUB building).

See [CHANGELOG.md](CHANGELOG.md) for recent updates and [TODO.md](TODO.md) for the roadmap.
# dala: Web-to-EPUB Downloader (E-Ink Optimized)

**dala** is a specialized tool to convert complex web content—threaded discussions, forums, transcripts, and paywalled articles—into clean, e-ink optimized EPUBs. Koreader recommended.

It solves the "read later" problem for the messy web:
*   **Hacker News & Reddit:** Recursively fetches nested comments and adds a clickable "navigation cluster" to every post, making it possible to read deep threads on an e-reader.
*   **Forums (XenForo, etc.):** Uses a browser extension to capture your login session, downloading gated attachments and high-res images that CLI tools miss.
*   **YouTube:** Downloads transcripts and optionally uses AI to format them into readable articles or generate summaries.
*   **Paywalls:** The extension shares your browser's cookies with the backend to access subscriber-only content (Substack, newspapers).

<img src="firefox_extension/icon.png" alt="dala icon" width="72" />

---

## ⚡ Quick Start

1.  **Get the Code:**
    ```bash
    git clone https://github.com/anaxonda/dala.git
    cd dala
    ```

2.  **Start the Server:**
    *(Requires [uv](https://github.com/astral-sh/uv) or Python 3.8+)*
    ```bash
    # This automatically installs dependencies and runs the backend
    uv run server.py
    ```

3.  **Install the Extension:**
    *   **Firefox:** Signed XPI provided in releases (otherwise load from about:debugging)
    *   **Chrome/Brave:** Go to `chrome://extensions` -> Enable **Developer Mode** -> **Load unpacked** -> Select `extension_chrome/` folder.

4.  **Download:**
    Navigate to a page (e.g., a Hacker News thread), click the **dala** icon, and hit **"Download Page"**. The EPUB will be generated in the project folder (or `Downloads` via the extension).

---

## 📖 Usage Guide

### 1. The Browser Extension (Recommended)
The extension is the primary way to use **dala**. It acts as a "Thin Client," capturing the current page's HTML and your session cookies, then sending them to the local Python server for processing.

*   **Single Page:** Click the icon -> "Download Page".
*   **Queue / Bundle:** Right-click multiple links and select **"Add to EPUB Queue"**. Open the popup to manage the queue and click **"Download Bundle"** to merge them into a single "Anthology" EPUB.

### 2. Command Line Interface (CLI)
For batch processing or automation, use the CLI directly. Doesn't work as well as the browser extension as it can't use already loaded content.

```bash
# Single URL
uv run main.py "https://news.ycombinator.com/item?id=123456"

# Bundle from a file (one URL per line)
uv run main.py -i links.txt --bundle --bundle-title "Weekly Digest"
```

### 3. Drivers & Features

#### 💬 Threaded Discussions (Hacker News / Reddit)
Reading nested comments on an e-reader is usually painful. **dala** flattens the layout and inserts a **Navigation Cluster** into every comment header:
> `↑ Parent` | `→ Next Sibling` | `⏮ Thread Root` | `⏭ Next Thread`

This allows you to skip boring branches or jump back up the tree easily using the touchscreen.

#### 🔐 Forums & Paywalls
Many forums (like XenForo) hide attachments or high-res images from guests. CLI tools fail here.
*   **How to use:** Log in to the site in your browser. Use the **Extension** to download with 'use site cookies option' (there is also the 'force forum driver' option it it is not downloading correctly)
*   **How it works:** The extension sends your cookies to the backend, allowing it to fetch gated images and attachments as *you*. 

#### 📺 YouTube Transcripts & AI
Convert videos into readable text.
*   **Basic (No LLM required):** `uv run main.py [YouTube URL]`. This fetches the raw transcript and uses timestamp gaps to create basic paragraphs.
*   **AI Formatting:** Use `--llm` to have an AI (Gemini/GPT) fix punctuation, capitalization, and remove filler words ("um", "uh"). The content remains the same but reads like a professionally edited article.
*   **AI Summary:** Use `--summary` to insert a 3-5 paragraph "Executive Summary" at the top of the EPUB.

**Setup for AI:**
Set your API key in a `.env` file or pass it via CLI. **dala** supports Google Gemini (free tier works great), OpenAI, and OpenRouter.
```bash
export GEMINI_API_KEY="AIzaSy..."
# OR
uv run main.py [URL] --llm --api-key "AIzaSy..."
```

---

## ⚙️ Extension Options Explained

| Option | What it does | When to use it |
| :--- | :--- | :--- |
| **Use Site Cookies** | Sends your browser login tokens to the backend. | Paywalled articles (Substack, WaPo), private blogs, and forum attachments. |
| **Force Forum Driver** | Triggers multi-page crawling and attachment scraping. | XenForo, vBulletin, or any threaded discussion board. |
| **Internet Archive** | Forces the backend to fetch the URL from the Wayback Machine. | Dead links, paywalls that block the scraper, or when the live site is broken. |

*Note: For forums, you usually need **both** enabled to download full-resolution attachments.*

### 🏛️ Internet Archive Fallback
**dala** tries to be resilient:
1.  **Automatic:** If a live fetch fails (404 Not Found, 403 Forbidden), it **automatically** falls back to the Internet Archive (Wayback Machine) to find the latest snapshot.
2.  **Manual:** You can force this behavior if you know a link is dead or want to view an older version:
    *   **Extension:** Check the "Internet Archive" box in the popup before downloading.
    *   **CLI:** Add the `-a` or `--archive` flag.

---

## 🎨 Advanced Customization (`sites.yaml`)
You can define custom extraction rules for specific websites in a `sites.yaml` file in the project root. This is useful for stubborn sites with weird layouts.

**Example `sites.yaml`:**
```yaml
- name: "The New York Times"
  domains:
    - "nytimes.com"
  content_selector: "article#story"  # Only extract text from this ID
  remove:                            # Delete these elements before generating EPUB
    - "#top-wrapper"
    - ".ad-container"
    - "div[data-testid='recirculation']"
```
*   **content_selector:** CSS selector to pinpoint the main article text (ignores everything else).
*   **remove:** List of CSS selectors to strip out (ads, sidebars, "read more" links).

---

## 🛠 Detailed Installation

### Prerequisites
*   **Python 3.8+**
*   **uv** (Highly recommended for zero-config dependency management):
    *   **macOS (Homebrew):** `brew install uv`
    *   **Windows (PowerShell):** `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
    *   **Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`

### 💻 Platform Specifics

#### macOS
1.  Open **Terminal**.
2.  Install `uv`: `brew install uv`
3.  Clone and run: `git clone ... && cd dala && uv run server.py`
4.  *Note:* macOS may prompt you to install "Command Line Tools" if you don't have Git installed.

#### Windows
1.  Open **PowerShell** (as Administrator).
2.  Install `uv`: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
3.  Close and reopen PowerShell to refresh your PATH.
4.  Run the server: `uv run server.py`
5.  *Troubleshooting:* If you see a "Execution Policy" error, run `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` then try again.

#### Linux
Standard installation as described in the [Quick Start](#-quick-start). For background execution, use the [Systemd guide](#-systemd-auto-start-linux).

### Backend Setup (Alternative: PIP)
If you prefer not to use `uv`, you can use standard Python virtual environments:
```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Systemd Auto-Start (Linux)
To keep the server running in the background automatically:

1.  Create `~/.config/systemd/user/dala.service`:
    ```ini
    [Unit]
    Description=dala EPUB Server
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
    systemctl --user enable --now dala
    ```

---

## ⚙️ Configuration

### Environment Variables (`.env`)
Create a `.env` file in the root directory to persist settings:
```env
# AI / LLM Keys
GEMINI_API_KEY=AIzaSy...
OPENROUTER_API_KEY=sk-or-v1-...
OPENAI_API_KEY=sk-...

# Default Model (optional)
LLM_MODEL=gemini-1.5-flash
```

### CLI Flags Reference

| Flag | Description |
| :--- | :--- |
| `--bundle` | Combine input URLs into a single anthology EPUB. |
| `--bundle-title "..."` | Set the title for the anthology. |
| `--no-comments` | Download only the article text (skip discussion). |
| `--no-images` | Text-only mode (saves space). |
| `-a`, `--archive` | Force fetch from the Internet Archive (Wayback Machine). |
| `--llm` | Use AI to format/clean text (e.g., transcripts). |
| `--summary` | Generate an AI summary at the beginning. |
| `--forum` | Force usage of the Forum driver (usually auto-detected). |
| `--cookie-file cookies.txt` | Load Netscape-format cookies for CLI authentication. |

---

## 🏗 Architecture
**dala** uses a "Modular Driver" pattern:
*   **`dala/drivers/`**: Contains site-specific logic (e.g., `hn.py`, `reddit.py`, `forum.py`).
*   **`dala/core/`**: Shared logic for text extraction, image processing, and EPUB generation.
*   **`firefox_extension/` & `extension_chrome/`**: Thin clients that handle the "View Source" & authentication part of the pipeline.

---

## 📄 License
[MIT](LICENSE)

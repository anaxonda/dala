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
*   **How to use:** Log in to the site in your browser. Use the **Extension** to download.
*   **How it works:** The extension sends your cookies to the backend, allowing it to fetch gated images and attachments as *you*. Need to select 

#### 📺 YouTube Transcripts & AI
Convert videos into text.
*   **Basic:** `uv run main.py [YouTube URL]` (Fetches transcript).
*   **AI Formatting:** Use `--llm` to have Gemini/GPT clean up the transcript (punctuation, capitalization).
*   **AI Summary:** Use `--summary` to add a 3-5 paragraph executive summary at the start.

**Setup for AI:**
Set your API key in the environment or pass it via CLI:
```bash
export GEMINI_API_KEY="AIzaSy..."
# OR
uv run main.py [URL] --llm --api-key "AIzaSy..."
```

---

## 🛠 Detailed Installation

### Prerequisites
*   **Linux/macOS/Windows**
*   **Python 3.8+**
*   **uv** (Highly recommended for zero-config dependency management):
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

### Backend Setup
If you don't use `uv`, you can install dependencies manually using `pip`:
```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
# Note: You may need to manually install 'youtube-transcript-api', 'fastapi', 'uvicorn', etc. if not in requirements.
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

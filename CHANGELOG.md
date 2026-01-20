# Changelog

## 🔄 Updates

### Keyboard Shortcuts & Feedback
- Fixed "Queue shortcut failed" error toast in Firefox by properly handling message responses.
- Fixed missing toast notifications for keyboard shortcuts in Chrome by implementing robust response handling.
- Added immediate visual feedback ("Starting EPUB download...", "Added to EPUB queue") for native keyboard shortcuts (Ctrl+Shift+E / Ctrl+Shift+Q) in both browsers, ensuring feedback even if the browser consumes the key event.

### YouTube Improvements
- **Periodic Thumbnails:** Added an option to embed 3 periodic thumbnails (at ~25%, 50%, 75% timestamps) into YouTube transcripts for visual context. Toggleable via the extension popup ("Video Thumbnails").

### Extension Reliability & Pipeline Alignment
- **Unified Data Context:** Refactored `background.js` to ensure keyboard shortcuts and context menu actions invoke the same data-gathering logic as the popup UI. Specifically, they now check the `include_cookies` option and fetch `browser.cookies.getAll()` for the target URL, transmitting the session state to the backend. This enables authenticated scraping (e.g., paywalls) via shortcuts which previously failed due to missing cookies.
- **Mitigating Background Throttling:** Bundle downloads previously hung on background tabs due to browser resource throttling of `executeScript` callbacks. Implemented a `Promise.race` wrapper around script injection with a 5-second timeout. If a background tab is unresponsive, the extension now fails soft, logging a timeout and proceeding to the next URL (falling back to server-side scraping) instead of stalling the entire queue.
- **Zombie/Discarded Tab Handling:** Added explicit checks for `tab.discarded` in `popup.js`. The extension now skips client-side DOM injection for suspended/unloaded tabs immediately, avoiding API errors and deadlocks, and relying on the backend to fetch the content freshly.

### Download Reliability
- **Browser Download Retry:** If the browser fails to save the file with the specific filename (e.g. due to invalid characters), the extension now automatically retries with a safe, generic filename (`web_to_epub_export.epub`).
- **Error Notifications:** If download fails completely, a desktop notification now shows the exact error message from the browser API.
- **Server-Side Backup:** The server now logs the full path of the generated EPUB in `/tmp` (e.g., `✅ Generated EPUB at: /tmp/tmpAbCdEf.epub`). If the browser download fails, the file can still be recovered from the server's temporary directory.

### Recursive Comment Fetching
- **HN + Source Comments:** When a Hacker News post links to a supported site (e.g. Substack), the downloader now fetches *both* the HN comments AND the original article's native comments.
- **Unified TOC:** The Table of Contents is structured with the Article at the top, and both comment threads (dynamically labeled, e.g., "Substack Comments" and "HN Comments") nested as children for easy navigation.

### Washington Post images
- Extract origin URLs from `imrs.php` proxies and try those first, so WaPo images download reliably.
- If no images survive extraction, parse `__NEXT_DATA__` and inject the listed images into the article body.
- Safer image wrapping tolerates detached tags; `LOGLEVEL=DEBUG` now shows per-image candidates/fetches when needed.
- Added missing `parse_srcset_with_width`, configurable logging, and origin/`__NEXT_DATA__` handling to unblock image downloads and document the WaPo improvements.

### UI & queue polish
- Popup queue is now an editable textarea: paste/edit URLs directly; changes auto-save and the queue persists across downloads.
- Added context-menu action “Download Page to EPUB” for immediate conversion of the current page/link; queue remains untouched.

### Metadata consistency
- Unified article meta block across drivers (Article Source/Author/Date/Site; archive notice where applicable). HN/Reddit external articles now show consistent metadata alongside thread context.

### Image cleanup
- Generic image processing strips placeholders (grey-placeholder), flattens wrappers, dedupes captions, and enforces `<div class="img-block"><img class="epub-image">` with a single caption when present.
- Figures/spans are unwrapped; duplicate captions removed.

### Reddit media fixes
- Image-link posts (`i.redd.it/...png`) render as the article with embedded images.
- Comment image links are inlined and fetched (skipping non-file wiki pages).

### Wikimedia fetch tuning
- Wikimedia images fetched with Commons referer + project UA; targeted fallbacks added and logging when blocked (ongoing).

### Forum image HTML simplification
- **Problem:** XenForo lightbox markup (`lazyloadPreSize`, `lbContainer*`, zoomer stubs, `data-lb-*`, `data-zoom-target`, empty `title`) leaked into the EPUB, leaving nested wrappers and non-reader-safe attributes.
- **Fix:** Added forum-specific cleanup in `ForumImageProcessor` to strip lightbox attrs, unwrap XenForo containers, and remove zoomer divs while still running the same asset mapping/dedup logic.
- **Result:** EPUB now emits minimal image HTML (`<div class="img-block"><img class="epub-image" src="..."></div>` plus caption when found) without breaking filename mapping or preload reuse.

### Forum attachment reliability
- **What was broken:** Page fragments (`#replies`) kept every fetch on page 1; popup closed before fetch finished; discovery crashed on lightbox nodes without `closest`; “enough assets” skipped re-fetch when only avatars/1x1s were present; 409/redirect fetches aborted; attachments on page 2/3 never entered the preload map, causing misses/dupes.
- **Fixes applied:** Strip fragments before building `page-N` URLs so pages 2/3 load; move all asset fetching to background after the popup closes; always re-fetch/merge assets and dedupe by URL instead of early-skipping; filter strictly to `/attachments/`, skip avatars/1x1/data GIFs; expand lightbox selectors (`[data-lb-*]`, `.bbImage`, `a.attachment`, `[data-attachment-id]`) with guards for missing `closest`; binary fetch uses `cache: reload` and retries query-stripped URLs on 409/opaque redirects. Result: all post attachments across pages are discovered and reused without duplication.

### Reddit Driver
- Reddit/old.reddit/redd.it links now fetch via the JSON API (`raw_json=1`), render self-posts or linked articles, and include threaded comments with navigation. Works in both CLI and the Firefox extension via the existing FastAPI backend.

### Forum Driver & Attachments
- Forum threads (e.g., XenForo) now support page ranges, asset preloading from the browser, and external images. The Firefox extension can fetch gated attachments with your session cookies and embed them into EPUBs.

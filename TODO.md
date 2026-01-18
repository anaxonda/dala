# Todo & Roadmap

- support wordpress comments, ex site: https://caseyhandmer.wordpress.com/2025/11/26/antimatter-development-program/
- refactor code, see note (gemini?)
- per site rules
- should run in background not need popup open if many tabs are bundled
- support crawling certain number of link hierarchy
- sign extension
- keyboard shortcut add selected to bundle
- right click menu to add selected to bundle
- keyboard shortcut to download bundle
- headless mode, better for rss?
- port to chrome extension 
- on koreader siden plugin for saving links to special file? or could just parse highlights later.  
- for youtube links: fetch transcript, optional formatting, optional summary?
- for any link summary option

## Other Ideas

### 1. Automated Testing Suite
* **Problem:** The codebase relies heavily on manual testing. As we've seen (e.g., with the Next.js recursion issue), changes can easily introduce regressions or unexpected behavior on specific sites.
* **Suggestion:** Implement a basic test suite using pytest.
    * Unit Tests: Test individual components like `_extract_origin_from_proxy`, `_clean_soup`, and `_seed_images_from_nextjs_data` with mocked inputs (HTML/JSON strings).
    * Integration Tests: Use `aioresponses` or a mock server to simulate site responses (including 403s, Timeouts, and specific HTML structures like WaPo) and verify that the drivers (Generic, HN, Reddit) behave as expected (retry logic, failover, extraction).
    * Snapshot Testing: Save "known good" HTML inputs for key sites (WaPo, Substack, HN) and ensure the extractor output matches a stored "golden" output. This detects layout breakages.

### 2. Refactor `GenericDriver` Complexity
* **Problem:** `GenericDriver` handles too much: standard scraping, Next.js hydration, fallback image injection, and cleanup. It's becoming a "God Class".
* **Suggestion:** Split the responsibilities.
    * Create a `ContentEnhancer` or `HydrationManager` class responsible for detecting and applying special handling (like `__NEXT_DATA__` seeding).
    * Create a dedicated `ImageInjector` class to handle the logic of "finding placeholders vs appending".
    * `GenericDriver` would then orchestrate these smaller, testable components.

### 3. Configurable Site Profiles
* **Problem:** Site-specific logic (like `imrs.php` handling, though generalized now) is hardcoded or relies on heuristics.
* **Suggestion:** Move site-specific configurations (proxy patterns, content selectors, anti-bot rules) into a configuration file (YAML/JSON) or a `SiteProfile` class registry.
    * Example: A profile for `washingtonpost.com` could define `proxy_pattern: "imrs.php"`, `nextjs_hydration: true`, `fail_fast: true`.
    * This makes adding support for new problematic sites easier without modifying core logic.

### 4. Unified "Context" Object
* **Problem:** Data like cookies, raw_html, assets, and options are passed around as loose arguments to many functions (`prepare_book_data`, `process_images`, `seed...`).
* **Suggestion:** Encapsulate this request context into a richer `ConversionContext` object that flows through the pipeline. This makes function signatures cleaner and easier to extend (e.g., adding headers or proxy settings later wouldn't require changing every function signature).

### 5. Better Progress Feedback
* **Problem:** Long operations (like the initial 5-minute timeout we saw) give little feedback to the user via the extension (just "Processing...").
* **Suggestion:** Implement a WebSocket or Server-Sent Events (SSE) endpoint for the server. The extension could listen to this to display real-time progress bars ("Fetching images: 3/10", "Retrying...", "Using Archive fallback"). This greatly improves perceived performance and troubleshooting.

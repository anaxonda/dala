# Todo & Roadmap

This file tracks current work. Older items that are now implemented have been moved to the completed section so they do not keep steering new work in the wrong direction.

## Current Priorities

- Move remaining request preparation and download edge cases fully into extension background flows where practical, then simplify popup responsibilities to UI state and commands.
- Persist server job metadata if restart-safe downloads become important. `/jobs` state and temporary download paths are currently process-local and disappear on restart.
- Add a server-side cleanup policy for temporary files created by direct `/convert` responses.
- Keep package and extension versions in sync. `pyproject.toml` currently reports the Python package version, while extension manifests carry their own version.
- Expand automated regression coverage for image extraction and site-specific HTML:
  - `_extract_origin_from_proxy`
  - `_clean_soup`
  - `_seed_images_from_nextjs_data`
  - forum attachment and quote-link rewriting
  - archive fallback paths
- Add representative fixture-based tests for brittle sites and layouts instead of relying only on live manual runs.
- Improve progress reporting from server jobs to the extension. Polling exists today; Server-Sent Events or WebSockets could provide richer phase-level updates.

## Feature Ideas

- Support crawling a limited link hierarchy from a root URL, with guardrails for same-domain and main-content-only links.
- Add RSS/feed ingestion for building periodic bundles.
- Extend date-range discovery with more site-specific archive/feed patterns.
- Add HN/Reddit filtering for index/subreddit crawling, such as points, dates, or comment counts.
- Add Markdown output mode.
- Explore a KOReader-side workflow for saving links into a file that `dala` can later bundle.
- Sign and publish extension builds through the normal browser-store/release process.

## Completed / Implemented

- WordPress article and comment extraction.
- Translation output with LLM/Google providers, underneath/side-by-side/popup-footnote displays, caption/list support, scopes, caching, and glossary term preservation.
- Configurable site profiles via `sites.yaml`.
- Opt-in Playwright Chromium browser capture for CLI use on JavaScript-heavy pages and authenticated sessions.
- Browser test harness with local fixtures and skippable Playwright integration coverage.
- Bundle image filename remapping/deduplication to avoid collisions across many source articles.
- Shared image budget presets and pre-write budget failures for image-heavy bundles.
- Shared `ConversionContext` object for sessions, options, and profiles.
- Chrome/Brave/Edge extension port using Manifest V3.
- Chrome MV3 server-side HTML parsing helper at `/helper/extract-links`.
- Background extension download/job flow after popup initiation.
- Keyboard shortcuts for download and queue actions.
- Popup actions for selected-tab and all-tab queue import.
- Context menu actions for queueing and downloading pages.
- YouTube transcript fetching, optional LLM formatting, optional summaries, thumbnails, and comment download.
- AI summary option for generic articles, discussions, forums, and transcripts.
- Formal pytest suite covering unit behavior, server endpoints, saving behavior, drivers, and HN delegation.
- Ignored common generated/local artifacts including extension packages, `web-ext-artifacts/`, exports, logs, screenshots, and local `config/bpc/` helper files.
- Server browser fallback diagnostics via `/ping`, extension Options controls for fallback/BPC path, and local BPC unpacked extension validation.
- Extension server URL setting for local, LAN, or remote dala backends.
- Options diagnostics panel for server version, browser fallback status, retained jobs, and last conversion errors.
- Server-side finished job cleanup with configurable retention and interval.
- Extension retry flow for requeueing failed sources from the most recent job.
- Date-range post discovery and bundle generation for blog/archive pages.
- PDF output mode with document and e-reader presets.
- Browser challenge policy: default archive fallback, optional user-browser retry, and explicit warm-browser mode.
- Dedicated Dala Chromium profile default for server browser fallback, with extension UI to initialize the profile.
- Normalized package-relative Python imports.

## Maintenance Notes

- Prefer adding focused tests with local HTML/JSON fixtures when changing extraction heuristics.
- Keep real-network manual tests for extension round trips, gated forums, cookies, and image downloads.
- Avoid committing generated EPUBs, packaged extensions, logs, caches, and screenshots unless they are intentionally part of documentation or a release.

import argparse
import subprocess
import sys
from typing import List, Optional

from .core.browser import is_playwright_available, resolve_browser_executable


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up Dala headless browser support")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check headless browser support without installing Playwright-managed Chromium",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip the final Playwright browser launch check",
    )
    return parser.parse_args(argv)


def install_playwright_chromium() -> int:
    return subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    ).returncode


def verify_browser_launch(executable_path: Optional[str]) -> int:
    script = f"""
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    kwargs = {{"headless": True}}
    executable = {executable_path!r}
    if executable:
        kwargs["executable_path"] = executable
    browser = p.chromium.launch(**kwargs)
    browser.close()
"""
    return subprocess.run([sys.executable, "-c", script], check=False).returncode


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    existing_browser = resolve_browser_executable()

    if existing_browser:
        print(f"Found Chromium-compatible browser: {existing_browser}")
    else:
        print("No Chrome, Edge, Brave, or Chromium executable was detected.")

    if not is_playwright_available():
        print(
            "Playwright is not installed. Reinstall Dala with headless browser support:\n"
            '  uv tool install --force "dala[browser]"\n'
            "For a Git install before PyPI publishing, use:\n"
            "  uv tool install --force --with playwright git+https://github.com/anaxonda/dala.git"
        )
        return 2

    if existing_browser:
        if args.skip_verify:
            print("Headless browser support is configured. Verification skipped.")
            return 0
        result = verify_browser_launch(existing_browser)
        if result == 0:
            print("Headless browser support verified.")
            return 0
        print("Dala found a browser, but Playwright could not launch it.")
        print("Try installing Playwright-managed Chromium with: dala-setup-browser")
        return result or 1

    if args.check_only:
        print("Headless browser support needs Playwright-managed Chromium. Run: dala-setup-browser")
        return 1

    print("Installing Playwright-managed Chromium...")
    result = install_playwright_chromium()
    if result != 0:
        print("Playwright Chromium install failed.")
        return result

    if args.skip_verify:
        print("Playwright-managed Chromium installed. Verification skipped.")
        return 0

    result = verify_browser_launch(None)
    if result == 0:
        print("Headless browser support verified.")
        return 0
    print("Playwright-managed Chromium was installed, but launch verification failed.")
    return result or 1


if __name__ == "__main__":
    raise SystemExit(main())

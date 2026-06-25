import asyncio
import base64
import importlib.util
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse


class BrowserFetchError(RuntimeError):
    """Raised when browser-backed source acquisition cannot run."""


class BrowserChallengeError(BrowserFetchError):
    """Raised when browser-backed source acquisition reaches an interactive challenge."""

    def __init__(self, url: str, marker: str):
        self.url = url
        self.marker = marker
        super().__init__(
            "Browser was served an anti-bot/challenge page "
            f"({marker}) for {url}. Verification is required in the server browser."
        )


@dataclass
class BrowserFetchOptions:
    extension_path: Optional[str] = None
    profile_dir: Optional[str] = None
    executable_path: Optional[str] = None
    headed: bool = False
    timeout_ms: int = 30000
    wait_until: str = "load"
    settle_ms: int = 1000
    challenge_action: str = "archive"


@dataclass
class BrowserFetchResult:
    url: str
    html: str
    cookies: Dict[str, str]
    assets: list = field(default_factory=list)


DEFAULT_BPC_EXTENSION_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "bpc"
    / "chrome-unpacked"
    / "bypass-paywalls-chrome-clean-master"
)
DEFAULT_BROWSER_PROFILE_DIR = os.path.expanduser(
    os.getenv("DALA_BROWSER_PROFILE_DIR", "~/.local/share/dala/browser-profile")
)

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)


def is_playwright_available() -> bool:
    return (
        importlib.util.find_spec("playwright") is not None
        and importlib.util.find_spec("playwright.async_api") is not None
    )


def browser_executable_exists(executable_path: Optional[str]) -> bool:
    if not executable_path:
        return False
    expanded = Path(executable_path).expanduser()
    return bool(shutil.which(executable_path) or expanded.is_file())


def browser_executable_candidates(env: Optional[Dict[str, str]] = None) -> Iterable[str]:
    env = env or os.environ
    names = [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "microsoft-edge",
        "microsoft-edge-stable",
        "brave-browser",
    ]
    yield from names

    for path in [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ]:
        yield path

    windows_roots = [
        env.get("PROGRAMFILES"),
        env.get("PROGRAMFILES(X86)"),
        env.get("LOCALAPPDATA"),
    ]
    windows_relatives = [
        ("Google", "Chrome", "Application", "chrome.exe"),
        ("Microsoft", "Edge", "Application", "msedge.exe"),
        ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    ]
    for root in windows_roots:
        if not root:
            continue
        for parts in windows_relatives:
            yield str(Path(root, *parts))


def resolve_browser_executable(requested_path: Optional[str] = None) -> Optional[str]:
    candidate = requested_path or os.getenv("DALA_BROWSER_EXECUTABLE")
    if candidate:
        expanded = Path(candidate).expanduser()
        found = shutil.which(candidate)
        if found:
            return found
        if expanded.is_file():
            return str(expanded)
        return None
    for candidate in browser_executable_candidates():
        found = shutil.which(candidate)
        if found:
            return found
        expanded = Path(candidate).expanduser()
        if expanded.is_file():
            return str(expanded)
    return None


BROWSER_CHALLENGE_MARKERS = (
    "geo.captcha-delivery.com",
    "datadome",
    "you have been blocked from the new york times",
    "to continue to the new york times, please confirm that you are human",
    "verification required",
    "slide right to secure your access",
    "please enable js and disable any ad blocker",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
)


def detect_browser_challenge(html: str) -> Optional[str]:
    lowered = (html or "").lower()
    for marker in BROWSER_CHALLENGE_MARKERS:
        if marker in lowered:
            return marker
    return None


def resolve_browser_extension_path(requested_path: Optional[str] = None) -> Optional[str]:
    path = (
        requested_path
        or os.getenv("DALA_BPC_EXTENSION_PATH")
        or os.getenv("DALA_BROWSER_EXTENSION_PATH")
    )
    if path:
        return path
    if (DEFAULT_BPC_EXTENSION_PATH / "manifest.json").is_file():
        return str(DEFAULT_BPC_EXTENSION_PATH)
    return None


def validate_browser_options(options: BrowserFetchOptions) -> None:
    if options.extension_path:
        extension = Path(options.extension_path)
        if not extension.is_dir():
            raise BrowserFetchError(f"Browser extension path does not exist or is not a directory: {options.extension_path}")
        if not (extension / "manifest.json").is_file():
            raise BrowserFetchError(f"Browser extension path does not contain manifest.json: {options.extension_path}")
    if options.profile_dir:
        profile = Path(options.profile_dir)
        if profile.exists() and not profile.is_dir():
            raise BrowserFetchError(f"Browser profile path is not a directory: {options.profile_dir}")
    if options.executable_path and not browser_executable_exists(options.executable_path):
        raise BrowserFetchError(f"Browser executable path does not exist or is not a file: {options.executable_path}")
    if options.wait_until not in {"domcontentloaded", "load", "networkidle"}:
        raise BrowserFetchError(f"Unsupported browser wait mode: {options.wait_until}")
    if options.challenge_action not in {"archive", "user_browser", "warm", "error"}:
        raise BrowserFetchError(f"Unsupported browser challenge action: {options.challenge_action}")
    if options.timeout_ms <= 0:
        raise BrowserFetchError("--browser-timeout-ms must be greater than 0")
    if options.settle_ms < 0:
        raise BrowserFetchError("--browser-settle-ms must be 0 or greater")


def _cookie_applies_to_host(cookie: dict, host: str) -> bool:
    domain = (cookie.get("domain") or "").lstrip(".").lower()
    if not domain:
        return False
    return host == domain or host.endswith(f".{domain}")


def _cookies_for_url(cookies: list, url: str) -> Dict[str, str]:
    host = urlparse(url).hostname or ""
    host = host.lower()
    jar = {}
    for cookie in cookies:
        if _cookie_applies_to_host(cookie, host):
            name = cookie.get("name")
            value = cookie.get("value")
            if name is not None and value is not None:
                jar[str(name)] = str(value)
    return jar


async def fetch_rendered_source(url: str, options: BrowserFetchOptions) -> BrowserFetchResult:
    validate_browser_options(options)

    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserFetchError(
            "Browser mode requires Playwright. Install it with `uv sync --extra browser` "
            "and then run `uv run playwright install chromium`."
        ) from exc

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--lang=en-US",
    ]
    if options.extension_path:
        ext_path = str(Path(options.extension_path).resolve())
        launch_args.extend([
            f"--disable-extensions-except={ext_path}",
            f"--load-extension={ext_path}",
        ])

    temp_profile = None
    user_data_dir = options.profile_dir
    if not user_data_dir:
        temp_profile = tempfile.TemporaryDirectory(prefix="dala-browser-")
        user_data_dir = temp_profile.name

    async def launch_context(playwright, executable_path: Optional[str] = None):
        kwargs = {
            "headless": not options.headed,
            "args": launch_args,
            "locale": os.getenv("DALA_BROWSER_LOCALE", "en-US"),
            "timezone_id": os.getenv("DALA_BROWSER_TIMEZONE", "America/New_York"),
            "user_agent": os.getenv("DALA_BROWSER_USER_AGENT", DEFAULT_BROWSER_USER_AGENT),
            "viewport": {"width": 1365, "height": 900},
        }
        if executable_path:
            kwargs["executable_path"] = executable_path
        else:
            kwargs["channel"] = "chromium"
        return await playwright.chromium.launch_persistent_context(user_data_dir, **kwargs)

    try:
        async with async_playwright() as p:
            try:
                context = await launch_context(p, options.executable_path)
            except PlaywrightError as exc:
                fallback = None
                if not options.executable_path:
                    fallback = resolve_browser_executable()
                if fallback:
                    try:
                        context = await launch_context(p, fallback)
                    except PlaywrightError as fallback_exc:
                        raise BrowserFetchError(
                            "Failed to launch browser mode using both Playwright-managed Chromium "
                            f"and the detected Chromium-compatible browser at {fallback}. "
                            "Run `uv run playwright install chromium`, or pass --browser-executable "
                            "with a working Chrome, Edge, Brave, or Chromium binary."
                        ) from fallback_exc
                else:
                    raise BrowserFetchError(
                        "Failed to launch browser mode. Run `uv run playwright install chromium`, "
                        "install Chrome/Edge/Brave/Chromium, or pass --browser-executable with a working binary."
                    ) from exc

            try:
                await context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    """
                )
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(options.timeout_ms)
                captured_assets = []
                capture_tasks = []
                seen_asset_urls = set()
                max_assets = int(os.getenv("DALA_BROWSER_CAPTURE_MAX_IMAGES", "80"))
                max_asset_bytes = int(float(os.getenv("DALA_BROWSER_CAPTURE_MAX_MB", "24")) * 1024 * 1024)
                captured_bytes = 0

                async def capture_image_response(response):
                    nonlocal captured_bytes
                    if len(captured_assets) >= max_assets:
                        return
                    asset_url = response.url
                    if not asset_url or asset_url in seen_asset_urls or not asset_url.startswith(("http://", "https://")):
                        return
                    request = response.request
                    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if request.resource_type != "image" and not content_type.startswith("image/"):
                        return
                    try:
                        body = await response.body()
                    except Exception:
                        return
                    if not body:
                        return
                    if captured_bytes + len(body) > max_asset_bytes:
                        return
                    seen_asset_urls.add(asset_url)
                    captured_bytes += len(body)
                    filename_hint = os.path.basename(urlparse(asset_url).path) or "image"
                    captured_assets.append({
                        "original_url": asset_url,
                        "canonical_url": asset_url.split("?", 1)[0],
                        "filename_hint": filename_hint,
                        "content_type": content_type or "image/jpeg",
                        "content": base64.b64encode(body).decode("ascii"),
                    })

                def on_response(response):
                    capture_tasks.append(asyncio.create_task(capture_image_response(response)))

                page.on("response", on_response)
                response = await page.goto(url, wait_until=options.wait_until, timeout=options.timeout_ms)
                if options.settle_ms:
                    await page.wait_for_timeout(options.settle_ms)
                if capture_tasks:
                    await asyncio.gather(*capture_tasks, return_exceptions=True)
                html = await page.content()
                challenge_marker = detect_browser_challenge(html)
                if challenge_marker:
                    raise BrowserChallengeError(url, challenge_marker)
                if response and response.status >= 400:
                    raise BrowserFetchError(f"Browser navigation returned HTTP {response.status} for {url}")
                final_url = page.url
                cookies = _cookies_for_url(await context.cookies(final_url), final_url)
                return BrowserFetchResult(url=final_url, html=html, cookies=cookies, assets=captured_assets)
            except PlaywrightTimeoutError as exc:
                raise BrowserFetchError(f"Browser navigation timed out after {options.timeout_ms}ms for {url}") from exc
            finally:
                await context.close()
    finally:
        if temp_profile:
            await asyncio.to_thread(temp_profile.cleanup)

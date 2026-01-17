import os
import asyncio
import aiohttp
import socket
import random
from contextlib import asynccontextmanager
from typing import List, Dict, Optional, Any, Tuple
from aiohttp.resolver import ThreadedResolver
from . .models import log, REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

def load_cookie_file(path: str) -> List[Dict[str, str]]:
    """Parse Netscape cookie file format into a list of dict entries."""
    cookies = []
    if not path or not os.path.exists(path):
        return cookies
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if not line or line.startswith('#'): continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    domain, _, _, _, _, name, value = parts[:7]
                    cookies.append({"domain": domain.lstrip('.'), "name": name, "value": value})
    except Exception as e:
        log.warning(f"Failed to parse cookies file {path}: {e}")
    return cookies

@asynccontextmanager
async def get_session():
    # Use threaded DNS to avoid pycares issues on Termux/Android and force IPv4 where needed
    connector = aiohttp.TCPConnector(
        resolver=ThreadedResolver(),
        ttl_dns_cache=300,
        family=socket.AF_INET
    )
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, connector=connector) as session:
        yield session

async def fetch_with_retry(
    session,
    url,
    response_type='json',
    allow_redirects=True,
    referer=None,
    non_retry_statuses: Optional[set] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    max_retries: int = MAX_RETRIES,
    backoff: float = RETRY_DELAY,
    timeout=None
):
    final_url = url
    for attempt in range(max_retries):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            if referer:
                headers['Referer'] = referer
            if extra_headers:
                headers.update(extra_headers)

            async with session.get(url, allow_redirects=allow_redirects, headers=headers, timeout=timeout or REQUEST_TIMEOUT) as response:
                final_url = str(response.url)

                if response.status == 429:
                    retry_after = int(response.headers.get("Retry-After", 10))
                    wait_time = max(retry_after, backoff * (2 ** attempt))
                    log.warning(f"Rate limit hit (429). Cooling down for {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue

                if non_retry_statuses and response.status in non_retry_statuses:
                    log.warning(f"Non-retryable HTTP {response.status} for {url}")
                    return None, final_url

                if response.status >= 400:
                    if response.status == 404: return None, final_url
                    log.warning(f"HTTP {response.status} for {url}")

                response.raise_for_status()

                if response_type == 'json': return await response.json(), final_url
                elif response_type == 'bytes': return await response.read(), final_url
                elif response_type == 'text': return await response.text(encoding='utf-8', errors='replace'), final_url
                elif response_type == 'headers': return response.headers, final_url
                else: return response, final_url

        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError) as e:
            wait = backoff * (2 ** attempt)
            log.warning(f"Attempt {attempt + 1}/{max_retries} failed for {url}: {e}. Retrying in {wait}s.")
            if attempt + 1 == max_retries: return None, url
            await asyncio.sleep(wait)
        except Exception as e:
            log.error(f"Unexpected error for {url}: {e}")
            if attempt + 1 == max_retries: return None, url
            await asyncio.sleep(backoff * (2 ** attempt))
    return None, url

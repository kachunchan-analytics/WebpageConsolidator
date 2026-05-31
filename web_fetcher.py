#!/usr/bin/env python3
"""
web_fetcher.py - Async HTTP fetchers with anti-scraping fallbacks.
"""

import asyncio
import itertools
import random
from typing import Dict, List, Optional, Iterator
from concurrent.futures import ThreadPoolExecutor
from abc import ABC, abstractmethod

import aiohttp
from aiohttp import ClientTimeout, ClientError
import requests
from requests.exceptions import RequestException

try:
    from fake_useragent import UserAgent
    FAKE_USER_AGENT_AVAILABLE = True
except ImportError:
    FAKE_USER_AGENT_AVAILABLE = False
    UserAgent = None

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


from traceback_logger import TracebackLogger, Status

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
FETCH_TIMEOUT = 10

# ----------------------------------------------------------------------
# Data class for fetch results (improved charset detection)
# ----------------------------------------------------------------------
class FetchResult:
    def __init__(self, url: str, success: bool, content: bytes, content_type: str, text: str = "", error: Optional[str] = None):
        self.url = url
        self.success = success
        self.content = content          # raw bytes
        self.content_type = content_type
        self.text = text                # decoded string (only for text-based content)
        self.error = error

    @classmethod
    def failure(cls, url: str, error: str) -> "FetchResult":
        return cls(url=url, success=False, content=b'', content_type='', text='', error=error)

    @classmethod
    def success(cls, url: str, content: bytes, content_type: str) -> "FetchResult":
        text = cls._decode_if_text(content, content_type)
        return cls(url=url, success=True, content=content, content_type=content_type, text=text, error=None)

    @staticmethod
    def _decode_if_text(content: bytes, content_type: str) -> str:
        if not content_type:
            return ""
        ct_lower = content_type.lower()
        text_types = (
            'text/', 'application/json', 'application/xml', 'application/javascript',
            'application/x-www-form-urlencoded', 'application/rss+xml', 'application/atom+xml'
        )
        if not any(ct_lower.startswith(t) for t in text_types):
            return ""

        # 1. Robust charset extraction from Content-Type header
        charset = None
        try:
            from email.message import Message
            msg = Message()
            msg['content-type'] = content_type
            charset = msg.get_param('charset', None)
        except Exception:
            try:
                import cgi
                _, params = cgi.parse_header(content_type)
                charset = params.get('charset')
            except Exception:
                pass

        # 2. BOM handling (including UTF-8 BOM)
        bom_encodings = [
            (b'\xef\xbb\xbf', 'utf-8-sig'),   # UTF-8 with BOM
            (b'\xff\xfe', 'utf-16-le'),       # UTF-16 LE
            (b'\xfe\xff', 'utf-16-be'),       # UTF-16 BE
            (b'\xff\xfe\x00\x00', 'utf-32-le'),
            (b'\x00\x00\xfe\xff', 'utf-32-be'),
        ]
        for bom, enc in bom_encodings:
            if content.startswith(bom):
                try:
                    return content.decode(enc)
                except UnicodeDecodeError:
                    break   # BOM present but decoding failed, continue to next methods

        # 3. Try charset from Content-Type header
        if charset:
            try:
                return content.decode(charset)
            except (LookupError, UnicodeDecodeError):
                pass

        # 4. Try UTF-8 (no BOM)
        try:
            return content.decode('utf-8')
        except UnicodeDecodeError:
            # 5. Use charset_normalizer as primary detector
            try:
                from charset_normalizer import detect
                detected = detect(content)
                encoding = detected.get('encoding', 'utf-8')
                return content.decode(encoding, errors='replace')
            except ImportError:
                # Fallback to chardet if available
                try:
                    import chardet
                    detected = chardet.detect(content)
                    encoding = detected.get('encoding', 'utf-8')
                    return content.decode(encoding, errors='replace')
                except ImportError:
                    # Last resort: replace undecodable bytes
                    return content.decode('utf-8', errors='replace')

# ----------------------------------------------------------------------
# BaseFetcher
# ----------------------------------------------------------------------
class BaseFetcher(ABC):
    def __init__(self, logger: TracebackLogger, timeout: int = FETCH_TIMEOUT):
        self.logger = logger
        self.timeout = timeout

    def _validate_url(self, url: str) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        if not (url.startswith('http://') or url.startswith('https://')):
            return False
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if not parsed.netloc:
                return False
        except Exception:
            return False
        return True

    @abstractmethod
    async def fetch(self, url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None) -> FetchResult:
        """Fetch a URL asynchronously. Must be implemented by subclasses."""
        pass

# ----------------------------------------------------------------------
# AiohttpFetcher
# ----------------------------------------------------------------------
class AiohttpFetcher(BaseFetcher):
    async def fetch(self, url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None) -> FetchResult:
        if not self._validate_url(url):
            return FetchResult.failure(url, "Invalid URL")

        final_headers = DEFAULT_HEADERS.copy()
        if headers:
            final_headers.update(headers)

        timeout = ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(headers=final_headers, timeout=timeout) as session:
                async with session.get(url, ssl=False, proxy=proxy) as response:
                    if response.status != 200:
                        error_msg = f"HTTP {response.status} for {url}"
                        self.logger.log(Status.FETCH, message=error_msg)
                        return FetchResult.failure(url, error_msg)
                    content = await response.read()
                    content_type = response.headers.get('Content-Type', '').lower()
                    return FetchResult.success(url, content, content_type)
        except asyncio.TimeoutError:
            error_msg = f"Timeout after {self.timeout}s for {url}"
            self.logger.log(Status.TIMEOUT, message=error_msg)
            return FetchResult.failure(url, error_msg)
        except ClientError as e:
            error_msg = f"Client error for {url}: {str(e)}"
            self.logger.log(Status.FETCH, exc=e, message=error_msg)
            return FetchResult.failure(url, error_msg)
        except Exception as e:
            error_msg = f"Unexpected error for {url}: {str(e)}"
            self.logger.log(Status.ERROR, exc=e, message=error_msg)
            return FetchResult.failure(url, error_msg)

# ----------------------------------------------------------------------
# RequestsThreadFetcher
# ----------------------------------------------------------------------
class RequestsThreadFetcher(BaseFetcher):
    def __init__(self, logger: TracebackLogger, timeout: int = FETCH_TIMEOUT):
        super().__init__(logger, timeout)
        self._executor = ThreadPoolExecutor(max_workers=5)

    async def fetch(self, url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None) -> FetchResult:
        if not self._validate_url(url):
            return FetchResult.failure(url, "Invalid URL")

        final_headers = DEFAULT_HEADERS.copy()
        if headers:
            final_headers.update(headers)

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor,
                self._sync_fetch,
                url, final_headers, proxy
            )
            return result
        except Exception as e:
            error_msg = f"Thread executor error for {url}: {str(e)}"
            self.logger.log(Status.ERROR, exc=e, message=error_msg)
            return FetchResult.failure(url, error_msg)

    def _sync_fetch(self, url: str, headers: dict, proxy: Optional[str]) -> FetchResult:
        try:
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            resp = requests.get(url, headers=headers, timeout=self.timeout, verify=False, proxies=proxies)
            if resp.status_code != 200:
                error_msg = f"HTTP {resp.status_code} for {url}"
                self.logger.log(Status.FETCH, message=error_msg)
                return FetchResult.failure(url, error_msg)
            content_type = resp.headers.get('Content-Type', '').lower()
            return FetchResult.success(url, resp.content, content_type)
        except RequestException as e:
            error_msg = f"Requests error for {url}: {str(e)}"
            self.logger.log(Status.FETCH, exc=e, message=error_msg)
            return FetchResult.failure(url, error_msg)
        except Exception as e:
            error_msg = f"Unexpected error in sync fetch for {url}: {str(e)}"
            self.logger.log(Status.ERROR, exc=e, message=error_msg)
            return FetchResult.failure(url, error_msg)

# ----------------------------------------------------------------------
# CurlCffiFetcher
# ----------------------------------------------------------------------
class CurlCffiFetcher(BaseFetcher):
    def __init__(self, logger: TracebackLogger, timeout: int = FETCH_TIMEOUT, impersonate: str = "chrome120"):
        super().__init__(logger, timeout)
        self.impersonate = impersonate
        self._available = CURL_CFFI_AVAILABLE
        if not self._available:
            self.logger.log(Status.ERROR, message="curl_cffi not installed. Install with: pip install curl_cffi")

    async def fetch(self, url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None) -> FetchResult:
        if not self._validate_url(url):
            return FetchResult.failure(url, "Invalid URL")
        if not self._available:
            return FetchResult.failure(url, "curl_cffi not available")

        final_headers = DEFAULT_HEADERS.copy()
        if headers:
            final_headers.update(headers)

        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as session:
                response = await session.get(
                    url,
                    headers=final_headers,
                    timeout=self.timeout,
                    impersonate=self.impersonate,
                    proxy=proxy,
                    verify=False
                )
                if response.status_code != 200:
                    error_msg = f"HTTP {response.status_code} for {url}"
                    self.logger.log(Status.FETCH, message=error_msg)
                    return FetchResult.failure(url, error_msg)
                content_type = response.headers.get('Content-Type', '').lower()
                return FetchResult.success(url, response.content, content_type)
        except Exception as e:
            error_msg = f"curl_cffi error for {url}: {str(e)}"
            self.logger.log(Status.FETCH, exc=e, message=error_msg)
            return FetchResult.failure(url, error_msg)

# ----------------------------------------------------------------------
# HeaderRandomizerFetcher
# ----------------------------------------------------------------------
class HeaderRandomizerFetcher(BaseFetcher):
    def __init__(self, base_fetcher: BaseFetcher):
        super().__init__(base_fetcher.logger, base_fetcher.timeout)
        self.base = base_fetcher
        if FAKE_USER_AGENT_AVAILABLE:
            self.ua = UserAgent()
        else:
            self.ua = None
            self.logger.log(Status.WARNING, message="fake-useragent not installed, using static user agents")
        self.accept_languages = ['en-US,en;q=0.9', 'en-GB,en;q=0.8', 'fr-FR,fr;q=0.9', 'de-DE,de;q=0.9']

    def _random_headers(self) -> Dict[str, str]:
        if self.ua:
            user_agent = self.ua.random
        else:
            static_uas = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
            ]
            user_agent = random.choice(static_uas)

        return {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': random.choice(self.accept_languages),
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        }

    async def fetch(self, url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None) -> FetchResult:
        random_h = self._random_headers()
        if headers:
            random_h.update(headers)
        return await self.base.fetch(url, headers=random_h, proxy=proxy)

# ----------------------------------------------------------------------
# RotatingProxyFetcher
# ----------------------------------------------------------------------
class RotatingProxyFetcher(BaseFetcher):
    def __init__(self, base_fetcher: BaseFetcher, proxy_list: List[str], max_retries: int = 3):
        super().__init__(base_fetcher.logger, base_fetcher.timeout)
        self.base = base_fetcher
        self.proxies = proxy_list
        self._cycle: Iterator[str] = itertools.cycle(proxy_list) if proxy_list else iter([])
        self.max_retries = max_retries

    async def fetch(self, url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None) -> FetchResult:
        if not self.proxies:
            self.logger.log(Status.WARNING, message="No proxies provided, falling back to direct request")
            return await self.base.fetch(url, headers=headers, proxy=None)

        last_error = None
        for attempt in range(self.max_retries):
            proxy_url = next(self._cycle)
            self.logger.log(Status.FETCH, message=f"Attempt {attempt+1} with proxy {proxy_url}")
            result = await self.base.fetch(url, headers=headers, proxy=proxy_url)
            if result.success:
                return result
            error_lower = (result.error or '').lower()
            if '403' in error_lower or '429' in error_lower:
                last_error = result.error
                continue
            last_error = result.error
        return FetchResult.failure(url, f"All proxies failed: {last_error}")


# ----------------------------------------------------------------------
# AsyncUrlFetcher
# ----------------------------------------------------------------------
class AsyncUrlFetcher:
    def __init__(self, logger: TracebackLogger, timeout: int = FETCH_TIMEOUT,
                 anti_scrape: bool = False, proxy_list: Optional[List[str]] = None,
                 overall_timeout: int = 30):
        self.logger = logger
        self.overall_timeout = overall_timeout
        self.fetchers: List[BaseFetcher] = []

        # Always add basic fetchers
        self.fetchers.append(AiohttpFetcher(logger, timeout))
        self.fetchers.append(RequestsThreadFetcher(logger, timeout))

        if anti_scrape:
            if CURL_CFFI_AVAILABLE:
                curl_fetcher = CurlCffiFetcher(logger, timeout)
                self.fetchers.append(curl_fetcher)
                if proxy_list:
                    self.fetchers.append(RotatingProxyFetcher(curl_fetcher, proxy_list))
                # Add only one HeaderRandomizerFetcher (using aiohttp) – avoids duplication
                self.fetchers.append(HeaderRandomizerFetcher(AiohttpFetcher(logger, timeout)))
            else:
                self.logger.log(Status.WARNING, message="curl_cffi not available, anti-scrape capabilities reduced")
                self.fetchers.append(HeaderRandomizerFetcher(AiohttpFetcher(logger, timeout)))

    async def _fetch_one_with_fallback(self, url: str) -> FetchResult:
        """Try all fetchers with an overall timeout using racing behavior."""
        try:
            return await asyncio.wait_for(self._race_fetchers(url), timeout=self.overall_timeout)
        except asyncio.TimeoutError:
            self.logger.log(Status.TIMEOUT, message=f"Overall timeout after {self.overall_timeout}s for {url}")
            return FetchResult.failure(url, f"Overall timeout after fallback chain")

    async def _race_fetchers(self, url: str) -> FetchResult:
        """Run all fetchers concurrently and return the first successful result."""
        if not self.fetchers:
            return FetchResult.failure(url, "No fetchers available")
        
        # Create tasks for all fetchers
        tasks = [asyncio.create_task(fetcher.fetch(url)) for fetcher in self.fetchers]
        pending = set(tasks)
        last_result = None
        
        try:
            while pending:
                # Wait for the first task to complete (success or failure)
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                
                for task in done:
                    result = task.result()
                    if result.success:
                        # Cancel all still pending fetchers
                        for p in pending:
                            p.cancel()
                        # Also cancel any that might be in the done set but not processed
                        # (though this shouldn't happen with FIRST_COMPLETED)
                        self.logger.log(Status.FETCH, message=f"Success from {self._get_fetcher_name(task)} for {url}")
                        return result
                    else:
                        last_result = result
                        self.logger.log(Status.WARNING, 
                                      message=f"Fetcher {self._get_fetcher_name(task)} failed for {url}: {result.error}")
                
                # If we have pending tasks and no success yet, continue waiting
                # (pending may have been updated by the wait call)
            
            # All fetchers failed
            self.logger.log(Status.ERROR, message=f"All fetchers failed for {url}")
            return last_result or FetchResult.failure(url, "All fetchers failed")
            
        except Exception as e:
            # Cancel all tasks on unexpected error
            for task in tasks:
                if not task.done():
                    task.cancel()
            error_msg = f"Unexpected error during racing: {str(e)}"
            self.logger.log(Status.ERROR, exc=e, message=error_msg)
            return FetchResult.failure(url, error_msg)
        finally:
            # Ensure all tasks are cleaned up
            for task in tasks:
                if not task.done():
                    task.cancel()
    
    def _get_fetcher_name(self, task: asyncio.Task) -> str:
        """Extract fetcher class name from a task (for logging)."""
        try:
            # This is a bit hacky - we could store names when creating tasks
            # For now, we'll try to get the coroutine name or return generic
            coro = task.get_coro()
            if hasattr(coro, '__self__'):
                return coro.__self__.__class__.__name__
            return "Unknown"
        except Exception:
            return "Unknown"

    async def fetch_all(self, urls: List[str]) -> List[FetchResult]:
        """Fetch multiple URLs concurrently."""
        tasks = [self._fetch_one_with_fallback(url) for url in urls]
        results = await asyncio.gather(*tasks)
        return results
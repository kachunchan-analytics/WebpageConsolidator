#!/usr/bin/env python3
"""
Console app to fetch URLs asynchronously, extract readable text (HTML only),
and optionally wrap output with backticks and a prompt.
"""

import asyncio
import os
import sys
import re
from typing import List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor

# External libraries
import aiohttp
from aiohttp import ClientTimeout, ClientError
import requests
from requests.exceptions import RequestException
import trafilatura
from bs4 import BeautifulSoup

# Import logger and Status from external module (provided by user)
from traceback_logger import TracebackLogger, Status
from cli_selector import CliSelector

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
OUTPUT_FILENAME = "extracted_content.txt"
FETCH_TIMEOUT = 10  # seconds per request
ADD_PROMPT = True   # enable/disable prompt selection

# Predefined prompts (same as in original)
PROMPT_LIST = [
    "Explain and Summarize the above contents with reference to the materials given. also use mermaid diagram besides the wordy content",
    "Compare and Contrast the above contents",
    "Identify any gaps or missing information in the above content",
    "What real-world applications does this content suggest?",
    "Rewrite this content in your own words",
    "Organize this content as a step-by-step process",
    "What additional topics should I study to complement this?",
    "What historical or contextual background would help understand this?",
    "TL;DR in 2 sentences"
]

# Fake browser headers (to mimic a real browser)
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ----------------------------------------------------------------------
# BaseFetcher (abstract)
# ----------------------------------------------------------------------
class BaseFetcher:
    """Abstract base class for HTTP fetchers."""

    def __init__(self, logger: TracebackLogger, timeout: int = FETCH_TIMEOUT):
        self.logger = logger
        self.timeout = timeout

    def _validate_url(self, url: str) -> bool:
        """LBYL: Check if URL is valid (scheme http/https, non-empty host)."""
        if not isinstance(url, str) or not url.strip():
            self.logger.log(Status.WARNING, message=f"Empty URL provided")
            return False
        if not (url.startswith('http://') or url.startswith('https://')):
            self.logger.log(Status.WARNING, message=f"Unsupported URL scheme (must be http/https): {url}")
            return False
        # Basic host detection (after scheme)
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if not parsed.netloc:
                self.logger.log(Status.WARNING, message=f"Invalid URL (no host): {url}")
                return False
        except Exception:
            return False
        return True

    async def fetch(self, url: str) -> dict:
        """Fetch a URL and return a dict with keys: url, success, content (bytes), content_type, error."""
        raise NotImplementedError

# ----------------------------------------------------------------------
# AiohttpFetcher (primary, async)
# ----------------------------------------------------------------------
class AiohttpFetcher(BaseFetcher):
    """Async fetcher using aiohttp with fake browser headers."""

    async def fetch(self, url: str) -> dict:
        if not self._validate_url(url):
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': 'Invalid URL'}

        timeout = ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(headers=DEFAULT_HEADERS, timeout=timeout) as session:
                async with session.get(url, ssl=False) as response:
                    if response.status != 200:
                        error_msg = f"HTTP {response.status} for {url}"
                        self.logger.log(Status.FETCH, message=error_msg)
                        return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}
                    content = await response.read()
                    content_type = response.headers.get('Content-Type', '').lower()
                    return {
                        'url': url,
                        'success': True,
                        'content': content,
                        'content_type': content_type,
                        'error': None
                    }
        except asyncio.TimeoutError:
            error_msg = f"Timeout after {self.timeout}s for {url}"
            self.logger.log(Status.TIMEOUT, message=error_msg)
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}
        except ClientError as e:
            error_msg = f"Client error for {url}: {str(e)}"
            self.logger.log(Status.FETCH, exc=e, message=error_msg)
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}
        except Exception as e:
            error_msg = f"Unexpected error for {url}: {str(e)}"
            self.logger.log(Status.ERROR, exc=e, message=error_msg)
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}

# ----------------------------------------------------------------------
# RequestsThreadFetcher (fallback, sync in thread)
# ----------------------------------------------------------------------
class RequestsThreadFetcher(BaseFetcher):
    """Fallback fetcher using requests (synchronous) running in a thread pool."""

    def __init__(self, logger: TracebackLogger, timeout: int = FETCH_TIMEOUT):
        super().__init__(logger, timeout)
        self._executor = ThreadPoolExecutor(max_workers=5)

    async def fetch(self, url: str) -> dict:
        if not self._validate_url(url):
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': 'Invalid URL'}

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor,
                self._sync_fetch,
                url
            )
            return result
        except Exception as e:
            error_msg = f"Thread executor error for {url}: {str(e)}"
            self.logger.log(Status.ERROR, exc=e, message=error_msg)
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}

    def _sync_fetch(self, url: str) -> dict:
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=self.timeout, verify=False)
            if resp.status_code != 200:
                error_msg = f"HTTP {resp.status_code} for {url}"
                self.logger.log(Status.FETCH, message=error_msg)
                return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}
            content_type = resp.headers.get('Content-Type', '').lower()
            return {
                'url': url,
                'success': True,
                'content': resp.content,
                'content_type': content_type,
                'error': None
            }
        except RequestException as e:
            error_msg = f"Requests error for {url}: {str(e)}"
            self.logger.log(Status.FETCH, exc=e, message=error_msg)
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}
        except Exception as e:
            error_msg = f"Unexpected error in sync fetch for {url}: {str(e)}"
            self.logger.log(Status.ERROR, exc=e, message=error_msg)
            return {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': error_msg}

# ----------------------------------------------------------------------
# AsyncUrlFetcher (composite with fallback)
# ----------------------------------------------------------------------
class AsyncUrlFetcher:
    """Manages multiple fetchers and falls back if primary fails."""

    def __init__(self, logger: TracebackLogger, timeout: int = FETCH_TIMEOUT):
        self.logger = logger
        self.fetchers: List[BaseFetcher] = [
            AiohttpFetcher(logger, timeout),
            RequestsThreadFetcher(logger, timeout)
        ]

    async def _fetch_one_with_fallback(self, url: str) -> dict:
        """Try each fetcher in order until one succeeds; return last failure if all fail."""
        last_result = None
        for fetcher in self.fetchers:
            result = await fetcher.fetch(url)
            if result.get('success'):
                return result
            last_result = result
            # Log that this fetcher failed, but continue to next
            self.logger.log(Status.WARNING, message=f"Fetcher {fetcher.__class__.__name__} failed for {url}: {result.get('error')}")
        # All failed
        self.logger.log(Status.ERROR, message=f"All fetchers failed for {url}")
        return last_result or {'url': url, 'success': False, 'content': b'', 'content_type': '', 'error': 'All fetchers failed'}

    async def fetch_all(self, urls: List[str]) -> List[dict]:
        """Fetch all URLs concurrently, preserving order."""
        tasks = [self._fetch_one_with_fallback(url) for url in urls]
        results = await asyncio.gather(*tasks)
        return results

# ----------------------------------------------------------------------
# ContentExtractor (HTML only, no PDF)
# ----------------------------------------------------------------------
class ContentExtractor:
    """Extract plain text from HTML content. Skips non-HTML."""

    def __init__(self, logger: TracebackLogger):
        self.logger = logger

    def extract_text(self, content: bytes, url: str, content_type: str) -> str:
        """Return extracted plain text or error message."""
        # LBYL: check content type
        if not content:
            self.logger.log(Status.WARNING, message=f"Empty content for {url}")
            return f"[Empty content for {url}]"

        # Only process HTML
        if 'text/html' not in content_type:
            msg = f"Skipping non-HTML content (type: {content_type}) for {url}"
            self.logger.log(Status.WARNING, message=msg)
            return f"[Non-HTML content skipped: {content_type}]"

        # Decode bytes to string
        try:
            html = content.decode('utf-8')
        except UnicodeDecodeError:
            try:
                html = content.decode('latin-1')
            except Exception as e:
                self.logger.log(Status.ERROR, exc=e, message=f"Decoding failed for {url}")
                return f"[Decoding error for {url}]"

        # Try trafilatura first
        try:
            text = trafilatura.extract(html, include_comments=False, include_tables=True)
            if text and len(text.strip()) > 100:  # reasonable content
                return text.strip()
        except Exception as e:
            self.logger.log(Status.PARSE, exc=e, message=f"Trafilatura failed for {url}, falling back to BeautifulSoup")

        # Fallback to BeautifulSoup
        try:
            soup = BeautifulSoup(html, 'lxml')
            # Remove script/style tags
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            text = soup.get_text(separator='\n', strip=True)
            if text:
                # Collapse multiple newlines
                text = re.sub(r'\n\s*\n', '\n\n', text)
                return text.strip()
        except Exception as e:
            self.logger.log(Status.PARSE, exc=e, message=f"BeautifulSoup extraction failed for {url}")
            return f"[Extraction failed for {url}]"

        if not text:
            self.logger.log(Status.NOTFOUND, message=f"No textual content extracted from {url}")
            return f"[No text extracted from {url}]"
        return text.strip()

# ----------------------------------------------------------------------
# PromptHandler (text only, no PDF)
# ----------------------------------------------------------------------
from typing import List, Optional
from cli_selector import CliSelector   # assuming CliSelector is in cli_selector.py

class PromptHandler:
    MODE_RAW = 0
    MODE_FENCE_ONLY = 1
    MODE_PREDEFINED = 2
    MODE_CUSTOM = 3

    def __init__(self, prompt_list: Optional[List[str]] = None):
        """Initialize with an optional list of predefined prompts."""
        self.prompt_list = prompt_list if prompt_list is not None else []
        self.mode = self.MODE_RAW
        self.selected_prompt = None
        self.selector = CliSelector()   # reusable selector instance

    def _handle_raw_mode(self) -> bool:
        self.mode = self.MODE_RAW
        self.selected_prompt = None
        return False

    def _handle_fence_only(self) -> bool:
        self.mode = self.MODE_FENCE_ONLY
        self.selected_prompt = None
        return True

    def _handle_predefined_prompt(self) -> bool:
        if not self.prompt_list:
            print("No predefined prompts available. Falling back to fence only.")
            return self._handle_fence_only()

        # Build choices as indices (1..N) and display text as prompt strings
        choices = [str(i) for i in range(1, len(self.prompt_list) + 1)]
        display_dict = {str(i): prompt for i, prompt in enumerate(self.prompt_list, start=1)}

        self.selector.set(
            prompt="Select a predefined prompt (enter number):",
            choices=choices,
            display_dict=display_dict
        )
        selected_key = self.selector.ask()   # returns string like "1"
        idx = int(selected_key) - 1

        self.mode = self.MODE_PREDEFINED
        self.selected_prompt = self.prompt_list[idx]
        return True

    def _handle_custom_prompt(self) -> bool:
        print("\nEnter your custom prompt (cannot be empty):")
        while True:
            try:
                custom = input("> ").strip()
                if custom:
                    self.mode = self.MODE_CUSTOM
                    self.selected_prompt = custom
                    return True
                else:
                    print("Prompt cannot be empty.")
            except KeyboardInterrupt:
                print("\nCustom prompt cancelled. Using fence only.")
                return self._handle_fence_only()

    def display_and_select(self) -> bool:
        """Show formatting options using CliSelector and return whether to add fence."""
        # Use CliSelector for the main mode selection
        self.selector.set(
            prompt="Text output formatting options:",
            choices=["0", "1", "2", "3"],
            display_dict={
                "0": "No fence, no prompt (raw text)",
                "1": "Add backticks fence only",
                "2": "Add backticks fence + select a predefined prompt",
                "3": "Add backticks fence + write custom prompt"
            }
        )

        while True:
            choice = self.selector.ask()   # returns "0", "1", "2", or "3"
            mode = int(choice)
            if mode == self.MODE_RAW:
                return self._handle_raw_mode()
            elif mode == self.MODE_FENCE_ONLY:
                return self._handle_fence_only()
            elif mode == self.MODE_PREDEFINED:
                return self._handle_predefined_prompt()
            elif mode == self.MODE_CUSTOM:
                return self._handle_custom_prompt()
            # ask() already validates input, so we shouldn't reach here

    def format_output(self, raw_text: str) -> str:
        """Apply the selected formatting to the raw text."""
        if self.mode == self.MODE_RAW:
            return raw_text
        elif self.mode == self.MODE_FENCE_ONLY:
            return f"```\n{raw_text}\n```"
        elif self.mode in (self.MODE_PREDEFINED, self.MODE_CUSTOM):
            return f"```\n{raw_text}\n```\n{self.selected_prompt}"
        else:
            return raw_text

    def reset(self):
        """Reset to raw mode with no selected prompt."""
        self.mode = self.MODE_RAW
        self.selected_prompt = None

# ----------------------------------------------------------------------
# OutputGenerator
# ----------------------------------------------------------------------
class OutputGenerator:
    def __init__(self, prompt_handler: PromptHandler):
        self.prompt_handler = prompt_handler

    def generate_output(self, items: List[Tuple[str, str]]) -> str:
        """items: list of (url, extracted_text). Builds raw combined string, then applies prompt."""
        if not items:
            return ""
        raw_parts = []
        for url, text in items:
            raw_parts.append(f"--- Source: {url} ---")
            raw_parts.append(text)
            raw_parts.append("")  # blank line between sources
        raw_text = "\n".join(raw_parts).strip()
        return self.prompt_handler.format_output(raw_text)

# ----------------------------------------------------------------------
# ConsoleApp (orchestrator)
# ----------------------------------------------------------------------
class ConsoleApp:
    # ANSI color codes as class attributes
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def __init__(self):
        self.logger = TracebackLogger()
        self.fetcher = AsyncUrlFetcher(self.logger)
        self.extractor = ContentExtractor(self.logger)
        if ADD_PROMPT:
            self.prompt_handler = PromptHandler(PROMPT_LIST, self.logger)
        else:
            self.prompt_handler = None
        self.output_gen = OutputGenerator(self.prompt_handler) if self.prompt_handler else None

    def _get_urls_from_user(self) -> List[str]:
        print(f"{self.YELLOW}Enter URLs (one per line). Press Ctrl+D (Unix) or Ctrl+Z+Enter (Windows) when done:{self.RESET}")
        try:
            lines = sys.stdin.read().splitlines()
        except (KeyboardInterrupt, EOFError):
            return []
        
        urls = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith(('http://', 'https://')):
                urls.append(line)
            else:
                print(f"{self.RED}Invalid URL (must start with http:// or https://): {line}{self.RESET}")
                self.logger.log(Status.WARNING, message=f"User skipped invalid URL: {line}")
        
        if urls:
            print(f"{self.YELLOW}--[Start scraping]--{self.RESET}")
        return urls

    def _print_scraping_report(self, fetch_results: List[dict], extracted_items: List[Tuple[str, str]]) -> None:
        """Print a colored report showing which URLs succeeded and which failed."""
        print(f"\n{self.BOLD}{self.BLUE}=== Scraping Report ==={self.RESET}")
        
        # Count successes and failures
        success_count = len(extracted_items)
        total_count = len(fetch_results)
        fail_count = total_count - success_count
        
        # Print each result (just ✓ or ✗ and URL, no extra text)
        for res in fetch_results:
            if res['success']:
                print(f"  {self.GREEN}✓{self.RESET} {res['url']}")
            else:
                print(f"  {self.RED}✗{self.RESET} {res['url']}")
        
        # Print summary (plain text, no colors)
        print(f"\nSummary:")
        print(f"  Success: {success_count}")
        print(f"  Failed: {fail_count}")
        print(f"  Total: {total_count}")
        
        if fail_count > 0:
            print(f"\nNote: Failed URLs were skipped. Check logs for details.")

    async def _run_async(self):
        print(f"\n=== URL Text Extractor ===\n")
        
        urls = self._get_urls_from_user()
        if not urls:
            print(f"{self.RED}No valid URLs provided. Exiting.{self.RESET}")
            return

        print(f"\n{self.YELLOW}Fetching {len(urls)} URL(s)...{self.RESET}")
        fetch_results = await self.fetcher.fetch_all(urls)

        # Process successes
        extracted_items: List[Tuple[str, str]] = []
        for res in fetch_results:
            if res['success']:
                text = self.extractor.extract_text(res['content'], res['url'], res['content_type'])
                extracted_items.append((res['url'], text))

        # Print scraping report
        self._print_scraping_report(fetch_results, extracted_items)

        if not extracted_items:
            print(f"\n{self.RED}No content could be extracted. Exiting.{self.RESET}")
            return

        # Prompt handling (if enabled) - plain text, no color
        if self.prompt_handler and ADD_PROMPT:
            print("\nWould you like to add a prompt to the text output?")
            self.prompt_handler.display_and_select()

        # Generate final output
        final_text = self.output_gen.generate_output(extracted_items) if self.output_gen else "\n".join(f"--- {url} ---\n{text}" for url, text in extracted_items)

        # Write to file (LBYL: check directory writable)
        output_dir = os.path.dirname(OUTPUT_FILENAME) or "."
        if not os.access(output_dir, os.W_OK):
            self.logger.log(Status.ERROR, message=f"Output directory not writable: {output_dir}")
            print(f"{self.RED}Error: Cannot write to {output_dir}{self.RESET}")
            return

        try:
            with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
                f.write(final_text)
            print(f"\n{self.GREEN}✅ Saved to {OUTPUT_FILENAME}{self.RESET}")
            
            # Show file size
            file_size = os.path.getsize(OUTPUT_FILENAME)
            print(f"{self.BLUE}File size: {file_size} bytes{self.RESET}")
        except Exception as e:
            self.logger.log(Status.ERROR, exc=e, message=f"Failed to write {OUTPUT_FILENAME}")
            print(f"{self.RED}Error: Could not save file - {e}{self.RESET}")

    def run(self):
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            print(f"\n{self.YELLOW}Program interrupted by user.{self.RESET}")
        except Exception as e:
            print(f"{self.RED}Unexpected error: {e}{self.RESET}")
            self.logger.log(Status.ERROR, exc=e, message="Unhandled exception in main")
            raise

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
if __name__ == "__main__":
    app = ConsoleApp()
    app.run()
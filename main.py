#!/usr/bin/env python3
"""
Console app to fetch URLs asynchronously, extract readable text (HTML only),
and optionally wrap output with backticks and a prompt.

Uses web_fetcher module for HTTP requests with anti‑scraping fallbacks.
"""

import asyncio
import os
import sys
import re
import argparse
from typing import List, Tuple, Optional

# HTML extraction
import trafilatura
from bs4 import BeautifulSoup

# Import logger, status, and CLI selector
from traceback_logger import TracebackLogger, Status
from cli_selector import CliSelector

# Import async fetcher and result dataclass
from web_fetcher import AsyncUrlFetcher, FetchResult

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
OUTPUT_FILENAME = "extracted_content.txt"
FETCH_TIMEOUT = 10  # seconds per request
ADD_PROMPT = True   # enable/disable prompt selection

# Predefined prompts
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

# ----------------------------------------------------------------------
# ContentExtractor (HTML only, no PDF)
# ----------------------------------------------------------------------
class ContentExtractor:
    """Extract plain text from HTML content. Skips non-HTML."""

    def __init__(self, logger: TracebackLogger):
        self.logger = logger

    def extract_text(self, result: FetchResult) -> str:
        """Return extracted plain text or error message from a FetchResult."""
        content = result.content
        url = result.url
        content_type = result.content_type

        if not content:
            self.logger.log(Status.WARNING, message=f"Empty content for {url}")
            return f"[Empty content for {url}]"

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
            if text and len(text.strip()) > 100:
                return text.strip()
        except Exception as e:
            self.logger.log(Status.PARSE, exc=e, message=f"Trafilatura failed for {url}, falling back to BeautifulSoup")

        # Fallback to BeautifulSoup
        try:
            soup = BeautifulSoup(html, 'lxml')
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            text = soup.get_text(separator='\n', strip=True)
            if text:
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
# PromptHandler (same as original)
# ----------------------------------------------------------------------
class PromptHandler:
    MODE_RAW = 0
    MODE_FENCE_ONLY = 1
    MODE_PREDEFINED = 2
    MODE_CUSTOM = 3

    def __init__(self, prompt_list: Optional[List[str]] = None):
        self.prompt_list = prompt_list if prompt_list is not None else []
        self.mode = self.MODE_RAW
        self.selected_prompt = None
        self.selector = CliSelector()

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

        choices = [str(i) for i in range(1, len(self.prompt_list) + 1)]
        display_dict = {str(i): prompt for i, prompt in enumerate(self.prompt_list, start=1)}
        self.selector.set(
            prompt="Select a predefined prompt (enter number):",
            choices=choices,
            display_dict=display_dict
        )
        selected_key = self.selector.ask()
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
            choice = self.selector.ask()
            mode = int(choice)
            if mode == self.MODE_RAW:
                return self._handle_raw_mode()
            elif mode == self.MODE_FENCE_ONLY:
                return self._handle_fence_only()
            elif mode == self.MODE_PREDEFINED:
                return self._handle_predefined_prompt()
            elif mode == self.MODE_CUSTOM:
                return self._handle_custom_prompt()

    def format_output(self, raw_text: str) -> str:
        if self.mode == self.MODE_RAW:
            return raw_text
        elif self.mode == self.MODE_FENCE_ONLY:
            return f"```\n{raw_text}\n```"
        elif self.mode in (self.MODE_PREDEFINED, self.MODE_CUSTOM):
            return f"```\n{raw_text}\n```\n{self.selected_prompt}"
        else:
            return raw_text

    def reset(self):
        self.mode = self.MODE_RAW
        self.selected_prompt = None

# ----------------------------------------------------------------------
# OutputGenerator
# ----------------------------------------------------------------------
class OutputGenerator:
    def __init__(self, prompt_handler: PromptHandler):
        self.prompt_handler = prompt_handler

    def generate_output(self, items: List[Tuple[str, str]]) -> str:
        if not items:
            return ""
        raw_parts = []
        for url, text in items:
            raw_parts.append(f"--- Source: {url} ---")
            raw_parts.append(text)
            raw_parts.append("")
        raw_text = "\n".join(raw_parts).strip()
        return self.prompt_handler.format_output(raw_text)

# ----------------------------------------------------------------------
# ConsoleApp (orchestrator)
# ----------------------------------------------------------------------
class ConsoleApp:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def __init__(self, anti_scrape: bool = False, proxy_file: Optional[str] = None):
        self.logger = TracebackLogger()
        # Load proxy list if provided
        proxy_list = []
        if proxy_file and os.path.exists(proxy_file):
            with open(proxy_file, 'r') as f:
                proxy_list = [line.strip() for line in f if line.strip()]

        self.fetcher = AsyncUrlFetcher(
            self.logger,
            timeout=FETCH_TIMEOUT,
            anti_scrape=anti_scrape,
            proxy_list=proxy_list if proxy_list else None
        )
        self.extractor = ContentExtractor(self.logger)
        if ADD_PROMPT:
            self.prompt_handler = PromptHandler(PROMPT_LIST)
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

    def _print_scraping_report(self, fetch_results: List[FetchResult], extracted_items: List[Tuple[str, str]]) -> None:
        print(f"\n{self.BOLD}{self.BLUE}=== Scraping Report ==={self.RESET}")
        success_count = len(extracted_items)
        total_count = len(fetch_results)
        fail_count = total_count - success_count

        for res in fetch_results:
            if res.success:
                print(f"  {self.GREEN}✓{self.RESET} {res.url}")
            else:
                print(f"  {self.RED}✗{self.RESET} {res.url}")

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

        extracted_items: List[Tuple[str, str]] = []
        for res in fetch_results:
            if res.success:
                text = self.extractor.extract_text(res)
                extracted_items.append((res.url, text))

        self._print_scraping_report(fetch_results, extracted_items)

        if not extracted_items:
            print(f"\n{self.RED}No content could be extracted. Exiting.{self.RESET}")
            return

        if self.prompt_handler and ADD_PROMPT:
            print("\nWould you like to add a prompt to the text output?")
            self.prompt_handler.display_and_select()

        final_text = self.output_gen.generate_output(extracted_items) if self.output_gen else "\n".join(f"--- {url} ---\n{text}" for url, text in extracted_items)

        output_dir = os.path.dirname(OUTPUT_FILENAME) or "."
        if not os.access(output_dir, os.W_OK):
            self.logger.log(Status.ERROR, message=f"Output directory not writable: {output_dir}")
            print(f"{self.RED}Error: Cannot write to {output_dir}{self.RESET}")
            return

        try:
            with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
                f.write(final_text)
            print(f"\n{self.GREEN}✅ Saved to {OUTPUT_FILENAME}{self.RESET}")
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
    app = ConsoleApp(anti_scrape=True)   # enable anti‑scraping
    app.run()
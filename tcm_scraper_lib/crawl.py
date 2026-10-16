# -*- coding: utf-8 -*-
# Crawling layer: listing-page pagination and detail-page traversal (online), plus batch parsing
# of a local folder (offline).

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from .config import FIELD_COLUMNS, SELECTORS
from .fetch import BaseFetcher, RequestsFetcher, diagnose_empty_result, read_html_file, robots_allows
from .parse import DrugPageParser, SelectorEngine
from .utils import clean_text, polite_sleep, random_ua

# Web-page extensions supported in folder mode
SUPPORTED_PAGE_SUFFIXES: Tuple[str, ...] = (".html", ".htm", ".xhtml")


def crawl_folder(cfg: Dict[str, Any], logger: logging.Logger) -> List[Dict[str, Any]]:
    # Folder mode: treat **every page file** in a local folder as one label and parse it directly.
    #
    # This serves the "log in manually, then save pages" route:
    #     * no listing page, no link pairing, no network requests;
    #     * the filename acts as a fallback for the drug name (so name files like "黄芩片.html");
    #     * a single file failing only skips that file, never the rest.
    #
    # Use case: when the target database needs login/payment and the script should not (and will
    # not) bypass it, the user browses and saves pages in a browser and the script only extracts.
    folder = Path(cfg["list_url"])
    if not folder.exists():
        logger.error(
            "Folder does not exist: %s\n            Check the path (Windows example: D:\\project\\saved_pages), or create the folder and save pages into it.",
            folder,
        )
        return []
    try:
        files = sorted(
            p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_PAGE_SUFFIXES
        )
    except OSError as exc:
        logger.error("Failed to read the folder: %s (%s)", folder, exc)
        return []

    if not files:
        logger.error(
            "No web pages found in folder %s (supported: %s).\n"
            "            Check that (1) the save location is correct; (2) you chose \"Webpage, HTML Only\" rather than .mhtml.",
            folder.resolve(), ", ".join(SUPPORTED_PAGE_SUFFIXES),
        )
        return []

    logger.info("Folder mode: found %d page files under %s, parsing each (offline)", len(files), folder.resolve())
    keyword = (cfg.get("name_filter") or "").strip()
    if keyword:
        logger.info(
            "Note: folder mode does not filter by keyword; it parses every page in the chosen folder (the configured keyword \"%s\" is ignored)",
            keyword,
        )
    parser = DrugPageParser(SELECTORS, logger)
    records: List[Dict[str, Any]] = []

    for idx, path in enumerate(files, start=1):
        logger.info("--- [%d/%d] Parsing local page: %s", idx, len(files), path.name)
        try:
            html = read_html_file(path)
        except OSError as exc:
            logger.error("Read failed, skipping: %s (%s)", path.name, exc)
            continue

        try:
            # The filename is used as a drug-name fallback, so name files "drugname.html"
            record = parser.parse(html, str(path.resolve()), hint_name=path.stem)
        except Exception as exc:
            logger.exception("Parse error, skipping: %s (%s)", path.name, exc)
            continue

        filled = sum(1 for col in FIELD_COLUMNS.values() if record.get(col))
        logger.info("Parsed: %s (%d/5 fields filled)", record.get("drug_name") or path.stem, filled)
        if filled <= 1:
            logger.warning(
                "Only the drug name was parsed from this file; the page may not have finished loading when saved, or a login page was saved: %s", path.name,
            )
        records.append(record)

    logger.info("Folder mode finished: %d/%d files produced a record", len(records), len(files))
    return records


@dataclass
class DetailLink:
    # One item to fetch, as parsed from a listing page.

    url: str
    title: str = ""    # link anchor text (e.g. "view label")
    context: str = ""  # full text of the containing row (includes the drug name; used for keyword filtering)
    name: str = ""     # drug name extracted from the row separately (used as a detail-page fallback)


class Crawler:
    # Crawl scheduler: listing-page pagination, detail-page traversal, and end-to-end pacing and
    # fault tolerance.

    def __init__(self, cfg: Dict[str, Any], fetcher: BaseFetcher, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.fetcher = fetcher
        self.logger = logger
        self.parser = DrugPageParser(SELECTORS, logger)

    # ---------------- listing page ----------------
    def collect_detail_links(self, list_url: str) -> List[DetailLink]:
        # Paginate and collect detail-page links, bounded by max_pages / max_items / name_filter.
        links: List[DetailLink] = []
        seen: set = set()
        current_url: Optional[str] = list_url
        page_no = 1
        parsed_count = 0    # how many items were seen on the listing pages
        filtered_count = 0  # how many of them the keyword filtered out

        while current_url and page_no <= int(self.cfg["max_pages"]):
            self.logger.info("--- Listing page %d: %s", page_no, current_url)
            if self.cfg["respect_robots"] and not robots_allows(current_url, random_ua(), self.logger):
                self.logger.error("robots.txt disallows fetching this listing page; stopping.")
                break

            try:
                html = self._fetch_with_retry(current_url)
            except Exception as exc:
                self.logger.error("Listing page fetch failed; stopping pagination: %s", exc)
                break

            self._maybe_dump_html(html, current_url, f"list_p{page_no}")
            page_links = self._parse_detail_links(html, current_url)
            parsed_count += len(page_links)
            self.logger.info("Page %d yielded %d candidate items", page_no, len(page_links))

            # Not a single item parsed: first work out what the page actually is, then give a targeted hint
            if not page_links:
                diagnose_empty_result(html, current_url, self.logger)
                break

            for link in page_links:
                key = link.url.split("#")[0]
                if key in seen:
                    continue
                if not self._passes_name_filter(link):
                    filtered_count += 1
                    self.logger.debug("Excluded by keyword filter: %s", link.context or link.url)
                    continue
                seen.add(key)
                links.append(link)
                if len(links) >= int(self.cfg["max_items"]):
                    self.logger.info("Reached max_items=%s; stopping link collection", self.cfg["max_items"])
                    return links

            current_url = self._parse_next_page(html, current_url)
            page_no += 1
            if current_url:
                polite_sleep(self.cfg["min_delay"], self.cfg["max_delay"])

        # Items were parsed but all filtered out: say so, so the user does not think the script broke
        if not links and filtered_count > 0:
            keyword = (self.cfg.get("name_filter") or "").strip()
            self.logger.error(
                "Parsed %d items on the page, but all were removed by the keyword \"%s\", leaving none.\n"
                "            To collect everything: at wizard step 2 type \"all\", or re-run with --no-filter.",
                parsed_count, keyword,
            )
        return links

    def _parse_detail_links(self, html: str, base_url: str) -> List[DetailLink]:
        # Extract the detail link, name and context from each item container on the listing page.
        engine = SelectorEngine(html, self.logger)
        list_sel = SELECTORS["list"]
        results: List[DetailLink] = []

        for item in engine.find_nodes(list_sel.get("item", [])):
            # The item container's full HTML fragment -> locate the detail link within it as (href, anchor text)
            fragment = SelectorEngine(item.html or "", self.logger)
            for href, anchor_text in fragment.find_links(list_sel.get("item_link", [])):
                url = self._normalize_url(href, base_url)
                if not url:
                    continue
                results.append(
                    DetailLink(
                        url=url,
                        title=anchor_text,
                        context=clean_text(item.text) or "",
                        name=self._extract_item_name(fragment, anchor_text),
                    )
                )
                break  # only the first link per item, to avoid grabbing "more" and similar links

        # Fallback: if the item-container selector is not configured, look for all links on the page
        # and filter them by URL features
        if not results:
            self.logger.warning("Item selector did not match; falling back to \"all links + URL-feature filter\"")
            for href, anchor_text in engine.find_links(list_sel.get("item_link", ["a::attr(href)"])):
                url = self._normalize_url(href, base_url)
                if url and self._looks_like_detail(url):
                    results.append(DetailLink(url=url, title=anchor_text, context=anchor_text))
        return results

    def _extract_item_name(self, fragment: SelectorEngine, anchor_text: str) -> str:
        # Extract the drug name from the item fragment.
        #
        # Note: some listing pages use action words like "查看说明书" as the link text, which must not
        # be used as a drug name; a blocklist plus a length cap excludes them, preferring an empty
        # result over contaminating later fields.
        blacklist = {"查看说明书", "查看", "详情", "详细信息", "更多", "查询", "说明书", "点击查看"}
        for node in fragment.find_nodes(SELECTORS["list"].get("item_name", [])):
            candidate = clean_text(node.text)
            if not candidate or len(candidate) > 40:
                continue
            if candidate in blacklist or candidate == anchor_text:
                continue
            return candidate
        return ""

    def _parse_next_page(self, html: str, base_url: str) -> Optional[str]:
        # Parse the "next page" link; return None to end pagination when it cannot be found.
        engine = SelectorEngine(html, self.logger)
        nodes = engine.find_nodes(SELECTORS["list"].get("next_page", []))
        if not nodes:
            return None
        href = nodes[0].attrs.get("href") or nodes[0].text
        nxt = self._normalize_url(href, base_url)
        # Same as the current page (common with JS pagination) or empty -> treat as no next page
        return nxt if nxt and nxt != base_url else None

    @staticmethod
    def _normalize_url(href: str, base_url: str) -> Optional[str]:
        # Turn relative links into absolute ones and drop invalid links such as javascript:/#.
        href = (href or "").strip()
        if not href or href.startswith(("javascript:", "#", "void(")) or href.lower().startswith("mailto:"):
            return None
        return urljoin(base_url, href)

    def _looks_like_detail(self, url: str) -> bool:
        # Rough check that a link looks like a detail page (filters out navigation, pagination, home, etc.).
        keyword = (self.cfg.get("detail_link_keyword") or "").strip()
        if keyword:
            return keyword in url
        lowered = url.lower()
        if any(bad in lowered for bad in ("javascript", "login", "index.html", "home")):
            return False
        return bool(re.search(r"(detail|content|info|show|view|id=|\.html)", lowered))

    def _passes_name_filter(self, link: DetailLink) -> bool:
        # Filter items by keyword (e.g. only 黄芩-related drugs).
        #
        # Order of checks: full row context -> separately extracted name -> link anchor text.
        # The full context is preferred because many listing pages use "查看说明书" as the link text,
        # with the real drug name in another cell of the row.
        keyword = (self.cfg.get("name_filter") or "").strip()
        if not keyword:
            return True
        haystack = " ".join(filter(None, (link.context, link.name, link.title)))
        if not haystack:
            return True  # conservative pass when nothing could be read; the parse stage decides
        return keyword in haystack

    # ---------------- detail page ----------------
    def crawl_details(self, links: Iterable[DetailLink]) -> List[Dict[str, Any]]:
        # Fetch and parse each detail page; a single failure is logged and does not abort the run.
        records: List[Dict[str, Any]] = []
        links = list(links)

        for idx, link in enumerate(links, start=1):
            self.logger.info("--- [%d/%d] Fetching detail: %s", idx, len(links), link.url)
            if self.cfg["respect_robots"] and not robots_allows(link.url, random_ua(), self.logger):
                self.logger.warning("robots.txt disallows it; skipping: %s", link.url)
                continue

            try:
                html = self._fetch_with_retry(link.url)
                self._maybe_dump_html(html, link.url, f"detail_{idx}")
            except Exception as exc:
                self.logger.error("Detail page fetch failed, skipping: %s (%s)", link.url, exc)
                continue

            try:
                record = self.parser.parse(html, link.url, hint_name=link.name or None)
            except Exception as exc:  # a parse error must not abort the whole run either
                self.logger.exception("Detail page parse error, skipping: %s (%s)", link.url, exc)
                continue

            filled = sum(1 for col in FIELD_COLUMNS.values() if record.get(col))
            self.logger.info("Parsed: %s (%d/5 fields filled)", record.get("drug_name") or "unknown", filled)
            records.append(record)

            # Rate limiting: no sleep after the last item, to save time
            if idx < len(links):
                polite_sleep(self.cfg["min_delay"], self.cfg["max_delay"])

        return records

    # ---------------- shared: fetch a page with retries ----------------
    def _fetch_with_retry(self, url: str) -> str:
        # Fetch wrapper with retries and a timeout.
        #
        # When tenacity is available, RequestsFetcher injects retries at construction; for Selenium
        # and Local modes this wraps a retry layer here, so every mode gets max_retries attempts.
        max_attempts = int(self.cfg["max_retries"])

        if isinstance(self.fetcher, RequestsFetcher):
            return self.fetcher.get_html(url)  # retries already handled inside RequestsFetcher

        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    self.logger.info("Retry %d/%d: %s", attempt, max_attempts, url)
                return self.fetcher.get_html(url)
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                wait = 1.5 * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
                self.logger.warning("Fetch failed: %s; retrying in %.1fs", exc, wait)
                time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    # ---------------- optional raw-HTML dump ----------------
    def _maybe_dump_html(self, html: str, url: str, tag: str) -> None:
        # Save the raw HTML: very useful when tuning selectors -- fetch once, replay parsing many times
        # without re-requesting the target site.
        if not self.cfg.get("save_raw_html"):
            return
        try:
            out_dir = Path(self.cfg["raw_html_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", f"{tag}_{url}")[:120]
            path = out_dir / f"{safe}.html"
            path.write_text(html, encoding="utf-8")
            self.logger.debug("Raw HTML saved: %s", path)
        except Exception as exc:  # pragma: no cover
            self.logger.debug("Failed to save raw HTML (ignorable): %s", exc)

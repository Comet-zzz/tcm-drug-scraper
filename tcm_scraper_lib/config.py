# -*- coding: utf-8 -*-
# Configuration and constants: selectors, field mapping, default run parameters.
#
# When adapting to a new site, almost all edits belong in this file.

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# Project root (the directory holding the entry script tcm_scraper.py); used to locate config.json.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# ---------------- 1. Configuration area (about 80% of site-adaptation edits go here) ----------------
#
# The target URL is NOT set here:
#   It is entered in the setup wizard and saved to config.json next to the script, then reused on
#   later runs by pressing Enter; you can also edit config.json directly or pass --url on the
#   command line. DEFAULT_CONFIG["list_url"] is only a placeholder used before the first run.
#
# (1) Selectors: every entry in SELECTORS is a "candidate list". Parsing tries them in order and
#     keeps the first one that returns content. When a site is redesigned, just prepend a new
#     selector; there is no need to delete the old ones.
#
#     Selector syntax conventions (custom to this script, see utils.is_xpath()):
#       - starts with "//", "./", "(" or carries an "xpath:" prefix  -> parsed as XPath (needs lxml)
#       - anything else                                             -> parsed as a CSS selector (BeautifulSoup)
#     To extract a link target, append "::attr(href)" to the selector (XPath mode returns @href automatically).
#
# (2) Three-tier fallback: even if every selector fails, the script still falls back to Chinese
#     label markers such as "【不良反应】" and to a generic "label node + value node" scan, so you can
#     run it first and refine selectors afterwards.

USER_AGENTS: List[str] = [
    # Pool of random User-Agents: reduces the chance of being fingerprinted as a single crawler.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

# Standard Chinese label markers, e.g. "【不良反应】". The second-tier fallback uses them to delimit
# a field's text. Example: given "【不良反应】偶见皮疹…【禁忌】尚不明确" in the body, it can cut the
# two segments apart precisely.
# NOTE: {labels} must be wrapped in a (?:...) non-capturing group. Otherwise a multi-alias
#     alternation such as "主要成分|成份|成分" is torn apart by the regex's top-level "|", and
#     "成分" gets matched inside "【主要成分】", leaving the closing "】" at the start of the value.
LABEL_SECTION_RE = r"[【\[]\s*(?:{labels})\s*[】\]]"

# Chinese label aliases per field (used by the second- and third-tier fallback; extend as needed).
FIELD_LABELS: Dict[str, List[str]] = {
    "drug_name": ["药品名称", "通用名称", "品名", "名称"],
    "ingredients": ["主要成分", "主要成份", "成份", "成分", "主要成份及含量", "处方", "成份/药理作用"],
    "adverse_reactions": ["不良反应", "不良反应与处理", "药物不良反应"],
    "contraindications": ["禁忌", "禁忌症", "禁止"],
    "precautions": ["注意事项", "注意", "警告", "特别提示"],
}

# Maps a field key to its CSV column name; the order here is the CSV column order.
FIELD_COLUMNS: Dict[str, str] = {
    "drug_name": "drug_name",
    "ingredients": "ingredients",
    "adverse_reactions": "adverse_reactions",
    "contraindications": "contraindications",
    "precautions": "precautions",
}

SELECTORS: Dict[str, Any] = {
    # ---------------- Listing page (search-results page) ----------------
    "list": {
        # A single result row (located at the container so both title and link can be read)
        "item": [
            # Yaopinnet label listing: two uls under div#c_list1 / div#c_list2
            "div[id^='c_list'] li",
            "table tbody tr",
            "div.result-list div.item",
            "ul.list li",
            "//table//tr[td]",
        ],
        # Detail-page link inside a row
        "item_link": [
            "a::attr(href)",
            "//a[@href]",
        ],
        # Drug-name node inside a row (used for keyword filtering and as a detail-page name fallback;
        # if absent the script falls back to the whole row text for filtering)
        "item_name": [
            "td.name",
            "span.name",
            "div.drug-name",
            "h3",
            "h4",
        ],
        # Next-page button/link (if not found, only one page is assumed)
        "next_page": [
            # Yaopinnet: the current page number is a <span>; the first following <a> is the next page
            "//div[@id='sms_page']/span/following-sibling::a[1]",
            "a.next::attr(href)",
            "a[title='下一页']::attr(href)",
            "//a[contains(text(),'下一页')]",
            "//a[contains(@class,'next')]",
        ],
        # Element to wait for before the listing is considered rendered (Selenium only; "" = no wait)
        "wait_for": "table tbody tr",
    },
    # ---------------- Detail page (label page) ----------------
    "detail": {
        # Drug name: usually in h1 / h2 / the detail title / breadcrumb
        "drug_name": [
            "h1.drug-name",
            "div.detail-title h1",
            "div.drug-detail h1",
            "//h1",
            "div.drug-name",
            "div.page-head h2",
            "//h2[1]",
            "//*[contains(@class,'title')]/h1",
        ],
        # The four fields below each fall back through "selector -> section marker -> label node".
        # NOTE on the string-length guard in the XPath: it ensures the match is the short label
        #     itself, not a container that wraps the whole paragraph. Otherwise, for layouts like
        #     "<div>【主要成分】黄芩…</div>" where label and content share one element,
        #     following-sibling would wrongly pick up the next field's content.
        "ingredients": [
            "div.field-ingredients div.content",
            "div.ingredients",
            "//*[contains(text(),'主要成分') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        "adverse_reactions": [
            "div.field-adverse div.content",
            "div.adverse-reactions",
            "//*[contains(text(),'不良反应') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        "contraindications": [
            "div.field-contraindications div.content",
            "div.contraindications",
            "//*[contains(text(),'禁忌') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        "precautions": [
            "div.field-precautions div.content",
            "div.precautions",
            "//*[contains(text(),'注意事项') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        # Element to wait for before the detail page is considered rendered
        "wait_for": "body",
    },
}

# Default run parameters (overridable by command-line arguments)
DEFAULT_CONFIG: Dict[str, Any] = {
    "list_url": "https://www.nmpa.gov.cn/datasearch/home-index.html#category=yp",  # target URL
    "mode": "selenium",            # "selenium" (default, dynamic pages) or "requests" (fast, static pages)
    "headless": True,              # whether Selenium runs headless
    "min_delay": 2.0,              # lower bound of the random sleep between requests (seconds)
    "max_delay": 5.0,              # upper bound of the random sleep between requests (seconds)
    "max_retries": 3,              # max retries per request (including the first attempt)
    "timeout": 30,                 # per-page timeout (seconds)
    "max_items": 20,               # max detail pages to fetch (validate on a small sample first)
    "max_pages": 3,                # max listing pages to paginate
    "name_filter": "",             # keyword filter: only collect names containing this term; "" = no filter
    "out_csv": "output/tcm_drug_data.csv",
    "log_file": "logs/tcm_scraper.log",
    "respect_robots": True,        # whether to honour robots.txt
    "save_raw_html": False,        # whether to dump raw HTML to disk (for replaying parsing without re-requesting)
    "raw_html_dir": "output/raw_html",
}

# Config file: auto-generated on first run, editable in Notepad, no Python knowledge required.
CONFIG_FILE_NAME = "config.json"
# Settings that may be written to and read back from the config file (internal parameters are not
# persisted, to keep the config file simple).
PERSISTED_KEYS: List[str] = [
    "list_url",      # page to collect
    "name_filter",   # target herb name ("" = no filter)
    "max_items",     # max items to collect
    "max_pages",     # max listing pages
    "mode",          # selenium / requests / local
    "headless",      # run the browser in the background or not
    "min_delay",
    "max_delay",
]

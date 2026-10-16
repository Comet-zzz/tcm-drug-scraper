# -*- coding: utf-8 -*-
# Page-fetching layer (where the anti-ban strategy lives): three fetchers -- requests / selenium /
# local file -- plus the robots.txt check and the "0 items" classified diagnosis.

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Third-party imports are deferred / fault-tolerant so that a missing optional dependency still
# produces a clear message instead of a top-level ImportError (e.g. selenium is not needed when
# only parsing locally saved pages).
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment]

try:
    # lxml is used to build the BeautifulSoup tree with the lxml parser (faster, more robust);
    # XPath support lives in parse.py.
    import lxml.html as lxml_html  # type: ignore
except ImportError:  # pragma: no cover
    lxml_html = None  # type: ignore[assignment]

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    # tenacity provides declarative retries; if missing, we fall back to utils.simple_retry.
    from tenacity import (
        before_sleep_log,
        retry as _t_retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_random_exponential,
    )

    HAS_TENACITY = True
except ImportError:  # pragma: no cover
    HAS_TENACITY = False

from .utils import random_ua, simple_retry


def read_html_file(path: Path) -> str:
    # Read a local HTML file, handling the encoding automatically.
    #
    # Why this is needed: "Save as" in Chrome normally writes UTF-8, but some older sites are saved
    # as GBK/GB18030, and reading those as UTF-8 turns the Chinese text into mojibake. This first
    # reads the declared charset from the file, then tries UTF-8 -> GB18030 in order, to maximise
    # the chance of decoding the Chinese text correctly.
    raw = path.read_bytes()
    head = raw[:2048].decode("ascii", errors="ignore").lower()
    declared = re.search(r'charset\s*=\s*["\']?([\w-]+)', head)

    candidates: List[str] = []
    if declared:
        candidates.append(declared.group(1))
    candidates += ["utf-8", "gb18030", "utf-16"]

    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def looks_like_folder(value: str) -> bool:
    # Tell whether the input is a local folder (for the "manually saved pages" folder mode).
    if not value or value.startswith(("http://", "https://")):
        return False
    try:
        return Path(value).is_dir()
    except (OSError, ValueError):
        return False


class BaseFetcher:
    # Fetcher interface: every implementation exposes get_html(url) -> str.

    def get_html(self, url: str) -> str:  # pragma: no cover - abstract method
        raise NotImplementedError

    def quit(self) -> None:
        # Release resources (Selenium needs to close the browser).
        return None


class LocalFileFetcher(BaseFetcher):
    # Local-file fetcher: parses a single web page saved to disk (no network).
    #
    # Supports both 'file:///D:/.../x.html' and a plain absolute/relative path.
    # Local parsing goes through exactly the same parsing and cleaning code path as online fetching.

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    @staticmethod
    def _to_path(url: str) -> Path:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            # On Windows, file:///D:/a/b.html -> /D:/a/b.html; drop the leading extra '/'
            path = parsed.path
            if re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
            return Path(path)
        return Path(url)

    def get_html(self, url: str) -> str:
        path = self._to_path(url)
        self.logger.debug("Reading local file: %s", path)
        if not path.exists():
            raise FileNotFoundError(f"Local file not found: {path}")
        return read_html_file(path)


class RequestsFetcher(BaseFetcher):
    # Static-page fetcher (requests): fast, suited to server-rendered targets.
    #
    # Built in: random UA, timeout, tenacity retries, session reuse (keeps cookies).

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        if requests is None:
            raise RuntimeError("requests is not installed. Run: pip install requests")
        self.cfg = cfg
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": random_ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )
        self._fetch = self._build_fetch()

    def _build_fetch(self):
        # Choose a retry implementation depending on whether tenacity is available.
        max_attempts = int(self.cfg["max_retries"])
        if HAS_TENACITY:
            return _t_retry(
                stop=stop_after_attempt(max_attempts),
                wait=wait_random_exponential(multiplier=1.5, max=20),
                retry=retry_if_exception_type((requests.RequestException, ValueError)),
                before_sleep=before_sleep_log(self.logger, logging.WARNING),
                reraise=True,
            )(self._fetch_once)
        return simple_retry(max_attempts=max_attempts)(self._fetch_once)

    def _fetch_once(self, url: str) -> str:
        # Rotate the UA per request, so a single fingerprint is not used for a long session
        headers = {"User-Agent": random_ua()}
        resp = self.session.get(url, headers=headers, timeout=self.cfg["timeout"])
        if resp.status_code >= 400:
            raise ValueError(f"HTTP {resp.status_code}: {url}")
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        return resp.text

    def get_html(self, url: str) -> str:
        self.logger.info("GET(requests) %s", url)
        return self._fetch(url)


class SeleniumFetcher(BaseFetcher):
    # Dynamic-page fetcher (selenium): for JS-rendered / asynchronously loaded pages (the NMPA
    # data-query site is one).
    #
    # Anti-ban and stability measures:
    #   * random User-Agent + random window size
    #   * suppresses the "Chrome is being controlled by automated test software" banner and hides
    #     navigator.webdriver
    #   * does not load images (faster, and reduces the traffic fingerprint)
    #   * page-load timeout + explicit element waits (WebDriverWait)
    #   * driver acquisition order: Selenium Manager (bundled with selenium >= 4.6) -> webdriver-manager

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "selenium is not installed. Run: pip install selenium webdriver-manager"
            ) from exc

        self.cfg = cfg
        self.logger = logger
        self._webdriver = webdriver
        self._ua = random_ua()

        options = Options()
        if cfg["headless"]:
            options.add_argument("--headless=new")
        options.add_argument(f"--user-agent={self._ua}")
        options.add_argument(f"--window-size={random.randint(1200, 1600)},{random.randint(800, 1000)}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--lang=zh-CN")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        # Do not load images: markedly faster, and fewer useless requests
        options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

        self.driver = self._create_driver(options)

        # Hide one more automation signal (the minimum needed for publicly accessible pages only)
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
            )
        except Exception as exc:  # pragma: no cover - non-essential step, never blocks the run
            self.logger.debug("CDP anti-detection injection failed (ignorable): %s", exc)

        self.driver.set_page_load_timeout(cfg["timeout"])

    def _create_driver(self, options):
        # Prefer Selenium Manager for automatic driver matching; fall back to webdriver-manager.
        try:
            driver = self._webdriver.Chrome(options=options)
            self.logger.info("Chrome driver started via Selenium Manager")
            return driver
        except Exception as first_exc:
            self.logger.warning("Selenium Manager failed (%s); trying webdriver-manager...", first_exc)

        try:
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager

            service = Service(ChromeDriverManager().install())
            driver = self._webdriver.Chrome(service=service, options=options)
            self.logger.info("Chrome driver started via webdriver-manager")
            return driver
        except Exception as second_exc:  # pragma: no cover
            raise RuntimeError(
                "Could not start Chrome. Make sure Chrome is installed and the network is available "
                f"so the driver can be downloaded. Original error: {second_exc}"
            ) from second_exc

    def get_html(self, url: str) -> str:
        self.logger.info("GET(selenium) %s", url)
        self.driver.get(url)

        # Explicit wait: wait for the target element so a half-rendered page is not grabbed
        wait_css = (self.cfg.get("wait_css") or "").strip()
        if wait_css:
            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.support.ui import WebDriverWait

                WebDriverWait(self.driver, self.cfg["timeout"]).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
                )
            except Exception as exc:
                # A wait timeout is not fatal: some pages differ and parsing may still succeed
                self.logger.debug("Wait for element '%s' timed out (continuing to parse): %s", wait_css, exc)

        # Give asynchronous endpoints a little buffer time
        time.sleep(random.uniform(0.5, 1.5))
        if "登录" in self.driver.title and "说明书" not in self.driver.title:
            self.logger.warning("Page title looks like a login page; a login wall may have been hit: %s", self.driver.title)
        return self.driver.page_source

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:  # pragma: no cover
            pass


def build_fetcher(cfg: Dict[str, Any], logger: logging.Logger) -> BaseFetcher:
    # Factory: pick a fetcher based on the configuration.
    url = cfg["list_url"]
    if cfg["mode"] == "local" or is_local_url(url):
        logger.info("Local target detected; using LocalFileFetcher (offline, no network requests)")
        return LocalFileFetcher(logger)
    if cfg["mode"] == "requests":
        return RequestsFetcher(cfg, logger)
    return SeleniumFetcher(cfg, logger)


def is_local_url(url: str) -> bool:
    # Decide whether the target is a local file (file:// or a disk path).
    #
    # Note: http/https is explicitly excluded, so a local directory that happens to share the name
    # is not misread as a local file.
    if not url:
        return False
    if url.startswith("file://"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", url):  # Windows drive letter, e.g. D:\data\x.html
        return True
    if re.match(r"^https?://", url, re.I):
        return False  # an explicit network address always fetches over the network
    # Otherwise: a relative/absolute local path, decided by whether it actually exists on disk
    try:
        return Path(url).exists()
    except OSError:  # pragma: no cover - illegal path characters etc.
        return False


# Feature words for login walls / human verification: used to translate "0 items" into a
# user-comprehensible cause.
LOGIN_WALL_HINTS: List[str] = [
    "登录之后", "请先登录", "立即登录", "登录后查看", "登录后使用", "会员登录",
    "账号登录", "注册登录", "登录/注册", "用户登录", "请登录", "去登录",
]
CAPTCHA_HINTS: List[str] = [
    "验证码", "拖动滑块", "人机验证", "安全验证", "滑动验证", "点击验证",
]


def diagnose_empty_result(html: str, url: str, logger: logging.Logger) -> None:
    # When 0 detail links are found, inspect what the page actually is and report a plain-language cause.
    #
    # Motivation: if all the user gets is "no detail links parsed", they cannot tell whether the
    # script is broken, the URL is wrong, or the site requires login. This spells out the three
    # most common cases.
    if not html:
        logger.error("Page content is empty; the network may have dropped or the request was blocked.")
        return

    try:
        soup = BeautifulSoup(html, "lxml" if lxml_html is not None else "html.parser")
        text = soup.get_text(" ", strip=True)[:20000]
    except Exception:  # pragma: no cover
        text = html[:20000]

    lower_html = html.lower()
    has_password_box = 'type="password"' in lower_html

    # (1) Login wall
    if any(hint in text for hint in LOGIN_WALL_HINTS) or has_password_box:
        logger.error(
            "This page requires login before its content is visible, so no drug list could be parsed.\n"
            "            This script does not bypass login walls or solve CAPTCHAs. Options:\n"
            "            (a) switch to a public source that needs no login and configure selectors in config.SELECTORS;\n"
            "            (b) log in with a browser, save the page as HTML, and parse it offline with --mode local."
        )
        return

    # (2) Human verification / CAPTCHA
    if any(hint in text for hint in CAPTCHA_HINTS):
        logger.error("This page shows a human-verification / CAPTCHA step, which the script cannot pass (no bypass provided). Switch to a public source.")
        return

    # (3) JS-rendered or blocked: the source contains almost no body text
    if len(text) < 200:
        logger.error(
            "The page body is empty (visible in a browser but absent from the source, usually JS-loaded or blocked).\n"
            "            Try re-running with --no-headless --save-raw and inspect the raw HTML under output/raw_html/."
        )
        return

    # (4) The page is fine, but its structure is not a listing page
    logger.error(
        "No drug-detail links were found on the page. Possible causes:\n"
        "            (1) the address is a single drug's detail page, not a search-results listing page (the script enters each item from the listing);\n"
        "            (2) the listing HTML structure does not match the current selectors.\n"
        "            Run with --no-headless to open the browser window and adjust SELECTORS['list']['item'] against the page (F12)."
    )


# --- robots.txt check -------------------------------------------------------
_ROBOTS_CACHE: Dict[str, Any] = {}


def robots_allows(url: str, user_agent: str, logger: logging.Logger) -> bool:
    # Check whether the target URL is allowed by robots.txt.
    #
    # Note: robots.txt is fetched with requests (with a timeout) to avoid urllib's default
    # no-timeout hang; if the fetch fails the check is treated as "allowed" and the outcome is
    # logged, so the user can decide whether to continue.
    if requests is None:
        return True

    parsed = urlparse(url)
    if not parsed.scheme.startswith("http"):
        return True  # local files need no check

    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _ROBOTS_CACHE:
        from urllib.robotparser import RobotFileParser

        rp = RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        try:
            resp = requests.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": user_agent},
                timeout=10,
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                logger.info("Loaded robots.txt: %s", origin)
            else:
                rp.parse([])  # no robots.txt -> treated as unrestricted
                logger.info("robots.txt not found (HTTP %s); treated as unrestricted", resp.status_code)
        except Exception as exc:
            logger.warning("Failed to fetch robots.txt (%s); skipping the check", exc)
            rp.parse([])
        _ROBOTS_CACHE[origin] = rp

    allowed = _ROBOTS_CACHE[origin].can_fetch(user_agent, url)
    if not allowed:
        logger.warning("robots.txt disallows fetching: %s", url)
    return allowed

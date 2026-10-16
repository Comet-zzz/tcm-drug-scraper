# -*- coding: utf-8 -*-
# Shared utilities: logging, text cleaning, rate limiting, selector syntax helpers, retry decorator.

from __future__ import annotations

import html as html_lib
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .config import USER_AGENTS

# Third-party imports are deferred / fault-tolerant so that a missing optional dependency yields a
# clear message instead of crashing with a top-level ImportError.
try:
    import pandas as pd
except ImportError:  # pragma: no cover - actionable message when the environment lacks it
    pd = None  # type: ignore[assignment]


def setup_logging(log_file: Optional[str], level: str = "INFO") -> logging.Logger:
    # Initialise logging: outputs to both the console and a file for post-mortem debugging.
    logger = logging.getLogger("tcm_scraper")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()  # avoid duplicate output when called more than once
    logger.propagate = False

    # Encoding fallback for the Windows console: in the extreme case (stdout is ASCII) a Chinese log
    # line would raise UnicodeEncodeError and abort the task, so unencodable characters are replaced
    # instead, keeping the task alive.
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - some environments do not support reconfigure
        pass

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


LOG = logging.getLogger("tcm_scraper")  # module-level logger, active after setup_logging


def random_ua() -> str:
    # Pick a random User-Agent from the pool.
    return random.choice(USER_AGENTS)


def polite_sleep(min_delay: float, max_delay: float) -> None:
    # Random sleep between requests: turns a fixed rhythm into a random one, reducing ban risk and
    # easing load on the target server.
    delay = random.uniform(min_delay, max_delay)
    LOG.debug("Sleeping %.2fs before continuing...", delay)
    time.sleep(delay)


def is_xpath(selector: str) -> bool:
    # Decide whether a selector is XPath or CSS, based on its prefix.
    s = selector.strip()
    if s.startswith("xpath:"):
        return True
    return s.startswith("//") or s.startswith("./") or s.startswith("(/") or s.startswith("(")


def strip_xpath_prefix(selector: str) -> str:
    # Strip the custom 'xpath:' prefix.
    s = selector.strip()
    if s.startswith("xpath:"):
        return s[len("xpath:"):].strip()
    return s


def wants_attr(selector: str) -> Optional[str]:
    # Parse the custom '::attr(xxx)' suffix, used to "take an attribute instead of the text".
    # Example: 'a::attr(href)' -> returns 'href' and the caller trims the selector body to 'a'.
    m = re.search(r"::attr\(([^)]+)\)\s*$", selector)
    return m.group(1) if m else None


def clean_cell(value: Any) -> Optional[str]:
    # pandas-facing cleaning wrapper: handle missing values first, then delegate to clean_text.
    #
    # Why it is needed: in pandas a missing value may be None / NaN / pd.NA / NaT. Passing NaN
    # straight into clean_text would stringify it to "nan" and contaminate the CSV -- fatal noise
    # for downstream LLM extraction. This intercepts all such cases.
    if value is None:
        return None
    # NaN is not equal to itself; covers float('nan'), np.nan, etc.
    try:
        if isinstance(value, float) and value != value:
            return None
    except Exception:  # pragma: no cover
        pass
    if pd is not None:
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass  # array-like objects raise here; ignore
    return clean_text(value)


def clean_text(value: Any) -> Optional[str]:
    # Text cleaning: the single entry point for normalising a field.
    #
    # It handles:
    #   1) empty values / empty strings -> None
    #   2) full-width spaces, &nbsp; and other invisible characters -> normal space
    #   3) leftover HTML tags and entities (&amp; etc.)
    #   4) newlines, tabs and runs of whitespace -> a single space
    #   5) leading/trailing meaningless separators such as colons, dashes and commas
    if value is None:
        return None
    if not isinstance(value, str):
        if isinstance(value, float) and value != value:  # NaN
            return None
        value = str(value)

    # (3) Unescape entities, then strip tags (unescape first so that &lt;br&gt; does not slip past)
    value = html_lib.unescape(value)
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]{1,200}>", " ", value)

    # (2) Normalise invisible characters
    value = (
        value.replace("\u3000", " ")   # full-width space
        .replace("\xa0", " ")          # &nbsp;
        .replace("\u200b", "")         # zero-width space
        .replace("\ufeff", "")         # BOM
    )

    # (4) Collapse newlines / tabs / runs of whitespace
    value = re.sub(r"[\r\n\t\f\v]+", " ", value)
    value = re.sub(r"\s{2,}", " ", value)

    # (5) Strip leading/trailing noise symbols
    value = value.strip().strip(":：;；,，-—_|、·.。")

    value = value.strip()
    if value in ("nan", "NaN", "None", "null", "NULL", "-"):
        return None  # last-resort guard: never let a stringified missing value into the data
    return value or None


def normalize_label(text: str) -> str:
    # Normalise label text such as "主要成分：" for comparison against FIELD_LABELS.
    return re.sub(r"[\s:：()（）\[\]【】]", "", text or "")


def simple_retry(max_attempts: int = 3, base_delay: float = 1.5, exceptions=(Exception,)):
    # Custom retry decorator (the fallback when tenacity is missing).
    #
    # Uses exponential backoff with jitter: after the n-th failure it waits roughly
    # base_delay * 2^(n-1) seconds.

    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_exc = exc
                    if attempt >= max_attempts:
                        break
                    wait = base_delay * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
                    LOG.warning(
                        "%s failed on attempt %d/%d: %s; retrying in %.1fs",
                        getattr(func, "__name__", "fetch"), attempt, max_attempts, exc, wait,
                    )
                    time.sleep(wait)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator

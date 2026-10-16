# -*- coding: utf-8 -*-
# tcm_scraper_lib -- implementation package for the drug-label collection script.
# The entry point is the sibling tcm_scraper.py. Modules are split by responsibility:
#     config.py    configuration and constants (selectors, field mapping, defaults)  <- main file to edit for a new site
#     utils.py     logging, text cleaning, rate limiting, retry utilities
#     fetch.py     page fetching (requests / selenium / local file) + robots.txt + empty-result diagnosis
#     parse.py     label-field parsing (selector -> section marker -> label node, three-tier fallback)
#     crawl.py     listing-page pagination, detail-page traversal, local-folder batch parsing
#     pipeline.py  pandas cleaning, de-duplication, CSV export
#     cli.py       command-line arguments, setup wizard, main flow

from __future__ import annotations

__all__ = ["main"]


def main(argv=None):
    # Lazy entry point: importing `tcm_scraper_lib` does not pull in every dependency.
    from .cli import main as _main

    return _main(argv)

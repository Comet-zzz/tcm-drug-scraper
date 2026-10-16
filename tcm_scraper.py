# -*- coding: utf-8 -*-
# tcm_scraper.py -- collector for public drug-label information of Chinese medicines.
#
# Purpose
# Collects label fields for specified Chinese-medicine products from public drug-label listing
# pages (e.g. the NMPA data-query site, Yaopinnet). The keyword is user-defined and no filtering
# is applied by default:
#     drug name / main ingredients / adverse reactions / contraindications / precautions
# Results are cleaned, de-duplicated and exported as a UTF-8-SIG CSV for downstream LLM-based
# toxicity extraction and database construction.
#
# This file is the entry script. The implementation is split by responsibility into the
# tcm_scraper_lib/ package next to it:
#     config.py    configuration and constants (selectors, field mapping, defaults)  <- main file to edit for a new site
#     utils.py     logging, text cleaning, rate limiting, retry utilities
#     fetch.py     page fetching (requests / selenium / local file) + robots.txt + empty-result diagnosis
#     parse.py     label-field parsing (selector -> section marker -> label node, three-tier fallback)
#     crawl.py     listing-page pagination, detail-page traversal, local-folder batch parsing
#     pipeline.py  pandas cleaning, de-duplication, CSV export
#     cli.py       command-line arguments, setup wizard, main flow
#
# Design principles
# 1. Modular: the entry script only bootstraps; fetching / parsing / cleaning / exporting are separate.
# 2. Stability first: random User-Agent + random delay + timeout retries; a single failure never
#    aborts the whole run.
# 3. Fault-tolerant parsing: a missing field returns None instead of raising, and is finally filled
#    with "N/A". Each field falls back through three tiers --
#    "CSS/XPath selector -> section marker (e.g. 【不良反应】) -> label node / table pair".
#
# Disclaimer and compliance (please read)
# * This script is for academic research only. Respect the target site's robots.txt, terms of use
#   and terms of service.
# * Do not send high-frequency requests. A 2-5 second random interval and a per-run page cap are
#   enforced by default; do not lower them.
# * The copyright of the scraped label text belongs to the original rights holders. Do not publicly
#   redistribute the full text; prefer keeping only the structured fields for research.
# * This script is not intended for any medical decision and does not constitute medical advice.
#
# Run
#     python tcm_scraper.py                          # recommended: start the setup wizard
#     python tcm_scraper.py --url "https://..."      # specify the target directly, skip the wizard
#     python tcm_scraper.py --help
#
# Three ways to change the target site (none requires editing code)
#     1. Run the script and enter the new address in the setup wizard;
#     2. Edit config.json in the project root (auto-generated on first run);
#     3. Pass --url "new address" on the command line.

from __future__ import annotations

import sys

from tcm_scraper_lib.cli import main

if __name__ == "__main__":
    sys.exit(main())

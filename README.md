<div align="center">

# TCM Drug-Label Data Collector

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Fetching](https://img.shields.io/badge/fetching-requests%20%7C%20Selenium-informational)
![Output](https://img.shields.io/badge/output-UTF--8--SIG%20CSV-orange)
![Purpose](https://img.shields.io/badge/purpose-academic%20research-lightgrey)

**A single-machine Python tool that turns public Chinese-medicine drug-label pages into a clean, provenance-tagged dataset.**

</div>

## Overview

`tcm_scraper` collects structured label fields for Chinese-medicine (TCM) products from public
drug-label listing pages (e.g. the NMPA data-query site, Yaopinnet), cleans and de-duplicates them,
and exports a UTF-8-SIG CSV. The output is intended as an input dataset for downstream LLM-based
toxicity extraction and database construction.

It supports two routes: an **online crawler** (`requests` / Selenium) and an **offline parser** that
reads pages you saved by hand — so login-protected or paid sources can be used *without* bypassing
authentication.

Collected fields: `drug_name`, `ingredients`, `adverse_reactions`, `contraindications`, `precautions`
— plus `source_url` and `scraped_at` for provenance.

> The tool targets Chinese-language drug-label pages, so the collected **values** and the section
> markers used during parsing (such as `【不良反应】`) are in Chinese. The column names, console
> output and documentation are in English.

## Key features

- **Two collection routes** — an online crawler (`requests` / Selenium) and an offline parser for
  manually saved pages, so login-protected or paid sources are usable without bypassing auth.
- **Three-tier field extraction** — explicit CSS/XPath selectors → Chinese section-marker regex →
  generic label-node / table scan. Extraction still recovers fields when a site's markup changes.
- **Responsible crawling by design** — robots.txt enforcement, rotating User-Agents, a randomised
  2–5 s request interval, and exponential-backoff retries. No login or CAPTCHA bypass.
- **Actionable diagnostics** — a run that yields nothing is classified (login wall / CAPTCHA /
  JS-rendered shell / selector mismatch) instead of failing silently.
- **Research-ready output** — pandas cleaning, business-key de-duplication, and a UTF-8-SIG CSV in
  which every record carries its `source_url` and `scraped_at`.

## Requirements

- Python 3.9+ (verified on Python 3.14.7, Windows)
- If the target site is JS-rendered, **Chrome** must be installed locally (driven by Selenium)

## Install

Use a dedicated virtual environment so the global Python environment stays clean:

```powershell
# Run from the project root
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **Why a virtual environment**: running `pip install selenium` in the global environment can trigger
> pip's dependency backtracking — a silent, output-less phase that is easily mistaken for a hang, and
> can even end in a `ResolutionImpossible` error. A virtual environment avoids these conflicts entirely.
>
> **PyCharm users**: choose `File → Settings → Project → Python Interpreter → Add → Existing`, point it
> at `.venv\Scripts\python.exe`, and the script can be run directly afterwards.

## Quick start

```powershell
# Route A (offline, safest): parse a folder of pages you saved by hand
.venv\Scripts\python.exe tcm_scraper.py --mode folder --url "saved_pages"

# Route B (online): collect from a public listing page
.venv\Scripts\python.exe tcm_scraper.py --url "https://www.yaopinnet.com/zhongyao1/h1.htm" --mode requests --max-items 5

# Or just run it and answer the setup wizard
.venv\Scripts\python.exe tcm_scraper.py
```

## Data fields

| Column | Meaning | Example value |
| --- | --- | --- |
| `drug_name` | Drug name | `黄芩片` |
| `ingredients` | Main ingredients | `黄芩` |
| `adverse_reactions` | Adverse reactions | `偶见皮疹、瘙痒等过敏反应` |
| `contraindications` | Contraindications | `尚不明确` |
| `precautions` | Precautions | `孕妇慎用` |
| `source_url` | Page the record was read from | `https://…/goods.php?id=5156` |
| `scraped_at` | Scrape timestamp | `2026-09-22 00:46:29` |

## Architecture

The entry script only bootstraps; the implementation is split by responsibility into
`tcm_scraper_lib/`.

| Module | Responsibility |
| --- | --- |
| `tcm_scraper.py` | Entry point (`python tcm_scraper.py`) |
| `config.py` | **★ Configuration**: selectors, field aliases, default parameters — the main file to edit for a new site |
| `utils.py` | Logging, text cleaning, rate limiting, retry |
| `fetch.py` | Page fetching (`requests` / Selenium / local file) + robots.txt + empty-result diagnosis |
| `parse.py` | Three-tier field extraction (selector → section marker → label node) |
| `crawl.py` | Listing-page pagination, detail-page traversal, local-folder batch parsing |
| `pipeline.py` | pandas cleaning, de-duplication, CSV export |
| `cli.py` | Command-line arguments, setup wizard, main flow |

## Route A (recommended): save pages manually, batch-extract the fields

**When to use it**: the target database requires login or payment (e.g. the Yaozhi Data database), and
the script neither can nor should bypass the login. You browse and save pages normally in a browser,
and the script only bulk-extracts the five fields into a table. This route never touches the network
and never launches a browser, so there is no account risk.

### A1. Save pages in the browser (Chrome)

1. Log in to the target database, search for a drug, and open its **label** page.
2. **Save only after the page has fully rendered** (pressing `Ctrl+S` before the content loads saves
   an empty frame).
3. Press `Ctrl+S`.
4. Choose the `saved_pages` folder as the destination (create it if it does not exist).
5. Rename the file to the drug name (e.g. `黄芩片`), and pick the "**Webpage, HTML Only**" format.
6. Switch to the next drug and repeat steps 3–5. Saving 3–5 pages first, as a validation run, is
   recommended.

> Using the drug name as the filename is recommended: when the page yields no readable name, the
> script falls back to the filename. Irregular filenames are harmless — the page usually provides the
> name anyway.

When finished, the folder should look like: `saved_pages\黄芩片.html`.
(Do not choose "Webpage, Complete" — it also generates a `_files` folder; harmless but unnecessary.
Also do **not** save as `.mhtml`, which the script does not support.)

### A2. Parse the whole folder

```powershell
.venv\Scripts\python.exe tcm_scraper.py
```

At the first prompt, paste the **folder path** and the script recognises it as "folder mode":

```
[Step 1 / 3] Enter the target address. Three forms are supported:
    1. Search-results page URL (starts with http/https): the script collects online
    2. Local folder path: batch-parse already-saved pages (offline)
    Target > saved_pages
    OK Recognised as a local folder: running in "parse saved pages" mode (offline).
```

To copy the folder path: open the `saved_pages` folder, click the address bar, and copy the full path
(e.g. `D:\projects\tcm-drug-scraper\saved_pages`). The relative path `saved_pages` also works.

Non-interactive invocation (no wizard):

```powershell
.venv\Scripts\python.exe tcm_scraper.py --mode folder --url "saved_pages"
```

### A3. Check the results

Open `output\tcm_drug_data.csv` in Excel (generated automatically, UTF-8-SIG, no mojibake):

- All five fields populated → success
- A field is `N/A` → the page genuinely lacks it, or the selector needs adjustment (see §Selector tuning)
- **A row with only the drug name and all other fields `N/A`** → the page was not fully loaded when
  saved; re-save it in the browser
- A drug is missing → check whether the file was saved as `.mhtml`, or into a different folder

Once the results look right, add more saved pages in the same way and re-run (duplicates are removed
automatically by `drug_name + ingredients`).

## Route B: automated online collection

> **Recommended practice target (already tested)**: the "Chinese-medicine labels" alphabetical listing
> on Yaopinnet — public, no login, static pages, selectors pre-configured:
>
> ```powershell
> .venv\Scripts\python.exe tcm_scraper.py --url "https://www.yaopinnet.com/zhongyao1/h1.htm" --mode requests --no-filter --max-items 5
> ```
>
> That page lists 40 Chinese-medicine labels starting with the letter H (including `黄芩片` Huangqin
> tablets and `黄连上清片`). For older TCM products the "adverse reactions / contraindications"
> sections are often `尚不明确` ("not yet clear") or missing entirely, so an exported `N/A` is normal.

There are **three** ways to specify the target, ordered by preference:

### Option 1: run directly and answer the interactive prompts (recommended)

```powershell
.venv\Scripts\python.exe tcm_scraper.py
```

Paste the URL and press Enter at the prompt:

```
[Step 1 / 3] Enter the target address. Three forms are supported:
    1. Search-results page URL (starts with http/https): the script collects online
    2. Local folder path: batch-parse already-saved pages (offline)
    Type q to quit
    Target >  https://your-search-results-page
```

- After the three questions (target address / herb name / item count), collection starts
- The answers are written to `config.json`, so **subsequent runs can just press Enter**
- To change the address, re-run and enter a new one
- At step 2, pressing Enter keeps the current setting; entering `all` disables filtering

### Option 2: edit `config.json`

On first run the script generates a `config.json` in the same directory, editable with any text
editor, **no Python knowledge required**:

```json
{
  "list_url": "https://your-target-site/search-results",
  "name_filter": "",
  "max_items": 20,
  "max_pages": 3,
  "mode": "selenium",
  "headless": true,
  "min_delay": 2.0,
  "max_delay": 5.0
}
```

Notes:

- `list_url` must point to a **search-results listing page**, not a single drug's detail page
- An empty `name_filter` means no filtering; a keyword (e.g. `黄芩`) collects only matching names
- Wrap the URL in double quotes and do **not** add a trailing comma after the last item
- Changes take effect as soon as the file is saved; no restart needed

### Option 3: specify via command-line arguments (one-off, not persisted)

```powershell
.venv\Scripts\python.exe tcm_scraper.py --url "https://your-target-site/search-results"
```

When the command contains `--url`, no wizard is shown.

## Selector tuning

In Chrome, open the listing page → F12 → select the target element → right-click
`Copy → Copy selector` (CSS) or `Copy → Copy XPath`. Put the result at the **front of the candidate
list** for the corresponding field in `tcm_scraper_lib/config.py`; the old selectors do not need to
be removed:

```python
SELECTORS = {
    "list": {
        "item":      ["table tbody tr"],          # one search-result row
        "item_link": ["a::attr(href)"],           # detail-page link inside a row
        "item_name": ["td.name"],                 # drug name in the row (used for keyword filtering)
        "next_page": ["a.next::attr(href)"],      # next page
    },
    "detail": {
        "drug_name":         ["h1.drug-name", "//h1"],
        "ingredients":       ["div.field-ingredients div.content"],
        "adverse_reactions": ["div.field-adverse div.content"],
        "contraindications": ["div.field-contraindications div.content"],
        "precautions":       ["div.field-precautions div.content"],
    },
}
```

Selector syntax conventions:

| Syntax | How it is parsed |
| --- | --- |
| `div.content`, `table tbody tr` | CSS selector (BeautifulSoup) |
| `//h1`, `//a[contains(text(),'下一页')]`, `xpath://div[@id="x"]` | XPath (requires lxml) |
| `a::attr(href)` | take an attribute instead of the text |

### Debugging

```powershell
# Open a visible browser window to inspect the page and tune selectors
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --no-headless --max-items 2 --log-level DEBUG

# Save the raw HTML: fetch once, then replay parsing with different selectors without re-requesting the site
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --save-raw
```

Even if every selector fails, the script still falls back through three layers — **selector → Chinese
section marker (`【不良反应】`) → label node / table pair** — so a first run usually completes without
a fully tuned configuration.

## Parameters

```powershell
# Simplest: run and paste the URL at the prompt
.venv\Scripts\python.exe tcm_scraper.py

# URL on the command line (no wizard): all items, up to 20 detail pages / 3 listing pages
.venv\Scripts\python.exe tcm_scraper.py --url "https://your-target-site/search-results"

# Fetch only 5 items as a quick validation
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --max-items 5

# Collect a specific herb, paginate 5 pages, export to a custom path
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --name "甘草" --pages 5 --out "output/gancao.csv"
```

Running from PyCharm behaves identically — with no `--url`, the console wizard starts and asks for the
URL.

| Parameter | Description |
| --- | --- |
| (no arguments) | Start the setup wizard (asks for URL, herb name, item count) |
| `--url` | Listing-page URL; or a folder path (with `--mode folder`) |
| `--config` | Path to an alternative config file (one per herb is possible; default `config.json`) |
| `--no-prompt` | Skip the wizard; run purely from config and CLI arguments (for scheduled tasks) |
| `--name` | Keyword filter (no filtering if omitted) |
| `--no-filter` | Disable keyword filtering and collect every item on the page (in PowerShell `--name ""` is dropped, so use this flag) |
| `--max-items` | Max number of detail pages to fetch, default 20 (increase gradually) |
| `--pages` | Max number of listing pages to paginate, default 3 |
| `--mode` | `selenium` (default, dynamic pages) / `requests` (fast, static pages) / `local` (single local file) / `folder` (parse a folder of manually saved pages) |
| `--min-delay` / `--max-delay` | Random delay range between requests, default 2–5 s (**do not lower**) |
| `--retries` | Max retries per request, default 3 |
| `--timeout` | Per-page timeout in seconds, default 30 |
| `--no-headless` | Show the browser window (for debugging) |
| `--save-raw` | Save raw HTML to `output/raw_html/` |
| `--ignore-robots` | Skip the robots.txt check (use with caution) |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## Output

- File: `output/tcm_drug_data.csv`
- Encoding: **UTF-8-SIG (with BOM)** so Excel opens the Chinese text without mojibake
- Missing values: filled with `N/A` (note that `N/A` is Excel's/pandas' default NA marker; read with
  `keep_default_na=False` to preserve it)
- De-duplication: first by `drug_name + ingredients`, then by `drug_name + source_url`
- Records without a drug name are dropped (the name is the primary key for downstream LLM extraction)
- Logs: written to the console and to `logs/tcm_scraper.log` (UTF-8, with per-field hit/miss detail)

Example (truncated):

```csv
drug_name,ingredients,adverse_reactions,contraindications,precautions,source_url,scraped_at
复方丹参滴丸,丹参、三七、冰片,上市后不良反应监测数据显示…,对本品及所含成份过敏者禁用…,孕妇慎用…,https://www.hnysfww.com/mobile/goods.php?id=5361,2026-09-22 00:46:58
```

Read example:

```python
import pandas as pd
df = pd.read_csv("output/tcm_drug_data.csv", encoding="utf-8-sig")
print(df.shape, df.columns.tolist())
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Table has the header row but no data | Look at the classified diagnosis at the end of the log: login wall / CAPTCHA / empty-shell page / selector mismatch — the script names the concrete reason |
| Need to change the URL | Re-run and enter the new URL, or edit `list_url` in `config.json` |
| Asked for the URL on every run | Wizard answers are saved to `config.json`; afterwards just press Enter. To disable prompting entirely, add `--no-prompt` (requires `--url`) |
| Exits without waiting for input | The environment is non-interactive (e.g. a scheduled task). Use `--url "..." --no-prompt` |
| `config.json` is broken | Harmless: the script warns and continues with defaults. Delete the file and re-run to regenerate it |
| Garbled log text | Console code-page issue. Run `chcp 65001`; `logs/tcm_scraper.log` is always valid UTF-8 |
| 0 items collected / mostly `N/A` | Route A: see §A3; Route B: see §Selector tuning |
| Login wall or CAPTCHA | The script neither logs in nor bypasses CAPTCHAs. **Use Route A** (log in and save pages manually) |
| ChromeDriver fails to start | Make sure Chrome is installed and the network is available; the script tries Selenium Manager first, then falls back to webdriver-manager |
| Cannot paginate / duplicate data | `next_page` selector mismatch, or the site uses JS pagination (find the real endpoint via F12 → Network and switch to `--mode requests`) |
| Want it to run faster | Raising `--max-items` will not help — **the bottleneck is deliberate rate limiting**. Keep the 2–5 s interval; it is what avoids bans |

## Compliance and ethics

- This script is for academic research only. Respect the target site's `robots.txt`, terms of use, and
  terms of service (the robots check is enabled by default).
- The default 2–5 s random delay and the per-page cap are deliberate; **do not lower them**.
  High-frequency requests may get your IP banned and degrade the service for other users.
- The copyright of the scraped label text belongs to the original rights holders. Prefer keeping only
  the structured fields for research; do not publicly redistribute the full text.
- This script and its output **are not medical advice** and must not be used for clinical decisions.

## Repository structure

```
tcm-drug-scraper/
├── tcm_scraper.py          # entry script (run with: python tcm_scraper.py)
├── tcm_scraper_lib/        # implementation, split by responsibility
├── config.json             # user config (URL, herb name, item count); auto-generated on first run
├── requirements.txt        # dependency list
├── README.md               # this document
├── saved_pages/            # Route A: manually saved web pages (create it yourself)
├── output/                 # CSV and raw-HTML output (safe to delete)
└── logs/                   # run logs (safe to delete)
```

Suggested `.gitignore`:

```gitignore
.venv/
output/
logs/
__pycache__/
```

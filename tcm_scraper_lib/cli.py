# -*- coding: utf-8 -*-
# Main program: command-line arguments, config.json read/write, the interactive setup wizard, and
# the main flow.

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .config import CONFIG_FILE_NAME, DEFAULT_CONFIG, PERSISTED_KEYS, PROJECT_ROOT, SELECTORS
from .crawl import Crawler, crawl_folder
from .fetch import BaseFetcher, build_fetcher, is_local_url, looks_like_folder
from .pipeline import build_dataframe, export_csv
from .utils import LOG, setup_logging


def config_file_path(custom: Optional[str] = None) -> Path:
    # Config-file location: next to the script by default; --config can point elsewhere.
    return Path(custom) if custom else PROJECT_ROOT / CONFIG_FILE_NAME


def load_config_file(cfg_path: Path, logger: logging.Logger) -> Dict[str, Any]:
    # Read config.json.
    #
    # Fault-tolerance principle: if the config file is corrupt, mistyped or missing, only warn and
    # continue with the defaults -- a config problem never aborts a collection run.
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse config file %s (%s); using defaults for this run", cfg_path.name, exc)
        return {}
    except OSError as exc:
        logger.warning("Failed to read config file %s (%s); using defaults for this run", cfg_path.name, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("Config file %s has the wrong format (expected a JSON object); ignoring it", cfg_path.name)
        return {}

    clean: Dict[str, Any] = {}
    for key, value in data.items():
        if key.startswith("_"):  # keys starting with an underscore are treated as notes
            continue
        if key not in PERSISTED_KEYS:
            logger.warning("Config key \"%s\" is not a recognised setting; ignoring it", key)
            continue
        clean[key] = value
    return clean


def save_config_file(cfg_path: Path, cfg: Dict[str, Any], logger: logging.Logger) -> None:
    # Write the user's settings back to config.json so the next run reuses them.
    data: Dict[str, Any] = {
        "_note": (
            "This line is a note and does not affect execution. Usually no manual edit is needed: "
            "just run tcm_scraper.py and answer the setup wizard, which saves the settings automatically."
        ),
        "_list_url": "Target page address (search-results listing page)",
        "_name_filter": "Herb name used for filtering; leave empty for no filtering (collect all items)",
        "_max_items": "Maximum number of items to collect per run",
    }
    for key in PERSISTED_KEYS:
        if key in cfg:
            data[key] = cfg[key]

    try:
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("Config saved to %s (press Enter to reuse it next time)", cfg_path.name)
    except OSError as exc:  # pragma: no cover - read-only disk and similar edge cases
        logger.warning("Failed to save settings (does not affect this run): %s", exc)


def _ask(prompt: str) -> str:
    # Safe input helper: handles two exceptional cases.
    #
    # * Non-interactive environment (e.g. called from a scheduled task with no keyboard) -> suggest
    #   command-line arguments and exit, instead of hanging on an Enter that never comes.
    # * Ctrl+C -> clean exit.
    try:
        return input(prompt)
    except EOFError:
        print()
        raise SystemExit(
            "No standard input detected (this environment may not support interactive input).\n"
            "Run with command-line arguments instead, for example:\n"
            '    python tcm_scraper.py --url "https://target-site" --no-prompt'
        )
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


def run_setup_wizard(cfg: Dict[str, Any], cfg_path: Path) -> None:
    # Interactive setup wizard: collects "target address / herb name / item count" and saves them to config.json.
    #
    # Purpose: change the target site without editing code.
    line = "=" * 64
    print()
    print(line)
    print(" TCM Label Collector - Setup Wizard")
    print(line)

    # ---------------- Step 1: page address ----------------
    prev_url = str(cfg.get("list_url") or "").strip()
    if prev_url == DEFAULT_CONFIG["list_url"]:
        prev_url = ""  # the placeholder does not count as "the address used last time"

    while True:
        print()
        print("[Step 1 / 3] Enter the target address. Three forms are supported:")
        print("    1. Search-results page URL (starts with http/https): the script collects online")
        print("    2. Local folder path: batch-parse already-saved pages (offline)")
        if prev_url:
            print(f"    3. Press Enter: reuse the previous address {prev_url}")
        print("    Type q to quit")
        answer = _ask("    Target > ").strip().strip('"').strip("'")

        if answer.lower() == "q":
            print("Exited; nothing was collected.")
            raise SystemExit(0)
        if not answer and prev_url:
            cfg["list_url"] = prev_url
            cfg["mode"] = "folder" if looks_like_folder(prev_url) else cfg.get("mode", "selenium")
            break
        if not answer:
            print("    x Input cannot be empty: paste a URL or a local folder path.")
            continue

        # Case 1: a local folder -> "parse already-saved pages" mode
        if looks_like_folder(answer):
            cfg["list_url"] = answer
            cfg["mode"] = "folder"
            print("    OK Recognised as a local folder: running in \"parse saved pages\" mode (offline).")
            break
        # Case 2: a single local page file
        if is_local_url(answer):
            cfg["list_url"] = answer
            print("    OK Recognised as a local page file.")
            break
        # Case 3: a URL
        if answer.startswith(("http://", "https://")):
            cfg["list_url"] = answer
            print("    OK URL recorded.")
            break
        print("    x Invalid input: it is neither a URL nor an existing local folder or file.")
        print("      To copy a folder path: hold Shift, right-click the folder in Explorer -> \"Copy as path\".")

    # ---------------- Step 2: herb name ----------------
    print()
    print("[Step 2 / 3] Enter the herb name to filter by:")
    print(f"    Press Enter: {cfg['name_filter'] or 'no filtering (collect all items on the listing page)'}")
    if cfg["name_filter"]:
        print("    Type \"all\": no filtering")
    answer = _ask("    Herb name > ").strip()
    if answer:
        if answer in ("all", "any", "*", "全部", "不限"):
            cfg["name_filter"] = ""
            print("    OK Recorded: no filtering.")
        else:
            cfg["name_filter"] = answer
            print(f"    OK Recorded: {answer}.")
    else:
        print(f"    OK Keeping the current setting: {cfg['name_filter'] or 'no filtering'}.")

    # ---------------- Step 3: item count ----------------
    print()
    print("[Step 3 / 3] Enter the maximum number of items to collect per run:")
    print("    A first run of 5 is recommended; increase it once the results look right.")
    print(f"    Press Enter: {cfg['max_items']} items")
    answer = _ask("    Count > ").strip()
    if answer:
        if answer.isdigit() and int(answer) > 0:
            cfg["max_items"] = int(answer)
            print(f"    OK Recorded: {answer} items.")
        else:
            print(f"    x Invalid input: a positive integer is required; keeping {cfg['max_items']} for now.")
    else:
        print(f"    OK Keeping the current setting: {cfg['max_items']} items.")

    save_config_file(cfg_path, cfg, LOG)
    print()
    print(line)
    print(" Setup complete. Starting collection with:")
    if cfg.get("mode") == "folder":
        print(f"   Target:   {cfg['list_url']} (local folder, offline)")
    else:
        print(f"   Target:   {cfg['list_url']}")
    print(f"   Herb filter: {cfg['name_filter'] or '(none)'}")
    print(f"   Limit:    {cfg['max_items']} items / at most {cfg['max_pages']} pages")
    print(f"   Output:   {Path(cfg['out_csv']).resolve()}")
    print("   These settings are saved; next time you can just press Enter.")
    print(line)
    print(" Press Ctrl+C at any time to stop; run log: logs/tcm_scraper.log")
    print()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    # Command-line arguments: every tunable can be overridden here, while the config area stays the default.
    parser = argparse.ArgumentParser(
        description="Collector for public Chinese-medicine drug-label information (standalone)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        help="Listing-page URL; or a local folder path (with --mode folder) to parse saved pages",
    )
    parser.add_argument("--config", help=f"Config-file location; defaults to {CONFIG_FILE_NAME} next to the script")
    parser.add_argument("--no-prompt", action="store_true", help="Skip the setup wizard and run purely from the config file and CLI arguments (for scheduled tasks)")
    parser.add_argument("--out", help="Output CSV path (default output/tcm_drug_data.csv)")
    parser.add_argument(
        "--mode",
        choices=["selenium", "requests", "local", "folder"],
        help="Page-fetching mode: folder = parse saved pages in a local folder (offline)",
    )
    parser.add_argument("--max-items", type=int, help="Maximum number of detail pages to fetch")
    parser.add_argument("--pages", type=int, help="Maximum number of listing pages to paginate")
    parser.add_argument("--name", help="Keyword filter (no filtering if omitted)")
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Disable keyword filtering and collect every item on the page (in PowerShell --name \"\" is dropped, so use this flag)",
    )
    parser.add_argument("--min-delay", type=float, help="Lower bound of the random delay between requests (seconds)")
    parser.add_argument("--max-delay", type=float, help="Upper bound of the random delay between requests (seconds)")
    parser.add_argument("--timeout", type=int, help="Per-page timeout (seconds)")
    parser.add_argument("--retries", type=int, help="Maximum retries per request")
    parser.add_argument("--no-headless", action="store_true", help="Show the browser window (for tuning selectors)")
    parser.add_argument("--save-raw", action="store_true", help="Save raw HTML to output/raw_html/")
    parser.add_argument("--ignore-robots", action="store_true", help="Skip the robots.txt check (use with caution)")
    parser.add_argument("--log-level", default="INFO", help="Log level: DEBUG/INFO/WARNING/ERROR")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Dict[str, Any]:
    # Assemble the final configuration. Priority, lowest to highest:
    #
    #     built-in defaults  ->  config.json  ->  command-line arguments  ->  setup wizard (asks only
    #     for what is still unset)
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = config_file_path(args.config)

    # (1) config.json (auto-generated after the first run, editable in any text editor)
    file_cfg = load_config_file(cfg_path, LOG)
    if file_cfg:
        LOG.info("Loaded config file: %s", cfg_path.resolve())
        cfg.update(file_cfg)

    # (2) command-line arguments
    overrides = {
        "list_url": args.url,
        "out_csv": args.out,
        "mode": args.mode,
        "max_items": args.max_items,
        "max_pages": args.pages,
        "name_filter": args.name,
        "min_delay": args.min_delay,
        "max_delay": args.max_delay,
        "timeout": args.timeout,
        "max_retries": args.retries,
    }
    for key, value in overrides.items():
        if value is not None:
            cfg[key] = value

    if args.no_filter:
        cfg["name_filter"] = ""
    if args.no_headless:
        cfg["headless"] = False
    if args.save_raw:
        cfg["save_raw_html"] = True
    if args.ignore_robots:
        cfg["respect_robots"] = False

    # (3) interactive setup wizard: skipped when the target address is already given on the command line
    if not args.no_prompt and args.url is None:
        try:
            run_setup_wizard(cfg, cfg_path)
        except SystemExit:
            raise
        except Exception as exc:  # a wizard error must not block the task
            LOG.warning("Setup wizard error (%s); continuing with the existing configuration", exc)

    # Convert the detail-page wait_for into the CSS selector Selenium needs
    cfg["wait_css"] = SELECTORS.get("detail", {}).get("wait_for", "")
    cfg["log_level"] = args.log_level
    return cfg


def run(cfg: Dict[str, Any]) -> int:
    # Main flow: initialise -> collect links -> fetch details -> clean -> export. Returns an exit code.
    logger = setup_logging(cfg.get("log_file"), cfg.get("log_level", "INFO"))
    logger.info("=" * 78)
    logger.info("Collection task starting | URL=%s", cfg["list_url"])
    logger.info(
        "Parameters: mode=%s headless=%s delay=%.1f~%.1fs retries=%d limit=%s items/%s pages keyword=%r",
        cfg["mode"], cfg["headless"], cfg["min_delay"], cfg["max_delay"],
        cfg["max_retries"], cfg["max_items"], cfg["max_pages"], cfg["name_filter"],
    )
    logger.info("=" * 78)

    if not cfg["headless"] and cfg["mode"] == "selenium":
        logger.info("Browser window enabled: adjust selectors in config.SELECTORS against the page (F12).")

    fetcher: Optional[BaseFetcher] = None
    try:
        # Determine the actual mode for this run.
        # Note: config.json may retain "folder mode" from last time while the current target is not a
        # folder; in that case the mode must be corrected automatically, otherwise the run collects 0
        # items for no visible reason.
        target = str(cfg.get("list_url") or "")
        if cfg["mode"] == "folder" and not looks_like_folder(target):
            cfg["mode"] = "local" if is_local_url(target) else "selenium"
            logger.warning(
                "The config saved \"folder mode\", but this target is not a folder; switching to %s automatically.", cfg["mode"]
            )

        if cfg["mode"] == "folder" or looks_like_folder(target):
            # Route A: folder mode
            # Pages saved manually in a browser -> parsed one by one, fully offline, no rate limiting
            logger.info("Run mode: folder mode (parsing locally saved pages; offline, no browser)")
            records = crawl_folder(cfg, logger)
            total = len(records)
        else:
            # Route B: online collection
            fetcher = build_fetcher(cfg, logger)
            crawler = Crawler(cfg, fetcher, logger)

            # Step 1: collect detail-page links (including pagination)
            links = crawler.collect_detail_links(cfg["list_url"])
            logger.info("Collected %d detail page(s) to fetch", len(links))
            if not links:
                logger.error(
                    "No pages collected. See the classified diagnosis above "
                    "(login wall / CAPTCHA / dynamic loading / selector mismatch)."
                )

            # Step 2: fetch and parse details (with rate limiting and retries)
            records = crawler.crawl_details(links)
            total = len(links)

        # Step 3: clean + de-duplicate
        df, columns = build_dataframe(records, logger)

        # Step 4: export
        export_csv(df, columns, cfg["out_csv"], logger)

        logger.info("Task finished: %d succeeded / %d targeted", len(df), total)
        return 0 if len(df) else 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by the user (Ctrl+C): data collected so far is not exported. Reduce --max-items to run in batches.")
        return 130
    except Exception as exc:
        logger.exception("Task aborted with an exception: %s", exc)
        return 1
    finally:
        if fetcher is not None:
            fetcher.quit()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not str(args.log_level).upper() in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        args.log_level = "INFO"

    # Set up logging first: warnings from reading the config file and wizard messages both need it
    setup_logging(DEFAULT_CONFIG["log_file"], args.log_level)
    cfg = build_config(args)
    setup_logging(cfg.get("log_file"), args.log_level)  # takes effect if the config changed the log location
    return run(cfg)

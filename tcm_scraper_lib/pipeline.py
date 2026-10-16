# -*- coding: utf-8 -*-
# Data-processing layer: pandas cleaning + de-duplication + CSV export.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Sequence

from .config import FIELD_COLUMNS
from .utils import clean_cell

try:
    import pandas as pd
except ImportError:  # pragma: no cover - actionable message when the environment lacks it
    pd = None  # type: ignore[assignment]


def build_dataframe(records: Sequence[Dict[str, Any]], logger: logging.Logger):
    # Turn the parsed records into a DataFrame with uniform cleaning, missing-value stats and de-duplication.
    if pd is None:
        raise RuntimeError("pandas is not installed. Run: pip install pandas")

    columns = list(FIELD_COLUMNS.values()) + ["source_url", "scraped_at"]
    if not records:
        logger.warning("No records collected; exporting an empty CSV with only the header row")
        return pd.DataFrame(columns=columns), columns

    df = pd.DataFrame(records)

    # 1) Column order and fill in any missing columns
    for col in columns:
        if col not in df.columns:
            df[col] = None
    df = df[columns]

    # 2) Second-pass cleaning per field (a selector hit may still contain newlines, full-width spaces, etc.)
    text_cols = list(FIELD_COLUMNS.values())
    for col in text_cols:
        df[col] = df[col].map(clean_cell)

    # 3) Missing-value stats (written to the log to tell a failed selector from a genuinely empty page)
    missing_report = {col: int(df[col].isna().sum()) for col in text_cols}
    logger.info("Missing-value stats: %s", missing_report)

    # 4) Drop rows with no drug name (the name is the primary key for downstream LLM extraction)
    before = len(df)
    df = df[df["drug_name"].notna() & (df["drug_name"].astype(str).str.strip() != "")]
    dropped = before - len(df)
    if dropped:
        logger.warning("Dropped %d record(s) without a drug name", dropped)

    # 5) De-duplication: first by the business key "drug_name + ingredients", then by the whole row
    before = len(df)
    df = df.drop_duplicates(subset=[c for c in ["drug_name", "ingredients"] if c in df.columns], keep="first")
    df = df.drop_duplicates(subset=["drug_name", "source_url"], keep="first")
    logger.info("De-duplication: %d -> %d rows", before, len(df))

    # 6) Sort + reset index
    df = df.sort_values(by=["drug_name"], kind="stable").reset_index(drop=True)
    return df, columns


def export_csv(df, columns: Sequence[str], out_path: str, logger: logging.Logger) -> Path:
    # Export the CSV.
    #
    # Key point: encoding="utf-8-sig" -- includes the BOM so Excel opens the Chinese text without
    # mojibake. Missing values are filled with "N/A" so downstream LLM extraction can tell "the page
    # did not state it" from "parsing failed".
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    out = df.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = "N/A"
    out = out[list(columns)].fillna("N/A")

    out.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("Exported %d record(s) -> %s (UTF-8-SIG)", len(out), path.resolve())
    return path

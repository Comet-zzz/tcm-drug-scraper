# -*- coding: utf-8 -*-
# 数据处理区：pandas 清洗 + 去重 + CSV 导出。

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Sequence

from .config import FIELD_COLUMNS
from .utils import clean_cell

try:
    import pandas as pd
except ImportError:  # pragma: no cover - 环境缺失时给出可执行提示
    pd = None  # type: ignore[assignment]


def build_dataframe(records: Sequence[Dict[str, Any]], logger: logging.Logger):
    # 把解析结果列表转成 DataFrame，做统一清洗、缺失统计与去重。
    if pd is None:
        raise RuntimeError("未安装 pandas，请执行：pip install pandas")

    columns = list(FIELD_COLUMNS.values()) + ["数据来源URL", "抓取时间"]
    if not records:
        logger.warning("没有采集到任何记录，仅导出带表头的空 CSV")
        return pd.DataFrame(columns=columns), columns

    df = pd.DataFrame(records)

    # 1) 列顺序与缺列补齐
    for col in columns:
        if col not in df.columns:
            df[col] = None
    df = df[columns]

    # 2) 逐字段二次清洗（选择器命中的文本可能仍含换行、全角空格等）
    text_cols = list(FIELD_COLUMNS.values())
    for col in text_cols:
        df[col] = df[col].map(clean_cell)

    # 3) 缺失统计（写入日志，方便判断是选择器失效还是页面本身没写）
    missing_report = {col: int(df[col].isna().sum()) for col in text_cols}
    logger.info("字段缺失统计：%s", missing_report)

    # 4) 丢弃无药品名称的行（名称是后续 LLM 抽取的主键，缺失则无研究价值）
    before = len(df)
    df = df[df["药品名称"].notna() & (df["药品名称"].astype(str).str.strip() != "")]
    dropped = before - len(df)
    if dropped:
        logger.warning("丢弃 %d 条无药品名称的记录", dropped)

    # 5) 去重：先按「药品名称 + 主要成分」业务键去重，再按整行去重
    before = len(df)
    df = df.drop_duplicates(subset=[c for c in ["药品名称", "主要成分"] if c in df.columns], keep="first")
    df = df.drop_duplicates(subset=["药品名称", "数据来源URL"], keep="first")
    logger.info("去重：%d → %d 条", before, len(df))

    # 6) 排序 + 重置索引
    df = df.sort_values(by=["药品名称"], kind="stable").reset_index(drop=True)
    return df, columns


def export_csv(df, columns: Sequence[str], out_path: str, logger: logging.Logger) -> Path:
    # 导出 CSV。
    #
    # 关键点：encoding="utf-8-sig" —— 带 BOM，Excel 双击打开中文不乱码。
    # 缺失值统一填 "N/A"，便于后续 LLM 抽取时区分「页面没写」与「解析失败」。
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    out = df.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = "N/A"
    out = out[list(columns)].fillna("N/A")

    out.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info("已导出 %d 条记录 → %s（UTF-8-SIG）", len(out), path.resolve())
    return path

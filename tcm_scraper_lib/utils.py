# -*- coding: utf-8 -*-
# 通用工具：日志、文本清洗、限速休眠、选择器语法辅助、重试装饰器。

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

# 第三方库采用「延迟导入 / 容错导入」，保证任何一个可选依赖缺失时脚本仍能给出清晰提示，
# 而不是在最顶层直接 ImportError 崩溃。
try:
    import pandas as pd
except ImportError:  # pragma: no cover - 环境缺失时给出可执行提示
    pd = None  # type: ignore[assignment]


def setup_logging(log_file: Optional[str], level: str = "INFO") -> logging.Logger:
    # 初始化 logging：同时输出到控制台和文件，便于事后排查失败原因。
    logger = logging.getLogger("tcm_scraper")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()  # 避免重复调用时日志重复打印
    logger.propagate = False

    # Windows 中文控制台的编码兜底：极端情况下（stdout 为 ASCII）中文日志会抛
    # UnicodeEncodeError 并中断任务，这里统一改成「无法编码就替换」，保证任务不因日志挂掉。
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - 某些环境不支持 reconfigure
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


LOG = logging.getLogger("tcm_scraper")  # 模块级 logger，setup_logging 后生效


def random_ua() -> str:
    # 从 UA 池中随机取一个 User-Agent。
    return random.choice(USER_AGENTS)


def polite_sleep(min_delay: float, max_delay: float) -> None:
    # 请求之间的随机休眠：把固定节奏变成随机节奏，降低被封风险，也减轻对方服务器压力。
    delay = random.uniform(min_delay, max_delay)
    LOG.debug("休眠 %.2f 秒后继续…", delay)
    time.sleep(delay)


def is_xpath(selector: str) -> bool:
    # 根据前缀判断选择器类型：XPath 还是 CSS。
    s = selector.strip()
    if s.startswith("xpath:"):
        return True
    return s.startswith("//") or s.startswith("./") or s.startswith("(/") or s.startswith("(")


def strip_xpath_prefix(selector: str) -> str:
    # 去掉自定义的 'xpath:' 前缀。
    s = selector.strip()
    if s.startswith("xpath:"):
        return s[len("xpath:"):].strip()
    return s


def wants_attr(selector: str) -> Optional[str]:
    # 解析自定义的 '::attr(xxx)' 后缀，用于「取属性而不是取文本」。
    # 例：'a::attr(href)' → 返回 'href'，并把选择器主体截断为 'a'。
    m = re.search(r"::attr\(([^)]+)\)\s*$", selector)
    return m.group(1) if m else None


def clean_cell(value: Any) -> Optional[str]:
    # 面向 pandas 单元格的清洗包装：先处理缺失值，再走 clean_text。
    #
    # 为什么需要它：在 pandas 中缺失值可能是 None / NaN / pd.NA / NaT，
    # 如果直接把 NaN 传进 clean_text，会被 str() 转成字符串 "nan" 混进 CSV，
    # 这对后续 LLM 抽取是致命的噪声。这里统一拦截。
    if value is None:
        return None
    # NaN 自反不等；同时覆盖 float('nan')、np.nan 等情况
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
            pass  # 数组类对象会抛异常，忽略即可
    return clean_text(value)


def clean_text(value: Any) -> Optional[str]:
    # 文本清洗：统一的字段规整入口。
    #
    # 处理内容：
    #   1) 空值 / 空串 → None
    #   2) 全角空格、&nbsp; 等不可见字符 → 半角空格
    #   3) 残留 HTML 标签与 HTML 实体（&amp; 等）
    #   4) 换行符、制表符、连续空白 → 单个空格
    #   5) 去掉首尾的冒号、破折号、逗号等无意义分隔符
    if value is None:
        return None
    if not isinstance(value, str):
        if isinstance(value, float) and value != value:  # NaN
            return None
        value = str(value)

    # 3) HTML 实体还原 + 标签剥离（先还原实体，避免 &lt;br&gt; 类残留逃过标签清洗）
    value = html_lib.unescape(value)
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]{1,200}>", " ", value)

    # 2) 不可见字符归一
    value = (
        value.replace("\u3000", " ")   # 全角空格
        .replace("\xa0", " ")          # &nbsp;
        .replace("\u200b", "")         # 零宽空格
        .replace("\ufeff", "")         # BOM
    )

    # 4) 换行 / 制表 / 连续空白折叠
    value = re.sub(r"[\r\n\t\f\v]+", " ", value)
    value = re.sub(r"\s{2,}", " ", value)

    # 5) 去首尾噪声符号
    value = value.strip().strip(":：;；,，-—_|、·.。")

    value = value.strip()
    if value in ("nan", "NaN", "None", "null", "NULL", "-"):
        return None  # 兜底：杜绝缺失值被字符串化后混入数据
    return value or None


def normalize_label(text: str) -> str:
    # 把「主要成分：」这类标签文本归一化，用于和 FIELD_LABELS 比对。
    return re.sub(r"[\s:：()（）\[\]【】]", "", text or "")


def simple_retry(max_attempts: int = 3, base_delay: float = 1.5, exceptions=(Exception,)):
    # 自定义重试装饰器（tenacity 缺失时的降级方案）。
    #
    # 采用指数退避 + 随机抖动：第 n 次失败后等待 base_delay * 2^(n-1) 秒左右。

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
                        "%s 第 %d/%d 次尝试失败：%s；%.1f 秒后重试",
                        getattr(func, "__name__", "fetch"), attempt, max_attempts, exc, wait,
                    )
                    time.sleep(wait)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator

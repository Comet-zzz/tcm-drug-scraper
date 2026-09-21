# -*- coding: utf-8 -*-
# 爬取主逻辑区：列表页翻页与详情页遍历（在线抓取），以及本地文件夹批量解析（离线）。

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from .config import FIELD_COLUMNS, SELECTORS
from .fetch import BaseFetcher, RequestsFetcher, diagnose_empty_result, read_html_file, robots_allows
from .parse import DrugPageParser, SelectorEngine
from .utils import clean_text, polite_sleep, random_ua

# 文件夹模式支持的网页后缀
SUPPORTED_PAGE_SUFFIXES: Tuple[str, ...] = (".html", ".htm", ".xhtml")


def crawl_folder(cfg: Dict[str, Any], logger: logging.Logger) -> List[Dict[str, Any]]:
    # 文件夹模式：把本地文件夹里的**每一个网页文件当成一份说明书**直接解析。
    #
    # 这是为「人工登录后另存页面」这条路线准备的：
    #     * 不需要列表页，不需要链接配对，不发起任何网络请求；
    #     * 文件名会作为药品名称的兜底（所以建议存成「黄芩片.html」这样的名字）；
    #     * 单个文件解析失败只跳过它，不影响其他文件。
    #
    # 适用场景：目标数据库需要登录/付费，脚本不便（也不应）绕过时，
    # 由使用者在浏览器中正常浏览并保存页面，脚本仅负责批量抽取字段。
    folder = Path(cfg["list_url"])
    if not folder.exists():
        logger.error(
            "文件夹不存在：%s\n            请检查路径是否正确（Windows 路径示例：D:\\项目\\saved_pages），或先创建文件夹并保存页面。",
            folder,
        )
        return []
    try:
        files = sorted(
            p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_PAGE_SUFFIXES
        )
    except OSError as exc:
        logger.error("读取文件夹失败：%s（%s）", folder, exc)
        return []

    if not files:
        logger.error(
            "文件夹 %s 中未找到网页文件（支持 %s）。\n"
            "            请确认：① 保存位置是否正确；② Chrome 另存为时是否选择「网页，仅 HTML」而非 .mhtml。",
            folder.resolve(), "、".join(SUPPORTED_PAGE_SUFFIXES),
        )
        return []

    logger.info("文件夹模式：在 %s 下发现 %d 个网页文件，逐个解析（不联网）", folder.resolve(), len(files))
    keyword = (cfg.get("name_filter") or "").strip()
    if keyword:
        logger.info(
            "说明：文件夹模式不进行关键词筛选，解析范围即所选文件夹中的全部网页（配置中的关键词「%s」不生效）",
            keyword,
        )
    parser = DrugPageParser(SELECTORS, logger)
    records: List[Dict[str, Any]] = []

    for idx, path in enumerate(files, start=1):
        logger.info("--- [%d/%d] 解析本地页面：%s", idx, len(files), path.name)
        try:
            html = read_html_file(path)
        except OSError as exc:
            logger.error("读取失败，已跳过：%s（%s）", path.name, exc)
            continue

        try:
            # 文件名当药品名称兜底：所以建议把文件存成「药品名.html」
            record = parser.parse(html, str(path.resolve()), hint_name=path.stem)
        except Exception as exc:
            logger.exception("解析异常，已跳过：%s（%s）", path.name, exc)
            continue

        filled = sum(1 for col in FIELD_COLUMNS.values() if record.get(col))
        logger.info("解析完成：%s（有效字段 %d/5）", record.get("药品名称") or path.stem, filled)
        if filled <= 1:
            logger.warning(
                "该文件仅解析出药品名称，可能是保存时页面尚未加载完成，或保存的是登录页：%s", path.name,
            )
        records.append(record)

    logger.info("文件夹模式解析完毕：%d/%d 个文件产出了记录", len(records), len(files))
    return records


@dataclass
class DetailLink:
    # 列表页解析出的一个待抓取条目。

    url: str
    title: str = ""    # 链接锚文本（如「查看说明书」）
    context: str = ""  # 所在条目容器的全文（含药品名称，用于关键词过滤）
    name: str = ""     # 从条目中单独提取的药品名称（可用则为详情页兜底）


class Crawler:
    # 爬取调度器：负责列表页翻页、详情页遍历、以及全程的限速与容错。

    def __init__(self, cfg: Dict[str, Any], fetcher: BaseFetcher, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.fetcher = fetcher
        self.logger = logger
        self.parser = DrugPageParser(SELECTORS, logger)

    # ---------------- 列表页 ----------------
    def collect_detail_links(self, list_url: str) -> List[DetailLink]:
        # 翻页收集详情页链接，受 max_pages / max_items / name_filter 约束。
        links: List[DetailLink] = []
        seen: set = set()
        current_url: Optional[str] = list_url
        page_no = 1
        parsed_count = 0    # 列表页上总共看到多少个条目
        filtered_count = 0  # 其中被关键词筛掉多少个

        while current_url and page_no <= int(self.cfg["max_pages"]):
            self.logger.info("--- 列表页第 %d 页：%s", page_no, current_url)
            if self.cfg["respect_robots"] and not robots_allows(current_url, random_ua(), self.logger):
                self.logger.error("robots.txt 不允许抓取该列表页，已停止。")
                break

            try:
                html = self._fetch_with_retry(current_url)
            except Exception as exc:
                self.logger.error("列表页抓取失败，停止翻页：%s", exc)
                break

            self._maybe_dump_html(html, current_url, f"list_p{page_no}")
            page_links = self._parse_detail_links(html, current_url)
            parsed_count += len(page_links)
            self.logger.info("第 %d 页解析到 %d 个候选条目", page_no, len(page_links))

            # 一个条目都没解析出来：先判断页面本身是什么情况，再给出针对性提示
            if not page_links:
                diagnose_empty_result(html, current_url, self.logger)
                break

            for link in page_links:
                key = link.url.split("#")[0]
                if key in seen:
                    continue
                if not self._passes_name_filter(link):
                    filtered_count += 1
                    self.logger.debug("关键词过滤排除：%s", link.context or link.url)
                    continue
                seen.add(key)
                links.append(link)
                if len(links) >= int(self.cfg["max_items"]):
                    self.logger.info("已达 max_items=%s 上限，停止收集链接", self.cfg["max_items"])
                    return links

            current_url = self._parse_next_page(html, current_url)
            page_no += 1
            if current_url:
                polite_sleep(self.cfg["min_delay"], self.cfg["max_delay"])

        # 解析到了条目、但全被关键词筛掉：也要说清楚，免得用户以为脚本坏了
        if not links and filtered_count > 0:
            keyword = (self.cfg.get("name_filter") or "").strip()
            self.logger.error(
                "页面上解析到 %d 个条目，但全部被关键词「%s」筛除，未保留任何条目。\n"
                "            如需采集全部条目：在设置向导第 2 步输入「全部」，或使用 --no-filter 参数重新运行。",
                parsed_count, keyword,
            )
        return links

    def _parse_detail_links(self, html: str, base_url: str) -> List[DetailLink]:
        # 从列表页 HTML 中提取条目容器里的详情链接、名称与上下文。
        engine = SelectorEngine(html, self.logger)
        list_sel = SELECTORS["list"]
        results: List[DetailLink] = []

        for item in engine.find_nodes(list_sel.get("item", [])):
            # 条目容器的完整 HTML 片段 → 在其中定位详情链接，拿到 (href, 锚文本)
            fragment = SelectorEngine(item.html or "", self.logger)
            for href, anchor_text in fragment.find_links(list_sel.get("item_link", [])):
                url = self._normalize_url(href, base_url)
                if not url:
                    continue
                results.append(
                    DetailLink(
                        url=url,
                        title=anchor_text,
                        context=clean_text(item.text) or "",
                        name=self._extract_item_name(fragment, anchor_text),
                    )
                )
                break  # 一个条目只取第一条链接，避免抓到「更多」等附属链接

        # 兜底：如果条目容器选择器没配好，直接在整页里找所有链接，用 URL 特征过滤
        if not results:
            self.logger.warning("条目选择器未命中，回退为「全页链接 + URL 特征过滤」策略")
            for href, anchor_text in engine.find_links(list_sel.get("item_link", ["a::attr(href)"])):
                url = self._normalize_url(href, base_url)
                if url and self._looks_like_detail(url):
                    results.append(DetailLink(url=url, title=anchor_text, context=anchor_text))
        return results

    def _extract_item_name(self, fragment: SelectorEngine, anchor_text: str) -> str:
        # 从条目片段中提取药品名称。
        #
        # 注意：有些列表页的链接文字是「查看说明书」这类操作词，不能当药名用，
        # 因此这里用黑名单 + 长度限制把它排除，宁可返回空也不要污染后续字段。
        blacklist = {"查看说明书", "查看", "详情", "详细信息", "更多", "查询", "说明书", "点击查看"}
        for node in fragment.find_nodes(SELECTORS["list"].get("item_name", [])):
            candidate = clean_text(node.text)
            if not candidate or len(candidate) > 40:
                continue
            if candidate in blacklist or candidate == anchor_text:
                continue
            return candidate
        return ""

    def _parse_next_page(self, html: str, base_url: str) -> Optional[str]:
        # 解析「下一页」链接；取不到就返回 None 结束翻页。
        engine = SelectorEngine(html, self.logger)
        nodes = engine.find_nodes(SELECTORS["list"].get("next_page", []))
        if not nodes:
            return None
        href = nodes[0].attrs.get("href") or nodes[0].text
        nxt = self._normalize_url(href, base_url)
        # 与当前页相同（JS 分页常见）或为空则视为没有下一页
        return nxt if nxt and nxt != base_url else None

    @staticmethod
    def _normalize_url(href: str, base_url: str) -> Optional[str]:
        # 相对链接转绝对链接，过滤 javascript:/# 等无效链接。
        href = (href or "").strip()
        if not href or href.startswith(("javascript:", "#", "void(")) or href.lower().startswith("mailto:"):
            return None
        return urljoin(base_url, href)

    def _looks_like_detail(self, url: str) -> bool:
        # 粗判链接是否像详情页（过滤导航、分页、首页等噪声链接）。
        keyword = (self.cfg.get("detail_link_keyword") or "").strip()
        if keyword:
            return keyword in url
        lowered = url.lower()
        if any(bad in lowered for bad in ("javascript", "login", "index.html", "home")):
            return False
        return bool(re.search(r"(detail|content|info|show|view|id=|\.html)", lowered))

    def _passes_name_filter(self, link: DetailLink) -> bool:
        # 按关键词过滤条目（例如只看「黄芩」相关药品）。
        #
        # 判断顺序：整行上下文 → 单独提取的名称 → 链接锚文本。
        # 之所以优先用整行上下文，是因为很多列表页的链接文字是「查看说明书」，
        # 真正的药名在行内的其他单元格里。
        keyword = (self.cfg.get("name_filter") or "").strip()
        if not keyword:
            return True
        haystack = " ".join(filter(None, (link.context, link.name, link.title)))
        if not haystack:
            return True  # 什么都取不到时保守放行，交给解析阶段判定
        return keyword in haystack

    # ---------------- 详情页 ----------------
    def crawl_details(self, links: Iterable[DetailLink]) -> List[Dict[str, Any]]:
        # 逐条抓取详情页并解析，单条失败只记录日志、不中断整体流程。
        records: List[Dict[str, Any]] = []
        links = list(links)

        for idx, link in enumerate(links, start=1):
            self.logger.info("--- [%d/%d] 抓取详情：%s", idx, len(links), link.url)
            if self.cfg["respect_robots"] and not robots_allows(link.url, random_ua(), self.logger):
                self.logger.warning("robots.txt 禁止，跳过：%s", link.url)
                continue

            try:
                html = self._fetch_with_retry(link.url)
                self._maybe_dump_html(html, link.url, f"detail_{idx}")
            except Exception as exc:
                self.logger.error("详情页抓取失败，已跳过：%s（%s）", link.url, exc)
                continue

            try:
                record = self.parser.parse(html, link.url, hint_name=link.name or None)
            except Exception as exc:  # 解析异常同样不能中断整体
                self.logger.exception("详情页解析异常，已跳过：%s（%s）", link.url, exc)
                continue

            filled = sum(1 for col in FIELD_COLUMNS.values() if record.get(col))
            self.logger.info("解析完成：%s（有效字段 %d/5）", record.get("药品名称") or "未知", filled)
            records.append(record)

            # 限速：最后一条之后不再休眠，节省时间
            if idx < len(links):
                polite_sleep(self.cfg["min_delay"], self.cfg["max_delay"])

        return records

    # ---------------- 通用：带重试的取页 ----------------
    def _fetch_with_retry(self, url: str) -> str:
        # 带重试与超时的取页封装。
        #
        # tenacity 可用时由构造 RequestFetcher 时注入重试；Selenium 与 Local 模式
        # 则在这里统一套一层重试，保证任何一种模式下都有 3 次尝试机会。
        max_attempts = int(self.cfg["max_retries"])

        if isinstance(self.fetcher, RequestsFetcher):
            return self.fetcher.get_html(url)  # 重试已在 RequestsFetcher 内部完成

        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    self.logger.info("第 %d/%d 次重试：%s", attempt, max_attempts, url)
                return self.fetcher.get_html(url)
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                wait = 1.5 * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
                self.logger.warning("取页失败：%s；%.1f 秒后重试", exc, wait)
                time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    # ---------------- 原始 HTML 落盘（可选） ----------------
    def _maybe_dump_html(self, html: str, url: str, tag: str) -> None:
        # 保存原始 HTML：调试选择器时非常有用——一次抓取、反复解析，无需重复请求对方站点。
        if not self.cfg.get("save_raw_html"):
            return
        try:
            out_dir = Path(self.cfg["raw_html_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", f"{tag}_{url}")[:120]
            path = out_dir / f"{safe}.html"
            path.write_text(html, encoding="utf-8")
            self.logger.debug("原始 HTML 已保存：%s", path)
        except Exception as exc:  # pragma: no cover
            self.logger.debug("保存原始 HTML 失败（可忽略）：%s", exc)

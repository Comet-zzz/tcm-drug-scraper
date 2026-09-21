# -*- coding: utf-8 -*-
# 解析区（选择器 / 标注符号 / 标签节点 三级回退）。
#
# 第 1 级：config.SELECTORS 中的 CSS / XPath 选择器（最精确，优先）
# 第 2 级：中文说明书标注符号正则（【不良反应】…【禁忌】…）
# 第 3 级：标签节点 / 表格对通用扫描

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment]

try:
    # lxml 用于支持 XPath 选择器（BeautifulSoup 原生只支持 CSS）。
    import lxml.html as lxml_html  # type: ignore
    import lxml.etree as lxml_etree  # type: ignore
except ImportError:  # pragma: no cover
    lxml_html = None  # type: ignore[assignment]
    lxml_etree = None  # type: ignore[assignment]

from .config import FIELD_COLUMNS, FIELD_LABELS, LABEL_SECTION_RE
from .utils import clean_text, is_xpath, normalize_label, strip_xpath_prefix, wants_attr


@dataclass
class Node:
    # 统一的节点封装，屏蔽 BeautifulSoup 与 lxml 的 API 差异。

    text: str
    attrs: Dict[str, str] = field(default_factory=dict)
    html: str = ""  # 该节点的内部 HTML（调试用）


class SelectorEngine:
    # 选择器引擎：同时支持 CSS Selector（BeautifulSoup）与 XPath（lxml）。
    #
    # 之所以两者都支持，是因为 F12 里「Copy → Copy selector」得到的是 CSS，
    # 而「Copy → Copy XPath」得到的是 XPath，两种方式均会用到。

    def __init__(self, html: str, logger: logging.Logger) -> None:
        if BeautifulSoup is None:
            raise RuntimeError("未安装 beautifulsoup4，请执行：pip install beautifulsoup4 lxml")
        parser = "lxml" if lxml_html is not None else "html.parser"
        self.soup = BeautifulSoup(html, parser)
        self.logger = logger
        self._tree = None
        self._tree_built = False

    # ---------- lxml 树懒加载 ----------
    def _lxml_tree(self):
        if not self._tree_built:
            self._tree_built = True
            if lxml_html is not None:
                try:
                    # document_fromstring 始终返回单个根节点；HTML 片段（如 <tr>…）先包一层容器。
                    html_text = self.soup.encode("utf-8").decode("utf-8", "ignore")
                    if "<html" not in html_text[:2000].lower():
                        html_text = f"<html><body><div>{html_text}</div></body></html>"
                    self._tree = lxml_html.document_fromstring(html_text)
                except Exception as exc:  # pragma: no cover
                    self.logger.debug("lxml 解析失败，XPath 选择器将被跳过：%s", exc)
                    self._tree = None
        return self._tree

    # ---------- 对外接口 ----------
    def find_nodes(self, selectors: Sequence[str]) -> List[Node]:
        # 依次尝试候选选择器，返回「第一个有结果的选择器」所命中的全部节点。
        # 全部未命中则返回空列表（不抛异常，交由上层判定为字段缺失）。
        for selector in selectors:
            try:
                nodes = self._find_once(selector)
            except Exception as exc:
                self.logger.debug("选择器执行失败 [%s]：%s", selector, exc)
                continue
            if nodes:
                return nodes
        return []

    def first_text(self, selectors: Sequence[str]) -> Optional[str]:
        # 取第一个候选选择器的首个节点文本。
        nodes = self.find_nodes(selectors)
        if not nodes:
            return None
        return clean_text(nodes[0].text)

    def _find_once(self, selector: str) -> List[Node]:
        selector = selector.strip()
        if not selector:
            return []
        attr = wants_attr(selector)
        if attr:
            selector = re.sub(r"::attr\([^)]+\)\s*$", "", selector).strip()

        if is_xpath(selector):
            return self._find_xpath(strip_xpath_prefix(selector), attr)
        return self._find_css(selector, attr)

    def _find_css(self, selector: str, attr: Optional[str]) -> List[Node]:
        tags = self.soup.select(selector)
        if not tags:
            return []
        nodes: List[Node] = []
        for tag in tags[:200]:  # 上限保护，避免异常选择器命中整页
            if attr:
                value = tag.get(attr)
                if value is None:
                    continue
                nodes.append(Node(text=str(value), attrs={attr: str(value)}))
            else:
                nodes.append(self._tag_to_node(tag))
        return nodes

    def _tag_to_node(self, tag: Any) -> Node:
        text = tag.get_text(" ", strip=True) if hasattr(tag, "get_text") else str(tag)
        attrs = {k: " ".join(v) if isinstance(v, list) else str(v) for k, v in (tag.attrs or {}).items()}
        # 这里保留完整 HTML 片段：列表页需要用它二次定位条目内的详情链接
        return Node(text=text, attrs=attrs, html=str(tag))

    # ---------- 链接专用提取：同时拿到 href 与锚文本 ----------
    def find_links(self, selectors: Sequence[str]) -> List[Tuple[str, str]]:
        # 提取链接，返回 [(href, 锚文本), …]。
        #
        # 与 find_nodes 的区别：这里同时要 href 和链接文字。
        # 锚文本是后面做「关键词过滤（只看黄芩）」的依据，不能只取 href。
        for selector in selectors:
            try:
                links = self._links_once(selector)
            except Exception as exc:
                self.logger.debug("链接选择器执行失败 [%s]：%s", selector, exc)
                continue
            if links:
                return links
        return []

    def _links_once(self, selector: str) -> List[Tuple[str, str]]:
        selector = selector.strip()
        if not selector:
            return []
        attr = wants_attr(selector) or "href"
        base = re.sub(r"::attr\([^)]+\)\s*$", "", selector).strip()

        # XPath 分支
        if is_xpath(base):
            tree = self._lxml_tree()
            if tree is None:
                return []
            try:
                elems = tree.xpath(strip_xpath_prefix(base))
            except Exception:  # pragma: no cover
                return []
            out: List[Tuple[str, str]] = []
            for el in elems:
                if not hasattr(el, "attrib"):
                    continue
                href = el.attrib.get(attr)
                if href:
                    text = " ".join(t.strip() for t in el.itertext()).strip()
                    out.append((str(href), clean_text(text) or ""))
            return out

        # CSS 分支
        out = []
        for tag in self.soup.select(base)[:200]:
            href = tag.get(attr)
            if href:
                out.append((str(href), clean_text(tag.get_text(" ", strip=True)) or ""))
        return out

    def _find_xpath(self, xpath: str, attr: Optional[str]) -> List[Node]:
        tree = self._lxml_tree()
        if tree is None:
            self.logger.debug("未安装 lxml，跳过 XPath 选择器：%s", xpath)
            return []
        try:
            results = tree.xpath(xpath)
        except lxml_etree.XPathError as exc:  # type: ignore[union-attr]
            self.logger.debug("非法 XPath [%s]：%s", xpath, exc)
            return []

        nodes: List[Node] = []
        for item in results[:200]:
            if isinstance(item, str):  # XPath 直接返回字符串（如 text()）
                nodes.append(Node(text=item))
                continue
            if not hasattr(item, "itertext"):  # 属性/数字等非元素结果
                nodes.append(Node(text=str(item)))
                continue
            text = " ".join(t.strip() for t in item.itertext())
            attrs = {str(k): str(v) for k, v in (item.attrib or {}).items()}
            if attr:
                value = item.attrib.get(attr)
                if value is None:
                    continue
                nodes.append(Node(text=str(value), attrs={attr: str(value)}))
            else:
                nodes.append(Node(text=text, attrs=attrs, html=lxml_etree.tostring(item, encoding="unicode")[:2000]))  # type: ignore[union-attr]
        return nodes

    # ---------- 第三级回退：标签节点 / 表格对 ----------
    def build_label_index(self, labels: Dict[str, List[str]], max_label_len: int = 16) -> Dict[str, str]:
        # 扫描整页，建立「标签文本 → 字段值」映射。
        #
        # 适用结构：
        #     <span class="label">不良反应：</span><div class="content">…</div>
        #     <tr><td>禁忌</td><td>…</td></tr>
        # 实现要点：单次遍历全页，遇到「文本很短且归一化后等于字段名」的节点，
        # 就尝试从它的后继兄弟节点 / 父节点剩余文本 / 同行右侧单元格中取值。
        norm_to_field: Dict[str, str] = {}
        for field_name, aliases in labels.items():
            for alias in aliases:
                norm_to_field.setdefault(normalize_label(alias), field_name)

        result: Dict[str, str] = {}
        for tag in self.soup.find_all(True):
            raw = tag.get_text(" ", strip=True)
            if not raw or len(raw) > max_label_len:
                continue  # 长文本不可能是标签（顺带避免遍历到整个 body）
            norm = normalize_label(raw)
            if norm not in norm_to_field:
                continue
            field_name = norm_to_field[norm]
            if field_name in result:
                continue  # 已取到值，不覆盖

            value = self._value_after_label(tag, raw)
            if value:
                result[field_name] = value
                self.logger.debug("标签节点回退命中字段 [%s]", field_name)
        return result

    def _value_after_label(self, tag: Any, label_text: str) -> Optional[str]:
        # 从一个「标签节点」出发，按 4 种常见版式尝试取出字段值。
        # 版式 1：标签与值在同一个单元格里，如 <td>禁忌：尚不明确</td>
        whole = tag.get_text(" ", strip=True)
        if len(whole) > len(label_text) + 1:
            rest = whole[len(label_text):]
            if clean_text(rest):
                return clean_text(rest)

        # 版式 2：后继兄弟节点，如 <span>禁忌</span><div>尚不明确</div>
        sibling = getattr(tag, "find_next_sibling", lambda *a, **k: None)()
        if sibling is not None:
            sib_text = clean_text(sibling.get_text(" ", strip=True))
            if sib_text and len(sib_text) > 1:
                return sib_text

        # 版式 3：父节点整体文本减去标签本身
        parent = getattr(tag, "parent", None)
        if parent is not None and getattr(parent, "name", None) not in (None, "body", "[document]"):
            parent_text = clean_text(parent.get_text(" ", strip=True)) or ""
            remainder = parent_text.replace(label_text, "", 1).strip()
            if len(remainder) > 3:
                return clean_text(remainder)

        # 版式 4：表格版式，标签在 <th>/<td>，值在同行右侧单元格
        if getattr(tag, "name", "") in ("th", "td"):
            row = tag.find_parent("tr")
            if row is not None:
                cells = row.find_all(["td", "th"])
                for idx, cell in enumerate(cells):
                    if cell is tag and idx + 1 < len(cells):
                        nxt = clean_text(cells[idx + 1].get_text(" ", strip=True))
                        if nxt:
                            return nxt
        return None


class DrugPageParser:
    # 说明书详情页解析器：对每个字段执行三级回退。
    #
    #     第 1 级：SELECTORS 中的 CSS / XPath 选择器（最精确，优先）
    #     第 2 级：中文说明书标注符号正则（【不良反应】…【禁忌】…）
    #     第 3 级：标签节点 / 表格对通用扫描
    #
    # 任何一级都没命中 → 返回 None（绝不抛异常）。

    def __init__(self, selectors: Dict[str, Any], logger: logging.Logger) -> None:
        self.selectors = selectors
        self.logger = logger

    def parse(self, html: str, url: str, hint_name: Optional[str] = None) -> Dict[str, Any]:
        engine = SelectorEngine(html, self.logger)
        record: Dict[str, Any] = {"药品名称": None, "主要成分": None, "不良反应": None, "禁忌": None, "注意事项": None}

        detail_sel = self.selectors.get("detail", {})
        label_index: Optional[Dict[str, str]] = None  # 惰性构建，只有前两级都失败时才扫描

        for field_name, column in FIELD_COLUMNS.items():
            value: Optional[str] = None

            # ---------- 第 1 级：选择器 ----------
            if field_name == "drug_name":
                value = self._clean_drug_name(engine.first_text(detail_sel.get("drug_name", [])))
                if not value:
                    # 退化方案：<title> 标签 / og:title / 页面里最大的 h1
                    value = (
                        self._title_fallback(engine)
                        or clean_text(hint_name)  # 列表页链接文本作为最后兜底
                    )
            else:
                value = engine.first_text(detail_sel.get(field_name, []))
                value = self._reject_foreign_label(value, field_name)

            # ---------- 第 2 级：标注符号（仅正文型字段需要） ----------
            if not value and field_name != "drug_name":
                value = self.extract_by_markers(engine.soup.get_text("\n"), field_name)

            # ---------- 第 3 级：标签节点 / 表格对 ----------
            if not value and field_name != "drug_name":
                if label_index is None:
                    label_index = engine.build_label_index(FIELD_LABELS)
                value = clean_text(label_index.get(field_name))

            record[column] = value
            if value:
                self.logger.debug("字段 [%s] 提取成功，长度 %d", column, len(value))
            else:
                self.logger.debug("字段 [%s] 未找到，按缺失处理", column)

        # 补充溯源信息：研究场景下必须能回溯到原始页面
        record["数据来源URL"] = url
        record["抓取时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return record

    # ---------- 第 2 级实现：标注符号区间抽取 ----------
    def extract_by_markers(self, full_text: str, field_name: str) -> Optional[str]:
        # 从全文里用「【不良反应】…【禁忌】」这类标注切出字段内容。
        #
        # 做法：找到起始标注 → 截取到「下一个任意 【xxx】 标注」或文末 → 清洗。
        # 只匹配带方括号的标注，避免「禁忌症」这类词误触发。
        aliases = FIELD_LABELS.get(field_name, [])
        if not aliases or not full_text:
            return None

        pattern = LABEL_SECTION_RE.format(labels="|".join(re.escape(a) for a in aliases))
        match = re.search(pattern, full_text)
        if not match:
            return None

        rest = full_text[match.end():]
        nxt = re.search(r"[【\[]\s*[^】\]]{1,20}\s*[】\]]", rest)  # 下一个标注即结束
        segment = rest[: nxt.start()] if nxt else rest
        return clean_text(segment)

    # ---------- 第 1 级实现：防止选择器「串段」 ----------
    @staticmethod
    def _reject_foreign_label(value: Optional[str], field_name: str) -> Optional[str]:
        # 识别并丢弃「选择器抓到了别的字段」的值。
        #
        # 典型场景：页面把标签和正文写在同一个节点里，如
        # <li>【不良反应】尚不明确。</li>，而兜底 XPath 会把它当「短标签节点」，
        # 再取其兄弟节点，于是抓到下一段「【禁忌】孕妇忌服…」。
        #
        # 规则：值若以方括号标注开头——标注是本字段自己的别名则去掉标注保留正文，
        # 是别的标注（【禁忌】【贮藏】等）则判定为取错，放弃该值、交给后面的级别重取。
        if not value:
            return value
        match = re.match(r"^[【\[]\s*([^】\]]{1,20})\s*[】\]]\s*", value)
        if not match:
            return value
        own_labels = {normalize_label(alias) for alias in FIELD_LABELS.get(field_name, [])}
        if normalize_label(match.group(1)) in own_labels:
            return clean_text(value[match.end():]) or None
        return None

    # ---------- 第 1 级实现：药品名称清洗 ----------
    @staticmethod
    def _clean_drug_name(name: Optional[str]) -> Optional[str]:
        # 清洗选择器取到的药品名称：去掉「××说明书」这类页面标题后缀。
        #
        # 例：药源网页面标题是「黄连上清片说明书」，实际药名应为「黄连上清片」。
        if not name:
            return None
        cleaned = re.sub(r"[\s\u3000]*(?:药品)?说明书.*$", "", name).strip()
        return clean_text(cleaned) or None

    def _title_fallback(self, engine: SelectorEngine) -> Optional[str]:
        # 药品名称兜底：取 og:title 或 <title>。
        #
        # 需要处理两种噪声：
        #   ① 尾部通用词，如「黄芩片 说明书」「乙酰半胱氨酸泡腾片说明书」
        #   ② 站点后缀，如「- 国家药品监督管理局」「_某某药品数据库」
        # 如果清洗后仍含「查询 / 搜索 / 列表」等词，说明这是个栏目页而非详情页，
        # 此时返回 None，让上游改用列表页条目文本兜底，避免把栏目名当药名写进 CSV。
        raw: Optional[str] = None
        og = engine.soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            raw = clean_text(og["content"])
        elif engine.soup.title and engine.soup.title.string:
            raw = clean_text(engine.soup.title.string)
        if not raw:
            return None

        # ① 去掉「[药品]说明书…」及其后的所有内容
        name = re.sub(r"[\s\u3000]*(?:药品)?说明书.*$", "", raw).strip()
        # ② 按常见站点分隔符取第一段
        name = re.split(r"\s*[-_|｜]\s*", name)[0].strip()
        name = clean_text(name)

        if not name or len(name) < 2 or len(name) > 40:
            return None
        if any(bad in name for bad in ("查询", "搜索", "检索", "列表", "结果", "首页", "登录")):
            return None
        return name

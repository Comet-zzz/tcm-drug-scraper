# -*- coding: utf-8 -*-
# Parsing layer (selector / section marker / label node -- three-tier fallback).
#
# Tier 1: CSS / XPath selectors from config.SELECTORS (most precise, tried first)
# Tier 2: Chinese label-marker regex (【不良反应】…【禁忌】…)
# Tier 3: generic label-node / table-pair scan

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
    # lxml enables XPath selectors (BeautifulSoup alone supports only CSS).
    import lxml.html as lxml_html  # type: ignore
    import lxml.etree as lxml_etree  # type: ignore
except ImportError:  # pragma: no cover
    lxml_html = None  # type: ignore[assignment]
    lxml_etree = None  # type: ignore[assignment]

from .config import FIELD_COLUMNS, FIELD_LABELS, LABEL_SECTION_RE
from .utils import clean_text, is_xpath, normalize_label, strip_xpath_prefix, wants_attr


@dataclass
class Node:
    # Unified node wrapper that hides the API differences between BeautifulSoup and lxml.

    text: str
    attrs: Dict[str, str] = field(default_factory=dict)
    html: str = ""  # the node's inner HTML (for debugging)


class SelectorEngine:
    # Selector engine: supports both CSS selectors (BeautifulSoup) and XPath (lxml).
    #
    # Both are supported because F12's "Copy -> Copy selector" yields CSS while
    # "Copy -> Copy XPath" yields XPath, and either may be needed.

    def __init__(self, html: str, logger: logging.Logger) -> None:
        if BeautifulSoup is None:
            raise RuntimeError("beautifulsoup4 is not installed. Run: pip install beautifulsoup4 lxml")
        parser = "lxml" if lxml_html is not None else "html.parser"
        self.soup = BeautifulSoup(html, parser)
        self.logger = logger
        self._tree = None
        self._tree_built = False

    # ---------- lazily built lxml tree ----------
    def _lxml_tree(self):
        if not self._tree_built:
            self._tree_built = True
            if lxml_html is not None:
                try:
                    # document_fromstring always returns a single root; wrap HTML fragments (e.g. <tr>…)
                    html_text = self.soup.encode("utf-8").decode("utf-8", "ignore")
                    if "<html" not in html_text[:2000].lower():
                        html_text = f"<html><body><div>{html_text}</div></body></html>"
                    self._tree = lxml_html.document_fromstring(html_text)
                except Exception as exc:  # pragma: no cover
                    self.logger.debug("lxml parsing failed; XPath selectors will be skipped: %s", exc)
                    self._tree = None
        return self._tree

    # ---------- public interface ----------
    def find_nodes(self, selectors: Sequence[str]) -> List[Node]:
        # Try the candidate selectors in order and return every node matched by the first selector
        # that yields a result. Returns an empty list if none match (no exception; the caller treats
        # it as a missing field).
        for selector in selectors:
            try:
                nodes = self._find_once(selector)
            except Exception as exc:
                self.logger.debug("Selector failed [%s]: %s", selector, exc)
                continue
            if nodes:
                return nodes
        return []

    def first_text(self, selectors: Sequence[str]) -> Optional[str]:
        # Return the text of the first node matched by the first working candidate selector.
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
        for tag in tags[:200]:  # cap, in case a stray selector matches the whole page
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
        # The full HTML fragment is kept here: the listing page needs it to locate the detail link inside a row
        return Node(text=text, attrs=attrs, html=str(tag))

    # ---------- link extraction: both href and anchor text ----------
    def find_links(self, selectors: Sequence[str]) -> List[Tuple[str, str]]:
        # Extract links as [(href, anchor text), …].
        #
        # Unlike find_nodes, this needs both the href and the link text.
        # The anchor text is what keyword filtering ("only 黄芩") relies on, so href alone is not enough.
        for selector in selectors:
            try:
                links = self._links_once(selector)
            except Exception as exc:
                self.logger.debug("Link selector failed [%s]: %s", selector, exc)
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

        # XPath branch
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

        # CSS branch
        out = []
        for tag in self.soup.select(base)[:200]:
            href = tag.get(attr)
            if href:
                out.append((str(href), clean_text(tag.get_text(" ", strip=True)) or ""))
        return out

    def _find_xpath(self, xpath: str, attr: Optional[str]) -> List[Node]:
        tree = self._lxml_tree()
        if tree is None:
            self.logger.debug("lxml is not installed; skipping XPath selector: %s", xpath)
            return []
        try:
            results = tree.xpath(xpath)
        except lxml_etree.XPathError as exc:  # type: ignore[union-attr]
            self.logger.debug("Invalid XPath [%s]: %s", xpath, exc)
            return []

        nodes: List[Node] = []
        for item in results[:200]:
            if isinstance(item, str):  # XPath returned a string directly (e.g. text())
                nodes.append(Node(text=item))
                continue
            if not hasattr(item, "itertext"):  # non-element results such as attributes/numbers
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

    # ---------- tier 3 fallback: label node / table pair ----------
    def build_label_index(self, labels: Dict[str, List[str]], max_label_len: int = 16) -> Dict[str, str]:
        # Scan the whole page and build a "label text -> field value" map.
        #
        # Handles structures such as:
        #     <span class="label">不良反应：</span><div class="content">…</div>
        #     <tr><td>禁忌</td><td>…</td></tr>
        # Implementation: a single pass over the page; whenever a node's text is short and, after
        # normalisation, equals a field name, it tries to take the value from its next sibling /
        # its parent's remaining text / the cell to its right in the same row.
        norm_to_field: Dict[str, str] = {}
        for field_name, aliases in labels.items():
            for alias in aliases:
                norm_to_field.setdefault(normalize_label(alias), field_name)

        result: Dict[str, str] = {}
        for tag in self.soup.find_all(True):
            raw = tag.get_text(" ", strip=True)
            if not raw or len(raw) > max_label_len:
                continue  # a long text cannot be a label (also avoids walking the whole body)
            norm = normalize_label(raw)
            if norm not in norm_to_field:
                continue
            field_name = norm_to_field[norm]
            if field_name in result:
                continue  # a value was already captured; do not overwrite

            value = self._value_after_label(tag, raw)
            if value:
                result[field_name] = value
                self.logger.debug("Label-node fallback matched field [%s]", field_name)
        return result

    def _value_after_label(self, tag: Any, label_text: str) -> Optional[str]:
        # Starting from a "label node", try four common layouts to obtain the field value.
        # Layout 1: label and value in the same cell, e.g. <td>禁忌：尚不明确</td>
        whole = tag.get_text(" ", strip=True)
        if len(whole) > len(label_text) + 1:
            rest = whole[len(label_text):]
            if clean_text(rest):
                return clean_text(rest)

        # Layout 2: the next sibling node, e.g. <span>禁忌</span><div>尚不明确</div>
        sibling = getattr(tag, "find_next_sibling", lambda *a, **k: None)()
        if sibling is not None:
            sib_text = clean_text(sibling.get_text(" ", strip=True))
            if sib_text and len(sib_text) > 1:
                return sib_text

        # Layout 3: the parent's whole text minus the label itself
        parent = getattr(tag, "parent", None)
        if parent is not None and getattr(parent, "name", None) not in (None, "body", "[document]"):
            parent_text = clean_text(parent.get_text(" ", strip=True)) or ""
            remainder = parent_text.replace(label_text, "", 1).strip()
            if len(remainder) > 3:
                return clean_text(remainder)

        # Layout 4: table layout, where the label is in <th>/<td> and the value in the cell to its right
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
    # Label-page parser: runs the three-tier fallback for each field.
    #
    #     Tier 1: CSS / XPath selectors from SELECTORS (most precise, tried first)
    #     Tier 2: Chinese label-marker regex (【不良反应】…【禁忌】…)
    #     Tier 3: generic label-node / table-pair scan
    #
    # If none of the tiers matches, it returns None (never raises).

    def __init__(self, selectors: Dict[str, Any], logger: logging.Logger) -> None:
        self.selectors = selectors
        self.logger = logger

    def parse(self, html: str, url: str, hint_name: Optional[str] = None) -> Dict[str, Any]:
        engine = SelectorEngine(html, self.logger)
        record: Dict[str, Any] = {"drug_name": None, "ingredients": None, "adverse_reactions": None, "contraindications": None, "precautions": None}

        detail_sel = self.selectors.get("detail", {})
        label_index: Optional[Dict[str, str]] = None  # built lazily, only if tiers 1 and 2 both fail

        for field_name, column in FIELD_COLUMNS.items():
            value: Optional[str] = None

            # ---------- Tier 1: selectors ----------
            if field_name == "drug_name":
                value = self._clean_drug_name(engine.first_text(detail_sel.get("drug_name", [])))
                if not value:
                    # Fallbacks: <title> / og:title / the largest h1 on the page
                    value = (
                        self._title_fallback(engine)
                        or clean_text(hint_name)  # the listing link text as a last resort
                    )
            else:
                value = engine.first_text(detail_sel.get(field_name, []))
                value = self._reject_foreign_label(value, field_name)

            # ---------- Tier 2: section markers (body fields only) ----------
            if not value and field_name != "drug_name":
                value = self.extract_by_markers(engine.soup.get_text("\n"), field_name)

            # ---------- Tier 3: label node / table pair ----------
            if not value and field_name != "drug_name":
                if label_index is None:
                    label_index = engine.build_label_index(FIELD_LABELS)
                value = clean_text(label_index.get(field_name))

            record[column] = value
            if value:
                self.logger.debug("Field [%s] extracted, length %d", column, len(value))
            else:
                self.logger.debug("Field [%s] not found; treated as missing", column)

        # Provenance: a research workflow must be able to trace a record back to the original page
        record["source_url"] = url
        record["scraped_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return record

    # ---------- tier 2 implementation: marker-delimited extraction ----------
    def extract_by_markers(self, full_text: str, field_name: str) -> Optional[str]:
        # Cut a field's content out of the full text using markers such as "【不良反应】…【禁忌】".
        #
        # Approach: find the opening marker -> take everything up to the next "【xxx】" marker or the
        # end of the text -> clean. Only bracketed markers are matched, so words like "禁忌症" do not
        # trigger it by accident.
        aliases = FIELD_LABELS.get(field_name, [])
        if not aliases or not full_text:
            return None

        pattern = LABEL_SECTION_RE.format(labels="|".join(re.escape(a) for a in aliases))
        match = re.search(pattern, full_text)
        if not match:
            return None

        rest = full_text[match.end():]
        nxt = re.search(r"[【\[]\s*[^】\]]{1,20}\s*[】\]]", rest)  # the next marker ends the segment
        segment = rest[: nxt.start()] if nxt else rest
        return clean_text(segment)

    # ---------- tier 1 implementation: prevent a selector from spilling into another field ----------
    @staticmethod
    def _reject_foreign_label(value: Optional[str], field_name: str) -> Optional[str]:
        # Detect and discard a value where the selector grabbed a different field.
        #
        # Typical case: the page puts the label and the body text in one node, e.g.
        # <li>【不良反应】尚不明确。</li>, and the fallback XPath treats it as a "short label node"
        # whose sibling is then the next paragraph "【禁忌】孕妇忌服…".
        #
        # Rule: if the value starts with a bracketed marker -- when the marker is this field's own
        # alias, strip it and keep the body text; when it is another marker (【禁忌】【贮藏】 etc.),
        # treat the value as wrong, discard it, and let the later tiers re-fetch.
        if not value:
            return value
        match = re.match(r"^[【\[]\s*([^】\]]{1,20})\s*[】\]]\s*", value)
        if not match:
            return value
        own_labels = {normalize_label(alias) for alias in FIELD_LABELS.get(field_name, [])}
        if normalize_label(match.group(1)) in own_labels:
            return clean_text(value[match.end():]) or None
        return None

    # ---------- tier 1 implementation: clean the drug name ----------
    @staticmethod
    def _clean_drug_name(name: Optional[str]) -> Optional[str]:
        # Clean a drug name taken by a selector: drop page-title suffixes such as "××说明书".
        #
        # Example: the Yaopinnet page title is "黄连上清片说明书", while the drug name is "黄连上清片".
        if not name:
            return None
        cleaned = re.sub(r"[\s\u3000]*(?:药品)?说明书.*$", "", name).strip()
        return clean_text(cleaned) or None

    def _title_fallback(self, engine: SelectorEngine) -> Optional[str]:
        # Drug-name fallback: take og:title or <title>.
        #
        # Two kinds of noise need handling:
        #   1) trailing generic words, e.g. "黄芩片 说明书" / "乙酰半胱氨酸泡腾片说明书"
        #   2) site suffixes, e.g. "- 国家药品监督管理局" / "_某某药品数据库"
        # If the cleaned text still contains "查询 / 搜索 / 列表" etc., it is a section page rather
        # than a detail page; return None so the caller falls back to the listing row text, instead
        # of writing a section name into the CSV as if it were a drug name.
        raw: Optional[str] = None
        og = engine.soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            raw = clean_text(og["content"])
        elif engine.soup.title and engine.soup.title.string:
            raw = clean_text(engine.soup.title.string)
        if not raw:
            return None

        # (1) drop "[药品]说明书…" and everything after it
        name = re.sub(r"[\s\u3000]*(?:药品)?说明书.*$", "", raw).strip()
        # (2) take the first segment split by common site separators
        name = re.split(r"\s*[-_|｜]\s*", name)[0].strip()
        name = clean_text(name)

        if not name or len(name) < 2 or len(name) > 40:
            return None
        if any(bad in name for bad in ("查询", "搜索", "检索", "列表", "结果", "首页", "登录")):
            return None
        return name

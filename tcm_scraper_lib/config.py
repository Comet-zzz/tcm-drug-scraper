# -*- coding: utf-8 -*-
# 配置与常量：选择器、字段映射、默认运行参数。
#
# ★ 适配新站点时，改动主要集中在本文件。

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

# 项目根目录（即入口脚本 tcm_scraper.py 所在目录），用于定位 config.json 等项目文件。
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# ---------------- 1. 配置区（★ 真实站点适配时，80% 的改动都在这里） ----------------
#
# ★ 目标网址无需在此修改：
#   运行脚本时按设置向导填写，结果会自动保存到同目录的 config.json，下次回车沿用；
#   也可直接编辑 config.json，或在命令行用 --url 指定。
#   DEFAULT_CONFIG["list_url"] 仅为首次运行前的占位值。
#
# ① 选择器：SELECTORS 中每个字段给的是「候选列表」，解析时按顺序尝试，
#    命中第一个能取到内容的即可。站点改版时只需在前面补充一条新选择器，
#    无需删除旧配置。
#
#    选择器写法约定（本脚本自定义，见 utils.is_xpath()）：
#      - 以 "//"、"./"、"(" 开头，或带 "xpath:" 前缀  →  按 XPath 解析（需要 lxml）
#      - 其他写法                                     →  按 CSS Selector 解析（BeautifulSoup）
#    想抓取链接地址时，在选择器后加 "::attr(href)"（XPath 模式自动返回 @href）。
#
# ② 三级回退：即便选择器全部失效，脚本仍会用「【不良反应】」这类中文说明书标注符号，
#    以及「标签节点 + 值节点」的通用结构兜底抽取，因此可先运行验证，再逐步调整选择器。

USER_AGENTS: List[str] = [
    # 随机 User-Agent 池：降低被单一指纹识别为爬虫的概率。
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

# 中文说明书的标准标注符号。第二级回退解析会用它定位字段区间。
# 例：正文中出现「【不良反应】偶见皮疹…【禁忌】尚不明确」时，可精确切出两段。
# ★ 注意：{labels} 外围必须加 (?:…) 非捕获组。否则 "主要成分|成份|成分" 这类
#    多别名会被整条正则的 | 拆散，导致「成分」二字在「【主要成分】」内部被单独匹配到，
#    结果把标注符号的右括号 "】" 残留在字段值开头。
LABEL_SECTION_RE = r"[【\[]\s*(?:{labels})\s*[】\]]"

# 各字段对应的中文标签别名（用于第二、三级回退解析，可按需扩充）
FIELD_LABELS: Dict[str, List[str]] = {
    "drug_name": ["药品名称", "通用名称", "品名", "名称"],
    "ingredients": ["主要成分", "主要成份", "成份", "成分", "主要成份及含量", "处方", "成份/药理作用"],
    "adverse_reactions": ["不良反应", "不良反应与处理", "药物不良反应"],
    "contraindications": ["禁忌", "禁忌症", "禁止"],
    "precautions": ["注意事项", "注意", "警告", "特别提示"],
}

# 字段名（英文 key） ↔ 输出 CSV 列名（中文）的映射，顺序即 CSV 列顺序
FIELD_COLUMNS: Dict[str, str] = {
    "drug_name": "药品名称",
    "ingredients": "主要成分",
    "adverse_reactions": "不良反应",
    "contraindications": "禁忌",
    "precautions": "注意事项",
}

SELECTORS: Dict[str, Any] = {
    # ---------------- 列表页（搜索结果页） ----------------
    "list": {
        # 单个药品结果条目（定位到条目容器，便于同时取标题与链接）
        "item": [
            # 药源网（yaopinnet.com）说明书列表：div#c_list1 / div#c_list2 两个 ul
            "div[id^='c_list'] li",
            "table tbody tr",
            "div.result-list div.item",
            "ul.list li",
            "//table//tr[td]",
        ],
        # 条目内的详情页链接
        "item_link": [
            "a::attr(href)",
            "//a[@href]",
        ],
        # 条目内的药品名称节点（用于关键词过滤和详情页名称兜底；
        # 没配也没关系，脚本会退回用整行文本做过滤）
        "item_name": [
            "td.name",
            "span.name",
            "div.drug-name",
            "h3",
            "h4",
        ],
        # 下一页按钮/链接（取不到就认为只有一页）
        "next_page": [
            # 药源网：当前页码是 <span>，紧随其后的第一个 <a> 即下一页
            "//div[@id='sms_page']/span/following-sibling::a[1]",
            "a.next::attr(href)",
            "a[title='下一页']::attr(href)",
            "//a[contains(text(),'下一页')]",
            "//a[contains(@class,'next')]",
        ],
        # 等待列表渲染完成的元素（Selenium 模式下用，空字符串表示不等待）
        "wait_for": "table tbody tr",
    },
    # ---------------- 详情页（说明书页） ----------------
    "detail": {
        # 药品名称：常见于 h1 / h2 / 详情页标题 / 面包屑
        "drug_name": [
            "h1.drug-name",
            "div.detail-title h1",
            "div.drug-detail h1",
            "//h1",
            "div.drug-name",
            "div.page-head h2",
            "//h2[1]",
            "//*[contains(@class,'title')]/h1",
        ],
        # 以下四个字段均按「选择器 → 标注符号 → 标签节点」三级回退。
        # ★ 注意 XPath 里的 string-length 限制：它用来确保命中的是「短的标签本身」，
        #    而不是「装着整段内容的容器」。否则遇到「<div>【主要成分】黄芩…</div>」这种
        #    标签与内容同处一个元素的版式，following-sibling 会误取到下一个字段的内容。
        "ingredients": [
            "div.field-ingredients div.content",
            "div.ingredients",
            "//*[contains(text(),'主要成分') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        "adverse_reactions": [
            "div.field-adverse div.content",
            "div.adverse-reactions",
            "//*[contains(text(),'不良反应') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        "contraindications": [
            "div.field-contraindications div.content",
            "div.contraindications",
            "//*[contains(text(),'禁忌') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        "precautions": [
            "div.field-precautions div.content",
            "div.precautions",
            "//*[contains(text(),'注意事项') and string-length(normalize-space(.)) < 14]"
            "/following-sibling::*[1]",
        ],
        # 等待详情渲染完成的元素
        "wait_for": "body",
    },
}

# 默认运行参数（可被命令行参数覆盖）
DEFAULT_CONFIG: Dict[str, Any] = {
    "list_url": "https://www.nmpa.gov.cn/datasearch/home-index.html#category=yp",  # ★ 目标URL
    "mode": "selenium",            # "selenium"（默认，支持动态加载） 或 "requests"（快，静态页）
    "headless": True,              # Selenium 是否无头模式
    "min_delay": 2.0,              # 请求间随机休眠下限（秒）
    "max_delay": 5.0,              # 请求间随机休眠上限（秒）
    "max_retries": 3,              # 单次请求最大重试次数（含首次）
    "timeout": 30,                 # 单页超时（秒）
    "max_items": 20,               # 最多抓取多少条详情（建议先用小样本验证）
    "max_pages": 3,                # 最多翻多少页列表
    "name_filter": "",             # 关键词过滤：仅采集名称含该词的条目；留空表示不筛选
    "out_csv": "output/tcm_drug_data.csv",
    "log_file": "logs/tcm_scraper.log",
    "respect_robots": True,        # 是否遵守 robots.txt
    "save_raw_html": False,        # 是否把原始 HTML 落盘（便于事后重放解析，无需重新请求）
    "raw_html_dir": "output/raw_html",
}

# 配置文件：脚本第一次运行会自动生成，用记事本就能改，不需要懂 Python
CONFIG_FILE_NAME = "config.json"
# 允许写进配置文件、也允许被记住的设置项（其余内部参数不落盘，避免配置文件太复杂）
PERSISTED_KEYS: List[str] = [
    "list_url",      # 要抓的网页地址
    "name_filter",   # 目标中药名（空字符串 = 不过滤）
    "max_items",     # 最多抓多少条
    "max_pages",     # 最多翻多少页
    "mode",          # selenium / requests / local
    "headless",      # 是否后台运行浏览器
    "min_delay",
    "max_delay",
]

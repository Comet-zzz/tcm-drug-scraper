# -*- coding: utf-8 -*-
# tcm_scraper_lib —— 「中药说明书信息采集」脚本的实现包。
# 入口是同目录上一级的 tcm_scraper.py，按职责分为：
#     config.py    配置与常量（选择器、字段映射、默认参数）★ 适配新站点主要改这里
#     utils.py     日志、文本清洗、限速、重试等通用工具
#     fetch.py     页面获取（requests / selenium / 本地文件）+ robots.txt + 空结果诊断
#     parse.py     说明书字段解析（选择器 → 标注符号 → 标签节点 三级回退）
#     crawl.py     列表页翻页、详情页遍历、本地文件夹批量解析
#     pipeline.py  pandas 清洗、去重、CSV 导出
#     cli.py       命令行参数、设置向导、主流程

from __future__ import annotations

__all__ = ["main"]


def main(argv=None):
    # 懒加载入口，避免 `import tcm_scraper_lib` 时就拉起所有依赖。
    from .cli import main as _main

    return _main(argv)

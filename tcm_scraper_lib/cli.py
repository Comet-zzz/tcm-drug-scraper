# -*- coding: utf-8 -*-
# 主程序入口：命令行参数、config.json 读写、交互式设置向导与主流程调度。

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
    # 配置文件位置：默认放在脚本同目录，可用 --config 指定别的文件。
    return Path(custom) if custom else PROJECT_ROOT / CONFIG_FILE_NAME


def load_config_file(cfg_path: Path, logger: logging.Logger) -> Dict[str, Any]:
    # 读取 config.json。
    #
    # 容错原则：配置文件损坏、写错或缺失时，仅记录警告并使用默认设置继续运行，
    # 不因配置问题中断采集任务。
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("配置文件 %s 解析失败（%s），本次运行将使用默认设置", cfg_path.name, exc)
        return {}
    except OSError as exc:
        logger.warning("配置文件 %s 读取失败（%s），本次运行将使用默认设置", cfg_path.name, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("配置文件 %s 格式不正确（应为 JSON 对象），已忽略", cfg_path.name)
        return {}

    clean: Dict[str, Any] = {}
    for key, value in data.items():
        if key.startswith("_"):  # 以下划线开头的当作说明文字，跳过
            continue
        if key not in PERSISTED_KEYS:
            logger.warning("配置文件中的 \"%s\" 不是可识别的设置项，已忽略", key)
            continue
        clean[key] = value
    return clean


def save_config_file(cfg_path: Path, cfg: Dict[str, Any], logger: logging.Logger) -> None:
    # 把用户设置写回 config.json，下次运行自动沿用。
    data: Dict[str, Any] = {
        "_说明": (
            "本行为说明文字，不影响运行。通常无需手动修改："
            "直接运行 tcm_scraper.py，按设置向导填写即可自动保存。"
        ),
        "_list_url": "目标网页地址（搜索结果列表页）",
        "_name_filter": "用于筛选的中药名；留空表示不筛选，采集全部条目",
        "_max_items": "单次最多采集的条数",
    }
    for key in PERSISTED_KEYS:
        if key in cfg:
            data[key] = cfg[key]

    try:
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("配置已保存至 %s（下次运行可直接回车沿用）", cfg_path.name)
    except OSError as exc:  # pragma: no cover - 磁盘只读等极端情况
        logger.warning("设置保存失败（不影响本次运行）：%s", exc)


def _ask(prompt: str) -> str:
    # 安全的输入函数：处理两种异常情况。
    #
    # * 非交互环境（例如被定时任务调用、没有键盘输入）→ 提示改用命令行参数后退出，
    #   避免脚本卡在那里等一个永远不会来的回车。
    # * 用户按 Ctrl+C → 干净退出。
    try:
        return input(prompt)
    except EOFError:
        print()
        raise SystemExit(
            "未检测到标准输入（当前环境可能不支持交互式输入）。\n"
            "请改用命令行参数运行，例如：\n"
            '    python tcm_scraper.py --url "https://目标网址" --no-prompt'
        )
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


def run_setup_wizard(cfg: Dict[str, Any], cfg_path: Path) -> None:
    # 交互式设置向导：采集「目标地址 / 中药名 / 采集条数」，并保存至 config.json。
    #
    # 设计目的：无需修改代码即可更换目标站点。
    line = "=" * 64
    print()
    print(line)
    print(" 中药说明书采集 · 设置向导")
    print(line)

    # ---------------- 第 1 步：网页地址 ----------------
    prev_url = str(cfg.get("list_url") or "").strip()
    if prev_url == DEFAULT_CONFIG["list_url"]:
        prev_url = ""  # 占位地址不算「上次用过的」，避免误导

    while True:
        print()
        print("【第 1 步 / 共 3 步】请输入目标地址，支持以下三种形式：")
        print("    1. 搜索结果页网址（http/https 开头）：由脚本联网采集")
        print("    2. 本地文件夹路径：批量解析已保存的网页（不联网）")
        if prev_url:
            print(f"    3. 直接回车：沿用上次地址 {prev_url}")
        print("    输入 q 退出")
        answer = _ask("    目标地址 > ").strip().strip('"').strip("'")

        if answer.lower() == "q":
            print("已退出，未执行任何采集。")
            raise SystemExit(0)
        if not answer and prev_url:
            cfg["list_url"] = prev_url
            cfg["mode"] = "folder" if looks_like_folder(prev_url) else cfg.get("mode", "selenium")
            break
        if not answer:
            print("    × 输入不能为空：请粘贴网址或本地文件夹路径。")
            continue

        # 情况一：本地文件夹 → 走「解析已保存网页」模式
        if looks_like_folder(answer):
            cfg["list_url"] = answer
            cfg["mode"] = "folder"
            print("    √ 已识别为本地文件夹：将以「解析已保存网页」模式运行（不联网）。")
            break
        # 情况二：本地单个网页文件
        if is_local_url(answer):
            cfg["list_url"] = answer
            print("    √ 已识别为本地网页文件。")
            break
        # 情况三：网址
        if answer.startswith(("http://", "https://")):
            cfg["list_url"] = answer
            print("    √ 已记录网址。")
            break
        print("    × 输入无效：既不是网址，也不是本机存在的文件夹或文件。")
        print("      获取文件夹路径：在资源管理器中按住 Shift 右键单击文件夹 → 「复制文件地址」。")

    # ---------------- 第 2 步：中药名 ----------------
    print()
    print("【第 2 步 / 共 3 步】请输入用于筛选的中药名：")
    print(f"    直接回车：{cfg['name_filter'] or '不筛选（采集列表页全部条目）'}")
    if cfg["name_filter"]:
        print("    输入「全部」：不筛选")
    answer = _ask("    中药名 > ").strip()
    if answer:
        if answer in ("全部", "不限", "*", "all"):
            cfg["name_filter"] = ""
            print("    √ 已记录：不筛选。")
        else:
            cfg["name_filter"] = answer
            print(f"    √ 已记录：{answer}。")
    else:
        print(f"    √ 沿用当前设置：{cfg['name_filter'] or '不筛选'}。")

    # ---------------- 第 3 步：采集条数 ----------------
    print()
    print("【第 3 步 / 共 3 步】请输入单次最多采集的条数：")
    print("    建议首次运行填写 5，确认结果无误后再调大。")
    print(f"    直接回车：{cfg['max_items']} 条")
    answer = _ask("    条数 > ").strip()
    if answer:
        if answer.isdigit() and int(answer) > 0:
            cfg["max_items"] = int(answer)
            print(f"    √ 已记录：{answer} 条。")
        else:
            print(f"    × 输入无效：需要一个正整数，本次沿用 {cfg['max_items']} 条。")
    else:
        print(f"    √ 沿用当前设置：{cfg['max_items']} 条。")

    save_config_file(cfg_path, cfg, LOG)
    print()
    print(line)
    print(" 设置完成，即将开始采集：")
    if cfg.get("mode") == "folder":
        print(f"   目标地址： {cfg['list_url']}（本地文件夹，不联网）")
    else:
        print(f"   目标地址： {cfg['list_url']}")
    print(f"   中药名筛选： {cfg['name_filter'] or '（不筛选）'}")
    print(f"   采集上限： {cfg['max_items']} 条 / 最多 {cfg['max_pages']} 页")
    print(f"   输出文件： {Path(cfg['out_csv']).resolve()}")
    print("   以上设置已保存，下次运行可直接回车沿用。")
    print(line)
    print(" 采集过程中可随时按 Ctrl+C 中止；运行日志：logs/tcm_scraper.log")
    print()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    # 命令行参数：所有可调项都能在这里覆盖，配置区保持为「默认值」。
    parser = argparse.ArgumentParser(
        description="中药公开说明书信息采集脚本 · 单机版",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        help="列表页网址；或本地文件夹路径（配合 --mode folder 解析已保存的网页）",
    )
    parser.add_argument("--config", help=f"配置文件位置，默认脚本同目录的 {CONFIG_FILE_NAME}")
    parser.add_argument("--no-prompt", action="store_true", help="不启动设置向导，完全依据配置文件与命令行参数运行（适用于定时任务）")
    parser.add_argument("--out", help="输出 CSV 路径（默认 output/tcm_drug_data.csv）")
    parser.add_argument(
        "--mode",
        choices=["selenium", "requests", "local", "folder"],
        help="页面获取方式：folder=解析本地文件夹中已保存的网页（不联网）",
    )
    parser.add_argument("--max-items", type=int, help="最多抓取多少条详情")
    parser.add_argument("--pages", type=int, help="最多翻多少页列表")
    parser.add_argument("--name", help="关键词过滤（不指定则不筛选）")
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="关闭关键词过滤，采集列表页全部条目（PowerShell 下 --name \"\" 会被丢弃，建议使用本开关）",
    )
    parser.add_argument("--min-delay", type=float, help="请求间随机休眠下限（秒）")
    parser.add_argument("--max-delay", type=float, help="请求间随机休眠上限（秒）")
    parser.add_argument("--timeout", type=int, help="单页超时（秒）")
    parser.add_argument("--retries", type=int, help="单次请求最大重试次数")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口（用于调试选择器）")
    parser.add_argument("--save-raw", action="store_true", help="保存原始 HTML 到 output/raw_html/")
    parser.add_argument("--ignore-robots", action="store_true", help="跳过 robots.txt 检查（请谨慎使用）")
    parser.add_argument("--log-level", default="INFO", help="日志级别 DEBUG/INFO/WARNING/ERROR")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Dict[str, Any]:
    # 组装最终配置，优先级从低到高：
    #
    #     内置默认值  →  config.json  →  命令行参数  →  设置向导（只问前面都没给的东西）
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = config_file_path(args.config)

    # ① config.json（首次运行后自动生成，可用任意文本编辑器修改）
    file_cfg = load_config_file(cfg_path, LOG)
    if file_cfg:
        LOG.info("已读取配置文件：%s", cfg_path.resolve())
        cfg.update(file_cfg)

    # ② 命令行参数
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

    # ③ 交互式设置向导：仅在命令行已指定目标地址时跳过
    if not args.no_prompt and args.url is None:
        try:
            run_setup_wizard(cfg, cfg_path)
        except SystemExit:
            raise
        except Exception as exc:  # 向导异常不应阻断任务
            LOG.warning("设置向导异常（%s），改用现有配置继续", exc)

    # 详情页 wait_for 转换成 Selenium 需要的 CSS 选择器
    cfg["wait_css"] = SELECTORS.get("detail", {}).get("wait_for", "")
    cfg["log_level"] = args.log_level
    return cfg


def run(cfg: Dict[str, Any]) -> int:
    # 主流程：初始化 → 收集链接 → 抓取详情 → 清洗 → 导出。返回退出码。
    logger = setup_logging(cfg.get("log_file"), cfg.get("log_level", "INFO"))
    logger.info("=" * 78)
    logger.info("中药说明书采集任务开始 | URL=%s", cfg["list_url"])
    logger.info(
        "参数：mode=%s headless=%s 限速=%.1f~%.1fs 重试=%d 上限=%s条/%s页 关键词=%r",
        cfg["mode"], cfg["headless"], cfg["min_delay"], cfg["max_delay"],
        cfg["max_retries"], cfg["max_items"], cfg["max_pages"], cfg["name_filter"],
    )
    logger.info("=" * 78)

    if not cfg["headless"] and cfg["mode"] == "selenium":
        logger.info("已开启浏览器窗口：可按 F12 对照页面调整 config.SELECTORS 中的选择器。")

    fetcher: Optional[BaseFetcher] = None
    try:
        # 判断本次运行的实际模式。
        # 注意：配置文件可能保留上次的「文件夹模式」，而本次目标并非文件夹，
        # 此时必须按目标实际类型自动纠正，否则会出现采集 0 条且原因不明的情况。
        target = str(cfg.get("list_url") or "")
        if cfg["mode"] == "folder" and not looks_like_folder(target):
            cfg["mode"] = "local" if is_local_url(target) else "selenium"
            logger.warning(
                "配置里保存的是「文件夹模式」，但这次的目标不是文件夹，已自动改用 %s 方式抓取。", cfg["mode"]
            )

        if cfg["mode"] == "folder" or looks_like_folder(target):
            # 路线 A：文件夹模式
            # 手动在浏览器中保存好的网页 → 逐个解析，全程不联网、不限速
            logger.info("运行模式：文件夹模式（解析本地已保存的网页，不联网、不启动浏览器）")
            records = crawl_folder(cfg, logger)
            total = len(records)
        else:
            # 路线 B：在线抓取
            fetcher = build_fetcher(cfg, logger)
            crawler = Crawler(cfg, fetcher, logger)

            # 第 1 步：收集详情页链接（含翻页）
            links = crawler.collect_detail_links(cfg["list_url"])
            logger.info("共收集到 %d 个待抓取详情页", len(links))
            if not links:
                logger.error(
                    "未收集到任何待抓取页面。具体原因见上方的分类诊断"
                    "（登录墙 / 验证码 / 动态加载 / 选择器不匹配）。"
                )

            # 第 2 步：抓详情 + 解析（含限速与重试）
            records = crawler.crawl_details(links)
            total = len(links)

        # 第 3 步：清洗 + 去重
        df, columns = build_dataframe(records, logger)

        # 第 4 步：导出
        export_csv(df, columns, cfg["out_csv"], logger)

        logger.info("任务结束：成功 %d 条 / 目标 %d 条", len(df), total)
        return 0 if len(df) else 1
    except KeyboardInterrupt:
        logger.warning("用户中断（Ctrl+C）：本次已采集的数据不会导出。可减小 --max-items 分批运行。")
        return 130
    except Exception as exc:
        logger.exception("任务异常终止：%s", exc)
        return 1
    finally:
        if fetcher is not None:
            fetcher.quit()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not str(args.log_level).upper() in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        args.log_level = "INFO"

    # 先把日志准备好：读配置文件的警告、设置向导的提示，都需要它能正常输出
    setup_logging(DEFAULT_CONFIG["log_file"], args.log_level)
    cfg = build_config(args)
    setup_logging(cfg.get("log_file"), args.log_level)  # 配置里若改了日志位置，这里生效
    return run(cfg)

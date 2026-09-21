# -*- coding: utf-8 -*-
# 页面获取区（反爬策略集中在这里）：requests / selenium / 本地文件三种抓取器，
# 外加 robots.txt 检查与「抓到 0 条」的分类诊断。

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# 第三方库采用「延迟导入 / 容错导入」，保证任何一个可选依赖缺失时脚本仍能给出清晰提示，
# 而不是在最顶层直接 ImportError 崩溃（例如只解析本地已保存的网页时无需 selenium）。
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment]

try:
    # lxml 用于以 lxml 解析器构建 BeautifulSoup（更稳、更快），XPath 支持见 parse.py。
    import lxml.html as lxml_html  # type: ignore
except ImportError:  # pragma: no cover
    lxml_html = None  # type: ignore[assignment]

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    # tenacity 提供声明式重试；缺失时退化为 utils.simple_retry 装饰器。
    from tenacity import (
        before_sleep_log,
        retry as _t_retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_random_exponential,
    )

    HAS_TENACITY = True
except ImportError:  # pragma: no cover
    HAS_TENACITY = False

from .utils import random_ua, simple_retry


def read_html_file(path: Path) -> str:
    # 读取本地 HTML 文件，自动处理编码。
    #
    # 为什么需要它：Chrome「另存为」一般存成 UTF-8，但部分老站点存下来是 GBK/GB18030，
    # 直接按 UTF-8 硬读会把中文变成乱码。这里先看文件里声明的 charset，再按
    # UTF-8 → GB18030 的顺序逐个尝试，最大限度保证中文正确。
    raw = path.read_bytes()
    head = raw[:2048].decode("ascii", errors="ignore").lower()
    declared = re.search(r'charset\s*=\s*["\']?([\w-]+)', head)

    candidates: List[str] = []
    if declared:
        candidates.append(declared.group(1))
    candidates += ["utf-8", "gb18030", "utf-16"]

    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def looks_like_folder(value: str) -> bool:
    # 判断给的是不是一个本地文件夹（用于「手动存页面」的文件夹模式）。
    if not value or value.startswith(("http://", "https://")):
        return False
    try:
        return Path(value).is_dir()
    except (OSError, ValueError):
        return False


class BaseFetcher:
    # 抓取器抽象接口：统一对外暴露 get_html(url) -> str。

    def get_html(self, url: str) -> str:  # pragma: no cover - 抽象方法
        raise NotImplementedError

    def quit(self) -> None:
        # 释放资源（Selenium 需要关闭浏览器）。
        return None


class LocalFileFetcher(BaseFetcher):
    # 本地文件抓取器：解析手动保存到本机的单个网页（不联网）。
    #
    # 支持 'file:///D:/.../x.html' 和普通绝对/相对路径两种写法。
    # 本地解析与在线抓取走的是完全相同的解析与清洗代码路径。

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    @staticmethod
    def _to_path(url: str) -> Path:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            # Windows 下 file:///D:/a/b.html → /D:/a/b.html，需要去掉开头多余的 '/'
            path = parsed.path
            if re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
            return Path(path)
        return Path(url)

    def get_html(self, url: str) -> str:
        path = self._to_path(url)
        self.logger.debug("读取本地文件：%s", path)
        if not path.exists():
            raise FileNotFoundError(f"本地文件不存在：{path}")
        return read_html_file(path)


class RequestsFetcher(BaseFetcher):
    # 静态页抓取器（requests）：速度快，适合目标站点是服务端渲染的场景。
    #
    # 已内置：随机 UA、超时、tenacity 重试、会话复用（保留 cookie）。

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        if requests is None:
            raise RuntimeError("未安装 requests，请先执行：pip install requests")
        self.cfg = cfg
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": random_ua(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )
        self._fetch = self._build_fetch()

    def _build_fetch(self):
        # 按 tenacity 是否可用，选择重试实现。
        max_attempts = int(self.cfg["max_retries"])
        if HAS_TENACITY:
            return _t_retry(
                stop=stop_after_attempt(max_attempts),
                wait=wait_random_exponential(multiplier=1.5, max=20),
                retry=retry_if_exception_type((requests.RequestException, ValueError)),
                before_sleep=before_sleep_log(self.logger, logging.WARNING),
                reraise=True,
            )(self._fetch_once)
        return simple_retry(max_attempts=max_attempts)(self._fetch_once)

    def _fetch_once(self, url: str) -> str:
        # 每次请求轮换 UA，避免长时间使用同一指纹
        headers = {"User-Agent": random_ua()}
        resp = self.session.get(url, headers=headers, timeout=self.cfg["timeout"])
        if resp.status_code >= 400:
            raise ValueError(f"HTTP {resp.status_code}: {url}")
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        return resp.text

    def get_html(self, url: str) -> str:
        self.logger.info("GET(requests) %s", url)
        return self._fetch(url)


class SeleniumFetcher(BaseFetcher):
    # 动态页抓取器（selenium）：应对 JS 渲染 / 异步接口加载的页面（NMPA 数据查询属于此类）。
    #
    # 反爬与稳定性措施：
    #   * 随机 User-Agent + 随机窗口尺寸
    #   * 关闭「Chrome 正受到自动测试软件控制」提示，隐藏 navigator.webdriver
    #   * 不加载图片（提速并降低流量特征）
    #   * 页面加载超时 + 元素显式等待（WebDriverWait）
    #   * 驱动获取顺序：Selenium Manager（selenium ≥ 4.6 自带） → webdriver-manager 兜底

    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger) -> None:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 selenium，请执行：pip install selenium webdriver-manager"
            ) from exc

        self.cfg = cfg
        self.logger = logger
        self._webdriver = webdriver
        self._ua = random_ua()

        options = Options()
        if cfg["headless"]:
            options.add_argument("--headless=new")
        options.add_argument(f"--user-agent={self._ua}")
        options.add_argument(f"--window-size={random.randint(1200, 1600)},{random.randint(800, 1000)}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--lang=zh-CN")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        # 不加载图片：显著提速，且减少无用请求
        options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})

        self.driver = self._create_driver(options)

        # 进一步隐藏自动化特征（反爬对抗中最小必要的一步，仅用于可正常访问的公开页面）
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
            )
        except Exception as exc:  # pragma: no cover - 非关键步骤，失败不阻断
            self.logger.debug("CDP 反检测注入失败（可忽略）：%s", exc)

        self.driver.set_page_load_timeout(cfg["timeout"])

    def _create_driver(self, options):
        # 优先用 Selenium Manager 自动匹配驱动，失败则回退 webdriver-manager。
        try:
            driver = self._webdriver.Chrome(options=options)
            self.logger.info("已通过 Selenium Manager 启动 Chrome 驱动")
            return driver
        except Exception as first_exc:
            self.logger.warning("Selenium Manager 启动失败（%s），尝试 webdriver-manager…", first_exc)

        try:
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager

            service = Service(ChromeDriverManager().install())
            driver = self._webdriver.Chrome(service=service, options=options)
            self.logger.info("已通过 webdriver-manager 启动 Chrome 驱动")
            return driver
        except Exception as second_exc:  # pragma: no cover
            raise RuntimeError(
                "无法启动 Chrome，请确认已安装 Chrome 浏览器并联网以下载驱动。"
                f"原始错误：{second_exc}"
            ) from second_exc

    def get_html(self, url: str) -> str:
        self.logger.info("GET(selenium) %s", url)
        self.driver.get(url)

        # 显式等待：等目标元素出现，避免拿到还没渲染完的半成品 HTML
        wait_css = (self.cfg.get("wait_css") or "").strip()
        if wait_css:
            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.support.ui import WebDriverWait

                WebDriverWait(self.driver, self.cfg["timeout"]).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
                )
            except Exception as exc:
                # 等待超时不直接失败：有些页面结构不同，后续解析阶段仍可能成功
                self.logger.debug("等待元素 '%s' 超时（继续解析）：%s", wait_css, exc)

        # 给异步接口留一点缓冲时间
        time.sleep(random.uniform(0.5, 1.5))
        if "登录" in self.driver.title and "说明书" not in self.driver.title:
            self.logger.warning("页面标题疑似登录页，可能触发了站点登录墙：%s", self.driver.title)
        return self.driver.page_source

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:  # pragma: no cover
            pass


def build_fetcher(cfg: Dict[str, Any], logger: logging.Logger) -> BaseFetcher:
    # 工厂函数：根据配置选择抓取器。
    url = cfg["list_url"]
    if cfg["mode"] == "local" or is_local_url(url):
        logger.info("检测到本地目标，使用 LocalFileFetcher（离线模式，不发起网络请求）")
        return LocalFileFetcher(logger)
    if cfg["mode"] == "requests":
        return RequestsFetcher(cfg, logger)
    return SeleniumFetcher(cfg, logger)


def is_local_url(url: str) -> bool:
    # 判断是否为本地文件（file:// 或磁盘路径）。
    #
    # 注意：这里会显式排除 http/https，避免把「恰好同名的本地目录」误判为本地文件。
    if not url:
        return False
    if url.startswith("file://"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", url):  # Windows 盘符，如 D:\data\x.html
        return True
    if re.match(r"^https?://", url, re.I):
        return False  # 明确的网络地址，一定走网络抓取
    # 其余情况：相对/绝对本地路径，以磁盘上是否真实存在为准
    try:
        return Path(url).exists()
    except OSError:  # pragma: no cover - 非法路径字符等
        return False


# 登录墙 / 人机验证的特征词：用于把「抓到 0 条」翻译成用户看得懂的原因
LOGIN_WALL_HINTS: List[str] = [
    "登录之后", "请先登录", "立即登录", "登录后查看", "登录后使用", "会员登录",
    "账号登录", "注册登录", "登录/注册", "用户登录", "请登录", "去登录",
]
CAPTCHA_HINTS: List[str] = [
    "验证码", "拖动滑块", "人机验证", "安全验证", "滑动验证", "点击验证",
]


def diagnose_empty_result(html: str, url: str, logger: logging.Logger) -> None:
    # 抓到 0 个详情链接时，检查页面本身到底是什么，给出人话版原因。
    #
    # 动机：如果只丢一句「未解析到任何详情链接」，用户对着空表格根本不知道
    # 是脚本坏了、网址错了，还是站点要登录。这里把三种最常见的原因直接说清楚。
    if not html:
        logger.error("页面内容为空，可能是网络中断或请求被拦截。")
        return

    try:
        soup = BeautifulSoup(html, "lxml" if lxml_html is not None else "html.parser")
        text = soup.get_text(" ", strip=True)[:20000]
    except Exception:  # pragma: no cover
        text = html[:20000]

    lower_html = html.lower()
    has_password_box = 'type="password"' in lower_html

    # ① 登录墙
    if any(hint in text for hint in LOGIN_WALL_HINTS) or has_password_box:
        logger.error(
            "该页面需要登录后才能查看内容，因此未解析到药品列表。\n"
            "            本脚本不绕过登录墙、不破解验证码，可选方案如下：\n"
            "            (a) 更换无需登录的公开数据源，并在 config.SELECTORS 中配置对应选择器；\n"
            "            (b) 在浏览器中登录后，将页面另存为 HTML，使用 --mode local 离线解析。"
        )
        return

    # ② 人机验证 / 验证码
    if any(hint in text for hint in CAPTCHA_HINTS):
        logger.error("该页面出现【人机验证 / 验证码】，脚本无法继续（不提供验证码绕过）。建议更换公开数据源。")
        return

    # ③ JS 动态渲染或页面被拦截：源码里几乎没正文
    if len(text) < 200:
        logger.error(
            "页面正文为空（浏览器中可见但源码中没有，通常为 JS 动态加载或请求被拦截）。\n"
            "            建议使用 --no-headless --save-raw 重新运行，检查 output/raw_html/ 中的原始 HTML。"
        )
        return

    # ④ 页面正常，但结构不是列表页
    logger.error(
        "页面中未找到任何「药品详情」链接。可能原因：\n"
        "            ① 该地址是单条药品详情页，而非搜索结果列表页（脚本需要从列表页逐条进入）；\n"
        "            ② 列表页 HTML 结构与当前选择器不匹配。\n"
        "            可使用 --no-headless 打开浏览器窗口，按 F12 对照调整 SELECTORS['list']['item']。"
    )


# --- robots.txt 检查 ---------------------------------------------------------
_ROBOTS_CACHE: Dict[str, Any] = {}


def robots_allows(url: str, user_agent: str, logger: logging.Logger) -> bool:
    # 检查目标 URL 是否被 robots.txt 允许抓取。
    #
    # 说明：用 requests 拉取 robots.txt（带超时），避免 urllib 默认无超时导致卡死；
    # 拉取失败时按「允许」处理，并将判断结果写入日志，便于使用者自行决定是否继续。
    if requests is None:
        return True

    parsed = urlparse(url)
    if not parsed.scheme.startswith("http"):
        return True  # 本地文件无需检查

    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _ROBOTS_CACHE:
        from urllib.robotparser import RobotFileParser

        rp = RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        try:
            resp = requests.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": user_agent},
                timeout=10,
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                logger.info("已加载 robots.txt：%s", origin)
            else:
                rp.parse([])  # 无 robots.txt → 视为无限制
                logger.info("robots.txt 不存在（HTTP %s），视为无限制", resp.status_code)
        except Exception as exc:
            logger.warning("robots.txt 获取失败（%s），跳过检查", exc)
            rp.parse([])
        _ROBOTS_CACHE[origin] = rp

    allowed = _ROBOTS_CACHE[origin].can_fetch(user_agent, url)
    if not allowed:
        logger.warning("robots.txt 禁止抓取：%s", url)
    return allowed

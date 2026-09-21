# 药物说明书信息采集工具（tcm_scraper）

用于学术研究场景的单机 Python 采集工具：从公开药品说明书列表页（如国家药监局数据查询、药源网等），
采集指定中药相关药品的说明书字段（目标地址与关键词均可在设置向导中填写，默认不筛选），
经清洗去重后导出为 UTF-8-SIG 编码的 CSV，供后续 LLM 毒性信息抽取与数据库构建使用。

采集字段：`药品名称` `主要成分` `不良反应` `禁忌` `注意事项`（另附 `数据来源URL` `抓取时间`，用于数据溯源）

---

## 1. 环境要求

- Python 3.9+（已在 Python 3.14.7 + Windows 环境验证通过）
- 目标站点为 JS 动态渲染页面时，需本机安装 **Chrome 浏览器**（由 Selenium 驱动）

## 2. 安装依赖

建议使用独立虚拟环境，以免污染全局 Python 环境：

```powershell
# 在项目根目录执行
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **使用虚拟环境的原因**：在全局环境中直接执行 `pip install selenium` 可能触发 pip 依赖回溯（该过程无输出，容易被误判为进程卡死），
> 甚至报 `ResolutionImpossible` 错误。独立虚拟环境可完全避免此类依赖冲突。
>
> **PyCharm 用户**：依次选择 `File → Settings → Project → Python Interpreter → Add → Existing`，
> 指定 `.venv\Scripts\python.exe`，之后可直接运行脚本。

## 3. 路线 A（推荐）：手动保存页面，脚本批量抽取字段

**适用场景**：目标数据库需要登录或付费（例如药智数据），脚本不宜也不应绕过登录机制。
用户在浏览器中正常浏览并保存页面，脚本仅负责将 5 个字段批量抽取为表格。
此路线全程不联网、不启动浏览器，不存在账号风险。

### 3.1 在浏览器中保存页面（Chrome）

1. 登录目标数据库，搜索并打开一条药品的**说明书**页面
2. **待页面内容完整显示后再保存**（若在内容加载完成前按下 `Ctrl+S`，保存结果仅为空白框架）
3. 按 `Ctrl+S`
4. 保存位置选择 `saved_pages` 文件夹（不存在则新建）
5. 文件名改为药品名称，例如 `黄芩片`；格式选择「**网页，仅 HTML**」
6. 切换至下一条药品，重复第 3~5 步。建议先保存 3~5 条进行验证

> 文件名建议使用药品名称：当页面中读取不到药名时，脚本会以文件名作为兜底。
> 文件名不规范亦不影响使用，页面中通常均可读取到药名。

保存完成后，文件应形如：`saved_pages\黄芩片.html`。
（请勿选择「网页，全部」——该选项会额外生成 `_files` 文件夹，虽无副作用但并无必要；
也**不要**保存为 `.mhtml` 格式，脚本不支持该格式。）

### 3.2 解析整个文件夹

```powershell
.venv\Scripts\python.exe tcm_scraper.py
```

在第 1 步问答中直接**粘贴文件夹路径**，脚本会自动识别为「文件夹模式」：

```
【第 1 步 / 共 3 步】请输入目标地址，支持以下三种形式：
    1. 搜索结果页网址（http/https 开头）：由脚本联网采集
    2. 本地文件夹路径：批量解析已保存的网页（不联网）
    目标地址 > saved_pages
    √ 已识别为本地文件夹：将以「解析已保存网页」模式运行（不联网）。
```

文件夹路径获取方式：打开 `saved_pages` 文件夹，单击地址栏并完整复制（形如
`D:\PycharmProjects\tcm-drug-scraper\saved_pages`）。相对路径 `saved_pages` 亦可。

非交互式调用（不进入问答）：

```powershell
.venv\Scripts\python.exe tcm_scraper.py --mode folder --url "saved_pages"
```

### 3.3 结果检查

使用 Excel 打开 `output\tcm_drug_data.csv`（自动生成，UTF-8-SIG 编码，无乱码）：

- 5 个字段均有内容 → 采集成功
- 某字段为 `N/A` → 该页面确未包含此项，或选择器需调整（参见 4.1）
- **整行仅有药名、其余字段均为 `N/A`** → 保存页面时内容尚未加载完成，需返回浏览器重新保存该文件
- 缺少某个药品 → 检查文件是否保存为 `.mhtml`，或保存到了其他文件夹

确认结果无误后，可按同样方式增加保存页面数量并重新运行（重复数据会按
`药品名称 + 主要成分` 自动去重）。

## 4. 路线 B：由脚本自动联网采集

> **练习目标推荐（已实测）**：药源网「中药说明书」字母列表页——公开、无需登录、静态页面，选择器已配好：
>
> ```powershell
> .venv\Scripts\python.exe tcm_scraper.py --url "https://www.yaopinnet.com/zhongyao1/h1.htm" --mode requests --no-filter --max-items 5
> ```
>
> 该页为字母 H 开头的 40 条中药说明书（含黄芩片、黄连上清片等）。中药老品种的
> 「不良反应 / 禁忌」常为「尚不明确」或整项缺失，导出 `N/A` 属正常现象。

指定目标共有 **三种** 方式，按推荐顺序排列：

### 方式一：直接运行，根据交互提示输入（推荐）

```powershell
.venv\Scripts\python.exe tcm_scraper.py
```

出现如下问答后，粘贴网址并回车即可：

```
【第 1 步 / 共 3 步】请输入目标地址，支持以下三种形式：
    1. 搜索结果页网址（http/https 开头）：由脚本联网采集
    2. 本地文件夹路径：批量解析已保存的网页（不联网）
    输入 q 退出
    目标地址 >  https://你要抓的搜索结果页
```

- 回答完 3 个问题（目标地址 / 中药名 / 采集条数）后开始采集
- 答案自动写入 `config.json`，**后续运行可直接连续回车沿用**
- 需要更换地址时，重新运行并输入新地址即可
- 第 2 步直接回车 = 沿用当前设置；输入 `全部` = 不筛选

### 方式二：修改 config.json

脚本首次运行会自动在同目录生成 `config.json`，可用任意文本编辑器修改，**无需 Python 知识**：

```json
{
  "list_url": "https://你要抓的搜索结果页",
  "name_filter": "",
  "max_items": 20,
  "max_pages": 3,
  "mode": "selenium",
  "headless": true,
  "min_delay": 2.0,
  "max_delay": 5.0
}
```

注意事项：

- `list_url` 必须为**搜索结果列表页**地址，不能是单条药品的详情页
- `name_filter` 留空表示不筛选；填写关键词（如 `黄芩`）则仅采集名称包含该词的条目
- 网址需使用英文双引号包裹，最后一项后**不要**添加逗号
- 修改保存后即可生效，无需重启

### 方式三：通过命令行参数指定（一次性，不落盘）

```powershell
.venv\Scripts\python.exe tcm_scraper.py --url "https://你的目标站点/搜索结果页"
```

命令中包含 `--url` 时不再弹出问答。

### 4.1 选择器调整（进阶，仅当脚本无法抓取内容时）

在 Chrome 中打开搜索结果页 → F12 → 选中目标元素 → 右键 `Copy → Copy selector`（获取 CSS 选择器）或
`Copy → Copy XPath`。将结果填入 `tcm_scraper_lib/config.py` 中 `SELECTORS` 对应字段的**候选列表最前位置**，旧选择器无需删除：

```python
SELECTORS = {
    "list": {
        "item":      ["table tbody tr"],          # 一条搜索结果
        "item_link": ["a::attr(href)"],           # 条目中的详情页链接
        "item_name": ["td.name"],                 # 条目中的药名（用于关键词过滤）
        "next_page": ["a.next::attr(href)"],      # 下一页
    },
    "detail": {
        "drug_name":         ["h1.drug-name", "//h1"],
        "ingredients":       ["div.field-ingredients div.content"],
        "adverse_reactions": ["div.field-adverse div.content"],
        "contraindications": ["div.field-contraindications div.content"],
        "precautions":       ["div.field-precautions div.content"],
    },
}
```

选择器写法约定：

| 写法 | 解析方式 |
|---|---|
| `div.content`、`table tbody tr` | CSS Selector（BeautifulSoup） |
| `//h1`、`//a[contains(text(),'下一页')]`、`xpath://div[@id="x"]` | XPath（需 lxml） |
| `a::attr(href)` | 取属性而非文本 |

### 4.2 调试方法

```powershell
# 打开可见浏览器窗口，便于观察页面并调整选择器
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --no-headless --max-items 2 --log-level DEBUG

# 保存原始 HTML：一次抓取后可反复调整选择器并重放解析，避免重复请求目标站点
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --save-raw
```

即使选择器全部失效，脚本仍有三层兜底机制：**选择器 → 中文标注符号（【不良反应】）→ 标签节点/表格对**，
通常首次运行即可完成抓取，无需一次性配置完善。

## 5. 正式运行（路线 B 完整参数）

```powershell
# 最简方式：运行后按问答提示粘贴网址
.venv\Scripts\python.exe tcm_scraper.py

# 网址写入命令（不弹问答）：采集全部条目，最多 20 条详情 / 3 页列表
.venv\Scripts\python.exe tcm_scraper.py --url "https://你的目标站点/搜索结果页"

# 仅抓取 5 条以先行验证
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --max-items 5

# 改抓「甘草」，翻 5 页，导出至自定义路径
.venv\Scripts\python.exe tcm_scraper.py --url "https://..." --name "甘草" --pages 5 --out "output/gancao.csv"
```

在 PyCharm 中直接运行效果相同——未提供 `--url` 时会在控制台发起询问，需手动输入网址。

常用参数：

| 参数 | 说明 |
|---|---|
| （无参数） | 启动设置向导，询问网址、中药名与抓取条数 |
| `--url` | 列表页 URL；或文件夹路径（配合 `--mode folder`） |
| `--config` | 指定其他配置文件（可为每味中药分别保存配置，默认 `config.json`） |
| `--no-prompt` | 不启动设置向导，完全依据配置文件与命令行参数运行（适用于定时任务） |
| `--name` | 关键词过滤（不指定则不筛选） |
| `--no-filter` | 关闭关键词过滤（PowerShell 下 `--name ""` 会被吞掉，请使用本开关） |
| `--max-items` | 最多抓取的详情条数，默认 20（建议由小到大逐步调整） |
| `--pages` | 最多翻页数，默认 3 |
| `--mode` | `selenium`（默认，支持动态页面）/ `requests`（速度快，适用于静态页面）/ `local`（单个本地文件）/ `folder`（解析文件夹中手动保存的网页） |
| `--min-delay` / `--max-delay` | 请求间随机休眠区间，默认 2~5 秒（**请勿调低**） |
| `--retries` | 单次请求最大重试次数，默认 3 |
| `--timeout` | 单页超时秒数，默认 30 |
| `--no-headless` | 显示浏览器窗口（调试用） |
| `--save-raw` | 保存原始 HTML 至 `output/raw_html/` |
| `--ignore-robots` | 跳过 robots.txt 检查（请谨慎使用） |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## 6. 输出说明

- 文件：`output/tcm_drug_data.csv`
- 编码：**UTF-8-SIG（带 BOM）**，Excel 直接打开中文不乱码
- 缺失值：统一填充为 `N/A`（注意 `N/A` 是 Excel/pandas 的默认 NA 标记，读取时设置 `keep_default_na=False` 可保留原值）
- 去重规则：先按 `药品名称 + 主要成分` 去重，再按 `药品名称 + 数据来源URL` 去重
- 无药品名称的记录将被直接丢弃（名称是后续 LLM 抽取的主键）
- 日志：输出至控制台与 `logs/tcm_scraper.log`（UTF-8，含每个字段的命中/缺失详情）

读取示例：

```python
import pandas as pd
df = pd.read_csv("output/tcm_drug_data.csv", encoding="utf-8-sig")
print(df.shape, df.columns.tolist())
```

## 7. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 表格中仅有表头，无数据 | 查看日志末尾的分类诊断：登录墙 / 验证码 / 页面为空壳 / 选择器不匹配，脚本会明确指出具体原因 |
| 需要更换网址 | 重新运行脚本并在问答中输入新网址；或修改 `config.json` 中的 `list_url` |
| 每次运行均询问网址 | 问答结果会自动写入 `config.json`，此后直接连续回车即可。如需完全禁用询问，在命令中添加 `--no-prompt`（须同时提供 `--url`） |
| 运行后未等待输入即退出 | 当前环境非交互式（例如由定时任务调用）。请改用 `--url "..." --no-prompt` |
| `config.json` 被改坏 | 不影响运行，脚本会给出警告并使用默认值继续。删除该文件后重新运行即可再次生成 |
| 日志出现乱码 | 控制台代码页问题。`chcp 65001` 切换至 UTF-8；`logs/tcm_scraper.log` 始终为正常 UTF-8 |
| 抓到 0 条 / 字段多为 `N/A` | 路线 A 参见 3.3；路线 B 参见 4.1 调整选择器 |
| 页面为登录墙或需要验证码 | 脚本不提供登录与验证码绕过功能。**请改用路线 A**（登录后手动保存页面） |
| Chrome 驱动启动失败 | 确认已安装 Chrome 且网络可用；脚本会先通过 Selenium Manager 自动匹配驱动，失败后回退至 webdriver-manager |
| 页面无法翻页 / 抓到重复数据 | `next_page` 选择器不匹配，或该站点使用 JS 分页（可在 F12 Network 中定位真实分页接口，改用 `--mode requests`） |
| 希望加快采集速度 | 增大 `--max-items` 并不会更快——**性能瓶颈来自刻意的限速设计**。请保持 2~5 秒间隔，这是避免被封禁的关键 |

## 8. 合规与伦理（请务必遵守）

- 本脚本仅用于学术研究，请遵守目标站点的 `robots.txt`、用户协议与服务条款（脚本默认已启用 robots 检查）
- 默认 2~5 秒随机间隔与单页上限为刻意设计，**请勿为提升速度而调低**。高频请求可能导致 IP 封禁，并影响其他用户的正常使用
- 采集到的说明书文本版权归原权利人所有。建议仅保留结构化字段用于研究，不得公开再分发全文
- 本脚本及其输出**不构成任何用药建议**，不得用于医疗决策

## 9. 目录结构

```
tcm-drug-scraper/
├── tcm_scraper.py          # 入口脚本（python tcm_scraper.py 即可运行）
├── tcm_scraper_lib/        # 实现代码，按职责拆分
│   ├── config.py           # ★ 配置区：选择器、字段映射、默认参数（适配新站点主要修改此处）
│   ├── utils.py            # 日志、文本清洗、限速、重试
│   ├── fetch.py            # 页面获取（requests / selenium / 本地文件）+ robots.txt + 空结果诊断
│   ├── parse.py            # 说明书字段解析（选择器 → 标注符号 → 标签节点）
│   ├── crawl.py            # 列表页翻页、详情页遍历、本地文件夹批量解析
│   ├── pipeline.py         # pandas 清洗、去重、CSV 导出
│   └── cli.py              # 命令行参数、设置向导、主流程
├── config.json             # 用户配置（网址、中药名、抓取条数）；首次运行自动生成，任意文本编辑器可改
├── requirements.txt        # 依赖清单
├── README.md               # 本文档
├── saved_pages/            # 路线 A：手动保存的网页存放目录（需自行创建）
├── output/                 # CSV 与原始 HTML 输出（可安全删除，建议加入 .gitignore）
└── logs/                   # 运行日志（可安全删除，建议加入 .gitignore）
```

`.gitignore` 建议：

```gitignore
.venv/
output/
logs/
__pycache__/
```

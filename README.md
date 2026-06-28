# Daily Mail 全量数据采集系统

**目标网站**: https://www.dailymail.com  
**采集范围**: 2016年1月 至 2026年1月 (10年)  
**采集内容**: 新闻存档 → 文章详情 → 评论 → 用户汇总 → 用户历史评论

## 项目结构

```
dailymail/
├── main.py                # 主控入口，CLI 调度
├── config.py              # 配置（数据库、代理、采集范围）
├── database.py            # PostgreSQL 异步操作封装
├── schema.sql             # 数据库表结构
├── sitemap_scraper.py     # Sitemap 新闻列表采集
├── article_scraper.py     # 文章详情采集
├── comment_scraper.py     # 评论采集（Chrome CDP）
├── user_aggregator.py     # 用户数据汇总
├── verify.py              # 数据一致性校验
├── pyproject.toml         # 项目配置 & uv 依赖管理
├── requirements.txt       # pip 兼容依赖
└── uv.lock                # 依赖锁定文件
```

## 环境要求

- Python 3.12+
- PostgreSQL 数据库
- Chrome 浏览器（评论采集需要 CDP 调试模式）

## 安装

### uv（推荐）

```bash
pip install uv          # 安装 uv（仅首次）
uv sync                 # 同步依赖 + 创建 .venv
```

### pip（备用）

```bash
pip install -r requirements.txt
```

## 使用方式

> 使用 uv 时，命令前加 `uv run`，如 `uv run python main.py --full`

### 1. 初始化数据库

```bash
uv run python main.py --init
```

### 2. 全量采集（推荐）

```bash
# 串行模式
uv run python main.py --full

# 用户汇总使用 5 Tab 并行
uv run python main.py --full --parallel 5
```

### 3. 分步采集

```bash
uv run python main.py --sitemap              # Phase 1: 新闻列表
uv run python main.py --articles             # Phase 2: 文章详情
uv run python main.py --comments             # Phase 3: 评论（需 Chrome 调试模式）
uv run python main.py --users                # Phase 4: 用户汇总
uv run python main.py --verify               # Phase 5: 数据校验
```

### 4. 查看进度

```bash
uv run python main.py --status               # 各阶段完成度
uv run python main.py --stats                # 数据库统计
```

### 5. 测试模式

```bash
uv run python main.py --comments --limit 10   # 只采集 10 篇的评论
uv run python main.py --users --parallel 3 --limit 50  # 只处理 50 个用户
```

### 6. 中断续传

程序支持断点续传，中断后重新运行相同命令即可自动跳过已完成部分：

```bash
uv run python main.py --full                  # 中断后重跑，已完成部分自动跳过
uv run python main.py --reset --phase users   # 重置用户阶段重新采集
```

## Chrome 调试模式

评论采集和用户汇总需要 Chrome 以调试模式运行。

### 启动命令（Windows）

```powershell
# 无代理（默认）
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222

# 如果端口冲突（已有普通 Chrome 在跑），加独立 profile
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\chrome_debug_profile
```

### 带代理

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --proxy-server=127.0.0.1:10809
```

## 代理配置

编辑 `config.py`：

```python
PROXY_ENABLED = True
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 10809
```

## 数据库

默认使用本地测试库，正式环境需修改 `config.py` 中的数据库连接参数。

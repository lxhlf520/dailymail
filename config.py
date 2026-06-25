"""Daily Mail 数据采集配置"""

import os

# PostgreSQL 数据库配置
PG_HOST = "localhost"
PG_PORT = 5432
PG_USER = "postgres"
PG_PASSWORD = "long123456"
PG_DB = "dailymail"
PG_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}?sslmode=disable"

# 采集范围
START_DATE = "2016-01"
END_DATE = "2026-01"

# 请求配置
REQUEST_TIMEOUT = 30
REQUEST_RETRY = 3
REQUEST_DELAY = 0.8  # 文章页面请求间隔（秒）

# 评论 API 配置
COMMENT_BATCH_SIZE = 50  # 每页评论数
COMMENT_DELAY = 3.0      # 评论 API 请求间隔（秒），低于3秒可能触发429限流
COMMENT_MAX_RETRY = 5
COMMENT_RATELIMIT_BACKOFF = [15, 45, 90, 180, 300]  # 429/403 限流时退避秒数

# CDP 配置
CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
CDP_WS_URL = f"ws://{CDP_HOST}:{CDP_PORT}/devtools/browser/"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def get_chrome_cmd() -> str:
    """获取 Chrome 调试模式启动命令"""
    proxy_arg = get_chrome_proxy_arg()
    parts = [f'"{CHROME_PATH}"', f"--remote-debugging-port={CDP_PORT}"]
    if proxy_arg:
        parts.append(proxy_arg)
    return " ".join(parts)


# 基础 URL
BASE_URL = "https://www.dailymail.com"
SITEMAP_URL = f"{BASE_URL}/home/sitemaparchive/index.html"

# 用户 Profile 页面字段采集开关
# 设为 False 跳过 CDP 导航到 Profile 页面的耗时操作
# （Country, Profile_Photo, Facebook_URL, Member_Since）
# Arrow Factor 和评论采集不受影响
COLLECT_PROFILE_FIELDS = False

# ==================== 代理配置 ====================
# 国内 IP 无法直接访问 dailymail.com，需要代理
# 代理开关：总控，True=使用代理，False=直连
PROXY_ENABLED = False

# 代理服务器
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 10809
PROXY_USER = ""  # 无需认证则留空
PROXY_PASS = ""


def get_proxy_url() -> str | None:
    """构建代理 URL（Python requests 使用）"""
    if not PROXY_ENABLED:
        return None
    if PROXY_USER and PROXY_PASS:
        return f"http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    return f"http://{PROXY_HOST}:{PROXY_PORT}"


def get_proxies() -> dict | None:
    """获取 requests 库的 proxies 参数"""
    url = get_proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def get_chrome_proxy_arg() -> str:
    """获取 Chrome 启动时的 --proxy-server 参数

    使用方式:
      chrome --remote-debugging-port=9222 {get_chrome_proxy_arg()}

    如需代理认证:
      chrome 不支持命令行传认证信息，需要用扩展或 proxychains
      隧道代理通常不需要认证（已包含在代理URL中）
    """
    if not PROXY_ENABLED:
        return ""
    if PROXY_USER and PROXY_PASS:
        return f"--proxy-server=http://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    return f"--proxy-server=http://{PROXY_HOST}:{PROXY_PORT}"

# 日志
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# User-Agent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

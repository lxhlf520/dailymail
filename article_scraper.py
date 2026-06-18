"""Daily Mail 文章详情采集模块"""

import asyncio
import json
import logging
import re

import requests
from bs4 import BeautifulSoup

from config import HEADERS, REQUEST_DELAY, REQUEST_RETRY, REQUEST_TIMEOUT, get_proxies
from database import get_db, insert_article_info, get_articles_without_details, set_progress, get_progress

logger = logging.getLogger(__name__)


async def fetch_html(url: str, retries: int = REQUEST_RETRY) -> str | None:
    """获取页面 HTML，带重试（在线程池中运行避免阻塞）"""
    for attempt in range(retries):
        try:
            resp = await asyncio.to_thread(
                requests.get, url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                proxies=get_proxies(),
            )
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.warning(f"请求失败 {url} (attempt {attempt + 1}/{retries}): {e}")
            await asyncio.sleep(2 ** attempt)
    logger.error(f"请求最终失败: {url}")
    return None


def extract_json_ld(html: str) -> dict:
    """从页面中提取 JSON-LD schema 数据"""
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "NewsArticle":
                return data
            # 有时 JSON-LD 是数组
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "NewsArticle":
                        return item
        except (json.JSONDecodeError, AttributeError):
            continue
    return {}


def extract_comments_count(html: str) -> int:
    """从页面中提取评论数量"""
    soup = BeautifulSoup(html, "lxml")

    # 方法1: 寻找包含评论数的元素
    # 常见模式: "283 View comments", "5 comments", etc.
    patterns = [
        r"(\d+)\s+View\s+comments",
        r"(\d+)\s+comments",
        r"comments-count.*?>(\d+)",
    ]

    text = soup.get_text()
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    # 方法2: 查找特定 data attribute
    count_el = soup.find(attrs={"data-track-module": lambda x: x and "comments-count" in x})
    if count_el:
        text = count_el.get_text()
        match = re.search(r"(\d+)", text)
        if match:
            return int(match.group(1))

    return 0


def extract_share_count(html: str) -> int:
    """从页面中提取分享数量"""
    soup = BeautifulSoup(html, "lxml")

    # 常见模式: "35 shares", "SHARE SELECTION 35"
    text = soup.get_text()
    match = re.search(r"(\d+)\s+shares?", text, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # 查找 share 相关元素
    share_el = soup.find(attrs={"data-track-module": lambda x: x and "share" in x})
    if share_el:
        text = share_el.get_text()
        match = re.search(r"(\d+)", text)
        if match:
            return int(match.group(1))

    return 0


def parse_article(html: str, url: str, article_id: str) -> dict | None:
    """解析文章页面，提取元数据"""
    soup = BeautifulSoup(html, "lxml")
    json_ld = extract_json_ld(html)

    # 标题
    title = json_ld.get("headline", "").strip()
    if not title:
        title_el = soup.find("h1") or soup.find("title")
        if title_el:
            title = title_el.get_text(strip=True)

    # 作者
    author = ""
    author_data = json_ld.get("author")
    if isinstance(author_data, dict):
        author = author_data.get("name", "")
    elif isinstance(author_data, list) and author_data:
        author = author_data[0].get("name", "") if isinstance(author_data[0], dict) else ""
    elif isinstance(author_data, str):
        author = author_data

    # 从 DOM 补充作者
    if not author:
        author_el = soup.find("a", href=re.compile(r"/profile-"))
        if author_el:
            author = author_el.get_text(strip=True)

    # 发布时间
    published = json_ld.get("datePublished", "")
    updated = json_ld.get("dateModified", "")

    # 标签
    tag1 = ""
    tag2 = ""
    article_section = json_ld.get("articleSection", "")
    keywords = json_ld.get("keywords", "")
    if isinstance(article_section, str):
        tag1 = article_section
    if isinstance(keywords, str):
        tag2 = keywords
    elif isinstance(keywords, list):
        tag2 = ", ".join(str(k) for k in keywords[:3])

    # 评论数
    comments_count = extract_comments_count(html)

    # 分享数
    share_count = extract_share_count(html)

    if not title:
        logger.warning(f"无法解析标题: {url}")
        return None

    return {
        "art_id": article_id,
        "title": title,
        "author": author,
        "published_at": published,
        "updated_at": updated,
        "tag1": tag1,
        "tag2": tag2,
        "comments_count": comments_count,
        "share_count": share_count,
        "url": url,
    }


async def scrape_articles(batch_size: int = 100, limit: int | None = None) -> dict:
    """采集文章详情

    Args:
        batch_size: 每批处理的文章数量
        limit: 最多采集篇数，None=全部

    Returns:
        统计信息

    设计要点：每个数据库操作使用独立的短连接（async with get_db()），
    避免长时间持有连接导致 Python 3.13 + asyncpg 下连接被提前释放。
    HTTP 请求在数据库操作之间进行，不持有连接。
    """
    stats = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
    }

    while True:
        # 每批获取文章列表（短连接）
        async with get_db() as db:
            articles = await get_articles_without_details(db, batch_size)
        if not articles:
            logger.info("所有文章详情已采集完毕")
            break

        logger.info(f"本批处理 {len(articles)} 篇文章")

        for art in articles:
            if limit and stats["processed"] >= limit:
                logger.info(f"已达到限制 {limit} 篇，停止采集")
                return stats

            article_id = art["article_id"]
            url = art["url"]
            progress_key = f"article_{article_id}"

            try:
                # 1. 检查进度（短连接）
                async with get_db() as db:
                    if await get_progress(db, progress_key) == "done":
                        stats["skipped"] += 1
                        continue

                # 2. HTTP 请求（不持有数据库连接）
                html = await fetch_html(url)
                if not html:
                    stats["failed"] += 1
                    continue

                result = parse_article(html, url, article_id)
                if not result:
                    stats["failed"] += 1
                    continue

                # 3. 保存结果 + 标记进度（短连接，一次完成）
                async with get_db() as db:
                    await insert_article_info(
                        db,
                        result["art_id"],
                        result["title"],
                        result["author"],
                        result["published_at"],
                        result["updated_at"],
                        result["tag1"],
                        result["tag2"],
                        result["comments_count"],
                        result["share_count"],
                        result["url"],
                    )
                    await set_progress(db, progress_key, "done")

                stats["success"] += 1
                stats["processed"] += 1

                if stats["processed"] % 50 == 0:
                    logger.info(f"进度: {stats['processed']} 篇已处理")

                await asyncio.sleep(REQUEST_DELAY)

            except Exception as e:
                logger.error(f"处理文章异常 {article_id}: {e}")
                stats["failed"] += 1

    logger.info(
        f"文章详情采集完成: 处理 {stats['processed']} 篇, "
        f"成功 {stats['success']} 篇, 失败 {stats['failed']} 篇"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(scrape_articles())
    print(result)

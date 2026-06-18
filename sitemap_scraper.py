"""Daily Mail Sitemap 新闻列表采集模块"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import BASE_URL, HEADERS, REQUEST_DELAY, REQUEST_RETRY, REQUEST_TIMEOUT, get_proxies
from database import get_db, insert_daily_article, set_progress, get_progress

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


def parse_sitemap_index(html: str) -> list[str]:
    """解析 Sitemap 首页，获取所有月度存档链接"""
    soup = BeautifulSoup(html, "lxml")
    month_links = []
    for a in soup.find_all("a", href=re.compile(r"month_\d{6}\.html")):
        href = a.get("href")
        if href:
            month_links.append(urljoin(BASE_URL, href))
    # 去重并保持顺序
    seen = set()
    result = []
    for link in month_links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


def parse_month_page(html: str) -> list[str]:
    """解析月度存档页，获取所有日期存档链接"""
    soup = BeautifulSoup(html, "lxml")
    day_links = []
    for a in soup.find_all("a", href=re.compile(r"day_\d{8}\.html")):
        href = a.get("href")
        if href:
            day_links.append(urljoin(BASE_URL, href))
    seen = set()
    result = []
    for link in day_links:
        if link not in seen:
            seen.add(link)
            result.append(link)
    return result


def parse_day_page(html: str, archive_date: str) -> list[dict]:
    """解析日期存档页，获取当天所有文章"""
    soup = BeautifulSoup(html, "lxml")
    articles = []
    # 文章链接模式: /news/article-{id}/{slug}.html
    pattern = re.compile(r"/news/article-(\d+)/")

    for a in soup.find_all("a", href=pattern):
        href = a.get("href")
        if not href:
            continue
        match = pattern.search(href)
        if not match:
            continue
        article_id = match.group(1)
        title = a.get_text(strip=True)
        if not title or not article_id:
            continue
        articles.append({
            "article_id": article_id,
            "title": title,
            "url": urljoin(BASE_URL, href),
        })

    # 去重（同一文章可能在多个位置出现）
    seen = set()
    result = []
    for art in articles:
        if art["article_id"] not in seen:
            seen.add(art["article_id"])
            result.append(art)

    return result


def extract_date_from_url(url: str) -> str | None:
    """从日期页面 URL 中提取日期 YYYY-MM-DD"""
    match = re.search(r"day_(\d{4})(\d{2})(\d{2})\.html", url)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def extract_month_from_url(url: str) -> str | None:
    """从月度页面 URL 中提取月份 YYYY-MM"""
    match = re.search(r"month_(\d{4})(\d{2})\.html", url)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return None


async def scrape_sitemap(start_month: str = "2016-01", end_month: str = "2026-01") -> dict:
    """采集 Sitemap 新闻列表

    Args:
        start_month: 起始月份 YYYY-MM
        end_month: 结束月份 YYYY-MM

    Returns:
        统计信息
    """
    stats = {
        "months_total": 0,
        "days_total": 0,
        "articles_total": 0,
        "errors": 0,
    }

    async with get_db() as db:
        # 1. 获取 Sitemap 首页
        logger.info(f"获取 Sitemap 首页: {BASE_URL}/home/sitemaparchive/index.html")
        html = await fetch_html(f"{BASE_URL}/home/sitemaparchive/index.html")
        if not html:
            logger.error("无法获取 Sitemap 首页")
            return stats

        month_links = parse_sitemap_index(html)
        logger.info(f"发现 {len(month_links)} 个月度存档链接")

        # 2. 过滤指定月份范围
        filtered_months = []
        for link in month_links:
            month = extract_month_from_url(link)
            if month and start_month <= month <= end_month:
                filtered_months.append((month, link))

        filtered_months.sort(key=lambda x: x[0])
        stats["months_total"] = len(filtered_months)
        logger.info(f"过滤后需采集 {len(filtered_months)} 个月")

        # 3. 遍历每个月，获取日期页面
        for month, month_url in filtered_months:
            logger.info(f"处理月度存档: {month}")

            # 检查进度
            progress_key = f"sitemap_month_{month}"
            if await get_progress(db, progress_key) == "done":
                logger.info(f"  跳过已完成的月份: {month}")
                continue

            month_html = await fetch_html(month_url)
            if not month_html:
                stats["errors"] += 1
                continue

            day_links = parse_month_page(month_html)
            logger.info(f"  {month} 包含 {len(day_links)} 个日期")

            # 4. 遍历每个日期，获取文章列表
            for day_url in day_links:
                archive_date = extract_date_from_url(day_url)
                if not archive_date:
                    continue

                # 检查是否已采集
                from database import get_daily_article_count
                existing = await get_daily_article_count(db, archive_date)
                if existing > 0:
                    logger.debug(f"    跳过已采集日期: {archive_date} ({existing} 篇)")
                    continue

                day_html = await fetch_html(day_url)
                if not day_html:
                    stats["errors"] += 1
                    continue

                articles = parse_day_page(day_html, archive_date)
                stats["days_total"] += 1
                stats["articles_total"] += len(articles)

                # 写入数据库
                for art in articles:
                    await insert_daily_article(
                        db, archive_date, art["article_id"], art["title"], art["url"]
                    )

                logger.info(f"    {archive_date}: 采集 {len(articles)} 篇文章")
                await asyncio.sleep(REQUEST_DELAY)

            # 标记月份完成
            await set_progress(db, progress_key, "done")

    logger.info(
        f"Sitemap 采集完成: {stats['months_total']} 个月, "
        f"{stats['days_total']} 天, {stats['articles_total']} 篇文章"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(scrape_sitemap())
    print(result)

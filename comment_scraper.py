"""Daily Mail 评论采集模块 - 通过 Chrome CDP 浏览器环境获取"""

import asyncio
import json
import logging
import re
import time
import urllib.parse

import asyncpg
import websockets

from config import (
    BASE_URL,
    CDP_HOST,
    CDP_PORT,
    COMMENT_BATCH_SIZE,
    COMMENT_DELAY,
    COMMENT_MAX_RETRY,
)
from database import (
    get_db,
    insert_comment,
    upsert_user,
    get_articles_with_comments,
    get_comment_count_by_article,
    set_progress,
    get_progress,
)

logger = logging.getLogger(__name__)


class CDPCommentFetcher:
    """通过 Chrome DevTools Protocol 获取评论"""

    def __init__(self, host: str = CDP_HOST, port: int = CDP_PORT):
        self.host = host
        self.port = port
        self.ws = None
        self.msg_id = 0
        self.session_id = None

    async def connect(self) -> bool:
        """连接到 Chrome CDP"""
        try:
            import requests

            # 获取页面列表，找到 dailymail.com 页面
            targets = await asyncio.to_thread(
                lambda: requests.get(
                    f"http://{self.host}:{self.port}/json/list", timeout=10
                ).json()
            )

            page_ws_url = None
            for t in targets:
                url = t.get("url", "")
                if "dailymail.com" in url:
                    page_ws_url = t.get("webSocketDebuggerUrl")
                    break

            if not page_ws_url and targets:
                # 使用第一个页面
                page_ws_url = targets[0].get("webSocketDebuggerUrl")

            if not page_ws_url:
                logger.error("无法获取页面 WebSocket URL")
                return False

            self.ws = await websockets.connect(
                page_ws_url, open_timeout=10, close_timeout=10
            )
            logger.info(f"已连接到 Chrome CDP 页面: {page_ws_url[:80]}...")
            return True

        except Exception as e:
            logger.error(f"连接 Chrome CDP 失败: {e}")
            return False

    async def _send(self, method: str, params: dict = None) -> dict:
        """发送 CDP 命令并接收响应（自动 drain 中间的事件消息）"""
        self.msg_id += 1
        msg = {"id": self.msg_id, "method": method}
        if params:
            msg["params"] = params

        await self.ws.send(json.dumps(msg))
        # drain：可能有页面事件在响应之前到达，需要循环接收直到拿到对应 id 的响应
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=30)
            resp = json.loads(raw)
            if resp.get("id") == self.msg_id:
                return resp
            # 否则是事件消息，忽略（如 Page.frameStoppedLoading 等）

    async def connect_to_url(self, ws_url: str) -> bool:
        """连接到指定的 WebSocket URL（用于多 Tab 场景）"""
        try:
            self.ws = await websockets.connect(
                ws_url, open_timeout=10, close_timeout=10
            )
            return True
        except Exception as e:
            logger.error(f"连接 WebSocket 失败: {ws_url[:80]}..., {e}")
            return False

    @staticmethod
    async def create_tab(host: str = CDP_HOST, port: int = CDP_PORT) -> str | None:
        """通过 browser-level CDP 创建新的 Chrome Tab

        返回新 Tab 的 webSocketDebuggerUrl，失败返回 None。
        需要先有至少一个可用的 tab 已打开（作为 browser WS 的入口）。
        """
        import requests

        try:
            # 获取 browser-level WebSocket URL
            version = await asyncio.to_thread(
                lambda: requests.get(
                    f"http://{host}:{port}/json/version", timeout=10
                ).json()
            )
            browser_ws_url = version.get("webSocketDebuggerUrl")
            if not browser_ws_url:
                logger.error("无法获取 browser WebSocket URL")
                return None

            # 连接 browser WS 来创建新 tab
            async with websockets.connect(
                browser_ws_url, open_timeout=10, close_timeout=10
            ) as browser_ws:
                # 发送 Target.createTarget
                msg = json.dumps({
                    "id": 1,
                    "method": "Target.createTarget",
                    "params": {"url": "about:blank"}
                })
                await browser_ws.send(msg)
                # drain responses
                while True:
                    raw = await asyncio.wait_for(browser_ws.recv(), timeout=10)
                    resp = json.loads(raw)
                    if resp.get("id") == 1:
                        target_id = resp.get("result", {}).get("targetId")
                        if not target_id:
                            logger.error("Target.createTarget 未返回 targetId")
                            return None
                        break

            # 等 chrome 注册新 tab
            await asyncio.sleep(0.5)

            # 获取新 tab 的 page-level WS URL
            targets = await asyncio.to_thread(
                lambda: requests.get(
                    f"http://{host}:{port}/json/list", timeout=10
                ).json()
            )
            for t in targets:
                if t.get("id") == target_id:
                    ws_url = t.get("webSocketDebuggerUrl")
                    if ws_url:
                        return ws_url

            logger.error(f"无法找到新 tab 的 WS URL: targetId={target_id}")
            return None

        except Exception as e:
            logger.error(f"创建 Chrome Tab 失败: {e}")
            return None

    async def navigate(self, url: str) -> bool:
        """通过 CDP 导航到指定页面"""
        try:
            result = await self._send("Page.navigate", {"url": url})
            if "error" in result:
                logger.error(f"导航失败: {result['error']}")
                return False
            # 等待页面加载完成
            await asyncio.sleep(5)
            return True
        except Exception as e:
            logger.error(f"导航异常: {e}")
            return False

    async def fetch_comments(
        self,
        article_id: str,
        max_count: int = COMMENT_BATCH_SIZE,
        offset: int = 0,
    ) -> dict | None:
        """通过浏览器 XMLHttpRequest 获取评论 API 数据"""
        url = (
            f"https://www.dailymail.com/reader-comments/p/asset/readcomments/"
            f"{article_id}?max={max_count}&offset={offset}&order=desc"
        )

        # 使用 XMLHttpRequest 代替 fetch，Daily Mail 的 Bot Manager 会拦截 fetch
        script = f"""
        (() => {{
            return new Promise((resolve) => {{
                const xhr = new XMLHttpRequest();
                xhr.open("GET", "{url}", true);
                xhr.setRequestHeader("Accept", "application/json, text/plain, */*");
                xhr.onload = function() {{
                    resolve({{ status: xhr.status, body: xhr.responseText }});
                }};
                xhr.onerror = function() {{
                    resolve({{ error: "XHR error" }});
                }};
                xhr.ontimeout = function() {{
                    resolve({{ error: "XHR timeout" }});
                }};
                xhr.send();
            }});
        }})()
        """

        for attempt in range(COMMENT_MAX_RETRY):
            try:
                result = await self._send(
                    "Runtime.evaluate",
                    {
                        "expression": script,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )

                if "error" in result:
                    logger.warning(f"CDP 错误 (attempt {attempt + 1}): {result['error']}")
                    await asyncio.sleep(2 ** attempt)
                    continue

                eval_result = result.get("result", {}).get("result", {})
                if eval_result.get("type") == "object" and "value" in eval_result:
                    value = eval_result["value"]
                    if isinstance(value, dict):
                        if "error" in value:
                            logger.warning(f"浏览器 XHR 错误: {value['error']}")
                            await asyncio.sleep(2 ** attempt)
                            continue
                        if value.get("status") == 200:
                            try:
                                return json.loads(value["body"])
                            except json.JSONDecodeError as e:
                                logger.error(f"JSON 解析错误: {e}")
                                return None
                        else:
                            logger.warning(
                                f"评论 API 返回状态 {value.get('status')}: {value.get('body', '')[:100]}"
                            )
                            await asyncio.sleep(2 ** attempt)
                            continue

            except Exception as e:
                logger.warning(f"获取评论异常 (attempt {attempt + 1}): {e}")
                await asyncio.sleep(2 ** attempt)

        logger.error(f"获取评论最终失败: article_id={article_id}")
        return None

    async def reconnect(self) -> bool:
        """重新连接到 Chrome CDP（断线重连）"""
        try:
            if self.ws:
                try:
                    await self.ws.close()
                except Exception:
                    pass
            self.ws = None
            self.msg_id = 0
        except Exception:
            pass

        logger.info("尝试重连 Chrome CDP...")
        return await self.connect()

    async def ensure_connected(self) -> bool:
        """确保 CDP 连接有效，断开则自动重连"""
        if self.ws is None:
            return await self.reconnect()
        try:
            # 通过发送轻量 JS 探测连接
            self.msg_id += 1
            msg = {"id": self.msg_id, "method": "Runtime.evaluate",
                   "params": {"expression": "1", "returnByValue": True}}
            await asyncio.wait_for(self.ws.send(json.dumps(msg)), timeout=5)
            raw = await asyncio.wait_for(self.ws.recv(), timeout=5)
            return True
        except Exception:
            logger.warning("CDP 连接已断，自动重连...")
            return await self.reconnect()

    async def close(self):
        """关闭 CDP 连接"""
        if self.ws:
            await self.ws.close()
            logger.info("CDP 连接已关闭")


def parse_location(location_str: str) -> tuple[str, str]:
    """解析用户位置字符串为 (城市, 国家)

    格式通常: "City, Country" 或 "City, State, Country"
    """
    if not location_str:
        return "", ""

    parts = [p.strip() for p in location_str.split(",")]
    if len(parts) >= 2:
        # 最后一部分是国家，前面是城市/州
        country = parts[-1]
        city = ", ".join(parts[:-1])
        return city, country
    elif len(parts) == 1:
        return parts[0], ""
    return "", ""


def build_user_url(user_id: str, user_alias: str) -> str:
    """构建用户 profile URL"""
    # URL-friendly alias
    safe_alias = re.sub(r"[^\w\-]", "-", user_alias).strip("-")
    return f"{BASE_URL}/registration/{user_id}/{safe_alias}/profile.html"


def build_comment_url(article_url: str, comment_id: str) -> str:
    """构建评论链接"""
    return f"{article_url}#comment-{comment_id}"


async def process_comment_tree(
    db: asyncpg.Connection,
    article_id: str,
    article_url: str,
    comments: list,
    parent_id: str | None = None,
) -> int:
    """递归处理评论树，返回处理的评论数量"""
    count = 0
    for cmt in comments:
        comment_id = str(cmt.get("id", ""))
        if not comment_id:
            continue

        user_alias = cmt.get("userAlias", "")
        user_id = str(cmt.get("userIdentifier", ""))
        location_str = cmt.get("userLocation", "")
        city, country = parse_location(location_str)
        comment_text = cmt.get("message", "")
        comment_time = cmt.get("dateCreated", "")
        vote_count = cmt.get("voteCount", 0)
        vote_rating = cmt.get("voteRating", 0)
        # voteCount = 总投票数 (upvote + downvote)
        # voteRating = upvote - downvote (净评分)
        # 因此: upvote = (voteCount + voteRating) / 2
        #       downvote = (voteCount - voteRating) / 2
        upvote = (vote_count + vote_rating) // 2
        down_vote = (vote_count - vote_rating) // 2
        replies_count = cmt.get("replies", {}).get("totalCount", 0)

        user_url = build_user_url(user_id, user_alias) if user_id else ""
        comment_url = build_comment_url(article_url, comment_id)

        # 写入评论
        await insert_comment(
            db,
            article_id=article_id,
            comment_id=comment_id,
            reply_to=parent_id,
            replies=replies_count,
            comment=comment_text,
            comment_time=comment_time,
            city=city,
            country=country,
            upvote=upvote,
            down_vote=down_vote,
            vote_rating=vote_rating,
            user_alias=user_alias,
            user_id=user_id,
            user_url=user_url,
            comment_url=comment_url,
        )

        # 写入/更新用户
        if user_id:
            await upsert_user(db, user_alias, user_id, user_url, city, country)

        count += 1

        # 递归处理回复
        replies = cmt.get("replies", {}).get("comments", [])
        if replies:
            count += await process_comment_tree(db, article_id, article_url, replies, comment_id)

    return count


async def scrape_comments_for_article(
    fetcher: CDPCommentFetcher,
    db: asyncpg.Connection,
    article: dict,
) -> int:
    """采集单篇文章的所有评论

    Returns:
        采集到的评论总数
    """
    article_id = article["art_id"]
    article_url = article["url"]
    expected_count = article["comments_count"]

    logger.info(f"开始采集评论: {article_id} (预期 {expected_count} 条)")

    # 先导航到文章页面，建立 cookie/session 上下文
    if not await fetcher.navigate(article_url):
        logger.error(f"导航到文章失败: {article_id}")
        return 0

    total_collected = 0
    offset = 0

    while True:
        data = await fetcher.fetch_comments(article_id, COMMENT_BATCH_SIZE, offset)
        if not data:
            logger.error(f"无法获取评论数据: {article_id}")
            break

        payload = data.get("payload", {})
        comments = payload.get("page", [])
        parent_count = payload.get("parentCommentsCount", 0)

        if not comments:
            break

        count = await process_comment_tree(db, article_id, article_url, comments)
        total_collected += count

        logger.info(
            f"  {article_id}: offset={offset}, 采集 {count} 条, "
            f"累计 {total_collected} 条"
        )

        offset += len(comments)
        if offset >= parent_count:
            break

        await asyncio.sleep(COMMENT_DELAY)

    # 校验（允许少量差异，因为页面上显示的可能是包含回复的总数）
    if expected_count > 0 and total_collected < expected_count * 0.8:
        logger.warning(
            f"评论数量不匹配: {article_id} 预期 {expected_count}, 实际 {total_collected}"
        )
    else:
        logger.info(f"评论采集完成: {article_id} 共 {total_collected} 条")

    return total_collected


async def scrape_comments(limit: int | None = None) -> dict:
    """采集所有文章的评论

    Args:
        limit: 限制处理的文章数量（用于测试）

    Returns:
        统计信息
    """
    stats = {
        "articles_processed": 0,
        "articles_skipped": 0,
        "comments_collected": 0,
        "errors": 0,
    }

    # 连接 CDP
    fetcher = CDPCommentFetcher()
    if not await fetcher.connect():
        logger.error("无法连接到 Chrome CDP，评论采集终止")
        return stats

    try:
        async with get_db() as db:
            articles = await get_articles_with_comments(db, limit)
            logger.info(f"共有 {len(articles)} 篇文章需要采集评论")

            for article in articles:
                article_id = article["art_id"]

                # 检查进度
                progress_key = f"comments_{article_id}"
                if await get_progress(db, progress_key) == "done":
                    logger.debug(f"跳过已完成的评论: {article_id}")
                    stats["articles_skipped"] += 1
                    continue

                # 检查是否已采集足够
                existing = await get_comment_count_by_article(db, article_id)
                if existing >= article["comments_count"]:
                    logger.debug(f"评论已足够: {article_id} ({existing}/{article['comments_count']})")
                    stats["articles_skipped"] += 1
                    await set_progress(db, progress_key, "done")
                    continue

                # 短线重连检查
                if not await fetcher.ensure_connected():
                    logger.error(f"CDP 重连失败，跳过: {article_id}")
                    stats["errors"] += 1
                    continue

                try:
                    count = await scrape_comments_for_article(fetcher, db, article)
                    stats["comments_collected"] += count
                    stats["articles_processed"] += 1

                    await set_progress(db, progress_key, "done")

                except Exception as e:
                    logger.error(f"采集评论异常 {article_id}: {e}")
                    stats["errors"] += 1

                # 每 10 篇文章记录一次进度
                if stats["articles_processed"] % 10 == 0:
                    logger.info(
                        f"进度: {stats['articles_processed']} 篇文章, "
                        f"{stats['comments_collected']} 条评论"
                    )

    finally:
        await fetcher.close()

    logger.info(
        f"评论采集完成: {stats['articles_processed']} 篇文章, "
        f"{stats['comments_collected']} 条评论, {stats['errors']} 个错误"
    )
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(scrape_comments(limit=5))
    print(result)

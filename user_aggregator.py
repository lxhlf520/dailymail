"""Daily Mail 用户数据汇总模块

从 Daily Mail 用户 Profile API 获取用户统计数据和全部评论：
- arrowfactor API: 获取 VoteUp_All, VoteDown_All, Comments_Total
- readcomments API: 获取用户所有评论 (period=archive)

通过 Chrome CDP + XHR 方式请求，避免被 Akamai Bot Manager 拦截
"""

import asyncio
import json
import logging
import re

import asyncpg
import websockets

from config import (
    BASE_URL,
    CDP_HOST,
    CDP_PORT,
    COMMENT_DELAY,
    COMMENT_MAX_RETRY,
    COMMENT_RATELIMIT_BACKOFF,
    COLLECT_PROFILE_FIELDS,
)
from database import (
    get_db,
    update_user_stats,
    update_user_profile,
    upsert_user,
    insert_user_comment,
    set_progress,
    get_progress,
)
from comment_scraper import CDPCommentFetcher, build_user_url, parse_location

logger = logging.getLogger(__name__)

# 用户评论 API 每页数量
USER_COMMENT_BATCH_SIZE = 100


async def fetch_user_arrowfactor(fetcher: CDPCommentFetcher, user_id: str, user_url: str = "") -> dict | None:
    """获取用户的 Arrow Factor 统计数据 (总点赞/点踩/评论数)

    API: /reader-comments/p/user/arrowfactor/{user_id}?period=archive
    返回: {commentCount, votesUp, votesDown, voteRating}

    Args:
        user_url: 用户 profile URL，用于 429 限流时重新导航刷新 cookie
    """
    url = f"https://www.dailymail.com/reader-comments/p/user/arrowfactor/{user_id}?period=archive"

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
            xhr.send();
        }});
    }})()
    """

    for attempt in range(COMMENT_MAX_RETRY):
        try:
            result = await fetcher._send(
                "Runtime.evaluate",
                {"expression": script, "awaitPromise": True, "returnByValue": True},
            )

            eval_result = result.get("result", {}).get("result", {})
            if eval_result.get("type") == "object" and "value" in eval_result:
                value = eval_result["value"]
                if isinstance(value, dict):
                    if "error" in value:
                        logger.warning(f"Arrow Factor XHR 错误: {value['error']}")
                        await asyncio.sleep(2 ** attempt)
                        continue

                    status = value.get("status", 0)
                    body = value.get("body", "")

                    if status == 200:
                        try:
                            data = json.loads(body)
                            payload = data.get("payload", {})
                            return {
                                "comment_count": payload.get("commentCount", 0),
                                "votes_up": payload.get("votesUp", 0),
                                "votes_down": payload.get("votesDown", 0),
                                "vote_rating": payload.get("voteRating", 0),
                            }
                        except json.JSONDecodeError as e:
                            logger.error(f"Arrow Factor JSON 解析错误: {e}")
                            return None

                    if status == 429:
                        is_challenge = "cpr_chlge" in body
                        backoff = COMMENT_RATELIMIT_BACKOFF[min(attempt, len(COMMENT_RATELIMIT_BACKOFF) - 1)]
                        logger.warning(
                            f"Arrow Factor API 429 限流 (attempt {attempt + 1}/{COMMENT_MAX_RETRY}): "
                            f"{'challenge' if is_challenge else 'rate limit'}, "
                            f"等待 {backoff}s 后重试..."
                        )
                        if is_challenge and user_url:
                            logger.info(f"  re-navigate 刷新 session: {user_url[:80]}...")
                            await fetcher.navigate(user_url)
                        await asyncio.sleep(backoff)
                        continue

                    logger.warning(f"Arrow Factor API 状态 {status}: {body[:100]}")
                    await asyncio.sleep(2 ** attempt)
                    continue
        except Exception as e:
            logger.warning(f"Arrow Factor 请求异常 (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2 ** attempt)

    logger.error(f"Arrow Factor 获取失败: user_id={user_id}")
    return None


async def fetch_user_profile_fields(
    fetcher: CDPCommentFetcher,
    user_id: str,
    user_url: str,
) -> dict[str, str | None]:
    """从用户 Profile 页面一次性提取所有个人信息字段

    通过 CDP 导航到用户 profile 页面，解析 HTML 提取：
    - Country:      <p class="f-12"><span class="f-b">Country:</span> US</p>
    - Member Since: <p class="f-12"><span class="f-b">Member Since:</span> 01/6/2021</p>
    - Profile Photo: <img src="..."/> (跳过 dummy_91x91 占位图)
    - Facebook URL: <li class="facebook"><a href="...">

    返回 dict，字段不存在时为 None。
    """
    script = """
    (() => {
        const result = {};

        // Country 和 Member Since (都在 .f-12 段落中)
        const paragraphs = document.querySelectorAll('.f-12');
        for (const p of paragraphs) {
            const bold = p.querySelector('.f-b');
            if (!bold) continue;
            const label = bold.textContent.trim();
            if (label === 'Country:') {
                result.country = p.textContent.replace(bold.textContent, '').trim();
            } else if (label === 'Member Since:') {
                result.member_since = p.textContent.replace(bold.textContent, '').trim();
            }
        }

        // Profile Photo (用户头像，跳过默认占位图)
        const img = document.querySelector('.usr-masthead img');
        if (img && img.src && !img.src.includes('dummy_91x91')) {
            result.profile_photo = img.src;
        }

        // Facebook URL (仅在有链接时)
        const fb = document.querySelector('.dms-profile .facebook a');
        if (fb && fb.href) {
            result.facebook_url = fb.href;
        }

        return result;
    })()
    """

    for attempt in range(COMMENT_MAX_RETRY):
        try:
            # 导航到用户 profile 页
            if not await fetcher.navigate(user_url):
                logger.warning(f"导航到用户页失败: {user_url}")
                return {}

            await asyncio.sleep(3)

            result = await fetcher._send(
                "Runtime.evaluate",
                {"expression": script, "returnByValue": True},
            )

            eval_result = result.get("result", {}).get("result", {})
            if eval_result.get("type") == "object" and "value" in eval_result:
                value = eval_result["value"]
                if isinstance(value, dict):
                    if value:
                        logger.debug(f"Profile 字段: {value}")
                    return value
                else:
                    logger.warning(f"Profile 字段提取返回非预期类型: {type(value)}")
                    return {}
            else:
                logger.warning(f"Profile 字段提取失败: user_id={user_id}")
                return {}

        except Exception as e:
            logger.warning(f"Profile 字段请求异常 (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2 ** attempt)

    logger.error(f"Profile 字段获取失败: user_id={user_id}")
    return {}


async def fetch_user_comments_page(
    fetcher: CDPCommentFetcher,
    user_id: str,
    offset: int = 0,
    max_count: int = USER_COMMENT_BATCH_SIZE,
    user_url: str = "",
) -> tuple[list[dict], int] | None:
    """获取用户评论的一页数据

    API: /reader-comments/p/user/readcomments/{user_id}?max=N&offset=O&period=archive&order=desc
    返回: (评论列表, 总父评论数) 或 None

    Args:
        user_url: 用户 profile URL，用于 429 限流时重新导航刷新 cookie
    """
    url = (
        f"https://www.dailymail.com/reader-comments/p/user/readcomments/"
        f"{user_id}?max={max_count}&offset={offset}&period=archive&order=desc"
    )

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
            xhr.send();
        }});
    }})()
    """

    for attempt in range(COMMENT_MAX_RETRY):
        try:
            result = await fetcher._send(
                "Runtime.evaluate",
                {"expression": script, "awaitPromise": True, "returnByValue": True},
            )

            eval_result = result.get("result", {}).get("result", {})
            if eval_result.get("type") == "object" and "value" in eval_result:
                value = eval_result["value"]
                if isinstance(value, dict):
                    if "error" in value:
                        logger.warning(f"用户评论 XHR 错误: {value['error']}")
                        await asyncio.sleep(2 ** attempt)
                        continue

                    status = value.get("status", 0)
                    body = value.get("body", "")

                    if status == 200:
                        try:
                            data = json.loads(body)
                            payload = data.get("payload", {})
                            comments = payload.get("page", [])
                            parent_count = payload.get("parentCommentsCount", 0)
                            return comments, parent_count
                        except json.JSONDecodeError as e:
                            logger.error(f"用户评论 JSON 解析错误: {e}")
                            return None

                    if status == 429:
                        is_challenge = "cpr_chlge" in body
                        backoff = COMMENT_RATELIMIT_BACKOFF[min(attempt, len(COMMENT_RATELIMIT_BACKOFF) - 1)]
                        logger.warning(
                            f"用户评论 API 429 限流 (attempt {attempt + 1}/{COMMENT_MAX_RETRY}): "
                            f"{'challenge' if is_challenge else 'rate limit'}, "
                            f"等待 {backoff}s 后重试..."
                        )
                        if is_challenge and user_url:
                            logger.info(f"  re-navigate 刷新 session: {user_url[:80]}...")
                            await fetcher.navigate(user_url)
                        await asyncio.sleep(backoff)
                        continue

                    logger.warning(f"用户评论 API 状态 {status}: {body[:100]}")
                    await asyncio.sleep(2 ** attempt)
                    continue
        except Exception as e:
            logger.warning(f"用户评论请求异常 (attempt {attempt + 1}): {e}")
            await asyncio.sleep(2 ** attempt)

    logger.error(f"用户评论获取失败: user_id={user_id}")
    return None


async def process_user_comments(
    db: asyncpg.Connection,
    fetcher: CDPCommentFetcher,
    user_id: str,
    user_alias: str,
    user_url: str,
    max_comments: int | None = None,
) -> int:
    """采集单个用户的所有评论并写入 User_Comment_Info

    Args:
        max_comments: 限制每用户采集的评论数量，None 表示不限

    Returns:
        插入的评论数量
    """
    total_inserted = 0
    offset = 0

    while True:
        result = await fetch_user_comments_page(fetcher, user_id, offset, USER_COMMENT_BATCH_SIZE, user_url)
        if result is None:
            logger.error(f"获取用户评论失败: {user_alias} ({user_id})")
            break

        comments, parent_count = result
        if not comments:
            break

        for cmt in comments:
            comment_id = str(cmt.get("id", ""))
            if not comment_id:
                continue

            # 从用户评论API获取的数据结构
            asset_id = str(cmt.get("assetId", ""))
            vote_count = cmt.get("voteCount", 0)
            vote_rating = cmt.get("voteRating", 0)
            upvote = (vote_count + vote_rating) // 2
            down_vote = (vote_count - vote_rating) // 2

            comment_text = cmt.get("message") or ""
            comment_time = cmt.get("dateCreated") or ""
            location_str = cmt.get("userLocation") or ""
            city, country = parse_location(location_str)

            # 构建评论URL
            asset_url = cmt.get("assetUrl") or ""
            if asset_url:
                comment_url = f"https://www.dailymail.com{asset_url}#comment-{comment_id}"
            else:
                comment_url = ""

            # 获取回复信息（用户评论API也可能返回replies）
            reply_to = None  # 用户评论API不返回reply_to信息
            replies_data = cmt.get("replies", {})
            if isinstance(replies_data, dict):
                sub_comments = replies_data.get("comments", [])
            else:
                sub_comments = []

            try:
                await insert_user_comment(
                    db,
                    article_id=asset_id,
                    comment_id=comment_id,
                    reply_to=reply_to,
                    comment=comment_text,
                    comment_time=comment_time,
                    city=city,
                    country=country,
                    upvote=upvote,
                    down_vote=down_vote,
                    vote_rating=vote_rating,
                    user_alias=user_alias,
                    user_id=user_id,
                    comment_url=comment_url,
                )
                total_inserted += 1
            except Exception as e:
                logger.error(f"插入用户评论失败 {comment_id}: {e}")

            # 处理子回复
            if sub_comments:
                for sub in sub_comments:
                    sub_id = str(sub.get("id", ""))
                    if not sub_id:
                        continue
                    sub_vc = sub.get("voteCount", 0)
                    sub_vr = sub.get("voteRating", 0)
                    sub_up = (sub_vc + sub_vr) // 2
                    sub_down = (sub_vc - sub_vr) // 2
                    sub_text = sub.get("message") or ""
                    sub_time = sub.get("dateCreated") or ""
                    sub_loc = sub.get("userLocation") or ""
                    sub_city, sub_country = parse_location(sub_loc)
                    sub_asset_url = sub.get("assetUrl") or asset_url
                    sub_comment_url = f"https://www.dailymail.com{sub_asset_url}#comment-{sub_id}" if sub_asset_url else ""

                    try:
                        await insert_user_comment(
                            db,
                            article_id=asset_id,
                            comment_id=sub_id,
                            reply_to=comment_id,
                            comment=sub_text,
                            comment_time=sub_time,
                            city=sub_city,
                            country=sub_country,
                            upvote=sub_up,
                            down_vote=sub_down,
                            vote_rating=sub_vr,
                            user_alias=user_alias,
                            user_id=user_id,
                            comment_url=sub_comment_url,
                        )
                        total_inserted += 1
                    except Exception as e:
                        logger.error(f"插入用户子评论失败 {sub_id}: {e}")

        offset += len(comments)
        if offset >= parent_count:
            break

        # 检查评论数量限制
        if max_comments and total_inserted >= max_comments:
            logger.debug(f"用户 {user_alias} 已达到评论数量限制 {max_comments}")
            break

        await asyncio.sleep(COMMENT_DELAY)

    return total_inserted


async def aggregate_users(limit: int | None = None, max_comments_per_user: int | None = None, force: bool = False) -> dict:
    """汇总所有用户数据

    从 User_Info 表获取已采集的用户列表，
    通过 CDP + XHR 调用用户 Profile API 获取：
    1. Arrow Factor (VoteUp_All, VoteDown_All, Comments_Total)
    2. 用户所有评论 (User_Comment_Info)

    Args:
        limit: 限制处理的用户数量（用于测试）
        max_comments_per_user: 限制每用户采集的评论数量，None 表示不限
        force: 是否强制重新处理所有用户（包括已有统计数据的）

    需要 Chrome 以调试模式运行: chrome --remote-debugging-port=9222
    """
    stats = {
        "users_updated": 0,
        "user_comments_inserted": 0,
        "arrow_factor_ok": 0,
        "arrow_factor_fail": 0,
        "errors": 0,
    }

    # 连接 CDP
    fetcher = CDPCommentFetcher()
    if not await fetcher.connect():
        logger.error("无法连接到 Chrome CDP，用户汇总终止")
        return stats

    try:
        async with get_db() as db:
            logger.info("开始用户数据汇总...")

            # 先导航到 dailymail 页面以建立 cookie/session
            await fetcher.navigate("https://www.dailymail.com")
            await asyncio.sleep(3)

            # 获取需要处理的用户
            # - force=True: 处理所有用户（全量重跑）
            # - force=False: 跳过已完成的用户（scrape_progress 记录）
            if force:
                sql = """
                    SELECT ui.User_ID, ui.User_Alias, ui.User_URL, ui.City, ui.Country
                    FROM User_Info ui
                    ORDER BY ui.User_ID
                """
                params = []
            else:
                sql = """
                    SELECT ui.User_ID, ui.User_Alias, ui.User_URL, ui.City, ui.Country
                    FROM User_Info ui
                    LEFT JOIN scrape_progress sp ON sp.key = 'user_' || ui.User_ID
                    WHERE sp.key IS NULL
                    ORDER BY ui.User_ID
                """
                params = []

            if limit:
                sql += " LIMIT $1"
                params.append(limit)
            users = await db.fetch(sql, *params)
        logger.info(f"发现 {len(users)} 个用户需要汇总 (force={force})")

        for user in users:
            user_id = user["user_id"]
            user_alias = user["user_alias"]
            user_url = user["user_url"]

            if not user_id:
                continue

            # 短线重连检查（在 DB 操作前，不持有连接）
            if not await fetcher.ensure_connected():
                logger.error(f"CDP 重连失败，跳过用户: {user_alias} ({user_id})")
                stats["errors"] += 1
                continue

            try:
                # 1. CDP: 获取 Arrow Factor 统计（不持有数据库连接）
                arrow_data = await fetch_user_arrowfactor(fetcher, user_id, user_url)

                # 2. CDP: 提取 Profile 页面字段（不持有数据库连接）
                profile_fields = {}
                if COLLECT_PROFILE_FIELDS and user_url:
                    profile_fields = await fetch_user_profile_fields(
                        fetcher, user_id, user_url
                    )
                    if profile_fields:
                        logger.info(
                            f"  {user_alias}: "
                            + ", ".join(f"{k}={v}" for k, v in profile_fields.items() if v)
                        )

                # 3. DB: 获取评论 + 写入数据库（短连接，一次完成）
                async with get_db() as db:
                    comment_count = await process_user_comments(
                        db, fetcher, user_id, user_alias, user_url,
                        max_comments=max_comments_per_user,
                    )
                    stats["user_comments_inserted"] += comment_count

                    if arrow_data:
                        await update_user_stats(
                            db,
                            user_id=user_id,
                            vote_up_all=arrow_data["votes_up"],
                            vote_down_all=arrow_data["votes_down"],
                            comments_total=arrow_data["comment_count"],
                        )
                        stats["arrow_factor_ok"] += 1
                        logger.info(
                            f"  {user_alias}: up={arrow_data['votes_up']}, "
                            f"down={arrow_data['votes_down']}, "
                            f"comments={arrow_data['comment_count']}"
                        )
                    else:
                        stats["arrow_factor_fail"] += 1
                        logger.warning(f"  {user_alias}: Arrow Factor 获取失败")

                    if profile_fields:
                        await update_user_profile(
                            db,
                            user_id=user_id,
                            country=profile_fields.get("country"),
                            profile_photo=profile_fields.get("profile_photo"),
                            facebook_url=profile_fields.get("facebook_url"),
                            member_since=profile_fields.get("member_since"),
                        )

                    await set_progress(db, f"user_{user_id}", "done")

                stats["users_updated"] += 1

                if stats["users_updated"] % 100 == 0:
                    logger.info(f"进度: {stats['users_updated']} 个用户已处理")

                await asyncio.sleep(COMMENT_DELAY)

            except Exception as e:
                logger.error(f"处理用户 {user_alias} ({user_id}) 异常: {e}")
                stats["errors"] += 1

    finally:
        await fetcher.close()

    logger.info(
        f"用户汇总完成: {stats['users_updated']} 个用户, "
        f"Arrow Factor 成功 {stats['arrow_factor_ok']} 失败 {stats['arrow_factor_fail']}, "
        f"用户评论 {stats['user_comments_inserted']} 条"
    )
    return stats


async def _process_user_worker(
    worker_id: int,
    fetcher: CDPCommentFetcher,
    queue: asyncio.Queue,
    max_comments_per_user: int | None,
    stats: dict,
    lock: asyncio.Lock,
) -> None:
    """Worker: 从队列消费用户，逐人处理

    Args:
        worker_id: worker 编号（用于日志）
        fetcher: 已连接 dailymail.com 的 CDP fetcher
        queue: 用户队列
        max_comments_per_user: 每用户评论上限
        stats: 共享统计 dict
        lock: 异步锁
    """
    while True:
        try:
            # 非阻塞取任务，队列空则 worker 退出
            user = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        user_id = user["user_id"]
        user_alias = user["user_alias"]
        user_url = user["user_url"]

        if not user_id:
            queue.task_done()
            continue

        # 短线重连检查
        if not await fetcher.ensure_connected():
            logger.error(f"[W{worker_id}] CDP 重连失败，跳过: {user_alias} ({user_id})")
            async with lock:
                stats["errors"] += 1
            queue.task_done()
            continue

        try:
            async with get_db() as db:
                # 1. Arrow Factor
                arrow_data = await fetch_user_arrowfactor(fetcher, user_id, user_url)

                # 2. 用户评论
                comment_count = await process_user_comments(
                    db, fetcher, user_id, user_alias, user_url,
                    max_comments=max_comments_per_user,
                )

                # 3. Profile 字段（仅在开关启用时采集）
                profile_fields = {}
                if COLLECT_PROFILE_FIELDS and user_url:
                    profile_fields = await fetch_user_profile_fields(
                        fetcher, user_id, user_url
                    )

                # 4. 更新 Arrow Factor
                if arrow_data:
                    await update_user_stats(
                        db,
                        user_id=user_id,
                        vote_up_all=arrow_data["votes_up"],
                        vote_down_all=arrow_data["votes_down"],
                        comments_total=arrow_data["comment_count"],
                    )

                # 5. 更新 Profile 字段
                if profile_fields:
                    await update_user_profile(
                        db,
                        user_id=user_id,
                        country=profile_fields.get("country"),
                        profile_photo=profile_fields.get("profile_photo"),
                        facebook_url=profile_fields.get("facebook_url"),
                        member_since=profile_fields.get("member_since"),
                    )

                # 标记用户完成（续传支持）
                await set_progress(db, f"user_{user_id}", "done")

            # 更新共享统计
            async with lock:
                stats["users_updated"] += 1
                stats["user_comments_inserted"] += comment_count
                if arrow_data:
                    stats["arrow_factor_ok"] += 1
                else:
                    stats["arrow_factor_fail"] += 1

                done = stats["users_updated"]
                total = stats["_total"]
                logger.info(
                    f"[W{worker_id}] {user_alias}: "
                    f"up={arrow_data['votes_up'] if arrow_data else '?'}, "
                    f"comments={arrow_data['comment_count'] if arrow_data else '?'}, "
                    f"({done}/{total})"
                )

            queue.task_done()

        except Exception as e:
            logger.error(f"[W{worker_id}] 处理用户 {user_alias} ({user_id}) 异常: {e}")
            async with lock:
                stats["errors"] += 1
            queue.task_done()


async def aggregate_users_parallel(
    parallel: int = 5,
    limit: int | None = None,
    max_comments_per_user: int | None = None,
    force: bool = False,
) -> dict:
    """并行汇总所有用户数据（多 Tab 模式）

    创建 parallel 个 Chrome Tab，每个 Tab 独立的 CDP WebSocket，
    通过 asyncio.Queue 分发用户到多个 worker 并行处理。

    Args:
        parallel: 并行 Tab 数量（默认 5）
        limit: 限制处理的用户数量
        max_comments_per_user: 限制每用户采集的评论数量
        force: 是否强制重新处理所有用户

    需要 Chrome 以调试模式运行: chrome --remote-debugging-port=9222
    """
    t_start = asyncio.get_event_loop().time()

    stats = {
        "users_updated": 0,
        "user_comments_inserted": 0,
        "arrow_factor_ok": 0,
        "arrow_factor_fail": 0,
        "errors": 0,
        "_total": 0,
    }

    # 1. 查询用户列表
    async with get_db() as db:
        if force:
            sql = """
                SELECT ui.User_ID, ui.User_Alias, ui.User_URL, ui.City, ui.Country
                FROM User_Info ui ORDER BY ui.User_ID
            """
            params = []
        else:
            sql = """
                SELECT ui.User_ID, ui.User_Alias, ui.User_URL, ui.City, ui.Country
                FROM User_Info ui
                LEFT JOIN scrape_progress sp ON sp.key = 'user_' || ui.User_ID
                WHERE sp.key IS NULL
                ORDER BY ui.User_ID
            """
            params = []
        if limit:
            sql += " LIMIT $1"
            params.append(limit)
        users = await db.fetch(sql, *params)

    total = len(users)
    stats["_total"] = total
    logger.info(f"发现 {total} 个用户需要汇总 (force={force}, parallel={parallel})")

    if total == 0:
        return stats

    # 2. 创建 parallel 个 Chrome Tab
    tab_ws_urls = []
    for i in range(parallel):
        ws_url = await CDPCommentFetcher.create_tab()
        if ws_url:
            tab_ws_urls.append(ws_url)
            logger.info(f"  Tab {i + 1}/{parallel} 已创建")
        else:
            logger.error(f"  Tab {i + 1}/{parallel} 创建失败")

    if not tab_ws_urls:
        logger.error("所有 Tab 创建失败")
        return stats

    logger.info(f"共创建 {len(tab_ws_urls)}/{parallel} 个 Tab")

    # 3. 为每个 Tab 创建 fetcher 并导航到 dailymail.com
    fetchers = []
    for idx, ws_url in enumerate(tab_ws_urls):
        f = CDPCommentFetcher()
        if not await f.connect_to_url(ws_url):
            logger.error(f"Tab {idx} 连接 WS 失败")
            continue
        # 导航到 home 页建立 session
        await f.navigate("https://www.dailymail.com")
        await asyncio.sleep(3)
        fetchers.append(f)
        logger.info(f"  Worker {idx + 1}: 已就绪")

    if not fetchers:
        logger.error("所有 fetcher 连接失败")
        return stats

    try:
        # 4. 填充队列
        queue = asyncio.Queue()
        for u in users:
            queue.put_nowait(dict(u))

        # 5. 启动并行 worker
        lock = asyncio.Lock()
        workers = [
            _process_user_worker(
                worker_id=idx + 1,
                fetcher=f,
                queue=queue,
                max_comments_per_user=max_comments_per_user,
                stats=stats,
                lock=lock,
            )
            for idx, f in enumerate(fetchers)
        ]

        await asyncio.gather(*workers)

    finally:
        for f in fetchers:
            await f.close()

    elapsed = asyncio.get_event_loop().time() - t_start
    logger.info(
        f"用户汇总完成: {stats['users_updated']} 个用户, "
        f"Arrow Factor 成功 {stats['arrow_factor_ok']} 失败 {stats['arrow_factor_fail']}, "
        f"用户评论 {stats['user_comments_inserted']} 条, "
        f"耗时 {elapsed:.0f}s ({elapsed/60:.1f}min), "
        f"平均 {elapsed/max(stats['users_updated'],1):.1f}s/人"
    )

    del stats["_total"]
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(aggregate_users())
    print(result)

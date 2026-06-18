"""Daily Mail 数据库操作模块 (PostgreSQL + asyncpg)"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import asyncpg

from config import PG_DSN


# ========================================================================
# 连接管理
# ========================================================================

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """获取或创建连接池（懒初始化，异常时自动重建）"""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            PG_DSN,
            min_size=2,
            max_size=10,
            command_timeout=60,
            max_inactive_connection_lifetime=300,
            ssl=False,
        )
    return _pool


@asynccontextmanager
async def get_db():
    """异步数据库连接上下文管理器

    使用 asyncpg 内置的 pool.acquire() 上下文管理器，
    确保连接生命周期由 PoolConnectionProxy 正确管理，
    避免 Python 3.13 下手动 acquire/release 导致的提前释放问题。
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            yield conn
    except asyncpg.exceptions.InterfaceError:
        # 连接池可能已损坏（如 PG 服务重启），重建池后重试一次
        global _pool
        try:
            await _pool.close()
        except Exception:
            pass
        _pool = None
        pool = await get_pool()
        async with pool.acquire() as conn:
            yield conn


async def close_pool():
    """关闭连接池"""
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            pass
        _pool = None


# ========================================================================
# 初始化
# ========================================================================

async def init_db():
    """初始化数据库（异步，首次创建表）"""
    conn = await asyncpg.connect(PG_DSN, ssl=False)
    try:
        with open("schema.sql", "r", encoding="utf-8") as f:
            await conn.execute(f.read())
    finally:
        await conn.close()


def sync_init_db():
    """同步初始化数据库（用于命令行首次运行）"""
    import psycopg2

    conn = psycopg2.connect(PG_DSN)
    try:
        with open("schema.sql", "r", encoding="utf-8") as f:
            conn.cursor().execute(f.read())
        conn.commit()
    finally:
        conn.close()


# ========================================================================
# Daily_Articles 操作
# ========================================================================

async def insert_daily_article(
    db: asyncpg.Connection,
    archive_date: str,
    article_id: str,
    title: str,
    url: str,
    category: str = None,
) -> None:
    """插入 Daily_Articles 记录"""
    await db.execute(
        """
        INSERT INTO Daily_Articles (archive_date, article_id, title, url, category, scrape_time)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (archive_date, article_id) DO NOTHING
        """,
        archive_date, article_id, title, url, category,
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    )


async def get_daily_article_count(db: asyncpg.Connection, archive_date: str) -> int:
    """获取某天的新闻数量"""
    return await db.fetchval(
        "SELECT COUNT(*) FROM Daily_Articles WHERE archive_date = $1",
        archive_date,
    ) or 0


async def get_all_article_ids(db: asyncpg.Connection) -> list[str]:
    """获取所有已采集的文章ID列表"""
    rows = await db.fetch("SELECT DISTINCT article_id FROM Daily_Articles")
    return [r["article_id"] for r in rows]


async def get_dates_summary(db: asyncpg.Connection) -> list[dict]:
    """获取按日期汇总的新闻数量"""
    rows = await db.fetch(
        "SELECT archive_date, COUNT(*) as cnt FROM Daily_Articles GROUP BY archive_date ORDER BY archive_date"
    )
    return [{"date": r["archive_date"], "count": r["cnt"]} for r in rows]


# ========================================================================
# Article_Info 操作
# ========================================================================

async def insert_article_info(
    db: asyncpg.Connection,
    art_id: str,
    title: str,
    author: str,
    published_at: str,
    updated_at: str,
    tag1: str,
    tag2: str,
    comments_count: int,
    share_count: int,
    url: str,
) -> None:
    """插入或更新 Article_Info 记录"""
    await db.execute(
        """
        INSERT INTO Article_Info
        (Art_ID, Art_Title, Art_Author, Published_At, Updated_At, Art_Tag_1, Art_Tag_2,
         Comments_Count, Share_Count, Art_URL, Scrape_Time)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (Art_ID) DO UPDATE SET
            Art_Title = EXCLUDED.Art_Title,
            Art_Author = EXCLUDED.Art_Author,
            Published_At = EXCLUDED.Published_At,
            Updated_At = EXCLUDED.Updated_At,
            Art_Tag_1 = EXCLUDED.Art_Tag_1,
            Art_Tag_2 = EXCLUDED.Art_Tag_2,
            Comments_Count = EXCLUDED.Comments_Count,
            Share_Count = EXCLUDED.Share_Count,
            Art_URL = EXCLUDED.Art_URL,
            Scrape_Time = EXCLUDED.Scrape_Time
        """,
        art_id, title, author, published_at, updated_at,
        tag1, tag2, comments_count, share_count, url,
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    )


async def get_articles_without_details(db: asyncpg.Connection, limit: int = 100) -> list[dict]:
    """获取尚未采集详情的文章"""
    rows = await db.fetch(
        """
        SELECT da.article_id, da.title, da.url
        FROM Daily_Articles da
        LEFT JOIN Article_Info ai ON da.article_id = ai.Art_ID
        WHERE ai.Art_ID IS NULL
        LIMIT $1
        """,
        limit,
    )
    return [{"article_id": r["article_id"], "title": r["title"], "url": r["url"]} for r in rows]


async def get_article_by_id(db: asyncpg.Connection, art_id: str) -> dict | None:
    """根据ID获取文章详情"""
    row = await db.fetchrow("SELECT * FROM Article_Info WHERE Art_ID = $1", art_id)
    return dict(row) if row else None


async def get_articles_with_comments(db: asyncpg.Connection, limit: int = None) -> list[dict]:
    """获取有评论的文章列表（用于评论采集）"""
    sql = """
        SELECT Art_ID, Art_Title, Art_URL, Comments_Count
        FROM Article_Info
        WHERE Comments_Count > 0
        ORDER BY Art_ID
    """
    params = []
    if limit:
        sql += " LIMIT $1"
        params.append(limit)
    rows = await db.fetch(sql, *params)
    return [
        {
            "art_id": r["art_id"],
            "title": r["art_title"],
            "url": r["art_url"],
            "comments_count": r["comments_count"],
        }
        for r in rows
    ]


# ========================================================================
# Comment_Info 操作
# ========================================================================

async def insert_comment(
    db: asyncpg.Connection,
    article_id: str,
    comment_id: str,
    reply_to: str | None,
    replies: int,
    comment: str,
    comment_time: str,
    city: str,
    country: str,
    upvote: int,
    down_vote: int,
    vote_rating: int,
    user_alias: str,
    user_id: str,
    user_url: str,
    comment_url: str,
) -> None:
    """插入评论记录"""
    await db.execute(
        """
        INSERT INTO Comment_Info
        (Article_ID, Comment_ID, Reply_2_Comment_ID, Replies, Comment, Comment_Time,
         Comment_From_City, Comment_From_Country, UpVote, Down_Vote, Vote_Rating,
         User_Alias, User_ID, User_URL, Comment_URL, scrape_time)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
        ON CONFLICT (Article_ID, Comment_ID) DO UPDATE SET
            Reply_2_Comment_ID = EXCLUDED.Reply_2_Comment_ID,
            Replies = EXCLUDED.Replies,
            Comment = EXCLUDED.Comment,
            Comment_Time = EXCLUDED.Comment_Time,
            Comment_From_City = EXCLUDED.Comment_From_City,
            Comment_From_Country = EXCLUDED.Comment_From_Country,
            UpVote = EXCLUDED.UpVote,
            Down_Vote = EXCLUDED.Down_Vote,
            Vote_Rating = EXCLUDED.Vote_Rating,
            User_Alias = EXCLUDED.User_Alias,
            User_ID = EXCLUDED.User_ID,
            User_URL = EXCLUDED.User_URL,
            Comment_URL = EXCLUDED.Comment_URL,
            scrape_time = EXCLUDED.scrape_time
        """,
        article_id, comment_id, reply_to, replies, comment, comment_time,
        city, country, upvote, down_vote, vote_rating,
        user_alias, user_id, user_url, comment_url,
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    )


async def get_comment_count_by_article(db: asyncpg.Connection, article_id: str) -> int:
    """获取某文章的评论总数"""
    return await db.fetchval(
        "SELECT COUNT(*) FROM Comment_Info WHERE Article_ID = $1",
        article_id,
    ) or 0


async def get_articles_without_comments(db: asyncpg.Connection, limit: int = 100) -> list[dict]:
    """获取有评论但尚未采集的文章"""
    rows = await db.fetch(
        """
        SELECT ai.Art_ID, ai.Art_Title, ai.Art_URL, ai.Comments_Count
        FROM Article_Info ai
        LEFT JOIN (
            SELECT Article_ID, COUNT(*) as cnt FROM Comment_Info GROUP BY Article_ID
        ) c ON ai.Art_ID = c.Article_ID
        WHERE ai.Comments_Count > 0
        AND (c.cnt IS NULL OR c.cnt < ai.Comments_Count)
        LIMIT $1
        """,
        limit,
    )
    return [
        {
            "art_id": r["art_id"],
            "title": r["art_title"],
            "url": r["art_url"],
            "comments_count": r["comments_count"],
        }
        for r in rows
    ]


# ========================================================================
# User_Info 操作
# ========================================================================

async def upsert_user(
    db: asyncpg.Connection,
    user_alias: str,
    user_id: str,
    user_url: str,
    city: str,
    country: str,
) -> None:
    """插入或更新用户基本信息（从评论中提取）"""
    await db.execute(
        """
        INSERT INTO User_Info (User_Alias, User_ID, User_URL, City, Country)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (User_ID) DO UPDATE SET
            User_Alias = EXCLUDED.User_Alias,
            City = EXCLUDED.City,
            Country = EXCLUDED.Country
        """,
        user_alias, user_id, user_url, city, country,
    )


async def update_user_stats(
    db: asyncpg.Connection,
    user_id: str,
    vote_up_all: int,
    vote_down_all: int,
    comments_total: int,
) -> None:
    """更新用户 Arrow Factor 统计数据"""
    await db.execute(
        """
        UPDATE User_Info
        SET VoteUp_All = $1, VoteDown_All = $2, Comments_Total = $3
        WHERE User_ID = $4
        """,
        vote_up_all, vote_down_all, comments_total, user_id,
    )


async def update_user_profile(
    db: asyncpg.Connection,
    user_id: str,
    country: str | None = None,
    profile_photo: str | None = None,
    facebook_url: str | None = None,
    member_since: str | None = None,
) -> None:
    """更新用户 Profile 页面字段（Country, Profile_Photo, Facebook_URL, Member_Since）

    这些字段来自用户 Profile 页面 HTML，通过 CDP DOM 解析提取。
    只有非 None 的字段才会被更新。
    """
    fields = []
    params = []
    idx = 1

    if country is not None:
        fields.append(f"Country = ${idx}")
        params.append(country)
        idx += 1
    if profile_photo is not None:
        fields.append(f"Profile_Photo = ${idx}")
        params.append(profile_photo)
        idx += 1
    if facebook_url is not None:
        fields.append(f"Facebook_URL = ${idx}")
        params.append(facebook_url)
        idx += 1
    if member_since is not None:
        fields.append(f"Member_Since = ${idx}")
        params.append(member_since)
        idx += 1

    if not fields:
        return

    params.append(user_id)
    sql = f"UPDATE User_Info SET {', '.join(fields)} WHERE User_ID = ${idx}"
    await db.execute(sql, *params)


# ========================================================================
# User_Comment_Info 操作
# ========================================================================

async def insert_user_comment(
    db: asyncpg.Connection,
    article_id: str,
    comment_id: str,
    reply_to: str | None,
    comment: str,
    comment_time: str,
    city: str,
    country: str,
    upvote: int,
    down_vote: int,
    vote_rating: int = 0,
    user_alias: str = "",
    user_id: str = "",
    comment_url: str = "",
) -> None:
    """插入用户评论记录"""
    await db.execute(
        """
        INSERT INTO User_Comment_Info
        (Article_ID, Comment_ID, Reply_2_Comment_ID, Comment, Comment_Time,
         Comment_From_City, Comment_From_Country, UpVote, Down_Vote, Vote_Rating,
         User_Alias, User_ID, Comment_URL, scrape_time)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        ON CONFLICT (User_ID, Comment_ID) DO UPDATE SET
            Article_ID = EXCLUDED.Article_ID,
            Reply_2_Comment_ID = EXCLUDED.Reply_2_Comment_ID,
            Comment = EXCLUDED.Comment,
            Comment_Time = EXCLUDED.Comment_Time,
            Comment_From_City = EXCLUDED.Comment_From_City,
            Comment_From_Country = EXCLUDED.Comment_From_Country,
            UpVote = EXCLUDED.UpVote,
            Down_Vote = EXCLUDED.Down_Vote,
            Vote_Rating = EXCLUDED.Vote_Rating,
            User_Alias = EXCLUDED.User_Alias,
            Comment_URL = EXCLUDED.Comment_URL,
            scrape_time = EXCLUDED.scrape_time
        """,
        article_id, comment_id, reply_to, comment, comment_time,
        city, country, upvote, down_vote, vote_rating,
        user_alias, user_id, comment_url,
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    )


# ========================================================================
# 进度追踪
# ========================================================================

async def set_progress(db: asyncpg.Connection, key: str, value: str) -> None:
    """设置进度"""
    await db.execute(
        "INSERT INTO scrape_progress (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key, value,
    )


async def get_progress(db: asyncpg.Connection, key: str, default: str = "") -> str:
    """获取进度"""
    row = await db.fetchrow(
        "SELECT value FROM scrape_progress WHERE key = $1",
        key,
    )
    return row["value"] if row else default


# ========================================================================
# 统计查询
# ========================================================================

async def get_stats(db: asyncpg.Connection) -> dict[str, Any]:
    """获取数据库统计信息"""
    stats = {}

    stats["daily_articles"] = await db.fetchval("SELECT COUNT(*) FROM Daily_Articles") or 0
    stats["article_info"] = await db.fetchval("SELECT COUNT(*) FROM Article_Info") or 0
    stats["comments"] = await db.fetchval("SELECT COUNT(*) FROM Comment_Info") or 0
    stats["users"] = await db.fetchval("SELECT COUNT(*) FROM User_Info") or 0
    stats["user_comments"] = await db.fetchval("SELECT COUNT(*) FROM User_Comment_Info") or 0
    stats["articles_with_comments"] = await db.fetchval(
        "SELECT COUNT(*) FROM Article_Info WHERE Comments_Count > 0"
    ) or 0

    return stats


# ========================================================================
# 进度统计查询
# ========================================================================

async def get_progress_stats(db: asyncpg.Connection) -> dict[str, Any]:
    """获取各采集阶段的进度统计"""
    p = {}

    # Sitemap 阶段: 天数进度
    p["sitemap_days"] = await db.fetchval("SELECT COUNT(*) FROM Daily_Articles") or 0
    sitemap_done = await db.fetchval(
        "SELECT COUNT(*) FROM scrape_progress WHERE key LIKE 'sitemap_month_%'"
    ) or 0
    p["sitemap_months_done"] = sitemap_done

    # Article 阶段
    p["articles_total"] = await db.fetchval("SELECT COUNT(DISTINCT article_id) FROM Daily_Articles") or 0
    p["articles_done"] = await db.fetchval("SELECT COUNT(*) FROM Article_Info") or 0
    p["articles_progress"] = await db.fetchval(
        "SELECT COUNT(*) FROM scrape_progress WHERE key LIKE 'article_%'"
    ) or 0

    # Comment 阶段
    p["articles_with_comments"] = await db.fetchval(
        "SELECT COUNT(*) FROM Article_Info WHERE Comments_Count > 0"
    ) or 0
    p["comments_done"] = await db.fetchval(
        "SELECT COUNT(*) FROM scrape_progress WHERE key LIKE 'comments_%'"
    ) or 0

    # User 阶段
    p["users_total"] = await db.fetchval("SELECT COUNT(*) FROM User_Info") or 0
    p["users_done"] = await db.fetchval(
        "SELECT COUNT(*) FROM scrape_progress WHERE key LIKE 'user_%'"
    ) or 0

    return p

"""Daily Mail 数据校验模块"""

import asyncio
import logging
from datetime import datetime, timezone

from database import get_db, get_stats

logger = logging.getLogger(__name__)


async def verify_comment_counts() -> list[dict]:
    """校验文章评论数量一致性

    检查 Article_Info.Comments_Count 是否等于实际采集的评论数
    """
    issues = []
    async with get_db() as db:
        rows = await db.fetch(
            """
            SELECT
                ai.Art_ID,
                ai.Art_Title,
                ai.Comments_Count,
                COUNT(ci.Comment_ID) as actual_count
            FROM Article_Info ai
            LEFT JOIN Comment_Info ci ON ai.Art_ID = ci.Article_ID
            WHERE ai.Comments_Count > 0
            GROUP BY ai.Art_ID
            HAVING COUNT(ci.Comment_ID) != ai.Comments_Count
            LIMIT 100
            """
        )
        for row in rows:
            issues.append({
                "art_id": row["art_id"],
                "title": row["art_title"][:60] if row["art_title"] else "",
                "expected": row["comments_count"],
                "actual": row["actual_count"],
                "diff": row["comments_count"] - row["actual_count"],
            })
    return issues


async def verify_daily_article_counts() -> list[dict]:
    """校验 Daily_Articles 与 Article_Info 的数量匹配

    检查 Daily_Articles 中每天的新闻条数是否与 Article_Info 中对应
    """
    issues = []
    async with get_db() as db:
        # 按日期对比两个表的数量
        rows = await db.fetch(
            """
            SELECT
                da.archive_date,
                COUNT(DISTINCT da.article_id) as daily_count,
                COUNT(DISTINCT ai.Art_ID) as article_count
            FROM Daily_Articles da
            LEFT JOIN Article_Info ai ON da.article_id = ai.Art_ID
            GROUP BY da.archive_date
            HAVING COUNT(DISTINCT da.article_id) != COUNT(DISTINCT ai.Art_ID)
            LIMIT 50
            """
        )
        for row in rows:
            issues.append({
                "date": row["archive_date"],
                "daily_articles": row["daily_count"],
                "article_info": row["article_count"],
                "diff": row["daily_count"] - row["article_count"],
            })
    return issues


async def verify_comment_hierarchy() -> dict:
    """校验评论层级完整性

    检查所有 Reply_2_Comment_ID 是否对应存在的 Comment_ID
    """
    async with get_db() as db:
        # 统计顶层评论和回复评论
        top_level = await db.fetchval(
            "SELECT COUNT(*) FROM Comment_Info WHERE Reply_2_Comment_ID IS NULL"
        ) or 0

        replies = await db.fetchval(
            "SELECT COUNT(*) FROM Comment_Info WHERE Reply_2_Comment_ID IS NOT NULL"
        ) or 0

        # 检查无效的父评论ID
        invalid_parents = await db.fetchval(
            """
            SELECT COUNT(*) FROM Comment_Info c1
            WHERE c1.Reply_2_Comment_ID IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM Comment_Info c2
                WHERE c2.Comment_ID = c1.Reply_2_Comment_ID
            )
            """
        ) or 0

        return {
            "top_level_comments": top_level,
            "reply_comments": replies,
            "invalid_parent_references": invalid_parents,
        }


async def verify_user_consistency() -> dict:
    """校验用户数据一致性"""
    async with get_db() as db:
        # User_Info 中但 Comment_Info 中没有评论的用户
        orphan_users = await db.fetchval(
            """
            SELECT COUNT(*) FROM User_Info u
            WHERE NOT EXISTS (
                SELECT 1 FROM Comment_Info c WHERE c.User_ID = u.User_ID
            )
            """
        ) or 0

        # Comment_Info 中有但 User_Info 中没有的用户
        missing_users = await db.fetchval(
            """
            SELECT COUNT(DISTINCT User_ID) FROM Comment_Info c
            WHERE c.User_ID IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM User_Info u WHERE u.User_ID = c.User_ID
            )
            """
        ) or 0

        # User_Comment_Info 数量是否与 Comment_Info 一致
        comment_with_user = await db.fetchval(
            "SELECT COUNT(*) FROM Comment_Info WHERE User_ID IS NOT NULL"
        ) or 0

        user_comment_count = await db.fetchval(
            "SELECT COUNT(*) FROM User_Comment_Info"
        ) or 0

        return {
            "orphan_users": orphan_users,
            "missing_users": missing_users,
            "comments_with_user": comment_with_user,
            "user_comment_records": user_comment_count,
            "user_comment_match": comment_with_user == user_comment_count,
        }


async def run_verification() -> dict:
    """执行全部校验并返回报告"""
    logger.info("=" * 60)
    logger.info("开始数据校验")
    logger.info("=" * 60)

    report = {
        "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "database_stats": {},
        "comment_count_issues": [],
        "daily_article_issues": [],
        "hierarchy_check": {},
        "user_consistency": {},
        "summary": {},
    }

    # 1. 数据库总体统计
    async with get_db() as db:
        report["database_stats"] = await get_stats(db)
    logger.info(f"数据库统计: {report['database_stats']}")

    # 2. 评论数量校验
    report["comment_count_issues"] = await verify_comment_counts()
    if report["comment_count_issues"]:
        logger.warning(f"发现 {len(report['comment_count_issues'])} 条评论数量不匹配")
        for issue in report["comment_count_issues"][:5]:
            logger.warning(
                f"  {issue['art_id']}: 预期 {issue['expected']}, 实际 {issue['actual']}"
            )
    else:
        logger.info("评论数量校验通过")

    # 3. Daily_Articles 数量校验
    report["daily_article_issues"] = await verify_daily_article_counts()
    if report["daily_article_issues"]:
        logger.warning(f"发现 {len(report['daily_article_issues'])} 天数量不匹配")
        for issue in report["daily_article_issues"][:5]:
            logger.warning(
                f"  {issue['date']}: Daily_Articles={issue['daily_articles']}, "
                f"Article_Info={issue['article_info']}"
            )
    else:
        logger.info("Daily_Articles 数量校验通过")

    # 4. 评论层级校验
    report["hierarchy_check"] = await verify_comment_hierarchy()
    logger.info(
        f"评论层级: 顶层 {report['hierarchy_check']['top_level_comments']}, "
        f"回复 {report['hierarchy_check']['reply_comments']}"
    )
    if report["hierarchy_check"]["invalid_parent_references"] > 0:
        logger.warning(
            f"发现 {report['hierarchy_check']['invalid_parent_references']} 条无效父评论引用"
        )
    else:
        logger.info("评论层级校验通过")

    # 5. 用户一致性校验
    report["user_consistency"] = await verify_user_consistency()
    logger.info(
        f"用户一致性: 孤立用户 {report['user_consistency']['orphan_users']}, "
        f"缺失用户 {report['user_consistency']['missing_users']}"
    )
    if report["user_consistency"]["user_comment_match"]:
        logger.info("User_Comment_Info 数量校验通过")
    else:
        logger.warning(
            f"User_Comment_Info 数量不匹配: "
            f"Comment_Info={report['user_consistency']['comments_with_user']}, "
            f"User_Comment_Info={report['user_consistency']['user_comment_records']}"
        )

    # 汇总
    total_issues = (
        len(report["comment_count_issues"])
        + len(report["daily_article_issues"])
        + report["hierarchy_check"].get("invalid_parent_references", 0)
        + report["user_consistency"].get("missing_users", 0)
    )
    report["summary"] = {
        "total_issues": total_issues,
        "status": "PASS" if total_issues == 0 else "WARNING",
    }

    logger.info("=" * 60)
    logger.info(f"校验完成: {report['summary']['status']} ({total_issues} 个问题)")
    logger.info("=" * 60)

    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(run_verification())
    import json
    print(json.dumps(result, indent=2, default=str))

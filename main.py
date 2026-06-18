"""Daily Mail 数据采集主控模块"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from config import START_DATE, END_DATE, LOG_FORMAT, LOG_LEVEL, get_chrome_cmd
from database import init_db, get_db, get_stats, get_progress_stats, set_progress, get_progress
from sitemap_scraper import scrape_sitemap
from article_scraper import scrape_articles
from comment_scraper import scrape_comments
from user_aggregator import aggregate_users, aggregate_users_parallel
from verify import run_verification

logger = logging.getLogger(__name__)


def setup_logging():
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(os.path.dirname(__file__), "scraper.log"),
                encoding="utf-8",
            ),
        ],
    )


async def cmd_init():
    """初始化数据库"""
    print("初始化数据库...")
    response = input("是否重新初始化? 这将清空所有数据! (y/N): ")
    if response.lower() == "y":
        async with get_db() as db:
            await db.execute("DROP TABLE IF EXISTS Daily_Articles CASCADE")
            await db.execute("DROP TABLE IF EXISTS Article_Info CASCADE")
            await db.execute("DROP TABLE IF EXISTS Comment_Info CASCADE")
            await db.execute("DROP TABLE IF EXISTS User_Info CASCADE")
            await db.execute("DROP TABLE IF EXISTS User_Comment_Info CASCADE")
            await db.execute("DROP TABLE IF EXISTS scrape_progress CASCADE")
            print("旧表已删除")

    await init_db()
    print("数据库表已创建")

    async with get_db() as db:
        stats = await get_stats(db)
        print(f"表已创建，当前统计: {stats}")


async def cmd_sitemap(start: str, end: str):
    """采集 Sitemap 新闻列表"""
    print(f"开始采集 Sitemap: {start} 至 {end}")
    stats = await scrape_sitemap(start, end)
    print(f"Sitemap 采集完成: {stats}")


async def cmd_articles(batch_size: int):
    """采集文章详情"""
    print("开始采集文章详情...")
    stats = await scrape_articles(batch_size)
    print(f"文章详情采集完成: {stats}")


async def cmd_comments(limit: int | None):
    """采集评论"""
    print("开始采集评论...")
    print(f"注意: 请确保 Chrome 已启动调试模式:\n  {get_chrome_cmd()}")
    stats = await scrape_comments(limit)
    print(f"评论采集完成: {stats}")


async def cmd_users(limit: int | None, max_comments: int | None, force: bool = False, parallel: int | None = None):
    """汇总用户数据"""
    if parallel and parallel > 1:
        print(f"开始并行汇总用户数据 (parallel={parallel})...")
        stats = await aggregate_users_parallel(
            parallel=parallel, limit=limit,
            max_comments_per_user=max_comments, force=force,
        )
    else:
        print("开始汇总用户数据...")
        stats = await aggregate_users(
            limit=limit, max_comments_per_user=max_comments, force=force,
        )
    print(f"用户汇总完成: {stats}")


async def cmd_verify():
    """执行数据校验"""
    print("开始数据校验...")
    report = await run_verification()
    print(f"校验结果: {report['summary']['status']}")
    if report["summary"]["total_issues"] > 0:
        print(f"发现 {report['summary']['total_issues']} 个问题")


async def cmd_stats():
    """显示数据库统计"""
    async with get_db() as db:
        stats = await get_stats(db)
        print("=" * 50)
        print("数据库统计")
        print("=" * 50)
        print(f"Daily_Articles (新闻列表): {stats['daily_articles']:,}")
        print(f"Article_Info (文章详情):   {stats['article_info']:,}")
        print(f"Comment_Info (评论):       {stats['comments']:,}")
        print(f"User_Info (用户):          {stats['users']:,}")
        print(f"User_Comment_Info:         {stats['user_comments']:,}")
        print(f"有评论的文章:              {stats['articles_with_comments']:,}")
        print("=" * 50)


async def cmd_full(start: str, end: str, parallel: int = 0):
    """执行完整采集流程（支持续传 + 错误隔离）

    Args:
        start/end: 日期范围
        parallel: 用户汇总并行 Tab 数（0=串行）
    """
    print("=" * 60)
    print("Daily Mail 全量数据采集")
    print(f"时间范围: {start} 至 {end}")
    if parallel:
        print(f"用户汇总: 并行模式 ({parallel} Tab)")
    print("=" * 60)

    phases = [
        ("Phase 1/5", "Sitemap 新闻列表", lambda: cmd_sitemap(start, end)),
        ("Phase 2/5", "文章详情", lambda: cmd_articles(batch_size=100)),
        ("Phase 3/5", "评论采集", lambda: cmd_comments(limit=None)),
        ("Phase 4/5", "用户汇总", lambda: cmd_users(limit=None, max_comments=None, force=False, parallel=parallel if parallel else None)),
        ("Phase 5/5", "数据校验", lambda: cmd_verify()),
    ]

    failed = []

    for label, name, action in phases:
        print(f"\n{label} 采集{name}...")
        try:
            await action()
        except Exception as e:
            logger.error(f"{label} {name} 异常: {e}", exc_info=True)
            print(f"  ⚠ {name} 阶段异常: {e}")
            failed.append(name)
            # 非校验阶段失败不中断整体流程
            if name == "数据校验":
                continue

    print("\n" + "=" * 60)
    if failed:
        print(f"采集完成，但以下阶段有异常: {', '.join(failed)}")
        print("可重新运行 --full 进行续传（已完成部分自动跳过）")
    else:
        print("全量采集完成!")
    print("=" * 60)


async def cmd_status():
    """显示各阶段采集进度"""
    async with get_db() as db:
        stats = await get_stats(db)
        p = await get_progress_stats(db)

        print("=" * 60)
        print("Daily Mail 采集进度报告")
        print("=" * 60)

        # Phase 1: Sitemap
        print(f"\n[Phase 1] Sitemap 新闻列表")
        print(f"  已采集月份:   {p['sitemap_months_done']}")
        print(f"  已入库文章:   {p['sitemap_days']:,} 篇（去重天级记录）")

        # Phase 2: Articles
        print(f"\n[Phase 2] 文章详情")
        total = p['articles_total']
        done = p['articles_done']
        pct = f"{done / max(total, 1) * 100:.1f}%" if total else "N/A"
        print(f"  完成: {done:,} / {total:,}  ({pct})")
        print(f"  剩余: {total - done:,}")

        # Phase 3: Comments
        print(f"\n[Phase 3] 评论采集")
        total_c = p['articles_with_comments']
        done_c = p['comments_done']
        pct_c = f"{done_c / max(total_c, 1) * 100:.1f}%" if total_c else "N/A"
        print(f"  完成: {done_c:,} / {total_c:,} 篇  ({pct_c})")
        print(f"  剩余: {total_c - done_c:,} 篇")
        print(f"  已采集评论: {stats['comments']:,} 条")

        # Phase 4: Users
        print(f"\n[Phase 4] 用户汇总")
        total_u = p['users_total']
        done_u = p['users_done']
        pct_u = f"{done_u / max(total_u, 1) * 100:.1f}%" if total_u else "N/A"
        print(f"  完成: {done_u:,} / {total_u:,}  ({pct_u})")
        print(f"  剩余: {total_u - done_u:,}")
        print(f"  已采集用户评论: {stats['user_comments']:,} 条")

        print("\n" + "=" * 60)


async def cmd_reset_progress(phase: str | None):
    """重置采集进度"""
    async with get_db() as db:
        if phase:
            await db.execute(
                "DELETE FROM scrape_progress WHERE key LIKE $1",
                f"{phase}%",
            )
            print(f"已重置进度: {phase}*")
        else:
            await db.execute("DELETE FROM scrape_progress")
            print("已重置所有进度")


def main():
    parser = argparse.ArgumentParser(
        description="Daily Mail 新闻与评论数据采集系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py --init                    # 初始化数据库
  python main.py --sitemap                 # 采集新闻列表
  python main.py --articles                # 采集文章详情
  python main.py --comments --limit 10     # 采集评论(测试10篇)
  python main.py --users                   # 汇总用户数据
  python main.py --verify                  # 数据校验
  python main.py --stats                   # 查看统计
  python main.py --full                    # 执行全量采集
  python main.py --reset --phase comments  # 重置评论采集进度
        """,
    )

    parser.add_argument("--init", action="store_true", help="初始化数据库")
    parser.add_argument("--sitemap", action="store_true", help="采集 Sitemap 新闻列表")
    parser.add_argument("--articles", action="store_true", help="采集文章详情")
    parser.add_argument("--comments", action="store_true", help="采集评论")
    parser.add_argument("--users", action="store_true", help="汇总用户数据")
    parser.add_argument("--verify", action="store_true", help="执行数据校验")
    parser.add_argument("--stats", action="store_true", help="显示统计信息")
    parser.add_argument("--status", action="store_true", help="显示各阶段采集进度")
    parser.add_argument("--full", action="store_true", help="执行完整采集流程")
    parser.add_argument("--reset", action="store_true", help="重置采集进度")

    parser.add_argument(
        "--start", default=START_DATE, help=f"起始月份 (默认: {START_DATE})"
    )
    parser.add_argument(
        "--end", default=END_DATE, help=f"结束月份 (默认: {END_DATE})"
    )
    parser.add_argument(
        "--batch-size", type=int, default=100, help="文章详情批次大小 (默认: 100)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="限制处理数量 (用于测试)"
    )
    parser.add_argument(
        "--max-comments", type=int, default=None,
        help="限制每用户采集的评论数量 (用于测试)"
    )
    parser.add_argument(
        "--phase", default=None, help="重置指定阶段的进度 (sitemap/article/comment/user)"
    )
    parser.add_argument(
        "--parallel", type=int, default=None,
        help="并行 Tab 数量，启用多 Tab 并行模式 (默认: 串行)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="用户汇总: 强制重新处理所有用户（包括已有统计数据的）"
    )

    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info(f"Daily Mail Scraper started at {datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}")

    # 如果没有指定任何命令，显示帮助
    if not any([
        args.init, args.sitemap, args.articles, args.comments,
        args.users, args.verify, args.stats, args.status, args.full, args.reset,
    ]):
        parser.print_help()
        return

    # 注意: PostgreSQL 数据库需要预先创建好
    # 如未创建，请先执行: python main.py --init

    # 执行命令
    try:
        if args.init:
            asyncio.run(cmd_init())

        if args.reset:
            asyncio.run(cmd_reset_progress(args.phase))

        if args.sitemap:
            asyncio.run(cmd_sitemap(args.start, args.end))

        if args.articles:
            asyncio.run(cmd_articles(args.batch_size))

        if args.comments:
            asyncio.run(cmd_comments(args.limit))

        if args.users:
            asyncio.run(cmd_users(args.limit, args.max_comments, args.force, args.parallel))

        if args.verify:
            asyncio.run(cmd_verify())

        if args.stats:
            asyncio.run(cmd_stats())

        if args.status:
            asyncio.run(cmd_status())

        if args.full:
            asyncio.run(cmd_full(args.start, args.end, args.parallel or 0))

    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"执行异常: {e}")
        sys.exit(1)

    logger.info(f"Daily Mail Scraper finished at {datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}")


if __name__ == "__main__":
    main()

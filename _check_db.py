import asyncio, asyncpg
async def check():
    conn = await asyncpg.connect('postgresql://postgres:long123456@localhost:5432/dailymail?sslmode=disable')
    total = await conn.fetchval('SELECT COUNT(*) FROM Article_Info')
    no_title = await conn.fetchval("SELECT COUNT(*) FROM Article_Info WHERE Art_Title = '' OR Art_Title IS NULL")
    no_author = await conn.fetchval("SELECT COUNT(*) FROM Article_Info WHERE Art_Author = '' OR Art_Author IS NULL")
    has_comments = await conn.fetchval("SELECT COUNT(*) FROM Article_Info WHERE Comments_Count > 0")
    print('Article_Info total:', total)
    print('No title:', no_title, 'No author:', no_author, 'Has comments:', has_comments)
    rows = await conn.fetch("SELECT Art_ID, Art_Title, Art_Author, Comments_Count FROM Article_Info LIMIT 5")
    for r in rows:
        tid = r["art_id"]
        ttl = (r["art_title"] or "(empty)")[:60]
        print('  [%s] %s | author=%s | comments=%s' % (tid, ttl, r["art_author"], r["comments_count"]))
    await conn.close()
asyncio.run(check())

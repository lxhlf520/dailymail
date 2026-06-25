import asyncio, asyncpg
from collections import Counter

async def check():
    conn = await asyncpg.connect('postgresql://postgres:long123456@localhost:5432/dailymail?sslmode=disable')
    total = await conn.fetchval('SELECT COUNT(*) FROM Daily_Articles')
    # Check URL patterns
    rows = await conn.fetch("SELECT url FROM Daily_Articles WHERE article_id NOT IN (SELECT Art_ID FROM Article_Info) LIMIT 100")
    prefixes = Counter()
    for r in rows:
        url = r['url']
        # extract path pattern
        path = url.replace('https://www.dailymail.com', '')
        seg = path.split('/')[1] if '/' in path else path
        prefixes[seg] += 1
    
    print(f'Total Daily_Articles: {total}')
    print(f'URL prefix distribution (sample 100):')
    for k, v in prefixes.most_common(10):
        print(f'  /{k}/...  : {v}')
    
    await conn.close()
asyncio.run(check())

# Daily Mail 数据采集执行计划

## 一、项目概述

**目标网站**: https://www.dailymail.co.uk/home/sitemaparchive/index.html  
**采集范围**: 2016年1月至2026年1月（10年）  
**采集内容**: 新闻存档、文章详情、评论、评论用户信息、用户历史评论  
**技术方案**: Python + Playwright/CDP + SQLite  
**项目路径**: `d:\PycharmProjects\AiSpiderProject\dailymail\`

---

## 二、网站结构分析

### 2.1 Sitemap 层级结构

```
https://www.dailymail.com/home/sitemaparchive/index.html
    └── 年度列表 (1996-2026)
        └── month_YYYYMM.html (如 month_201601.html)
            └── day_YYYYMMDD.html (如 day_20160101.html)
                └── 文章列表 (~100-200篇/天)
                    └── article-{ID}/{slug}.html
```

### 2.2 URL 模式

| 页面类型 | URL 模式 | 示例 |
|---------|---------|------|
| Sitemap 首页 | `/home/sitemaparchive/index.html` | - |
| 月度存档 | `/home/sitemaparchive/month_YYYYMM.html` | month_201601.html |
| 日期存档 | `/home/sitemaparchive/day_YYYYMMDD.html` | day_20160101.html |
| 文章详情 | `/news/article-{id}/{slug}.html` | article-12917509/...html |
| 评论 API | `/reader-comments/p/asset/readcomments/{id}` | - |
| 用户 Profile | `/registration/{user_id}/{alias}/profile.html` | - |

---

## 三、数据接口分析

### 3.1 新闻列表（日期页面）

- **采集方式**: `requests + BeautifulSoup`（无反爬虫）
- **URL**: `https://www.dailymail.com/home/sitemaparchive/day_YYYYMMDD.html`
- **提取字段**: 文章标题、文章链接
- **文章链接格式**: `<a href="/news/article-{id}/{slug}.html">标题</a>`
- **典型数量**: 100-200 篇/天

### 3.2 文章详情

- **采集方式**: `requests + BeautifulSoup`（无反爬虫）
- **URL**: `https://www.dailymail.com/news/article-{id}/{slug}.html`
- **提取方式**:
  - JSON-LD schema (`<script type="application/ld+json">`): headline, author, datePublished, dateModified
  - DOM 提取: 评论数、分享数
  - DOM 提取: 标签 (articleSection, keywords)

**文章元数据字段**:
| 字段 | 来源 | 说明 |
|-----|------|------|
| Art_ID | URL | article-12917509 → 12917509 |
| Art_Title | JSON-LD `headline` | 文章标题 |
| Art_Author | JSON-LD `author.name` | 作者名 |
| Published_At | JSON-LD `datePublished` | ISO 8601 格式 |
| Updated_At | JSON-LD `dateModified` | ISO 8601 格式 |
| Art_Tag_1 | JSON-LD `articleSection` | 文章分类 |
| Art_Tag_2 | JSON-LD `keywords` | 关键词标签 |
| Comments_Count | DOM `.comments-count` | 页面显示评论数 |
| Share_Count | DOM `.share-count` | 分享数 |
| Art_URL | 页面URL | 完整链接 |

### 3.3 评论 API（关键发现）

- **API 地址**: `GET https://www.dailymail.com/reader-comments/p/asset/readcomments/{article_id}?max=N&offset=O&order=desc`
- **反爬虫**: **Akamai Bot Manager 保护**，直接 Python requests 会返回 `429 + {"cpr_chlge":"true"}`
- **解决方案**: **必须通过浏览器环境（CDP/Playwright）fetch 获取**
- **分页参数**:
  - `max`: 每页数量（建议 50-100）
  - `offset`: 偏移量
  - `order`: `desc` 倒序

**评论 JSON 结构**:
```json
{
  "status": "success",
  "code": "200",
  "payload": {
    "parentCommentsCount": 200,
    "offset": "0",
    "assetId": "",
    "page": [
      {
        "id": 1282337423,
        "userAlias": "Scorched earth 1",
        "userIdentifier": "1646304894123612",
        "userLocation": "Flyover, United States",
        "dateCreated": "2024-01-02T20:03:42.552Z",
        "message": "评论内容...",
        "voteCount": 10,
        "voteRating": 10,
        "replies": {
          "totalCount": 1,
          "comments": [
            {
              "id": 1282075589,
              "userAlias": "Starowl",
              "userIdentifier": "1585100832621461",
              "message": "回复内容...",
              "replies": { "totalCount": 0, "comments": [] }
            }
          ]
        },
        "assetCommentCount": 276,
        "assetId": 12917509,
        "assetUrl": "/news/article-12917509/..."
      }
    ]
  }
}
```

**评论字段映射**:
| 目标字段 | JSON 字段 | 说明 |
|---------|----------|------|
| Comment_ID | `id` | 评论唯一ID |
| Reply_2_Comment_ID | 父评论 `id` | 回复时记录父ID |
| Replies | `replies.totalCount` | 该评论的回复数 |
| Comment | `message` | 评论内容 |
| Comment_Time | `dateCreated` | ISO 8601 |
| Comment_From_City | `userLocation` 分割 | "City, Country" |
| Comment_From_Country | `userLocation` 分割 | "City, Country" |
| UpVote | `voteCount` | 点赞数 |
| Down_Vote | 推算 | `voteCount - voteRating` (若 voteRating < voteCount) |
| Vote_Rating | `voteRating` | 净评分 |
| User_Alias | `userAlias` | 用户别名 |
| User_ID | `userIdentifier` | 用户数字ID |
| User_URL | 拼接 | `/registration/{user_id}/{alias}/profile.html` |
| Comment_URL | 拼接 | 文章URL + `#comment-{id}` |

**评论层级实现**:
- 顶层评论: `Reply_2_Comment_ID = NULL`
- 回复评论: `Reply_2_Comment_ID = 父评论ID`
- 支持多级嵌套（递归处理 `replies.comments`）

### 3.4 用户 Profile

- **Profile URL**: `https://www.dailymail.com/registration/{user_id}/{alias}/profile.html`
- **限制**: 当前测试发现 profile 页面被重定向到首页，**可能需要登录**
- **替代方案**: 用户详情从评论数据中汇总提取
  - User_Alias, User_ID, User_URL, City, Country 可直接从评论获取
  - VoteUp_All, VoteDown_All, Comments_Total 通过汇总所有评论计算
  - Profile_Photo, Member_Since, Facebook_URL 暂时无法获取（需登录）

### 3.5 用户历史评论

- **API 未发现**: 未找到公开的用户历史评论 API 端点
- **替代方案**: 在全局评论采集中，汇总每个用户的所有评论写入 `User_Comment_Info` 表
- 实现方式: 采集完所有文章评论后，按 User_ID 分组汇总

---

## 四、数据库表结构

详见 `schema.sql`，共 6 张表:

1. **Daily_Articles** - 每天新闻存档列表
2. **Article_Info** - 新闻详情
3. **Comment_Info** - 评论详情（含层级关系）
4. **User_Info** - 用户详情（从评论汇总）
5. **User_Comment_Info** - 用户个人评论详情（从评论汇总）
6. **scrape_progress** - 采集进度追踪

---

## 五、采集流程设计

### 5.1 总体流程

```
Phase 1: 新闻列表采集
  ├── 1.1 遍历 Sitemap 获取所有日期 URL (2016-01 至 2026-01)
  ├── 1.2 遍历每个日期页面获取所有文章链接
  └── 1.3 写入 Daily_Articles 表

Phase 2: 文章详情采集
  ├── 2.1 从 Daily_Articles 读取待采集文章
  ├── 2.2 访问文章页面获取元数据 (requests)
  └── 2.3 写入 Article_Info 表

Phase 3: 评论采集（核心，需浏览器环境）
  ├── 3.1 筛选 Comments_Count > 0 的文章
  ├── 3.2 启动 Chrome CDP 连接
  ├── 3.3 通过浏览器 fetch 调用评论 API
  ├── 3.4 解析评论层级，写入 Comment_Info
  └── 3.5 提取用户数据，写入 User_Info

Phase 4: 用户评论汇总
  ├── 4.1 从 Comment_Info 按 User_ID 分组
  ├── 4.2 汇总每个用户的总点赞/点踩/评论数
  ├── 4.3 更新 User_Info 统计字段
  └── 4.4 将用户评论写入 User_Comment_Info
```

### 5.2 关键逻辑

**评论数校验**: 
- `Article_Info.Comments_Count` 必须等于该文章实际采集的评论总数（含回复）
- 若不一致，记录差异日志

**点赞/点踩校验**:
- `Comment_Info.UpVote` 必须匹配页面显示数字
- `voteRating` 为净评分，用于校验一致性

**Daily_Articles 数量校验**:
- 表中每天新闻条数 = 网站上实际新闻条数 = Article_Info 中当天发布的新闻条数

**跳过零评论**:
- `Comments_Count == 0` 的文章直接跳过评论采集

---

## 六、技术实现方案

### 6.1 核心模块

| 模块 | 文件 | 职责 |
|-----|------|------|
| 数据库操作 | `database.py` | SQLite 连接、表初始化、CRUD |
| Sitemap 采集 | `sitemap_scraper.py` | 年度→月度→日期→文章列表 |
| 文章详情采集 | `article_scraper.py` | 文章元数据提取 |
| 评论采集 | `comment_scraper.py` | CDP/浏览器环境获取评论 |
| 用户汇总 | `user_aggregator.py` | 用户数据统计汇总 |
| 主控 | `main.py` | 流程调度、断点续传、配置 |
| 校验 | `verify.py` | 数据一致性校验 |

### 6.2 评论采集方案（关键技术点）

由于评论 API 有 Akamai Bot Manager 保护，必须使用浏览器环境：

**方案**: Chrome CDP + `Runtime.evaluate` 执行 fetch

```python
# 通过 CDP 在浏览器中执行 fetch 获取评论
async def fetch_comments_via_cdp(page, article_id, max_count=50, offset=0):
    script = f'''
    async () => {{
        const resp = await fetch(
            'https://www.dailymail.com/reader-comments/p/asset/readcomments/{article_id}?max={max_count}&offset={offset}&order=desc',
            {{
                credentials: 'include',
                headers: {{ 'accept': 'application/json, text/plain, */*' }}
            }}
        );
        return await resp.json();
    }}
    '''
    result = await cdp.evaluate(script)
    return result
```

**注意事项**:
- Chrome 需保持运行状态
- 每次 fetch 间隔 1-2 秒防 rate limit
- 分页获取直到 `offset >= parentCommentsCount`

### 6.3 依赖库

```
requests
beautifulsoup4
lxml
aiosqlite
playwright  # 或直接使用 websockets + CDP
websockets
python-dateutil
```

---

## 七、实现步骤

### Task 1: 基础架构搭建
- [ ] 创建 `database.py` - SQLite 异步操作封装
- [ ] 创建 `config.py` - 配置参数（日期范围、请求间隔等）
- [ ] 初始化数据库 (`schema.sql`)

### Task 2: Sitemap 新闻列表采集
- [ ] 实现 `sitemap_scraper.py`
- [ ] 遍历 2016-01 至 2026-01 的所有日期
- [ ] 提取每天所有文章链接
- [ ] 写入 `Daily_Articles` 表

### Task 3: 文章详情采集
- [ ] 实现 `article_scraper.py`
- [ ] 解析文章页面 JSON-LD + DOM
- [ ] 提取: 标题、作者、时间、标签、评论数、分享数
- [ ] 写入 `Article_Info` 表
- [ ] 实现断点续传机制

### Task 4: 评论采集（核心）
- [ ] 实现 `comment_scraper.py`
- [ ] Chrome CDP 连接管理
- [ ] 浏览器环境 fetch 评论 API
- [ ] 递归解析评论层级（含嵌套回复）
- [ ] 分页获取所有评论
- [ ] 写入 `Comment_Info` 表
- [ ] 评论数为 0 的文章自动跳过

### Task 5: 用户数据汇总
- [ ] 实现 `user_aggregator.py`
- [ ] 从 `Comment_Info` 提取用户信息
- [ ] 汇总用户统计（总点赞、总评论等）
- [ ] 写入 `User_Info` 和 `User_Comment_Info`

### Task 6: 校验与验证
- [ ] 实现 `verify.py`
- [ ] 校验评论数量一致性
- [ ] 校验点赞/点踩数字匹配
- [ ] 校验 Daily_Articles 数量匹配
- [ ] 生成校验报告

### Task 7: 主控与调度
- [ ] 实现 `main.py`
- [ ] 命令行参数支持（--phase, --date-range, --resume 等）
- [ ] 日志记录
- [ ] 异常处理与重试

---

## 八、注意事项与风险

### 8.1 反爬虫风险
- 评论 API 有 **Akamai Bot Manager** 保护
- **严禁**直接用 Python requests/httpx 请求评论 API
- 必须通过浏览器环境（CDP）获取评论
- 文章列表和详情页无保护，可用 requests

### 8.2 Rate Limiting
- 建议请求间隔: 文章页面 0.5-1 秒，评论 API 1-2 秒
- 单日文章量巨大（10年 × 365天 × ~150篇 = 约 55万篇文章）
- 考虑分批采集、增量更新

### 8.3 数据量预估
- 天数: ~3650 天
- 文章: ~55 万篇
- 评论: 假设 10% 文章有评论，平均 50 条 = ~275 万条评论
- 存储: 预计 SQLite 数据库 5-10GB

### 8.4 用户 Profile 限制
- Profile 页面可能需要登录才能访问
- 用户部分字段（Member_Since, Facebook_URL 等）可能无法获取
- 需在计划中标记为"依赖登录"

### 8.5 评论数差异
- 页面显示评论数 vs API 返回 `assetCommentCount` 可能存在差异
- 原因: 页面显示可能包含被删除/隐藏的评论
- 以 API 实际返回为准，记录差异

---

## 九、执行命令示例

```bash
# 初始化数据库
cd d:\PycharmProjects\AiSpiderProject\dailymail
python main.py --init

# Phase 1: 采集新闻列表
python main.py --phase sitemap --start 2016-01 --end 2026-01

# Phase 2: 采集文章详情
python main.py --phase articles --workers 5

# Phase 3: 采集评论（需 Chrome 运行）
python main.py --phase comments --chrome-port 9222

# Phase 4: 汇总用户数据
python main.py --phase users

# 验证数据
python verify.py

# 全量运行
python main.py --full --start 2016-01 --end 2026-01
```

---

## 十、数据校验规则

1. **评论数量校验**: `Article_Info.Comments_Count` ≈ `COUNT(Comment_Info WHERE Article_ID = X)`
2. **点赞数校验**: `Comment_Info.UpVote` == API 返回 `voteCount`
3. **日期文章数校验**: `Daily_Articles` 每天条数 == 网站实际条数
4. **层级校验**: 所有 `Reply_2_Comment_ID` 必须对应存在的 `Comment_ID`
5. **用户一致性**: `User_Info.User_ID` 必须在 `Comment_Info` 中存在

---

*计划生成时间: 2026-05-21*  
*基于 chrome-devtools-mcp 实际调研结果*

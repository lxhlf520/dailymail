-- Daily Mail 数据采集数据库表结构 (PostgreSQL)

-- ============================================
-- 1. Daily_Articles: 每天新闻存档列表
-- ============================================
CREATE TABLE IF NOT EXISTS Daily_Articles (
    id SERIAL PRIMARY KEY,
    archive_date TEXT NOT NULL,           -- 存档日期 YYYY-MM-DD
    article_id TEXT NOT NULL,             -- 文章ID (如 12917509)
    title TEXT NOT NULL,                  -- 文章标题
    url TEXT NOT NULL,                    -- 文章完整URL
    category TEXT,                        -- 新闻分类
    scrape_time TEXT NOT NULL,            -- 采集时间
    UNIQUE(archive_date, article_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_articles_date ON Daily_Articles(archive_date);
CREATE INDEX IF NOT EXISTS idx_daily_articles_article_id ON Daily_Articles(article_id);

-- ============================================
-- 2. Article_Info: 新闻详情
-- ============================================
CREATE TABLE IF NOT EXISTS Article_Info (
    Art_ID TEXT PRIMARY KEY,              -- 文章ID
    Art_Title TEXT NOT NULL,              -- 文章标题
    Art_Author TEXT,                      -- 作者
    Published_At TEXT,                    -- 发布时间 ISO 8601
    Updated_At TEXT,                      -- 更新时间 ISO 8601
    Art_Tag_1 TEXT,                       -- 标签1
    Art_Tag_2 TEXT,                       -- 标签2
    Comments_Count INTEGER DEFAULT 0,     -- 页面显示评论数
    Share_Count INTEGER DEFAULT 0,        -- 分享数
    Art_URL TEXT NOT NULL,                -- 文章URL
    Scrape_Time TEXT NOT NULL             -- 采集时间
);

CREATE INDEX IF NOT EXISTS idx_article_info_published ON Article_Info(Published_At);

-- ============================================
-- 3. Comment_Info: 评论详情
-- ============================================
CREATE TABLE IF NOT EXISTS Comment_Info (
    id SERIAL PRIMARY KEY,
    Article_ID TEXT NOT NULL,             -- 关联文章ID
    Comment_ID TEXT NOT NULL,             -- 评论唯一ID
    Reply_2_Comment_ID TEXT,              -- 回复的目标评论ID (NULL=顶层评论)
    Replies INTEGER DEFAULT 0,            -- 该评论的回复数量
    Comment TEXT NOT NULL,                -- 评论内容
    Comment_Time TEXT,                    -- 评论时间 ISO 8601
    Comment_From_City TEXT,               -- 评论者城市
    Comment_From_Country TEXT,            -- 评论者国家
    UpVote INTEGER DEFAULT 0,             -- 点赞数 = (voteCount+voteRating)/2
    Down_Vote INTEGER DEFAULT 0,          -- 点踩数 = (voteCount-voteRating)/2
    Vote_Rating INTEGER DEFAULT 0,        -- 净评分 = upvote-downvote
    User_Alias TEXT NOT NULL,             -- 用户别名
    User_ID TEXT NOT NULL,                -- 用户数字ID
    User_URL TEXT,                        -- 用户profile链接
    Comment_URL TEXT,                     -- 评论链接
    scrape_time TEXT NOT NULL,            -- 采集时间
    UNIQUE(Article_ID, Comment_ID)
);

CREATE INDEX IF NOT EXISTS idx_comment_article ON Comment_Info(Article_ID);
CREATE INDEX IF NOT EXISTS idx_comment_user ON Comment_Info(User_ID);
CREATE INDEX IF NOT EXISTS idx_comment_reply ON Comment_Info(Reply_2_Comment_ID);

-- ============================================
-- 4. User_Info: 用户详情
-- ============================================
CREATE TABLE IF NOT EXISTS User_Info (
    User_Alias TEXT,                      -- 用户别名
    User_ID TEXT PRIMARY KEY,             -- 用户数字ID
    User_URL TEXT,                        -- 用户profile链接
    Profile_Photo TEXT,                   -- 头像URL
    City TEXT,                            -- 城市
    Country TEXT,                         -- 国家
    Member_Since TEXT,                    -- 注册时间
    Facebook_URL TEXT,                    -- Facebook链接
    VoteUp_All INTEGER DEFAULT 0,         -- 总点赞数
    VoteDown_All INTEGER DEFAULT 0,       -- 总点踩数
    Comments_Total INTEGER DEFAULT 0      -- 总评论数
);

CREATE INDEX IF NOT EXISTS idx_user_alias ON User_Info(User_Alias);

-- ============================================
-- 5. User_Comment_Info: 用户个人评论详情
-- ============================================
CREATE TABLE IF NOT EXISTS User_Comment_Info (
    id SERIAL PRIMARY KEY,
    Article_ID TEXT NOT NULL,             -- 关联文章ID
    Comment_ID TEXT NOT NULL,             -- 评论ID
    Reply_2_Comment_ID TEXT,              -- 回复目标评论ID
    Comment TEXT NOT NULL,                -- 评论内容
    Comment_Time TEXT,                    -- 评论时间
    Comment_From_City TEXT,               -- 城市
    Comment_From_Country TEXT,            -- 国家
    UpVote INTEGER DEFAULT 0,             -- 点赞数 = (voteCount+voteRating)/2
    Down_Vote INTEGER DEFAULT 0,          -- 点踩数 = (voteCount-voteRating)/2
    Vote_Rating INTEGER DEFAULT 0,        -- 净评分 = upvote-downvote
    User_Alias TEXT NOT NULL,             -- 用户别名
    User_ID TEXT NOT NULL,                -- 用户ID
    Comment_URL TEXT,                     -- 评论链接
    scrape_time TEXT NOT NULL,            -- 采集时间
    UNIQUE(User_ID, Comment_ID)
);

CREATE INDEX IF NOT EXISTS idx_user_comment_user ON User_Comment_Info(User_ID);
CREATE INDEX IF NOT EXISTS idx_user_comment_article ON User_Comment_Info(Article_ID);

-- ============================================
-- 6. 采集进度追踪表
-- ============================================
CREATE TABLE IF NOT EXISTS scrape_progress (
    key TEXT PRIMARY KEY,
    value TEXT
);

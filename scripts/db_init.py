#!/usr/bin/env python3
"""
db_init.py — 初始化念念全部9个SQLite数据库
============================================
从旧DB schema复制，加FTS全文搜索。
与旧DB完全对齐的表结构，但数据为空。
"""

import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "niannian-data"

def init_db(path: Path, schema_sql: str):
    """初始化单个数据库（删除旧文件重建）。"""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()
    print(f"  ✅ {path.name}")


# ═══════════════════════════════════════════════════════
#  facts.db — 事实三元组 + 变更日志
# ═══════════════════════════════════════════════════════

FACTS_SCHEMA = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    source TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    importance REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0
);

CREATE TABLE facts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER,
    action TEXT,
    old_value TEXT,
    new_value TEXT,
    ts TEXT DEFAULT (datetime('now'))
);
"""

# ═══════════════════════════════════════════════════════
#  knowledge.db — 知识条目 + FTS5
# ═══════════════════════════════════════════════════════

KNOWLEDGE_SCHEMA = """
CREATE TABLE knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT,
    category TEXT,
    title TEXT,
    reason TEXT,
    source TEXT,
    trust_score REAL DEFAULT 0.5,
    helpful_count INTEGER DEFAULT 0,
    retrieval_count INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    title, reason, content='knowledge', content_rowid='id'
);
"""

# ═══════════════════════════════════════════════════════
#  tree.db — 记忆树 + 实体 + 链接 + 同步日志
# ═══════════════════════════════════════════════════════

TREE_SCHEMA = """
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    level INTEGER DEFAULT 0,
    node_type TEXT DEFAULT 'leaf',
    source_db TEXT,
    source_id INTEGER,
    content TEXT,
    summary TEXT,
    child_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (parent_id) REFERENCES nodes(id)
);

CREATE TABLE tree_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER REFERENCES nodes(id),
    entity_name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'generic',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE tree_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_node INTEGER REFERENCES nodes(id),
    to_node INTEGER REFERENCES nodes(id),
    rel_type TEXT DEFAULT 'related',
    weight REAL DEFAULT 1.0
);

CREATE TABLE tree_sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_db TEXT,
    source_id INTEGER,
    action TEXT,
    node_id INTEGER,
    ts TEXT DEFAULT (datetime('now'))
);
"""

# ═══════════════════════════════════════════════════════
#  forgotten.db — 遗忘归档 + FTS5
# ═══════════════════════════════════════════════════════

FORGOTTEN_SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT,
    url TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'raw'
        CHECK(status IN ('raw','pending','active','archived','deprecated')),
    category TEXT,
    tags TEXT,
    reason TEXT,
    synced_to_knowledge INTEGER DEFAULT 0,
    source_db TEXT,
    source_id INTEGER,
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE items_fts USING fts5(
    title, content, category, tags,
    content='items', content_rowid='id'
);
"""

# ═══════════════════════════════════════════════════════
#  lesson.db — 教训 + FTS5 + 质量评分
# ═══════════════════════════════════════════════════════

LESSON_SCHEMA = """
CREATE TABLE lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    task TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',
    quality_score INTEGER DEFAULT 5 CHECK(quality_score BETWEEN 1 AND 10),
    source TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE lessons_fts USING fts5(
    task, content, tags, content='lessons', content_rowid='id'
);
"""

# ═══════════════════════════════════════════════════════
#  tasklog.db — 任务日志 + FTS5
# ═══════════════════════════════════════════════════════

TASKLOG_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT,
    finished TEXT,
    status TEXT DEFAULT 'active',
    summary TEXT,
    keywords TEXT,
    full_log TEXT,
    category TEXT DEFAULT '对话'
);

CREATE VIRTUAL TABLE tasks_fts USING fts5(
    summary, keywords, full_log, content=tasks, content_rowid=id
);
"""

# ═══════════════════════════════════════════════════════
#  sessions.db — 会话归档 + FTS5
# ═══════════════════════════════════════════════════════

SESSIONS_SCHEMA = """
CREATE TABLE session_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    archived_at TEXT,
    role TEXT,
    content TEXT,
    msg_index INTEGER,
    msg_timestamp REAL,
    topic TEXT DEFAULT '',
    category TEXT DEFAULT '对话'
);

CREATE VIRTUAL TABLE session_archive_fts USING fts5(
    session_id, content,
    content=session_archive, content_rowid=id
);

CREATE TABLE session_archive_cron (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, source TEXT, archived_at TEXT,
    msg_index INTEGER, role TEXT, content TEXT,
    msg_timestamp REAL
);

CREATE TABLE session_archive_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, archived_at TEXT,
    msg_index INTEGER, role TEXT, content TEXT,
    msg_timestamp REAL, topic TEXT DEFAULT ''
);
"""

# ═══════════════════════════════════════════════════════
#  state.db — 持久状态 + 会话 + 消息 + FTS5 + trigram
# ═══════════════════════════════════════════════════════

STATE_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (1);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT
);

CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content, tokenize='trigram'
);

CREATE TABLE state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# ═══════════════════════════════════════════════════════
#  cron.db — 定时任务 + 执行记录 + 状态存储
# ═══════════════════════════════════════════════════════

CRON_SCHEMA = """
CREATE TABLE cron_jobs (
    id TEXT PRIMARY KEY,
    name TEXT,
    schedule TEXT,
    profile TEXT DEFAULT 'default',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE cron_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES cron_jobs(id),
    status TEXT NOT NULL DEFAULT 'pending',
    output TEXT,
    exit_code INTEGER,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE job_store (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


# ═══════════════════════════════════════════════════════
#  执行初始化
# ═══════════════════════════════════════════════════════

DB_SCHEMAS = {
    "facts.db": FACTS_SCHEMA,
    "knowledge.db": KNOWLEDGE_SCHEMA,
    "tree.db": TREE_SCHEMA,
    "forgotten.db": FORGOTTEN_SCHEMA,
    "lesson.db": LESSON_SCHEMA,
    "tasklog.db": TASKLOG_SCHEMA,
    "sessions.db": SESSIONS_SCHEMA,
    "state.db": STATE_SCHEMA,
    "cron.db": CRON_SCHEMA,
}

if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for db_name, schema in DB_SCHEMAS.items():
        init_db(DATA_DIR / db_name, schema)
    print(f"\n全部9个DB初始化完成 → {DATA_DIR}")

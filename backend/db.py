"""SQLite 存储层：对话、消息、记忆、知识库、学习沉淀、反馈"""
import os
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campus_agent.db")
_local = threading.local()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS conversations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT DEFAULT '新对话',
        school_id TEXT DEFAULT 'hlby',
        created_at TEXT,
        updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        role TEXT,
        content TEXT,
        tool_events TEXT DEFAULT '[]',
        source_refs TEXT DEFAULT '[]',
        rating INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT,
        category TEXT,
        source TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS knowledge(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        source TEXT,
        content TEXT,
        ktype TEXT,
        school_id TEXT DEFAULT 'hlby',
        hits INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS knowledge_chunks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER,
        chunk_index INTEGER,
        content TEXT
    );
    CREATE TABLE IF NOT EXISTS learned(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER,
        question TEXT,
        answer TEXT,
        note TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER,
        rating TEXT,
        comment TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        created_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "source_refs" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN source_refs TEXT DEFAULT '[]'")
    knowledge_columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
    if "school_id" not in knowledge_columns:
        conn.execute("ALTER TABLE knowledge ADD COLUMN school_id TEXT DEFAULT 'hlby'")
    # 旧版单校资料属于原始学校，切换模板后不会泄漏到其他学校。
    conn.execute("UPDATE knowledge SET school_id='hlby' WHERE school_id IS NULL OR school_id='' ")
    conversation_columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "school_id" not in conversation_columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN school_id TEXT DEFAULT 'hlby'")
    if "user_id" not in conversation_columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER")
    conn.execute("UPDATE conversations SET school_id='hlby' WHERE school_id IS NULL OR school_id='' ")
    conn.commit()

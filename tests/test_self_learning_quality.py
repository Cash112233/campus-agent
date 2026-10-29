import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db
from learning import LearningEngine
from memory import MemoryManager


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.prompt = ""

    def chat(self, messages, **kwargs):
        self.prompt = messages[0]["content"]
        return SimpleNamespace(content=self.content)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with patch("db.get_conn", return_value=conn):
        db.init_db()
    return conn


def test_learning_extract_includes_evidence_and_filters_low_quality_items():
    conn = _db()
    conn.execute("INSERT INTO conversations(id,title,school_id,created_at,updated_at) VALUES(1,'对话','hlby','now','now')")
    conn.execute(
        "INSERT INTO messages(conversation_id,role,content,source_refs,tool_events,created_at) VALUES(?,?,?,?,?,?)",
        (1, "user", "奖学金申请需要什么？", "[]", "[]", "now"),
    )
    conn.execute(
        "INSERT INTO messages(conversation_id,role,content,source_refs,tool_events,created_at) VALUES(?,?,?,?,?,?)",
        (1, "assistant", "请依据官网通知准备材料。", json.dumps([{"title": "奖学金通知", "source_url": "https://www.byau.edu.cn/a"}], ensure_ascii=False), json.dumps([{"name": "search_knowledge"}], ensure_ascii=False), "now"),
    )
    conn.commit()
    llm = FakeLLM(json.dumps([
        {"question": "奖学金申请需要什么？", "answer": "依据官网通知准备材料，具体以当年通知为准。", "note": "来源：官网通知"},
        {"question": "太短", "answer": "好"},
    ], ensure_ascii=False))
    try:
        with patch("learning.get_conn", return_value=conn):
            created = LearningEngine(None).extract(1, llm)
    finally:
        conn.close()
    assert len(created) == 1
    assert "https://www.byau.edu.cn/a" in llm.prompt
    assert "不得提取只有‘好/可以/不知道’等低信息量回答" in llm.prompt


def test_memory_extract_prompt_excludes_sensitive_and_temporary_facts():
    conn = _db()
    conn.execute("INSERT INTO conversations(id,title,school_id,created_at,updated_at) VALUES(1,'对话','hlby','now','now')")
    conn.executemany(
        "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
        [(1, "user", "我是工程学院大二学生，喜欢晚上在图书馆学习。", "now"), (1, "assistant", "好的。", "now")],
    )
    conn.commit()
    llm = FakeLLM(json.dumps([{"content": "用户是工程学院大二学生", "category": "学生背景"}], ensure_ascii=False))
    try:
        with patch("memory.get_conn", return_value=conn):
            added = MemoryManager().extract_from_conversation(1, llm)
    finally:
        conn.close()
    assert added == ["用户是工程学院大二学生"]
    assert "不要保存 API Key、密码、身份证号" in llm.prompt
    assert "临时计划" in llm.prompt


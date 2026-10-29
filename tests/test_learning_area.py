import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db
import learning


class FakeLLM:
    def __init__(self):
        self.messages = []

    def chat(self, messages, temperature=None):
        self.messages.append((messages, temperature))
        return SimpleNamespace(content=json.dumps({
            "overview": "课程总览",
            "key_points": [{"point": "考点一", "explanation": "重点说明", "source": "原资料"}],
            "questions": [{"type": "判断题", "question": "说法正确吗？", "options": [], "answer": "正确", "explanation": "依据资料", "source": "上传资料"}],
        }, ensure_ascii=False))


def test_analyze_materials_combines_selected_and_uploaded_content():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with patch.object(db, "get_conn", return_value=conn):
        db.init_db()
    conn.execute(
        "INSERT INTO knowledge(title, source, content, ktype, school_id, created_at) VALUES(?,?,?,?,?,?)",
        ("原资料", "官网", "原资料中的考点", "文档", "hlby", "2026-10-29 10:00:00"),
    )
    conn.commit()
    doc_id = conn.execute("SELECT id FROM knowledge").fetchone()["id"]
    llm = FakeLLM()

    try:
        with patch.object(learning, "get_conn", return_value=conn):
            result = learning.LearningEngine(None).analyze_materials([doc_id], "上传资料", "上传资料中的考点", llm, question_count=3)
    finally:
        conn.close()

    prompt = llm.messages[0][0][0]["content"]
    assert "原资料中的考点" in prompt
    assert "上传资料中的考点" in prompt
    assert result["overview"] == "课程总览"
    assert result["key_points"][0]["point"] == "考点一"
    assert result["questions"][0]["answer"] == "正确"
    assert result["materials"] == [{"id": doc_id, "title": "原资料", "source": "官网", "kind": "知识库"}, {"id": None, "title": "上传资料", "source": "本次上传", "kind": "上传资料"}]


def test_analyze_materials_requires_source_material():
    with patch.object(learning, "get_conn"):
        try:
            learning.LearningEngine(None).analyze_materials([], "", "", FakeLLM())
        except ValueError as error:
            assert str(error) == "请先选择知识库资料或上传资料"
        else:
            raise AssertionError("应拒绝空资料")


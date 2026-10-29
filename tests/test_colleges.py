import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import services


class FakeRag:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def search(self, query, top_k=4):
        self.queries.append((query, top_k))
        return self.rows[:top_k]


def test_list_colleges_returns_clickable_names():
    rows = services.list_colleges()
    assert rows
    assert rows[0]["name"] in services.COLLEGES
    assert all(row["id"] and row["name"] for row in rows)


def test_college_detail_contains_verified_contacts_and_matching_references(monkeypatch):
    contacts = [{"department": "工程学院", "role": "教学秘书", "phone": "1234567", "address": "工程楼"}]
    monkeypatch.setattr(services, "list_contacts", lambda query: contacts if query == "工程学院" else [])
    rag = FakeRag([
        {"title": "工程学院本科生通知", "content": "工程学院相关说明", "source": "官网", "doc_id": 1},
        {"title": "无关资料", "content": "其他内容", "source": "官网", "doc_id": 2},
    ])

    result = services.college_detail("工程学院", rag)

    assert result["name"] == "工程学院"
    assert result["contacts"] == contacts
    assert [row["title"] for row in result["references"]] == ["工程学院本科生通知"]
    assert result["status"] == "has_information"


def test_college_detail_rejects_unknown_college():
    with pytest.raises(ValueError, match="未找到该学院"):
        services.college_detail("不存在学院", FakeRag([]))


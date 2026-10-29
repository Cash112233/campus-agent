from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import web_search


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _response(results):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"results": results}
    return response


def test_search_latest_sorts_recent_dates_and_puts_undated_last():
    results = [
        {"title": "较早", "url": "https://old.example.com/a", "content": "old", "published_date": _iso(20)},
        {"title": "昨天", "url": "https://new.example.com/b", "content": "new", "published_date": _iso(1)},
        {"title": "无日期", "url": "https://none.example.com/c", "content": "none"},
        {"title": "今天", "url": "https://today.example.com/d", "content": "today", "published_date": _iso(0)},
    ]
    with patch("web_search.requests.post", return_value=_response(results)) as post:
        output = web_search.search_latest("通知", days=7, max_results=5, api_key="test-key")

    assert [item["title"] for item in output] == ["今天", "昨天", "无日期"]
    assert output[0]["domain"] == "today.example.com"
    assert output[-1]["published_date"] is None
    post.assert_called_once_with(
        "https://api.tavily.com/search",
        json={
            "api_key": "test-key",
            "query": "通知",
            "search_depth": "advanced",
            "days": 7,
            "max_results": 10,
            "include_raw_content": False,
        },
        timeout=15,
    )


def test_search_latest_requires_api_key():
    with pytest.raises(ValueError, match="TAVILY_API_KEY 未配置"):
        web_search.search_latest("通知", api_key="")


def test_search_latest_wraps_request_errors():
    with patch("web_search.requests.post", side_effect=RuntimeError("network boom")):
        with pytest.raises(RuntimeError, match="network boom"):
            web_search.search_latest("通知", api_key="test-key")


def test_dedup_and_rank_keeps_first_duplicate():
    prefix = "重复标题" * 5
    items = [
        {"title": prefix + "第一条", "domain": "example.com", "is_official": False, "published_date": None},
        {"title": prefix + "第二条", "domain": "example.com", "is_official": True, "published_date": _iso(0)},
    ]
    output = web_search.dedup_and_rank(items)
    assert len(output) == 1
    assert output[0]["title"] == items[0]["title"]
    assert output is not items


def test_dedup_and_rank_prioritizes_official_domains():
    items = [
        {"title": "普通来源", "domain": "news.example.com", "is_official": False, "published_date": _iso(0)},
        {"title": "学校来源", "domain": "school.edu.cn", "is_official": True, "published_date": _iso(2)},
    ]
    output = web_search.dedup_and_rank(items)
    assert [item["title"] for item in output] == ["学校来源", "普通来源"]


def test_search_latest_clean_calls_search_latest_once():
    items = [{"title": "结果", "domain": "example.com", "is_official": False, "published_date": None}]
    with patch.object(web_search, "search_latest", return_value=items) as search:
        output = web_search.search_latest_clean("通知", days=3, max_results=2)

    search.assert_called_once_with("通知", 3, 2, api_key=None)
    assert output == items


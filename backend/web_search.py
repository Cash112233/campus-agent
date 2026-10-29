"""Tavily 最新信息搜索工具。

该模块只负责联网搜索结果的请求、清洗和排序；API Key 从调用参数或服务端配置读取，
不会在结果中返回或写入日志。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def _parse_datetime(value: object) -> datetime | None:
    """解析常见 ISO8601 日期，解析失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _published_value(value: object) -> str | None:
    """只保留可解析的 ISO8601 发布时间。"""
    if _parse_datetime(value) is None:
        return None
    return str(value).strip()


def _date_sort_key(item: dict) -> tuple[bool, datetime]:
    parsed = _parse_datetime(item.get("published_date"))
    return parsed is not None, parsed or datetime.min.replace(tzinfo=timezone.utc)


def search_latest(
    query: str,
    days: int = 7,
    max_results: int = 5,
    api_key: str | None = None,
) -> list[dict]:
    """
    只返回最近 days 天内的搜索结果，按发布时间倒序。

    Tavily 的结果若没有发布日期会保留，以便调用方按规则明确标注“该页面未标注发布日期”；
    带有明确但超出时间窗口的旧结果会被过滤。
    """
    if not api_key or not str(api_key).strip():
        raise ValueError("TAVILY_API_KEY 未配置")

    days = int(days)
    max_results = int(max_results)
    if days < 1 or days > 30:
        raise ValueError("days 必须在 1–30 之间")
    if max_results < 1 or max_results > 10:
        raise ValueError("max_results 必须在 1–10 之间")

    payload = {
        "api_key": str(api_key).strip(),
        "query": str(query or "").strip(),
        "search_depth": "advanced",
        "days": days,
        "max_results": max_results * 2,
        "include_raw_content": False,
    }

    try:
        response = requests.post(TAVILY_SEARCH_URL, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as error:
        raise RuntimeError(str(error)) from error

    raw_results = data.get("results", []) if isinstance(data, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cleaned: list[dict] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "")
        parsed_url = urlparse(url)
        domain = (parsed_url.hostname or "").lower()
        published_date = _published_value(raw.get("published_date"))
        published_at = _parse_datetime(published_date)
        # 有日期但早于窗口的条目不返回；无日期条目保留并排在最后。
        if published_at is not None and published_at < cutoff:
            continue
        cleaned.append(
            {
                "title": str(raw.get("title") or ""),
                "url": url,
                "content": str(raw.get("content") or ""),
                "published_date": published_date,
                "domain": domain,
                "is_official": ".edu.cn" in url.lower(),
            }
        )

    cleaned.sort(key=_date_sort_key, reverse=True)
    return cleaned[:max_results]


def dedup_and_rank(items: list[dict]) -> list[dict]:
    """
    去重 + 权威加权：保留第一次出现的条目，学校官方域名优先。
    """
    unique: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        title = str(item.get("title") or "")
        domain = str(item.get("domain") or "")
        key = (title[:20], domain)
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(item))

    unique.sort(
        key=lambda item: (bool(item.get("is_official")), _date_sort_key(item)[1]),
        reverse=True,
    )
    return unique


def search_latest_clean(query: str, days: int = 7, max_results: int = 5) -> list[dict]:
    """search_latest 之后套一层 dedup_and_rank，返回最终结果。"""
    # 延迟读取配置，避免模块导入时锁定配置，也避免把密钥放入工具参数或事件。
    try:
        try:
            import config as cfgmod
        except ImportError:
            from backend import config as cfgmod

        api_key = cfgmod.load_config().get("tavily_api_key") or None
    except Exception:
        api_key = None
    return dedup_and_rank(search_latest(query, days, max_results, api_key=api_key))


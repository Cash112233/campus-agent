"""基于火山引擎联网搜索的国内最新信息检索。"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import requests

VOLC_SEARCH_URL = "https://open.feedcoopapi.com/search_api/web_search"

def _parse_datetime(value):
    if not isinstance(value, str) or not value.strip(): return None
    try: parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError: return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

def _date_sort_key(item):
    parsed = _parse_datetime(item.get("published_date"))
    return parsed is not None, parsed or datetime.min.replace(tzinfo=timezone.utc)

def _configured_key():
    try: import config as cfgmod
    except ImportError: from backend import config as cfgmod
    return str(cfgmod.load_config().get("volcengine_api_key") or "").strip()

def search_latest(query: str, days: int = 7, max_results: int = 5, api_key: str | None = None) -> list[dict]:
    days, max_results = int(days), int(max_results)
    if days < 1 or days > 30: raise ValueError("days 必须在 1–30 之间")
    if max_results < 1 or max_results > 10: raise ValueError("max_results 必须在 1–10 之间")
    key = str(api_key).strip() if api_key else _configured_key()
    if not key: raise ValueError("VOLCENGINE_API_KEY 未配置")
    payload = {"Query": str(query or "").strip(), "SearchType": "web", "EnableWaiting": True, "MaxWaitTime": 5000}
    try:
        response = requests.post(VOLC_SEARCH_URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload, timeout=15)
        response.raise_for_status(); data = response.json()
    except Exception as error: raise RuntimeError(f"火山引擎搜索失败：{error}") from error
    cutoff = datetime.now(timezone.utc) - timedelta(days=days); cleaned = []
    result = (data.get("Result") or data.get("result") or {}) if isinstance(data, dict) else {}
    raw_results = (result.get("WebResults") or result.get("web_results") or []) if isinstance(result, dict) else []
    for raw in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(raw, dict): continue
        url = str(raw.get("Url") or raw.get("url") or "")
        raw_date = raw.get("PublishTime") or raw.get("DatePublished") or raw.get("published_date") or raw.get("date")
        published = str(raw_date).strip() if _parse_datetime(raw_date) else None
        published_at = _parse_datetime(published)
        if published_at is not None and published_at < cutoff: continue
        domain = (urlparse(url).hostname or "").lower()
        cleaned.append({"title": str(raw.get("Title") or raw.get("title") or ""), "url": url, "content": str(raw.get("Content") or raw.get("Snippet") or raw.get("content") or ""), "published_date": published, "domain": domain, "is_official": ".edu.cn" in domain})
    cleaned.sort(key=_date_sort_key, reverse=True); return cleaned[:max_results]

def dedup_and_rank(items: list[dict]) -> list[dict]:
    unique = []; seen = set()
    for item in items:
        key = (str(item.get("title") or "")[:20], str(item.get("domain") or ""))
        if key in seen: continue
        seen.add(key); unique.append(dict(item))
    unique.sort(key=lambda item: (bool(item.get("is_official")), _date_sort_key(item)[1]), reverse=True)
    return unique

def search_latest_clean(query: str, days: int = 7, max_results: int = 5) -> list[dict]:
    return dedup_and_rank(search_latest(query, days, max_results))


def status() -> dict:
    """检测火山引擎 API Key 是否配置，不触发实际搜索。"""
    key = _configured_key()
    return {"available": bool(key), "url": VOLC_SEARCH_URL, "message": "火山引擎 API Key 已配置" if key else "火山引擎 API Key 未配置"}

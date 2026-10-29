"""到梦空间活动实时检索。

优先使用到梦空间公开活动查询接口；如果配置了 dmkj_web_token，则同时携带
当前用户会话令牌。令牌只从服务端配置读取，不写入对话、知识库或日志。
"""
from datetime import datetime
from html import unescape
import re
import time
from typing import Any, Dict, Iterable, List, Optional

import httpx


BASE_URL = "https://www.5idream.net"
LIST_PATH = "/public/activity/list"
DETAIL_PATH = "/public/activity/detail"
SOURCE_NAME = "到梦空间实时活动"


def activity_url(activity_id: Any) -> str:
    return f"{BASE_URL}/public/activity/detail.html#{activity_id}"


def is_activity_request(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(term in text for term in (
        "到梦空间", "校园活动", "活动报名", "活动详情", "活动通知", "有什么活动",
        "最近活动", "今日活动", "明天活动", "讲座", "竞赛报名", "志愿活动",
        "第二课堂活动", "报名截止",
    )) or ("活动" in text and any(term in text for term in ("报名", "参加", "查询", "详情", "最近", "今天", "明天", "有哪些")))


def activity_keyword(message: str) -> str:
    text = re.sub(r"[，。！？、,.!?：:；;（）()\[\]【】]", " ", str(message or ""))
    generic = (
        "请问", "请帮我", "帮我", "查询", "查一下", "查查", "看看", "告诉我", "有没有",
        "最近", "今日", "今天", "明天", "后天", "现在", "当前", "有哪些", "有什么", "可以",
        "参加", "报名", "详情", "信息", "活动", "活动相关", "到梦空间", "校园", "学校",
        "的", "吗", "呢", "啊", "一下", "我想", "我是", "我", "适合我", "适合", "推荐", "个",
    )
    for word in generic:
        text = text.replace(word, " ")
    # 学院、年级属于用户筛选背景，公开活动接口只按活动名称检索，避免把背景词当成活动名。
    text = re.sub(r"[\u4e00-\u9fff]{2,16}学院", " ", text)
    text = re.sub(r"[\u4e00-\u9fff]{1,8}(?:专业|年级|班)", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:80]


def _strip_markup(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _rows(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    rows = payload.get("rows")
    if isinstance(rows, list):
        return rows
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        return result["rows"]
    return []


def normalize_list_response(payload: Dict[str, Any], keyword: str = "") -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for row in _rows(payload):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        end_value = str(row.get("endDateStr") or "").strip()
        try:
            if end_value and datetime.strptime(end_value, "%Y-%m-%d %H:%M:%S") < datetime.now():
                continue
        except ValueError:
            # 日期格式异常时保留原行，让用户通过官网详情核对，而不是擅自丢弃。
            pass
        rows.append({
            "id": str(row.get("id")),
            "title": str(row.get("activityName") or "未命名活动").strip(),
            "start": str(row.get("startDateStr") or "").strip(),
            "end": end_value,
            "source_url": activity_url(row.get("id")),
            "source": SOURCE_NAME,
        })
    return {
        "available": True,
        "query": keyword,
        "results": rows,
        "count": len(rows),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE_NAME,
        "source_url": f"{BASE_URL}/public/activity/list.html",
    }


def normalize_detail_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("到梦空间没有返回活动详情")
    activity_id = data.get("id")
    return {
        "id": str(activity_id) if activity_id is not None else "",
        "title": str(data.get("activityName") or "未命名活动").strip(),
        "description": _strip_markup(data.get("description")),
        "abstract": _strip_markup(data.get("activityAbstract")),
        "start": str(data.get("startDateStr") or "").strip(),
        "end": str(data.get("endDateStr") or "").strip(),
        "organizer": str(data.get("undertakeUnits") or data.get("sponsorUnits") or "").strip(),
        "co_organizer": str(data.get("coSponsorUnits") or "").strip(),
        "source_url": activity_url(activity_id),
        "source": SOURCE_NAME,
    }


class DreamSpaceClient:
    def __init__(self, base_url: str = BASE_URL, web_token: str = "", timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.web_token = (web_token or "").strip()
        cookies = {"dmkj_web_token": self.web_token} if self.web_token else None
        self.client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "CampusAgent/1.0", "Accept": "application/json"},
            cookies=cookies,
        )

    def close(self):
        self.client.close()

    def _safe_error(self, error):
        message = str(error or "未知错误")
        if self.web_token:
            message = message.replace(self.web_token, "[已隐藏]")
        return message[:240]

    def _post_json(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """对偶发的 TLS EOF 做一次短退避重试，不改变认证信息。"""
        last_error = None
        for attempt in range(2):
            try:
                response = self.client.post(self.base_url + path, data=data)
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.2)
        raise last_error

    def search(self, keyword: str = "", page: int = 1, page_size: int = 8, include_details: bool = True) -> Dict[str, Any]:
        try:
            payload = self._post_json(
                LIST_PATH,
                {"activityName": keyword, "page": max(1, int(page)), "pagesize": max(1, min(int(page_size), 20))},
            )
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "available": False,
                "authenticated": bool(self.web_token),
                "results": [],
                "count": 0,
                "message": f"到梦空间活动查询暂时失败：{self._safe_error(exc)}",
                "source": SOURCE_NAME,
            }
        result = normalize_list_response(payload, keyword)
        result["authenticated"] = bool(self.web_token)
        if include_details:
            for row in result["results"][:3]:
                detail = self.detail(row["id"])
                if detail.get("available"):
                    row.update({key: detail[key] for key in ("description", "abstract", "organizer", "co_organizer") if detail.get(key)})
        result["note"] = "每次查询均从到梦空间实时获取；活动报名资格和个人可见范围以到梦空间页面为准。"
        return result

    def detail(self, activity_id: Any) -> Dict[str, Any]:
        if not str(activity_id or "").strip():
            return {"available": False, "message": "缺少活动编号", "source": SOURCE_NAME}
        try:
            detail = normalize_detail_response(self._post_json(DETAIL_PATH, {"id": str(activity_id)}))
            detail.update({"available": True, "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            return detail
        except (httpx.HTTPError, ValueError) as exc:
            return {"available": False, "message": f"活动详情查询失败：{self._safe_error(exc)}", "source": SOURCE_NAME}


def search_activities(query: str = "", cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or {}
    client = DreamSpaceClient(web_token=cfg.get("dreamspace_web_token", ""))
    try:
        return client.search(keyword=str(query or "").strip(), page_size=8, include_details=True)
    finally:
        client.close()


def format_activities(result: Dict[str, Any]) -> str:
    if not result.get("available"):
        return result.get("message") or "暂时无法查询到梦空间活动。"
    rows = result.get("results") or []
    if not rows:
        return "到梦空间实时活动查询没有找到匹配活动。可以换一个活动名称、主办单位或关键词再查。"
    lines = [f"到梦空间实时查到 {len(rows)} 个活动（查询于 {result.get('fetched_at', '刚刚')}）："]
    for index, row in enumerate(rows, 1):
        period = " ~ ".join(x for x in (row.get("start"), row.get("end")) if x) or "时间未标注"
        lines.append(f"\n{index}. **{row.get('title', '未命名活动')}**\n   时间：{period}")
        if row.get("organizer"):
            lines.append(f"   承办单位：{row['organizer']}")
        if row.get("abstract"):
            lines.append(f"   摘要：{row['abstract'][:180]}")
        lines.append(f"   详情：{row.get('source_url', '')}")
    lines.append("\n报名资格、报名入口和个人可见范围请打开详情链接，以到梦空间页面为准。")
    if not result.get("authenticated"):
        lines.append("当前查询使用到梦空间公开活动列表；账号专属活动需配置有效登录会话后才能显示。")
    return "\n".join(lines)

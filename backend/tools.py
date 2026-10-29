"""工具层：课表/成绩/计算器/时间/知识检索/通知检索/记忆读写

含 OpenAI function calling 格式的工具清单（TOOL_SCHEMAS）与执行入口 run_tool()。
校历与通知检索已入库资料；个人教务功能尚未接入真实系统。
"""
import ast
import json
import operator
import re
import services
import dining
import chat_panels
import maps
import campus_profiles
import dreamspace
import web_search
import config as cfgmod
from retrieval_rules import topic_labels
from datetime import datetime, timedelta

# ---------------- 工具实现 ----------------

WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "天": 7, "日": 7}

# 供 RAG / 记忆管理器注入（由 main.py 启动时设置）
_ctx = {"rag": None, "memory": None}


def set_context(rag, memory):
    _ctx["rag"] = rag
    _ctx["memory"] = memory


def _unavailable(subject):
    return {"available": False, "message": f"未接入真实{subject}，请通过学校官方系统查询。"}


def _coverage_guidance(query):
    """资料未命中时给出下一步，不把缺口误说成学校没有规定。"""
    try:
        cfg = maps.config.load_config()
        profile = campus_profiles.get_profile(cfg.get("current_school_id", "hlby"), cfg) or {}
    except Exception:
        profile = {}
    school = profile.get("name") or "当前学校"
    website = profile.get("website") or ""
    topics = topic_labels(query)
    topic_text = "、".join(topics) if topics else "这个问题"
    next_steps = [
        f"确认当前学校是{school}，并补充学院、年级、学期或通知年份等范围",
        f"把问题改成更具体的关键词，例如“{topic_text}+办理入口/截止日期/咨询电话”",
        "如果你有学校官网通知、办事指南或文件链接，可以提供给我，整理后再纳入当前学校知识库；也可以先按学校官方页面核对",
    ]
    if website:
        next_steps.append(f"先到当前学校官网核对最新原文：{website}")
    else:
        next_steps.append("当前学校尚未配置官网地址，请先补充官网后再做官方核对")
    return {
        "coverage": {"school": school, "topics": topics, "direct_evidence": False},
        "next_steps": next_steps,
        "message": f"当前{school}知识库没有找到可直接支持“{query}”的正式资料。这个结果只说明资料未覆盖，不能据此判断学校没有相关规定。",
    }


def _document_search(query):
    if not _ctx["rag"]:
        return {"available": False, "query": query, "results": [], "count": 0,
                "message": "知识库未初始化，暂时无法查询。"}
    result = search_knowledge(query)
    result["available"] = True
    if not result["results"]:
        result.update(_coverage_guidance(query))
    return result


def query_course_schedule(weekday=None):
    return _unavailable("个人课表")


def query_grades(semester=None):
    return _unavailable("个人成绩")


_ALLOWED_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calc(node):
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BIN:
        return _ALLOWED_BIN[type(node.op)](_calc(node.left), _calc(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_calc(node.operand))
    raise ValueError("不支持的表达式元素")


def calculator(expression):
    """安全数学计算（AST 白名单，禁止任意代码）"""
    if not expression or not re.fullmatch(r"[0-9+\-*/%().\s]+", str(expression)):
        raise ValueError("表达式仅支持数字与 + - * / % ( ) 运算符")
    result = _calc(ast.parse(str(expression), mode="eval"))
    return {"expression": str(expression), "result": round(result, 10)}


def get_current_time():
    dt = datetime.now()
    return {"datetime": dt.strftime("%Y-%m-%d %H:%M:%S"), "weekday": WEEKDAY_CN[dt.weekday() + 1]}


def query_academic_calendar():
    """返回知识库中的校历原文片段，不推造结构化日期。"""
    result = _document_search("校历")
    result["note"] = "来自已入库资料，并非实时接口；核对学年学期，后续调整以学校最新通知为准。"
    return result


def search_knowledge(query):
    if not _ctx["rag"]:
        raise ValueError("知识库未初始化")
    results = _ctx["rag"].search(query, top_k=4)
    result = {"query": query, "results": results, "count": len(results)}
    if not results:
        result.update(_coverage_guidance(query))
    return result


def search_campus_notice(keyword):
    """从已入库资料检索通知，不再返回内置模拟公告。"""
    result = _document_search(str(keyword or "").strip() or "通知公告")
    return {"keyword": keyword, "matches": result["results"], "count": result["count"],
            "available": result["available"], "message": result.get("message", ""),
            "next_steps": result.get("next_steps", []), "coverage": result.get("coverage", {}),
            "note": "知识库检索结果，并非实时通知列表；请核对原文标题、发布日期和适用范围。"}


def search_dreamspace_activities(query=""):
    """实时查询到梦空间活动；令牌只由服务端配置读取，不进入工具结果。"""
    import config as cfgmod
    return dreamspace.search_activities(str(query or "").strip(), cfgmod.load_config())


def save_memory(content, category="通用"):
    if not content or not str(content).strip():
        raise ValueError("记忆内容不能为空")
    r = _ctx["memory"].add(str(content).strip(), category or "通用", source="对话中保存")
    return {"saved": True, "id": r.get("id"), "duplicated": r.get("duplicated", False)}


def search_memory(query):
    if not _ctx["memory"]:
        raise ValueError("记忆系统未初始化")
    results = _ctx["memory"].search(query, top_k=3)
    return {"query": query, "results": results, "count": len(results)}


# ---------------- GPA 计算 / DDL 倒计时（与 campus_tools_mcp 同步） ----------------

def _score_to_gp(score, rule="standard4"):
    """百分制 → 绩点：standard4 四点制 / wq5 五级制（区间端点含）"""
    s = float(score)
    if s >= 60:
        if rule == "wq5":
            if s >= 90:
                return 4.5
            if s >= 80:
                return 3.5
            if s >= 70:
                return 2.5
            return 1.5
        if s >= 90:
            return 4.0
        if s >= 85:
            return 3.7
        if s >= 80:
            return 3.3
        if s >= 75:
            return 3.0
        if s >= 70:
            return 2.7
        if s >= 65:
            return 2.3
        return 2.0
    return 0.0


def calculate_gpa(courses, rule="standard4"):
    """计算 GPA：加权绩点、加权均分、总学分、最优最差课"""
    if not isinstance(courses, list) or not courses:
        raise ValueError("courses 需为非空列表，每项含 name/credit/score")
    if rule not in ("standard4", "wq5"):
        raise ValueError("rule 仅支持 standard4 或 wq5")
    details, total_credits, gp_sum, score_sum = [], 0.0, 0.0, 0.0
    for c in courses:
        try:
            credit, score = float(c["credit"]), float(c["score"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("每门课需含数字类型的 credit 与 score 字段")
        if not (0 < credit <= 20) or not (0 <= score <= 100):
            raise ValueError("credit 需在 (0,20]，score 需在 [0,100]")
        gp = _score_to_gp(score, rule)
        details.append({"name": str(c.get("name", "未命名")), "credit": credit, "score": score, "gp": gp})
        total_credits += credit
        gp_sum += gp * credit
        score_sum += score * credit
    best = max(details, key=lambda d: d["gp"])
    worst = min(details, key=lambda d: d["gp"])
    rule_note = ("standard4: 90-100→4.0, 85-89→3.7, 80-84→3.3, 75-79→3.0, 70-74→2.7, 65-69→2.3, 60-64→2.0, <60→0"
                 if rule == "standard4"
                 else "wq5: 90-100→4.5, 80-89→3.5, 70-79→2.5, 60-69→1.5, <60→0")
    return {
        "rule": rule,
        "gpa": round(gp_sum / total_credits, 4),
        "weighted_avg_score": round(score_sum / total_credits, 2),
        "total_credits": round(total_credits, 2),
        "course_count": len(details),
        "best": {"name": best["name"], "gp": best["gp"]},
        "worst": {"name": worst["name"], "gp": worst["gp"]},
        "details": details,
        "rule_note": rule_note,
    }


def countdown_deadlines(items, warn_days=7, include_calendar=True):
    """DDL 倒计时：个人事项 + 可选校历节点，urgent 预警"""
    from datetime import date as _date
    if not isinstance(items, list) or not items:
        raise ValueError("items 需为非空列表，每项含 title 与 date(YYYY-MM-DD)")
    warn_days = int(warn_days)
    today = _date.today()

    def rows_of(raw_items):
        rows = []
        for it in raw_items:
            d_str = str(it.get("date", "")).split("~")[0].strip()
            try:
                d = _date.fromisoformat(d_str)
            except ValueError:
                continue
            days = (d - today).days
            if days < -30:
                continue
            if days < 0:
                status, left = "已过期", f"已过 {-days} 天"
            elif days == 0:
                status, left = "今天截止", "剩 0 天"
            elif days <= warn_days:
                status, left = "urgent", f"剩 {days} 天"
            else:
                status, left = "normal", f"剩 {days} 天"
            rows.append({"title": str(it.get("title", "未命名")), "deadline": d_str,
                         "days_left": days, "status": status, "left": left})
        rows.sort(key=lambda r: r["days_left"])
        return rows

    item_rows = rows_of(items)
    # 尚无核验后的结构化校历，不能把原文片段当作确定的截止日期。
    cal_rows = []

    def count(rs):
        return {
            "urgent": sum(1 for r in rs if r["status"] == "urgent"),
            "normal": sum(1 for r in rs if r["status"] == "normal"),
            "expired": sum(1 for r in rs if r["status"] in ("已过期", "今天截止")),
        }

    return {
        "today": today.strftime("%Y-%m-%d"),
        "warn_days": warn_days,
        "items": item_rows,
        "calendar": cal_rows,
        "calendar_note": "未接入经核验的结构化校历；请用校历查询查看原文。" if include_calendar else "未请求校历节点。",
        "summary": {"items": count(item_rows), "calendar": count(cal_rows)},
    }


# ---------------- 空教室查询 / 排课冲突检测 / 学分审计 ----------------

def _slot_of(time_range):
    """时间段 → 上午/下午/晚上（按开始时刻判定）"""
    start = str(time_range).split("-")[0].strip()
    try:
        h = int(start.split(":")[0])
    except (ValueError, IndexError):
        return "未知"
    if h < 12:
        return "上午"
    if h < 18:
        return "下午"
    return "晚上"


def query_empty_classroom(weekday, slot="全部"):
    return _unavailable("教室占用")


def _parse_time_range(tr):
    """'HH:MM-HH:MM' → (分钟起, 分钟止)；不合法返回 None"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", str(tr).strip())
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    s, e = h1 * 60 + m1, h2 * 60 + m2
    if s >= e or not (0 <= s < 24 * 60 and 0 <= e <= 24 * 60):
        return None
    return s, e


def detect_schedule_conflict(candidates):
    return _unavailable("个人课表，无法判断排课冲突")


def _fmt_min(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def daily_brief(include_schedule=True, include_notices=True, notice_count=3):
    """聚合真实可用信息；个人课表未接入时不冒充无课。"""
    dt = datetime.now()
    payload = {"date": dt.strftime("%Y-%m-%d"), "weekday": WEEKDAY_CN[dt.weekday() + 1],
               "time": dt.strftime("%H:%M"), "calendar": query_academic_calendar()}
    if include_schedule:
        payload["schedule_available"] = False
        payload["schedule_message"] = "未接入真实个人课表，请查询教务系统。"
    if include_notices:
        notices = search_campus_notice("")
        payload["notice_search_results"] = notices["matches"][:max(1, min(int(notice_count), 8))]
        payload["notice_note"] = notices["note"]
    return payload


def audit_credits(completed_courses):
    return _unavailable("所在专业的真实培养方案，无法审计毕业学分")


# ---------------- 工具清单（OpenAI function calling 格式） ----------------

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "query_course_schedule",
        "description": "个人课表尚未接入；仅返回未接入说明，不能查询个人课程安排。",
        "parameters": {"type": "object", "properties": {
            "weekday": {"type": "string", "description": "星期，如 '今天'、'明天'、'周一'~'周日' 或 '1'~'7'，留空返回整周"}
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "query_grades",
        "description": "个人成绩尚未接入；仅返回未接入说明，不能查询学生成绩。",
        "parameters": {"type": "object", "properties": {
            "semester": {"type": "string", "description": "学期，如 '2025-2026-1'，留空返回全部"}
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "calculator",
        "description": "数学计算器，支持加减乘除、乘方、取模、括号。需要精确计算时务必使用。",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "数学表达式，如 (3.5*2+10)/4"}
        }, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "get_current_time",
        "description": "获取当前日期时间和星期。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "query_academic_calendar",
        "description": "检索已入库校历原文片段；必须核对学年学期，并以学校后续通知为准。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "检索校园知识库（校规校纪、教务规定、校园生活、奖助学金等文档），查找资料依据（须核对原文适用范围）。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词或问题"}
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "search_campus_notice",
        "description": "检索已入库的校园通知资料，结果附来源；并非实时通知列表。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "通知关键词，如 '奖学金'"}
        }, "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "save_memory",
        "description": "把学生透露的重要个人信息/偏好/安排保存到长期记忆（如专业年级、考试时间、饮食偏好），下次对话仍可记得。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "要记住的事实，一句话"},
            "category": {"type": "string", "description": "分类：学生背景/重要事项/偏好/通用"}
        }, "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "检索学生的长期记忆，回顾之前聊过的重要信息。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索关键词"}
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "calculate_gpa",
        "description": "GPA 绩点计算器：输入各门课程（课程名/学分/百分制成绩），加权计算 GPA、加权平均分、总学分与最优最差课程。rule 可选 'standard4'（4.0制，默认）或 'wq5'（五级制）。",
        "parameters": {"type": "object", "properties": {
            "courses": {"type": "array", "description": "课程列表", "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "课程名"},
                "credit": {"type": "number", "description": "学分，如 4.0"},
                "score": {"type": "number", "description": "百分制成绩 0~100"}
            }, "required": ["name", "credit", "score"]}},
            "rule": {"type": "string", "enum": ["standard4", "wq5"], "description": "绩点规则，默认 standard4"}
        }, "required": ["courses"]}}},
    {"type": "function", "function": {
        "name": "countdown_deadlines",
        "description": "DDL 倒计时：输入待办截止事项（名称+日期 YYYY-MM-DD），计算剩余天数并标记 urgent 预警（≤warn_days 天，默认7）；可同时返回校历节点（考试周/假期）倒计时。",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "description": "截止事项列表", "items": {"type": "object", "properties": {
                "title": {"type": "string", "description": "事项名称"},
                "date": {"type": "string", "description": "截止日期 YYYY-MM-DD，或日期段 YYYY-MM-DD ~ YYYY-MM-DD"}
            }, "required": ["title", "date"]}},
            "warn_days": {"type": "integer", "description": "预警阈值天数，默认 7"},
            "include_calendar": {"type": "boolean", "description": "是否包含校历节点倒计时，默认 true"}
        }, "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "query_empty_classroom",
        "description": "教室占用尚未接入，仅返回未接入说明，无法判断空闲教室。",
        "parameters": {"type": "object", "properties": {
            "weekday": {"type": "string", "description": "星期：'今天'、'明天'、'周一'~'周日' 或 '1'~'7'"},
            "slot": {"type": "string", "enum": ["全部", "上午", "下午", "晚上"], "description": "时段，默认 '全部'"}
        }, "required": ["weekday"]}}},
    {"type": "function", "function": {
        "name": "detect_schedule_conflict",
        "description": "个人课表尚未接入，仅返回未接入说明，无法判断排课冲突。",
        "parameters": {"type": "object", "properties": {
            "candidates": {"type": "array", "description": "候选课程列表", "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "课程名"},
                "weekday": {"type": "string", "description": "星期，如 '周三' 或 '3'"},
                "time": {"type": "string", "description": "时间段 HH:MM-HH:MM，如 '14:00-15:40'"}
            }, "required": ["name", "weekday", "time"]}},
        }, "required": ["candidates"]}}},
    {"type": "function", "function": {
        "name": "audit_credits",
        "description": "真实培养方案尚未接入，仅返回未接入说明，无法判断毕业学分缺口。",
        "parameters": {"type": "object", "properties": {
            "completed_courses": {"type": "array", "description": "已修课程列表", "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "课程名"},
                "credit": {"type": "number", "description": "学分"},
                "category": {"type": "string", "enum": ["通识必修", "专业必修", "专业选修", "实践教学", "个性发展"], "description": "培养方案类别"}
            }, "required": ["name", "credit", "category"]}},
        }, "required": ["completed_courses"]}}},
    {"type": "function", "function": {
        "name": "daily_brief",
        "description": "聚合日期、校历与通知知识库检索结果；个人课表未接入，不提供课程安排或实时最新通知。",
        "parameters": {"type": "object", "properties": {
            "include_schedule": {"type": "boolean", "description": "是否包含今日课表，默认 true"},
            "include_notices": {"type": "boolean", "description": "是否包含最新通知，默认 true"},
            "notice_count": {"type": "integer", "description": "通知条数，默认 3，范围 1~8"}
        }, "required": []}}},
]


TOOL_SCHEMAS.extend([
    {"type": "function", "function": {
        "name": "search_dreamspace_activities",
        "description": "实时查询到梦空间活动列表和活动详情。用户询问讲座、竞赛、志愿活动、报名截止时间或最近有什么活动时使用；结果带官网链接和查询时间，不把历史知识库资料冒充实时活动。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "活动名称、主办单位或关键词；没有具体关键词时传空字符串"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "web_search_latest",
        "description": "仅在知识库检索为空，或用户问题包含最新/今天/本周/刚刚/通知/调整等时效词时调用。只返回最近 days 天内的结果。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "联网搜索关键词或问题"},
            "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 30, "description": "只保留最近多少天的结果"},
            "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10, "description": "最多返回条数"}
        }, "required": ["query"]}
    }},
    {"type":"function","function":{
        "name":"search_campus_places",
        "description":"查询校内地点或学校周边餐馆、咖啡、商店等，返回高德地址、评分、人均、来源和导航。校内同时检索本地知识。scope=campus查校内名称，nearby以学校为中心，city查配置城市。周边不等于已确认校外；距离为直线距离。评分不是用户评论，缺失不能编造；sort=rating只对本次候选排序。",
        "parameters":{"type":"object","properties":{"query":{"type":"string","description":"具体地点名或类别，1—80字"},"scope":{"type":"string","enum":["campus","nearby","city"]},"radius":{"type":"integer","minimum":100,"maximum":50000},"sort":{"type":"string","enum":["distance","rating"]},"limit":{"type":"integer","minimum":1,"maximum":10},"origin_query":{"type":"string","description":"路线起点，如南门、一食堂；无法确认时回退学校中心点并明确提示"}},"required":["query"]}
    }},
    {"type":"function","function":{
        "name":"get_campus_place_detail","description":"通过已返回的高德POI编号获取当前地点详情和评分；不提供评论正文，不编造评论。",
        "parameters":{"type":"object","properties":{"poi_id":{"type":"string"}},"required":["poi_id"]}
    }},
    {"type": "function", "function": {
        "name": "open_chat_panel",
        "description": "用户要查看或管理知识库、长期记忆、学习审核、设置或办事目录时，在当前对话打开操作卡片。具体校园问题仍使用检索工具回答。不要让用户在聊天中发送 API Key。",
        "parameters": {"type": "object", "properties": {"panel": {"type": "string", "enum": ["services", "knowledge", "memory", "learning", "settings"]}}, "required": ["panel"]}
    }},
    {"type":"function","function":{
        "name":"recommend_campus_food", "description":"从已收录食堂菜单随机推荐3至5道正餐，返回菜名、原始价格、食堂楼层档口和来源；候选不足则如实返回。用户问吃什么、求食堂推荐时使用。",
        "parameters":{"type":"object","properties":{"query":{"type":"string","description":"用户的吃饭需求，可含食堂、楼层和预算；不要删去用户条件。"}},"required":["query"]}}},
    {"type": "function", "function": {
        "name": "find_campus_contact", "description": "按部门、学院或岗位名称查官网办公电话簿，返回原文电话、办公地址与来源。查询联系方式时优先使用，不推断这些联系人就是审批人。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "部门、学院或岗位，如学籍科、应用科技学院"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_campus_service", "description": "获取校园办事导航：相关原文、咨询联系人、尚待核实的细则。没有资料时不能补造步骤。",
        "parameters": {"type": "object", "properties": {"service_id": {"type": "string", "enum": [row["id"] for row in services.SERVICES]}, "college": {"type": "string", "description": "学生已提供学院名称时填写，附上该学院的官方咨询联系人"}}, "required": ["service_id"]}}},
])


def run_tool(name, args):
    """执行工具，返回 dict 结果；未知工具或参数错误抛异常（由调用方转为观察结果）"""
    args = args or {}
    if name == "search_campus_places":
        return maps.search_places(args.get("query",""),args.get("scope","nearby"),args.get("radius"),args.get("sort","distance"),args.get("limit",5),origin_query=args.get("origin_query",""),rag=_ctx["rag"])
    if name == "search_dreamspace_activities":
        return search_dreamspace_activities(args.get("query", ""))
    if name == "web_search_latest":
        cfg = cfgmod.load_config()
        days = args.get("days", cfg.get("web_search_default_days", 7))
        return web_search.search_latest_clean(
            str(args.get("query", "")),
            days=int(days),
            max_results=int(args.get("max_results", 5)),
        )
    if name == "get_campus_place_detail":
        return maps.AmapClient(maps.config.load_config()).detail(args.get("poi_id",""))
    if name == "open_chat_panel":
        return chat_panels.open_panel(args.get("panel"))
    if name == 'recommend_campus_food':
        return dining.recommend_food(args.get('query',''),args.get('exclude_names',()))
    if name == "find_campus_contact":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("请输入部门、学院或岗位名称")
        results = services.list_contacts(query)
        result = {"query": query, "results": results, "count": len(results), "note": "官网电话簿中的咨询联系人，不等于审批人；外线拨打方式请核对学校说明。"}
        if not results:
            result["message"] = "当前学校电话簿没有命中这个部门或岗位，不能据此判断学校没有对应联系人。"
            result["next_steps"] = ["尝试使用部门全称、学院全称或岗位名称", "补充所在学院和办理事项，我可以继续缩小查询范围", "以学校官网最新电话簿或该事项页面为准"]
        return result
    if name == "get_campus_service":
        if not _ctx["rag"]:
            raise ValueError("知识库未初始化")
        result = services.service_detail(str(args.get("service_id", "")), _ctx["rag"], str(args.get("college", "")))
        if result is None:
            raise ValueError("未找到该办事事项")
        return result
    if name == "query_course_schedule":
        return query_course_schedule(args.get("weekday"))
    if name == "query_grades":
        return query_grades(args.get("semester"))
    if name == "calculator":
        return calculator(args.get("expression"))
    if name == "get_current_time":
        return get_current_time()
    if name == "query_academic_calendar":
        return query_academic_calendar()
    if name == "search_knowledge":
        return search_knowledge(args.get("query", ""))
    if name == "search_campus_notice":
        return search_campus_notice(args.get("keyword", ""))
    if name == "save_memory":
        return save_memory(args.get("content", ""), args.get("category", "通用"))
    if name == "search_memory":
        return search_memory(args.get("query", ""))
    if name == "calculate_gpa":
        return calculate_gpa(args.get("courses", []), args.get("rule", "standard4"))
    if name == "countdown_deadlines":
        return countdown_deadlines(args.get("items", []), args.get("warn_days", 7),
                                   args.get("include_calendar", True))
    if name == "query_empty_classroom":
        return query_empty_classroom(args.get("weekday"), args.get("slot", "全部"))
    if name == "detect_schedule_conflict":
        return detect_schedule_conflict(args.get("candidates", []))
    if name == "audit_credits":
        return audit_credits(args.get("completed_courses", []))
    if name == "daily_brief":
        return daily_brief(args.get("include_schedule", True), args.get("include_notices", True),
                           args.get("notice_count", 3))
    raise ValueError(f"未知工具: {name}")

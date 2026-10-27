#!/usr/bin/env python3
"""
campus_tools_mcp - 校园工具 MCP 服务器

为 LLM 提供校园学生场景的只读工具：安全计算器、
当前时间、学期校历、校园通知检索。（课表/成绩查询工具已移除，
待接入真实教务系统后重建。）校历与通知数据源尚未接入，调用时明确返回不可用状态。

传输方式：StreamableHTTP（默认 http://127.0.0.1:8310/mcp）
启动：python server.py
"""

import ast
import json
import operator
import re
from datetime import date, datetime, timedelta
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from mcp.server.fastmcp import FastMCP

WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

HOST = "127.0.0.1"
PORT = 8310

# ============================================================
# 服务器初始化
# ============================================================

mcp = FastMCP("campus_tools_mcp", host=HOST, port=PORT)


def _dumps(obj) -> str:
    """统一的 JSON 序列化输出（中文原样输出）。"""
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ============================================================
# 输入模型
# ============================================================

class CalculatorInput(BaseModel):
    """计算器输入。"""
    model_config = ConfigDict(str_strip_whitespace=True)

    expression: str = Field(
        ...,
        description="数学表达式，支持 + - * / % // ** 与括号，如 '(3.5*2+10)/4'",
        min_length=1,
        max_length=200,
    )


class NoticeInput(BaseModel):
    """通知检索输入。"""
    model_config = ConfigDict(str_strip_whitespace=True)

    keyword: str = Field(
        ...,
        description="通知关键词，将在标题/正文/分类中匹配，如 '奖学金'、'停水'、'疫苗'",
        min_length=1,
        max_length=50,
    )


class CourseScore(BaseModel):
    """单门课程成绩条目（GPA 计算用）。"""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        ...,
        description="课程名称，如 '数据结构'",
        min_length=1,
        max_length=50,
    )
    credit: float = Field(
        ...,
        description="课程学分，如 4.0；范围 0.1~20",
        gt=0,
        le=20,
    )
    score: float = Field(
        ...,
        description="百分制成绩，如 92；范围 0~100（百分制，非等级制）",
        ge=0,
        le=100,
    )


class GpaInput(BaseModel):
    """GPA 计算输入。"""
    model_config = ConfigDict(str_strip_whitespace=True)

    courses: List[CourseScore] = Field(
        ...,
        description="课程成绩列表，每项含 name/credit/score；至少 1 门、至多 60 门",
        min_length=1,
        max_length=60,
    )
    rule: str = Field(
        default="standard4",
        description="绩点换算规则：'standard4' = 4.0 制（90-100→4.0，85-89→3.7，80-84→3.3，75-79→3.0，70-74→2.7，65-69→2.3，60-64→2.0，<60→0）；'wq5' = 五级制（优秀→4.5，良好→3.5，中等→2.5，及格→1.5，不及格→0）",
        pattern=r"^(standard4|wq5)$",
    )


class DeadlineItem(BaseModel):
    """单个截止事项。"""
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(
        ...,
        description="事项名称，如 '数据结构实验报告'、'奖学金申请'",
        min_length=1,
        max_length=60,
    )
    date: str = Field(
        ...,
        description="截止日期 YYYY-MM-DD（含）或日期段 YYYY-MM-DD ~ YYYY-MM-DD（取起始日倒计）",
        pattern=r"^\d{4}-\d{2}-\d{2}(\s*~\s*\d{4}-\d{2}-\d{2})?$",
    )


class DeadlineInput(BaseModel):
    """DDL 倒计时输入。"""
    model_config = ConfigDict(str_strip_whitespace=True)

    items: List[DeadlineItem] = Field(
        ...,
        description="截止事项列表，每项含 title/date；至多 30 项",
        min_length=1,
        max_length=30,
    )
    warn_days: int = Field(
        default=7,
        description="预警阈值（天）：剩余天数 ≤ 该值时标记 urgent；默认 7，范围 1~30",
        ge=1,
        le=30,
    )
    include_calendar: bool = Field(
        default=True,
        description="是否同时返回校历关键节点倒计时（期中/期末考试周、假期等）；默认 true",
    )


# ============================================================
# 工具定义
# ============================================================

_ALLOWED_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calc(node):
    """AST 白名单求值（禁止任意代码执行）。"""
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BIN:
        return _ALLOWED_BIN[type(node.op)](_calc(node.left), _calc(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_calc(node.operand))
    raise ValueError("不支持的表达式元素")


@mcp.tool(
    name="campus_calculator",
    annotations={
        "title": "数学计算器",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def campus_calculator(params: CalculatorInput) -> str:
    """安全数学计算器（AST 白名单求值）。

    支持 + - * / % // ** 与括号的四则运算，拒绝任何非数学字符，
    需要精确数值计算时务必使用本工具而非心算。

    Args:
        params (CalculatorInput): 计算参数：
            - expression (str): 数学表达式，如 '(3.5*2+10)/4'

    Returns:
        str: JSON 字符串 {"expression": "...", "result": 4.25}；
        非法表达式返回 "Error: 表达式仅支持数字与 + - * / % ( ) 等运算符"。

    Examples:
        - 使用场景："算一下 (3.5*2+10)/4 等于多少" -> expression='(3.5*2+10)/4'
        - 使用场景："2 的 10 次方" -> expression='2**10'
        - 不适用：含变量或函数的符号运算
    """
    if not re.fullmatch(r"[0-9+\-*/%().\s]+", params.expression):
        return "Error: 表达式仅支持数字与 + - * / % ( ) 等运算符"
    try:
        result = _calc(ast.parse(params.expression, mode="eval"))
        return _dumps({"expression": params.expression, "result": round(result, 10)})
    except Exception as e:
        return f"Error: 表达式无法计算（{type(e).__name__}），请检查格式"


@mcp.tool(
    name="campus_get_current_time",
    annotations={
        "title": "获取当前时间",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def campus_get_current_time() -> str:
    """获取当前日期、时间与星期。

    用于回答"今天几号""现在星期几"这类时间基准问题，
    也是计算"明天/后天"等相对日期的依据。

    Returns:
        str: JSON 字符串 {"datetime": "2026-10-25 14:30:00", "weekday": "周日"}。

    Examples:
        - 使用场景："今天星期几？" -> 直接调用
        - 使用场景："离期末考试还有几天" -> 先调用本工具得到今天日期，再结合校历计算
    """
    dt = datetime.now()
    return _dumps({"datetime": dt.strftime("%Y-%m-%d %H:%M:%S"), "weekday": WEEKDAY_CN[dt.weekday() + 1]})


@mcp.tool(
    name="campus_query_academic_calendar",
    annotations={
        "title": "查询学期校历",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def campus_query_academic_calendar() -> str:
    """结构化校历未接入；请在校园助手网页查询已入库校历原文。"""
    return _dumps({"available": False, "today": date.today().isoformat(), "upcoming": [], "all_nodes": [],
                   "message": "未接入真实结构化校历；请通过校园助手知识库或学校官网查询。"})


@mcp.tool(
    name="campus_search_notice",
    annotations={
        "title": "检索校园通知",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def campus_search_notice(params: NoticeInput) -> str:
    """通知数据源未接入，不返回模拟通知或虚构发布日期。"""
    return _dumps({"available": False, "keyword": params.keyword, "matches": [], "count": 0,
                   "message": "未接入真实通知数据源；请通过校园助手知识库或学校官网查询。"})


# ============================================================
# GPA 计算（共享辅助）
# ============================================================

def _score_to_gp(score: float, rule: str) -> float:
    """百分制成绩 → 绩点（按所选规则）。"""
    if score >= 60:
        if rule == "wq5":
            # 五级制（区间端点含）：90-100 优秀 4.5，80-89 良好 3.5，70-79 中等 2.5，60-69 及格 1.5
            if score >= 90:
                return 4.5
            if score >= 80:
                return 3.5
            if score >= 70:
                return 2.5
            return 1.5
        # standard4（区间端点含）
        if score >= 90:
            return 4.0
        if score >= 85:
            return 3.7
        if score >= 80:
            return 3.3
        if score >= 75:
            return 3.0
        if score >= 70:
            return 2.7
        if score >= 65:
            return 2.3
        return 2.0
    return 0.0


@mcp.tool(
    name="campus_calculate_gpa",
    annotations={
        "title": "GPA 绩点计算器",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def campus_calculate_gpa(params: GpaInput) -> str:
    """按用户所选通用规则计算 GPA（不代表学校官方绩点规则）。

    输入各门课程（课程名/学分/百分制成绩），按所选规则换算绩点后
    加权计算：GPA = Σ(课程绩点 × 学分) / Σ(学分)，同时给出
    加权平均分、总学分、最高/最低绩点课程与各科绩点明细。

    Args:
        params (GpaInput): 计算参数：
            - courses (List[CourseScore]): 每门课含 name/credit/score（百分制 0~100）
            - rule (str): 'standard4'（4.0 制，默认）或 'wq5'（五级制）

    Returns:
        str: JSON 字符串：
        {"rule": "standard4", "gpa": 3.31, "weighted_avg_score": 86.9, "total_credits": 22.5,
         "best": {"name": "Python程序设计", "gp": 4.0}, "worst": {"name": "概率论", "gp": 2.3},
         "details": [{"name": "...", "credit": 4.0, "score": 92, "gp": 4.0}, ...],
         "rule_note": "90-100→4.0, 85-89→3.7, ...（区间端点含）"}

    Examples:
        - 使用场景："帮我算下这学期绩点" -> courses=[{name:'数据结构',credit:4,score:92},...]
        - 使用场景："用五级制算" -> rule='wq5'
        - 不适用：真实教务系统的官方 GPA（以教务处为准，本工具是通用换算）
    """
    details = []
    for c in params.courses:
        gp = _score_to_gp(c.score, params.rule)
        details.append({"name": c.name, "credit": c.credit, "score": c.score, "gp": gp})
    total_credits = sum(c.credit for c in params.courses)
    gpa = sum(d["gp"] * d["credit"] for d in details) / total_credits
    weighted_avg = sum(c.score * c.credit for c in params.courses) / total_credits
    best = max(details, key=lambda d: d["gp"])
    worst = min(details, key=lambda d: d["gp"])
    rule_note = (
        "standard4: 90-100→4.0, 85-89→3.7, 80-84→3.3, 75-79→3.0, 70-74→2.7, 65-69→2.3, 60-64→2.0, <60→0（区间端点含）"
        if params.rule == "standard4"
        else "wq5: 90-100→4.5(优秀), 80-89→3.5(良好), 70-79→2.5(中等), 60-69→1.5(及格), <60→0（区间端点含）"
    )
    return _dumps({
        "rule": params.rule,
        "gpa": round(gpa, 4),
        "weighted_avg_score": round(weighted_avg, 2),
        "total_credits": round(total_credits, 2),
        "course_count": len(details),
        "best": {"name": best["name"], "gp": best["gp"]},
        "worst": {"name": worst["name"], "gp": worst["gp"]},
        "details": details,
        "rule_note": rule_note,
    })


# ============================================================
# DDL 倒计时（共享辅助）
# ============================================================

def _ddl_rows(items, warn_days: int, today: date) -> list:
    """把截止事项转换为倒计时行（跳过已过期 30 天以上的）。"""
    rows = []
    for it in items:
        start_str = it.date.split("~")[0].strip()
        try:
            d = date.fromisoformat(start_str)
        except ValueError:
            continue
        days = (d - today).days
        if days < -30:
            continue  # 过期太久的事项不展示
        if days < 0:
            status, left = "已过期", f"已过 {-days} 天"
        elif days == 0:
            status, left = "今天截止", "剩 0 天"
        elif days <= warn_days:
            status, left = "urgent", f"剩 {days} 天"
        else:
            status, left = "normal", f"剩 {days} 天"
        rows.append({"title": it.title, "deadline": start_str, "days_left": days, "status": status, "left": left})
    rows.sort(key=lambda r: r["days_left"])
    return rows


@mcp.tool(
    name="campus_countdown_deadlines",
    annotations={
        "title": "DDL 截止倒计时",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def campus_countdown_deadlines(params: DeadlineInput) -> str:
    """计算个人截止事项（DDL）的剩余天数，并可合并校历关键节点倒计时。

    输入你的待办截止事项（名称+截止日期），工具按今天日期计算每项
    剩余天数：≤预警阈值标记 urgent、当天标记"今天截止"、过期显示已过天数。
    结构化校历尚未接入，calendar 返回空列表并说明原因；个人事项仍按输入日期计算。

    Args:
        params (DeadlineInput): 倒计时参数：
            - items (List[DeadlineItem]): 每项含 title + date（YYYY-MM-DD 或日期段）
            - warn_days (int): urgent 阈值天数，默认 7
            - include_calendar (bool): 是否合并校历节点，默认 true

    Returns:
        str: JSON 字符串：
        {"today": "2026-10-25", "warn_days": 7,
         "items": [{"title": "...", "deadline": "...", "days_left": 15, "status": "normal", "left": "剩 15 天"}, ...],
         "calendar": [], "calendar_note": "未接入真实结构化校历",
         "summary": {"urgent": 1, "normal": 2, "expired": 0}}

    Examples:
        - 使用场景："我的 DDL 还有几天？实验报告 11月20日交，奖学金 10月30日截止" -> items=[{title:'数据结构实验报告',date:'2026-11-20'},{title:'奖学金申请',date:'2026-10-30'}]
        - 使用场景："别带校历了，只看我的待办" -> include_calendar=false
        - 不适用：周期性重复任务（每次需要重新输入）
    """
    from datetime import date as _date
    today = _date.today()
    item_rows = _ddl_rows(params.items, params.warn_days, today)
    cal_rows = []

    def _count(rows):
        return {
            "urgent": sum(1 for r in rows if r["status"] == "urgent"),
            "normal": sum(1 for r in rows if r["status"] == "normal"),
            "expired": sum(1 for r in rows if r["status"] in ("已过期", "今天截止")),
        }

    return _dumps({
        "today": today.strftime("%Y-%m-%d"),
        "warn_days": params.warn_days,
        "items": item_rows,
        "calendar": cal_rows,
        "calendar_note": "未接入真实结构化校历，未合并校历节点。" if params.include_calendar else "未请求校历节点。",
        "summary": {
            "items": _count(item_rows),
            "calendar": _count(cal_rows),
        },
    })


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    print(f"campus_tools_mcp 启动：http://{HOST}:{PORT}/mcp （StreamableHTTP）")
    mcp.run(transport="streamable-http")

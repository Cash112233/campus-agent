"""校园小助手 - FastAPI 入口

启动：python main.py（或运行项目根目录的 启动.bat）
访问：http://127.0.0.1:8300
"""
import json
import logging
import os
import secrets
import threading
import webbrowser
from urllib.parse import urlparse
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import config as cfgmod
import db
import tools as toolbox
import services
import maps
import campus_profiles
import school_crawler
import seed_knowledge
import auth
import web_search
from agent import Agent
from learning import LearningEngine
from llm import LLMClient
from memory import MemoryManager
from rag import RAG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_PATH = os.path.join(BASE_DIR, "..", "frontend", "index.html")

app = FastAPI(title="校园小助手")
logger = logging.getLogger("campus_agent")
MAX_API_REQUEST_BYTES = 2 * 1024 * 1024


def _safe_error(error, cfg=None):
    """返回可展示给客户端的脱敏、截断错误信息。"""
    message = str(error or "未知错误")
    settings = cfg or {}
    for key in ("api_key", "amap_key", "access_token"):
        secret = str(settings.get(key) or "")
        if secret:
            message = message.replace(secret, "[已隐藏]")
    return message[:300]


def _validate_base_url(value):
    """限制模型请求地址，避免把 API Key 发到任意地址。"""
    text = str(value or "").strip().rstrip("/")
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
    except ValueError as error:
        raise ValueError("base_url 不是有效的 URL") from error
    local_http = parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not local_http:
        raise ValueError("base_url 必须使用 HTTPS（本机 localhost 可使用 HTTP）")
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url 不得包含认证信息、查询参数或片段")
    return text


def _current_school_id(request=None):
    user = getattr(getattr(request, "state", None), "user", None) if request is not None else None
    if user and user.get("school_id"):
        return str(user["school_id"])
    return str(cfgmod.load_config().get("current_school_id") or "hlby")


def _user_cfg(request=None):
    cfg = cfgmod.load_config()
    school_id = _current_school_id(request)
    if school_id != cfg.get("current_school_id"):
        try:
            cfg = campus_profiles.apply_profile(cfg, school_id)
        except ValueError:
            pass
    return cfg


def _current_user_id(request=None):
    user = getattr(getattr(request, "state", None), "user", None) if request is not None else None
    try:
        user_id = int(user.get("id")) if user and user.get("id") else None
    except (TypeError, ValueError):
        user_id = None
    return user_id if user_id and user_id > 0 else None


def _require_admin(request):
    """系统级配置和学校目录只能由管理员修改；未启用账号认证时保留兼容模式。"""
    user = getattr(getattr(request, "state", None), "user", None) if request is not None else None
    if user and user.get("role") != "admin" and user.get("auth_type") != "access_token":
        return JSONResponse({"ok": False, "error": "只有管理员可以修改系统配置"}, status_code=403)
    return None


def _conversation_belongs_to_current_school(conversation_id, request=None):
    sql = "SELECT 1 FROM conversations WHERE id=? AND school_id=?"
    params = [conversation_id, _current_school_id(request)]
    user_id = _current_user_id(request)
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    row = db.get_conn().execute(sql, tuple(params)).fetchone()
    return bool(row)


def _learned_belongs_to_current_school(learned_id, request=None):
    sql = "SELECT 1 FROM learned l JOIN conversations c ON c.id=l.conversation_id WHERE l.id=? AND c.school_id=?"
    params = [learned_id, _current_school_id(request)]
    user_id = _current_user_id(request)
    if user_id is not None:
        sql += " AND c.user_id=?"
        params.append(user_id)
    row = db.get_conn().execute(sql, tuple(params)).fetchone()
    return bool(row)


def _message_belongs_to_current_school(message_id, request=None):
    sql = "SELECT 1 FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND c.school_id=?"
    params = [message_id, _current_school_id(request)]
    user_id = _current_user_id(request)
    if user_id is not None:
        sql += " AND c.user_id=?"
        params.append(user_id)
    row = db.get_conn().execute(sql, tuple(params)).fetchone()
    return bool(row)


@app.middleware("http")
async def request_size_guard(request: Request, call_next):
    """在解析 JSON 前拒绝异常大的 API 请求，避免上传体耗尽进程内存。"""
    if request.url.path.startswith("/api/"):
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > MAX_API_REQUEST_BYTES:
                    return JSONResponse({"error": "请求体过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "无效的请求长度"}, status_code=400)
    return await call_next(request)


# ---------- 访问口令（公网部署保护） ----------
@app.middleware("http")
async def access_guard(request: Request, call_next):
    """账号会话优先；兼容部署口令作为管理员后备认证。"""
    token = cfgmod.load_config().get("access_token") or ""
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/public/"):
        supplied_access = request.headers.get("x-access-token") or ""
        bearer = request.headers.get("authorization") or ""
        supplied_session = bearer[7:].strip() if bearer.lower().startswith("bearer ") else ""
        supplied_session = supplied_session or request.cookies.get("campus_session", "")
        user = auth.get_user_by_token(supplied_session)
        access_ok = bool(token and supplied_access and secrets.compare_digest(supplied_access, token))
        if user:
            request.state.user = user
        elif access_ok:
            request.state.user = {"id": 0, "username": "部署管理员", "role": "admin", "auth_type": "access_token"}
        elif auth.user_count():
            return JSONResponse({"error": "需要登录", "code": "AUTH_REQUIRED"}, status_code=401)
        elif token:
            return JSONResponse({"error": "需要访问口令", "code": "ACCESS_TOKEN_REQUIRED"}, status_code=401)
    return await call_next(request)

# ---------- 初始化 ----------
db.init_db()
seed_knowledge.seed_if_empty()   # 云端/首次启动：自动恢复知识库
rag = RAG()
memory = MemoryManager()
learning = LearningEngine(rag)
toolbox.set_context(rag, memory)
agent = Agent(rag, memory, learning, cfgmod.load_config)


# ---------- 页面 ----------
@app.get("/")
def index():
    return FileResponse(FRONTEND_PATH)


# ---------- 配置 ----------
class AuthReq(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class AccountReq(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


@app.get("/api/public/auth")
def auth_status():
    """前端启动时探测认证模式；不返回账号数量以外的敏感信息。"""
    count = auth.user_count()
    return {
        "required": bool(cfgmod.load_config().get("access_token")),
        "account_required": count > 0,
        "account_setup_required": count == 0,
    }


@app.post("/api/public/auth")
def auth_verify(req: AuthReq):
    """验证用户输入的口令"""
    token = cfgmod.load_config().get("access_token") or ""
    ok = bool(token) and secrets.compare_digest(req.token.strip(), token)
    return {"ok": ok}


def _set_session_cookie(response, token, request):
    response.set_cookie(
        "campus_session", token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )


@app.post("/api/public/register")
def register_account(req: AccountReq, request: Request):
    try:
        user = auth.register_first_user(req.username, req.password)
        token, _ = auth.create_session(user["id"])
    except ValueError as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
    response = JSONResponse({"ok": True, "user": user})
    _set_session_cookie(response, token, request)
    return response


@app.post("/api/public/login")
def login_account(req: AccountReq, request: Request):
    try:
        user = auth.authenticate(req.username, req.password)
        token, _ = auth.create_session(user["id"])
    except ValueError as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=401)
    response = JSONResponse({"ok": True, "user": user})
    _set_session_cookie(response, token, request)
    return response


def _request_session_token(request):
    bearer = request.headers.get("authorization") or ""
    return bearer[7:].strip() if bearer.lower().startswith("bearer ") else request.cookies.get("campus_session", "")


@app.get("/api/auth/me")
def current_account(request: Request):
    user = getattr(request.state, "user", None)
    if not user or user.get("auth_type") == "access_token":
        return {"authenticated": bool(user), "user": user}
    return {"authenticated": True, "user": user}


@app.post("/api/auth/logout")
def logout_account(request: Request):
    auth.revoke_token(_request_session_token(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie("campus_session")
    return response


@app.get("/api/config")
def get_config():
    cfg = cfgmod.load_config()
    key = cfg.get("api_key") or ""
    masked = (key[:4] + "****" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")
    return {
        "base_url": cfg["base_url"], "model": cfg["model"], "temperature": cfg["temperature"],
        "max_history": cfg.get("max_history", 20),
        "auto_learn": cfg.get("auto_learn", False),
        "auto_learn_threshold": cfg.get("auto_learn_threshold", 4),
        "has_key": bool(key), "masked_key": masked,
        "has_volcengine_key": bool(cfg.get("volcengine_api_key")),
        "masked_volcengine_key": ((cfg.get("volcengine_api_key")[:4] + "****" + cfg.get("volcengine_api_key")[-4:]) if len(cfg.get("volcengine_api_key") or "") > 8 else ("已设置" if cfg.get("volcengine_api_key") else "")),
        "web_search_default_days": cfg.get("web_search_default_days", 7),
    }


class ConfigReq(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=300)
    volcengine_api_key: Optional[str] = Field(default=None, max_length=300)
    base_url: Optional[str] = Field(default=None, max_length=300)
    model: Optional[str] = Field(default=None, max_length=80)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_history: Optional[int] = Field(default=None, ge=2, le=60)
    auto_learn: Optional[bool] = None
    auto_learn_threshold: Optional[int] = Field(default=None, ge=2, le=50)
    web_search_default_days: Optional[int] = Field(default=None, ge=1, le=30)


@app.post("/api/config")
def save_config(req: ConfigReq, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    cfg = cfgmod.load_config()
    if req.api_key is not None:
        cfg["api_key"] = req.api_key.strip()
    if req.volcengine_api_key is not None:
        cfg["volcengine_api_key"] = req.volcengine_api_key.strip()
    if req.base_url is not None:
        if cfg.get("lock_config"):
            return JSONResponse({"ok": False, "error": "部署模式下 base_url 已锁定（防密钥外发），请在本机修改 config.json"}, status_code=403)
        try:
            cfg["base_url"] = _validate_base_url(req.base_url)
        except ValueError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
    if req.model:
        cfg["model"] = req.model.strip()
    if req.temperature is not None:
        cfg["temperature"] = max(0.0, min(2.0, req.temperature))
    if req.max_history is not None:
        cfg["max_history"] = max(2, min(60, req.max_history))
    if req.auto_learn is not None:
        cfg["auto_learn"] = bool(req.auto_learn)
    if req.auto_learn_threshold is not None:
        cfg["auto_learn_threshold"] = max(2, min(50, req.auto_learn_threshold))
    if req.web_search_default_days is not None:
        cfg["web_search_default_days"] = max(1, min(30, req.web_search_default_days))
    cfgmod.save_config(cfg)
    return {"ok": True}


@app.post("/api/config/test")
def test_config(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    import time
    cfg = cfgmod.load_config()
    if not cfg.get("api_key"):
        return JSONResponse({"ok": False, "error": "未配置 API Key"}, status_code=400)
    try:
        t0 = time.time()
        msg = LLMClient(cfg).chat([{"role": "user", "content": "请只回复两个字：正常"}], temperature=0.1)
        return {"ok": True, "reply": (msg.content or "").strip()[:50],
                "latency_ms": int((time.time() - t0) * 1000), "model": cfg["model"]}
    except Exception as e:
        logger.exception("模型连接测试失败")
        return JSONResponse({"ok": False, "error": f"连接测试失败：{_safe_error(e, cfg)}"}, status_code=400)


# ---------- 高德地图（密钥与大模型分开维护） ----------
class MapsConfigReq(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=300)
    city: Optional[str] = Field(default=None, max_length=40)
    campus_name: Optional[str] = Field(default=None, max_length=100)
    campus_location: Optional[str] = Field(default=None, max_length=100)
    radius: Optional[int] = None


@app.get("/api/maps/config")
def get_maps_config(request: Request):
    return maps.public_config(_user_cfg(request))


@app.post("/api/maps/config")
def save_maps_config(req: MapsConfigReq, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    if req.radius is not None and not 100 <= req.radius <= 50000:
        return JSONResponse({"ok": False, "error": "radius 必须在 100 到 50000 米之间"}, status_code=400)
    try:
        changes = req.model_dump(exclude_none=True)
        cfg = maps.update_config(cfgmod.load_config(), changes)
        # 将当前学校的地图字段写回学校模板，切换回来时不会丢失坐标和半径。
        cfg = campus_profiles.update_profile_override(cfg, cfg.get("current_school_id", "hlby"), changes)
    except ValueError as error:
        return JSONResponse({"ok":False,"error":str(error)},status_code=400)
    cfgmod.save_config(cfg)
    return {"ok":True}


@app.post("/api/maps/test")
def test_maps_config(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    result = maps.AmapClient(_user_cfg(request)).test_connection()
    return JSONResponse(result,status_code=200 if result["ok"] else 400)


class MapsSearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=80)
    scope: str = Field(default="nearby", max_length=20)
    radius: Optional[int] = Field(default=None, ge=100, le=50000)
    sort: str = Field(default="distance", max_length=20)
    limit: int = Field(default=5, ge=1, le=10)
    origin_query: str = Field(default="", max_length=80)


@app.post("/api/maps/search")
def search_maps(req: MapsSearchReq, request: Request):
    try:
        return maps.search_places(**req.model_dump(), cfg=_user_cfg(request), rag=rag)
    except ValueError as error:
        return JSONResponse({"ok":False,"error":str(error)},status_code=400)


# ---------- 多学校模板 ----------
class SchoolSelectReq(BaseModel):
    school_id: str = Field(min_length=1, max_length=64)


class SchoolProfileReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=40)
    website: str = Field(default="", max_length=300)
    campus_location: str = Field(default="", max_length=100)
    radius: int = Field(default=3000, ge=100, le=50000)


class SchoolProfileUpdateReq(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    city: Optional[str] = Field(default=None, min_length=1, max_length=40)
    website: Optional[str] = Field(default=None, max_length=300)
    campus_location: Optional[str] = Field(default=None, max_length=100)
    radius: Optional[int] = Field(default=None, ge=100, le=50000)


class SchoolCrawlReq(BaseModel):
    max_pages: int = Field(default=80, ge=1, le=500)
    delay: float = Field(default=0.35, ge=0, le=10)
    timeout: int = Field(default=15, ge=1, le=60)


def _public_school_state(cfg, current_id=None):
    current_id = current_id or cfg.get("current_school_id", "hlby")
    profiles = campus_profiles.list_profiles(cfg)
    return {
        "current_school_id": current_id,
        "schools": [campus_profiles.public_profile(p, current_id) for p in profiles],
        "count": len(profiles),
        "template_note": "学校资料、官网和地图坐标需要按校补充；当前目录可扩展。",
    }


@app.get("/api/schools")
def list_schools(request: Request):
    return _public_school_state(_user_cfg(request), _current_school_id(request))


@app.post("/api/schools/select")
def select_school(req: SchoolSelectReq, request: Request):
    try:
        base_cfg = cfgmod.load_config()
        cfg = campus_profiles.apply_profile(base_cfg, req.school_id)
    except ValueError as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
    user_id = _current_user_id(request)
    if user_id is not None:
        auth.set_user_school(user_id, req.school_id)
    else:
        cfgmod.save_config(cfg)
    # 选择学校后马上刷新索引；当前对话和密钥保持不变。
    rag.reload()
    return {"ok": True, **_public_school_state(cfg, req.school_id), "current": campus_profiles.public_profile(campus_profiles.get_profile(req.school_id, cfg), req.school_id)}


@app.post("/api/schools/{school_id}/crawl")
def crawl_school(school_id: str, req: SchoolCrawlReq, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    cfg = cfgmod.load_config()
    try:
        result = school_crawler.crawl_and_import(
            school_id, cfg=cfg, max_pages=req.max_pages, delay=req.delay, timeout=req.timeout, rag=rag
        )
        profile = campus_profiles.get_profile(school_id, cfg)
    except (TypeError, ValueError) as error:
        return JSONResponse({"ok": False, "error": _safe_error(error, cfg)}, status_code=400)
    return {"ok": True, "school": campus_profiles.public_profile(profile, cfg.get("current_school_id")), **result}


@app.post("/api/schools")
def add_school(req: SchoolProfileReq, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        cfg, profile = campus_profiles.add_custom_profile(cfgmod.load_config(), req.model_dump())
    except (TypeError, ValueError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
    cfgmod.save_config(cfg)
    return {"ok": True, "school": campus_profiles.public_profile(profile, cfg.get("current_school_id")), **_public_school_state(cfg)}


@app.patch("/api/schools/{school_id}")
def update_school(school_id: str, req: SchoolProfileUpdateReq, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    changes = {key: value for key, value in req.model_dump(exclude_none=True).items()}
    if "website" in changes and changes["website"]:
        parsed = urlparse(changes["website"])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return JSONResponse({"ok": False, "error": "官网地址必须是 http 或 https 链接"}, status_code=400)
    try:
        cfg = campus_profiles.update_profile_override(cfgmod.load_config(), school_id, changes)
        if cfg.get("current_school_id") == school_id:
            cfg = campus_profiles.apply_profile(cfg, school_id)
        profile = campus_profiles.get_profile(school_id, cfg)
    except (TypeError, ValueError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
    cfgmod.save_config(cfg)
    return {"ok": True, "school": campus_profiles.public_profile(profile, cfg.get("current_school_id")), **_public_school_state(cfg)}


# ---------- 对话 ----------
class ChatReq(BaseModel):
    conversation_id: Optional[int] = None
    message: str = Field(min_length=1, max_length=12000)
    place_id: Optional[str] = Field(default=None, max_length=80)


@app.post("/api/chat")
def chat(req: ChatReq, request: Request):
    user_id = _current_user_id(request)
    cfg = _user_cfg(request)
    if req.conversation_id and user_id is not None and not _conversation_belongs_to_current_school(req.conversation_id, request):
        return JSONResponse({"ok": False, "error": "该对话不属于当前账号"}, status_code=404)
    def gen():
        try:
            for ev in agent.chat(req.conversation_id, req.message, place_id=req.place_id, user_id=user_id, cfg_override=cfg):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception("对话处理失败")
            yield f"data: {json.dumps({'type': 'error', 'data': '服务暂时不可用，请稍后重试；如果持续失败，请检查模型配置或联系管理员。'}, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/conversations")
def list_conversations(request: Request):
    conn = db.get_conn()
    school_id = _current_school_id(request)
    user_id = _current_user_id(request)
    if user_id is None:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE school_id=? ORDER BY updated_at DESC LIMIT 100",
            (school_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE school_id=? AND user_id=? ORDER BY updated_at DESC LIMIT 100",
            (school_id, user_id),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/conversations")
def new_conversation(request: Request):
    conn = db.get_conn()
    school_id = _current_school_id(request)
    user_id = _current_user_id(request)
    if user_id is None:
        cur = conn.execute(
            "INSERT INTO conversations(title, school_id, created_at, updated_at) VALUES('新对话', ?, ?, ?)",
            (school_id, db.now(), db.now()),
        )
    else:
        cur = conn.execute(
            "INSERT INTO conversations(title, school_id, user_id, created_at, updated_at) VALUES('新对话', ?, ?, ?, ?)",
            (school_id, user_id, db.now(), db.now()),
        )
    conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: int, request: Request):
    conn = db.get_conn()
    school_id = _current_school_id(request)
    user_id = _current_user_id(request)
    scope = " AND user_id=?" if user_id is not None else ""
    params = (cid, cid, school_id, user_id) if user_id is not None else (cid, cid, school_id)
    conn.execute(f"DELETE FROM messages WHERE conversation_id=? AND EXISTS (SELECT 1 FROM conversations WHERE id=? AND school_id=?{scope})", params)
    conn.execute(f"DELETE FROM conversations WHERE id=? AND school_id=?{scope}", (cid, school_id, user_id) if user_id is not None else (cid, school_id))
    conn.commit()
    return {"ok": True}


class RenameReq(BaseModel):
    title: str


@app.patch("/api/conversations/{cid}")
def rename_conversation(cid: int, req: RenameReq, request: Request):
    title = (req.title or "").strip()[:60]
    if not title:
        return JSONResponse({"ok": False, "error": "标题不能为空"}, status_code=400)
    conn = db.get_conn()
    school_id = _current_school_id(request)
    user_id = _current_user_id(request)
    if user_id is None:
        conn.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=? AND school_id=?", (title, db.now(), cid, school_id))
    else:
        conn.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=? AND school_id=? AND user_id=?", (title, db.now(), cid, school_id, user_id))
    conn.commit()
    return {"ok": True}


@app.get("/api/conversations/{cid}/messages")
def get_messages(cid: int, request: Request):
    conn = db.get_conn()
    school_id = _current_school_id(request)
    user_id = _current_user_id(request)
    if user_id is None:
        exists = conn.execute("SELECT 1 FROM conversations WHERE id=? AND school_id=?", (cid, school_id)).fetchone()
    else:
        exists = conn.execute("SELECT 1 FROM conversations WHERE id=? AND school_id=? AND user_id=?", (cid, school_id, user_id)).fetchone()
    if not exists:
        return JSONResponse({"ok": False, "error": "该对话不属于当前账号或学校模板"}, status_code=404)
    rows = conn.execute(
        "SELECT id, role, content, tool_events, source_refs, rating, created_at FROM messages "
        "WHERE conversation_id=? ORDER BY id", (cid,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["tool_events"] = json.loads(d.get("tool_events") or "[]")
        except Exception:
            d["tool_events"] = []
        try:
            d["source_refs"] = json.loads(d.get("source_refs") or "[]")
        except (TypeError, ValueError):
            d["source_refs"] = []
        out.append(d)
    return out


@app.post("/api/conversations/{cid}/learn")
def learn_conversation(cid: int, request: Request):
    """结束对话并学习：提炼长期记忆 + 待审核知识"""
    if not _conversation_belongs_to_current_school(cid, request):
        return JSONResponse({"ok": False, "error": "该对话不属于当前账号或学校模板"}, status_code=404)
    cfg = cfgmod.load_config()
    if not cfg.get("api_key"):
        return JSONResponse({"ok": False, "error": "未配置 API Key，无法执行学习提炼"}, status_code=400)
    llm = LLMClient(cfg)
    try:
        mem_added = memory.extract_from_conversation(cid, llm, user_id=_current_user_id(request))
        kno = learning.extract(cid, llm)
        return {"ok": True, "memories_added": mem_added, "knowledge_pending": kno}
    except Exception as e:
        logger.exception("对话学习提炼失败")
        return JSONResponse({"ok": False, "error": _safe_error(e, cfg)}, status_code=400)


# ---------- 知识库 ----------
class KnowledgeReq(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=120000)
    source: str = Field(default="手动添加", max_length=300)


@app.get("/api/knowledge")
def list_knowledge(request: Request):
    conn = db.get_conn()
    school_id = _current_school_id(request)
    rows = conn.execute(
        "SELECT id, title, source, ktype, hits, created_at, length(content) AS size "
        "FROM knowledge WHERE (school_id=? OR school_id='common') AND (owner_user_id IS NULL OR owner_user_id=?) ORDER BY id DESC", (school_id, _current_user_id(request))
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/knowledge")
def add_knowledge(req: KnowledgeReq, request: Request):
    if not req.title.strip() or not req.content.strip():
        return JSONResponse({"ok": False, "error": "标题和内容不能为空"}, status_code=400)
    doc_id, n = rag.add_document(req.title.strip(), req.content.strip(), req.source.strip() or "手动添加", "文档", school_id=_current_school_id(request), owner_user_id=_current_user_id(request))
    return {"ok": True, "id": doc_id, "chunks": n}


@app.delete("/api/knowledge/{kid}")
def delete_knowledge(kid: int, request: Request):
    user = getattr(request.state, "user", None) or {}
    rag.delete_document(kid, school_id=_current_school_id(request), owner_user_id=_current_user_id(request), allow_shared=user.get("role") == "admin")
    return {"ok": True}


@app.get("/api/knowledge/{kid}")
def get_knowledge_detail(kid: int, request: Request):
    conn = db.get_conn()
    school_id = _current_school_id(request)
    row = conn.execute(
        "SELECT id, title, source, ktype, content, hits, created_at FROM knowledge WHERE id=? AND (school_id=? OR school_id='common') AND (owner_user_id IS NULL OR owner_user_id=?)", (kid, school_id, _current_user_id(request))
    ).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "文档不存在"}, status_code=404)
    return dict(row)


class SearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=300)


@app.post("/api/knowledge/search")
def search_knowledge(req: SearchReq, request: Request):
    return rag.search(req.query, top_k=5, school_id=_current_school_id(request), user_id=_current_user_id(request))


# ---------- 办事导航与官网电话簿 ----------
@app.get("/api/services")
def list_services(q: str = Query(default="", max_length=100)):
    return services.list_services(q)


@app.get("/api/services/{service_id}")
def get_service(service_id: str, request: Request, college: str = Query(default="", max_length=100)):
    detail = services.service_detail(service_id, rag, college, school_id=_current_school_id(request))
    if detail is None:
        return JSONResponse({"error": "未找到该办事事项"}, status_code=404)
    return detail


@app.get("/api/colleges")
def list_colleges():
    return services.list_colleges()


@app.get("/api/colleges/{college_id}")
def get_college(college_id: str, request: Request):
    try:
        return services.college_detail(college_id, rag, school_id=_current_school_id(request))
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=404)


@app.get("/api/contacts")
def list_contacts(request: Request, q: str = Query(default="", max_length=100)):
    return services.list_contacts(q, school_id=_current_school_id(request))


# ---------- 记忆 ----------
class MemoryReq(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    category: str = Field(default="通用", max_length=40)


@app.get("/api/memories")
def list_memories(request: Request):
    return memory.list_all(_current_user_id(request))


@app.post("/api/memories")
def add_memory(req: MemoryReq, request: Request):
    if not req.content.strip():
        return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)
    r = memory.add(req.content.strip(), req.category.strip() or "通用", source="手动添加", user_id=_current_user_id(request))
    return {"ok": True, **r}


@app.post("/api/memories/search")
def search_memories(req: SearchReq, request: Request):
    """按相关性检索长期记忆（用于记忆召回测试）"""
    return memory.search(req.query, top_k=5, user_id=_current_user_id(request))


@app.delete("/api/memories/{mid}")
def delete_memory(mid: int, request: Request):
    return memory.delete(mid, _current_user_id(request))


@app.post("/api/memories/consolidate")
def consolidate_memories(request: Request):
    """记忆巩固：合并高度相似的记忆（保留较新），防止膨胀失真"""
    return memory.consolidate(user_id=_current_user_id(request))


# ---------- 学习中心 ----------
@app.get("/api/learning")
def list_learning(request: Request, status: str = "pending"):
    return learning.list(status, _current_school_id(request), _current_user_id(request))


@app.get("/api/learning/materials")
def list_learning_materials(request: Request):
    """只列出学生上传的学习资料，不返回校园通用知识库正文。"""
    conn = db.get_conn()
    school_id = _current_school_id(request)
    rows = conn.execute(
        "SELECT id,title,source,ktype,created_at,length(content) AS size "
        "FROM knowledge WHERE ktype='学习资料' AND (school_id=? OR school_id='common') AND (owner_user_id IS NULL OR owner_user_id=?) ORDER BY id DESC",
        (school_id, _current_user_id(request)),
    ).fetchall()
    return [dict(row) for row in rows]


class LearningAnalyzeReq(BaseModel):
    document_ids: list[int] = Field(default_factory=list, max_length=30)
    uploaded_title: str = Field(default="", max_length=200)
    uploaded_content: str = Field(default="", max_length=60000)
    question_count: int = Field(default=5, ge=1, le=20)
    save_upload: bool = False


@app.post("/api/learning/analyze")
def analyze_learning_materials(req: LearningAnalyzeReq, request: Request):
    """合并知识库资料与上传资料，调用 DeepSeek 生成考点和自测题。"""
    cfg = cfgmod.load_config()
    if not cfg.get("api_key"):
        return JSONResponse({"ok": False, "error": "未配置 API Key，无法分析学习资料"}, status_code=400)
    doc_ids = list(req.document_ids or [])
    uploaded_title = req.uploaded_title.strip()[:120]
    uploaded_content = req.uploaded_content.strip()
    uploaded_doc_id = None
    if req.save_upload and uploaded_content:
        uploaded_doc_id, _ = rag.add_document(
            uploaded_title or "学习区上传资料",
            uploaded_content[:60000],
            source="学习区上传",
            ktype="学习资料",
            school_id=_current_school_id(request),
            owner_user_id=_current_user_id(request),
        )
        doc_ids.append(uploaded_doc_id)
        uploaded_title = ""
        uploaded_content = ""
    try:
        result = learning.analyze_materials(
            doc_ids,
            uploaded_title,
            uploaded_content,
            LLMClient(cfg),
            question_count=req.question_count,
            school_id=_current_school_id(request),
            user_id=_current_user_id(request),
        )
        result["uploaded_document_id"] = uploaded_doc_id
        return {"ok": True, **result}
    except (TypeError, ValueError, RuntimeError) as error:
        logger.exception("学习资料分析失败")
        return JSONResponse({"ok": False, "error": _safe_error(error, cfg)}, status_code=400)


@app.get("/api/learning/stats")
def learning_stats(request: Request):
    return learning.stats(_current_school_id(request), _current_user_id(request))


class ReviewReq(BaseModel):
    action: str = Field(max_length=10)  # approve | reject


@app.post("/api/learning/{lid}/review")
def review_learning(lid: int, req: ReviewReq, request: Request):
    if not _learned_belongs_to_current_school(lid, request):
        return JSONResponse({"ok": False, "error": "该学习条目不属于当前账号或学校模板"}, status_code=404)
    if req.action == "approve":
        return learning.approve(lid)
    if req.action == "reject":
        return learning.reject(lid)
    return JSONResponse({"ok": False, "error": "无效操作"}, status_code=400)


# ---------- 反馈 ----------
class FeedbackReq(BaseModel):
    message_id: int
    rating: str = Field(max_length=10)  # up | down
    comment: str = Field(default="", max_length=1000)


@app.post("/api/feedback")
def add_feedback(req: FeedbackReq, request: Request):
    if req.rating not in ("up", "down"):
        return JSONResponse({"ok": False, "error": "无效评分"}, status_code=400)
    if not _message_belongs_to_current_school(req.message_id, request):
        return JSONResponse({"ok": False, "error": "该消息不属于当前账号或学校模板"}, status_code=404)
    return LearningEngine.add_feedback(req.message_id, req.rating, req.comment)


# ---------- 工具 ----------
@app.get("/api/tools")
def list_tools():
    return [{"name": s["function"]["name"], "description": s["function"]["description"]}
            for s in toolbox.TOOL_SCHEMAS]


class ToolRunReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    args: dict = Field(default_factory=dict)


@app.post("/api/tools/run")
def run_tool(req: ToolRunReq, request: Request):
    tokens = toolbox.set_request_context(_current_user_id(request), _user_cfg(request))
    try:
        return {"ok": True, "result": toolbox.run_tool(req.name, req.args)}
    except Exception as e:
        logger.exception("工具执行失败：%s", req.name)
        return JSONResponse({"ok": False, "error": _safe_error(e, cfgmod.load_config())}, status_code=400)
    finally:
        toolbox.reset_request_context(tokens)


@app.get("/api/notices")
def list_notices(request: Request):
    tokens = toolbox.set_request_context(_current_user_id(request), _user_cfg(request))
    try:
        return toolbox.search_campus_notice("")["matches"]
    finally:
        toolbox.reset_request_context(tokens)


@app.get("/api/web-search/status")
def web_search_status():
    return web_search.status()


# ---------- 启动 ----------
if __name__ == "__main__":
    cfg = cfgmod.load_config()
    # 云端适配：PORT 环境变量（Render 等平台注入）+ 0.0.0.0 绑定 + 不弹浏览器
    port = int(os.environ.get("PORT") or cfg["port"])
    cloud = bool(os.environ.get("PORT"))
    host = "0.0.0.0" if cloud else cfg["host"]
    url = f"http://127.0.0.1:{port}" if not cloud else f"http://0.0.0.0:{port}"
    print(f"\n  校园小助手已启动：{url}\n  按 Ctrl+C 停止服务\n")
    if cfg.get("open_browser", True) and not cloud:
        threading.Timer(1.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")

"""校园小助手 - FastAPI 入口

启动：python main.py（或运行项目根目录的 启动.bat）
访问：http://127.0.0.1:8300
"""
import json
import os
import secrets
import threading
import webbrowser
from urllib.parse import urlparse
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import config as cfgmod
import db
import tools as toolbox
import services
import maps
import campus_profiles
from agent import Agent
from learning import LearningEngine
from llm import LLMClient
from memory import MemoryManager
from rag import RAG

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_PATH = os.path.join(BASE_DIR, "..", "frontend", "index.html")

app = FastAPI(title="校园小助手")


# ---------- 访问口令（公网部署保护） ----------
@app.middleware("http")
async def access_guard(request: Request, call_next):
    """配置了 access_token 时，除 /api/public/* 外的所有 API 均需携带 X-Access-Token"""
    token = cfgmod.load_config().get("access_token") or ""
    path = request.url.path
    if token and path.startswith("/api/") and not path.startswith("/api/public/"):
        supplied = request.headers.get("x-access-token") or ""
        if not secrets.compare_digest(supplied, token):
            return JSONResponse({"error": "需要访问口令"}, status_code=401)
    return await call_next(request)

# ---------- 初始化 ----------
db.init_db()
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
    token: str


@app.get("/api/public/auth")
def auth_status():
    """前端启动时探测：是否需要口令"""
    return {"required": bool(cfgmod.load_config().get("access_token"))}


@app.post("/api/public/auth")
def auth_verify(req: AuthReq):
    """验证用户输入的口令"""
    token = cfgmod.load_config().get("access_token") or ""
    ok = bool(token) and secrets.compare_digest(req.token.strip(), token)
    return {"ok": ok}


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
    }


class ConfigReq(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_history: Optional[int] = None
    auto_learn: Optional[bool] = None
    auto_learn_threshold: Optional[int] = None


@app.post("/api/config")
def save_config(req: ConfigReq):
    cfg = cfgmod.load_config()
    if req.api_key is not None:
        cfg["api_key"] = req.api_key.strip()
    if req.base_url:
        if cfg.get("lock_config"):
            return JSONResponse({"ok": False, "error": "部署模式下 base_url 已锁定（防密钥外发），请在本机修改 config.json"}, status_code=403)
        cfg["base_url"] = req.base_url.strip()
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
    cfgmod.save_config(cfg)
    return {"ok": True}


@app.post("/api/config/test")
def test_config():
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
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ---------- 高德地图（密钥与大模型分开维护） ----------
class MapsConfigReq(BaseModel):
    api_key: Optional[str] = None
    city: Optional[str] = None
    campus_name: Optional[str] = None
    campus_location: Optional[str] = None
    radius: Optional[int] = None


@app.get("/api/maps/config")
def get_maps_config():
    return maps.public_config(cfgmod.load_config())


@app.post("/api/maps/config")
def save_maps_config(req: MapsConfigReq):
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
def test_maps_config():
    result = maps.AmapClient(cfgmod.load_config()).test_connection()
    return JSONResponse(result,status_code=200 if result["ok"] else 400)


class MapsSearchReq(BaseModel):
    query: str
    scope: str = "nearby"
    radius: Optional[int] = None
    sort: str = "distance"
    limit: int = 5


@app.post("/api/maps/search")
def search_maps(req: MapsSearchReq):
    try:
        return maps.search_places(**req.model_dump(),rag=rag)
    except ValueError as error:
        return JSONResponse({"ok":False,"error":str(error)},status_code=400)


# ---------- 多学校模板 ----------
class SchoolSelectReq(BaseModel):
    school_id: str


class SchoolProfileReq(BaseModel):
    name: str
    city: str
    website: str = ""
    campus_location: str = ""
    radius: int = 3000


class SchoolProfileUpdateReq(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    website: Optional[str] = None
    campus_location: Optional[str] = None
    radius: Optional[int] = None


def _public_school_state(cfg):
    current_id = cfg.get("current_school_id", "hlby")
    profiles = campus_profiles.list_profiles(cfg)
    return {
        "current_school_id": current_id,
        "schools": [campus_profiles.public_profile(p, current_id) for p in profiles],
        "count": len(profiles),
        "template_note": "学校资料、官网和地图坐标需要按校补充；当前目录可扩展。",
    }


@app.get("/api/schools")
def list_schools():
    return _public_school_state(cfgmod.load_config())


@app.post("/api/schools/select")
def select_school(req: SchoolSelectReq):
    try:
        cfg = campus_profiles.apply_profile(cfgmod.load_config(), req.school_id)
    except ValueError as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
    cfgmod.save_config(cfg)
    # 选择学校后马上刷新索引；当前对话和密钥保持不变。
    rag.reload()
    return {"ok": True, **_public_school_state(cfg), "current": campus_profiles.public_profile(campus_profiles.get_profile(req.school_id, cfg), req.school_id)}


@app.post("/api/schools")
def add_school(req: SchoolProfileReq):
    try:
        cfg, profile = campus_profiles.add_custom_profile(cfgmod.load_config(), req.model_dump())
    except (TypeError, ValueError) as error:
        return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
    cfgmod.save_config(cfg)
    return {"ok": True, "school": campus_profiles.public_profile(profile, cfg.get("current_school_id")), **_public_school_state(cfg)}


@app.patch("/api/schools/{school_id}")
def update_school(school_id: str, req: SchoolProfileUpdateReq):
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
    message: str
    place_id: Optional[str] = None


@app.post("/api/chat")
def chat(req: ChatReq):
    def gen():
        try:
            for ev in agent.chat(req.conversation_id, req.message, place_id=req.place_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'data': f'服务异常：{e}'}, ensure_ascii=False)}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/conversations")
def list_conversations():
    conn = db.get_conn()
    school_id = cfgmod.load_config().get("current_school_id", "hlby")
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE school_id=? ORDER BY updated_at DESC LIMIT 100",
        (school_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/conversations")
def new_conversation():
    conn = db.get_conn()
    school_id = cfgmod.load_config().get("current_school_id", "hlby")
    cur = conn.execute(
        "INSERT INTO conversations(title, school_id, created_at, updated_at) VALUES('新对话', ?, ?, ?)",
        (school_id, db.now(), db.now()),
    )
    conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: int):
    conn = db.get_conn()
    school_id = cfgmod.load_config().get("current_school_id", "hlby")
    conn.execute("DELETE FROM messages WHERE conversation_id=? AND EXISTS (SELECT 1 FROM conversations WHERE id=? AND school_id=?)", (cid, cid, school_id))
    conn.execute("DELETE FROM conversations WHERE id=? AND school_id=?", (cid, school_id))
    conn.commit()
    return {"ok": True}


class RenameReq(BaseModel):
    title: str


@app.patch("/api/conversations/{cid}")
def rename_conversation(cid: int, req: RenameReq):
    title = (req.title or "").strip()[:60]
    if not title:
        return JSONResponse({"ok": False, "error": "标题不能为空"}, status_code=400)
    conn = db.get_conn()
    school_id = cfgmod.load_config().get("current_school_id", "hlby")
    conn.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=? AND school_id=?", (title, db.now(), cid, school_id))
    conn.commit()
    return {"ok": True}


@app.get("/api/conversations/{cid}/messages")
def get_messages(cid: int):
    conn = db.get_conn()
    school_id = cfgmod.load_config().get("current_school_id", "hlby")
    exists = conn.execute("SELECT 1 FROM conversations WHERE id=? AND school_id=?", (cid, school_id)).fetchone()
    if not exists:
        return JSONResponse({"ok": False, "error": "该对话属于其他学校模板"}, status_code=404)
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
def learn_conversation(cid: int):
    """结束对话并学习：提炼长期记忆 + 待审核知识"""
    cfg = cfgmod.load_config()
    if not cfg.get("api_key"):
        return JSONResponse({"ok": False, "error": "未配置 API Key，无法执行学习提炼"}, status_code=400)
    llm = LLMClient(cfg)
    try:
        mem_added = memory.extract_from_conversation(cid, llm)
        kno = learning.extract(cid, llm)
        return {"ok": True, "memories_added": mem_added, "knowledge_pending": kno}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ---------- 知识库 ----------
class KnowledgeReq(BaseModel):
    title: str
    content: str
    source: str = "手动添加"


@app.get("/api/knowledge")
def list_knowledge():
    conn = db.get_conn()
    school_id = cfgmod.load_config().get("current_school_id", "hlby")
    rows = conn.execute(
        "SELECT id, title, source, ktype, hits, created_at, length(content) AS size "
        "FROM knowledge WHERE school_id=? OR school_id='common' ORDER BY id DESC", (school_id,)
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/knowledge")
def add_knowledge(req: KnowledgeReq):
    if not req.title.strip() or not req.content.strip():
        return JSONResponse({"ok": False, "error": "标题和内容不能为空"}, status_code=400)
    doc_id, n = rag.add_document(req.title.strip(), req.content.strip(), req.source.strip() or "手动添加", "文档")
    return {"ok": True, "id": doc_id, "chunks": n}


@app.delete("/api/knowledge/{kid}")
def delete_knowledge(kid: int):
    rag.delete_document(kid)
    return {"ok": True}


@app.get("/api/knowledge/{kid}")
def get_knowledge_detail(kid: int):
    conn = db.get_conn()
    school_id = cfgmod.load_config().get("current_school_id", "hlby")
    row = conn.execute(
        "SELECT id, title, source, ktype, content, hits, created_at FROM knowledge WHERE id=? AND (school_id=? OR school_id='common')", (kid, school_id)
    ).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "文档不存在"}, status_code=404)
    return dict(row)


class SearchReq(BaseModel):
    query: str


@app.post("/api/knowledge/search")
def search_knowledge(req: SearchReq):
    return rag.search(req.query, top_k=5)


# ---------- 办事导航与官网电话簿 ----------
@app.get("/api/services")
def list_services(q: str = ""):
    return services.list_services(q)


@app.get("/api/services/{service_id}")
def get_service(service_id: str, college: str = ""):
    detail = services.service_detail(service_id, rag, college)
    if detail is None:
        return JSONResponse({"error": "未找到该办事事项"}, status_code=404)
    return detail


@app.get("/api/contacts")
def list_contacts(q: str = ""):
    return services.list_contacts(q)


# ---------- 记忆 ----------
class MemoryReq(BaseModel):
    content: str
    category: str = "通用"


@app.get("/api/memories")
def list_memories():
    return memory.list_all()


@app.post("/api/memories")
def add_memory(req: MemoryReq):
    if not req.content.strip():
        return JSONResponse({"ok": False, "error": "内容不能为空"}, status_code=400)
    r = memory.add(req.content.strip(), req.category.strip() or "通用", source="手动添加")
    return {"ok": True, **r}


@app.post("/api/memories/search")
def search_memories(req: SearchReq):
    """按相关性检索长期记忆（用于记忆召回测试）"""
    return memory.search(req.query, top_k=5)


@app.delete("/api/memories/{mid}")
def delete_memory(mid: int):
    return memory.delete(mid)


@app.post("/api/memories/consolidate")
def consolidate_memories():
    """记忆巩固：合并高度相似的记忆（保留较新），防止膨胀失真"""
    return memory.consolidate()


# ---------- 学习中心 ----------
@app.get("/api/learning")
def list_learning(status: str = "pending"):
    return learning.list(status)


@app.get("/api/learning/stats")
def learning_stats():
    return learning.stats()


class ReviewReq(BaseModel):
    action: str  # approve | reject


@app.post("/api/learning/{lid}/review")
def review_learning(lid: int, req: ReviewReq):
    if req.action == "approve":
        return learning.approve(lid)
    if req.action == "reject":
        return learning.reject(lid)
    return JSONResponse({"ok": False, "error": "无效操作"}, status_code=400)


# ---------- 反馈 ----------
class FeedbackReq(BaseModel):
    message_id: int
    rating: str  # up | down
    comment: str = ""


@app.post("/api/feedback")
def add_feedback(req: FeedbackReq):
    if req.rating not in ("up", "down"):
        return JSONResponse({"ok": False, "error": "无效评分"}, status_code=400)
    return LearningEngine.add_feedback(req.message_id, req.rating, req.comment)


# ---------- 工具 ----------
@app.get("/api/tools")
def list_tools():
    return [{"name": s["function"]["name"], "description": s["function"]["description"]}
            for s in toolbox.TOOL_SCHEMAS]


class ToolRunReq(BaseModel):
    name: str
    args: dict = {}


@app.post("/api/tools/run")
def run_tool(req: ToolRunReq):
    try:
        return {"ok": True, "result": toolbox.run_tool(req.name, req.args)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/notices")
def list_notices():
    return toolbox.search_campus_notice("")["matches"]


# ---------- 启动 ----------
if __name__ == "__main__":
    cfg = cfgmod.load_config()
    url = f"http://{cfg['host']}:{cfg['port']}"
    print(f"\n  校园小助手已启动：{url}\n  按 Ctrl+C 停止服务\n")
    if cfg.get("open_browser", True):
        threading.Timer(1.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=cfg["host"], port=int(cfg["port"]), log_level="warning")

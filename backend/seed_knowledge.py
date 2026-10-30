# -*- coding: utf-8 -*-
"""首次启动自动播种知识库：导入随仓库分发的八一农垦大学官网爬取数据与电话簿。

用途：云端部署（Render 等）磁盘重置导致 SQLite 清空时自动恢复知识库。
幂等：知识库已有文档时直接跳过，不影响本地/已导入数据。
"""
import hashlib
import json
import os
import re

import db
from rag import chunk_text

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
STATE_FILE = os.path.join(OUT, "byau_crawl_state.json")
PHONE_FILE = os.path.join(OUT, "八一农垦大学部门电话簿.md")
KNOWLEDGE_SEED_FILE = os.path.join(OUT, "knowledge_seed.json")

MIN_CHARS = 50
EXCLUDE_EXT = (".xls", ".doc", ".docx", ".pdf", ".ppt", ".properties", ".psp", ".js", ".css")
SCHOOL_ID = "hlby"


def _norm_url(u):
    u = re.sub(r"^https?://", "", str(u).strip().lower())
    return re.sub(r"^www\.", "", u)


def _load_docs():
    """返回 (title, source, content, ktype) 列表；无数据文件时返回空"""
    docs = []
    # 本机已整理的全部知识库快照（官网、电话簿、食堂与学习资料）
    if os.path.exists(KNOWLEDGE_SEED_FILE):
        with open(KNOWLEDGE_SEED_FILE, encoding="utf-8-sig") as f:
            for item in json.load(f):
                content = (item.get("content") or "").strip()
                if len(content) < MIN_CHARS:
                    continue
                docs.append((
                    (item.get("title") or "无标题").strip(),
                    (item.get("source") or "知识库快照").strip(),
                    content,
                    (item.get("ktype") or "文档").strip(),
                ))
        if docs:
            return docs
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
        pages, seen_fp = {}, set()
        for url, v in data.items():
            if str(url).lower().endswith(EXCLUDE_EXT):
                continue
            text = (v.get("text") or "").strip()
            if (v.get("chars") or 0) < MIN_CHARS or len(text) < MIN_CHARS:
                continue
            key = _norm_url(url)
            old = pages.get(key)
            if old is None or len(text) > len(old["text"]):
                pages[key] = {"url": url, "title": (v.get("title") or "").strip(),
                              "group": (v.get("group") or "").strip(), "text": text}
        for p in pages.values():
            fp = hashlib.md5(p["text"].encode("utf-8")).hexdigest()[:16]
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            title = p["title"] or (p["url"].rstrip("/").rsplit("/", 1)[-1] or "无标题页面")
            if p["group"] and not title.startswith(f"[{p['group']}]"):
                title = f"[{p['group']}] {title}"
            docs.append((title, f"官网:{p['url']}", p["text"], "官网爬取"))
    if os.path.exists(PHONE_FILE):
        with open(PHONE_FILE, encoding="utf-8-sig") as f:
            phone_text = f.read().strip()
        if phone_text:
            docs.append(("八一农垦大学部门电话簿", "电话簿:output/八一农垦大学部门电话簿.md", phone_text, "电话簿"))
    return docs


def seed_if_empty():
    """知识库为空时导入随仓库数据；返回导入文档数"""
    try:
        conn = db.get_conn()
        cnt = conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"]
        if cnt:
            return 0
        docs = _load_docs()
        if not docs:
            return 0
        ts = db.now()
        total_chunks = 0
        for title, source, content, ktype in docs:
            cur = conn.execute(
                "INSERT INTO knowledge(title, source, content, ktype, school_id, created_at) VALUES(?,?,?,?,?,?)",
                (title, source, content, ktype, SCHOOL_ID, ts),
            )
            doc_id = cur.lastrowid
            chunks = chunk_text(content)
            conn.executemany(
                "INSERT INTO knowledge_chunks(doc_id, chunk_index, content) VALUES(?,?,?)",
                [(doc_id, i, ch) for i, ch in enumerate(chunks)],
            )
            total_chunks += len(chunks)
        conn.commit()
        print(f"  [seed] 首次启动：已自动导入知识库 {len(docs)} 份文档 / {total_chunks} 个知识块")
        return len(docs)
    except Exception as e:
        print(f"  [seed] 知识库自动导入失败（不影响启动）：{e}")
        return 0

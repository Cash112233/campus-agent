# -*- coding: utf-8 -*-
"""把官网爬取数据 (byau_crawl_state.json) + 部门电话簿导入 campus-agent RAG 知识库。

用法:
    python byau_import_rag.py stats    # 只统计，不写库
    python byau_import_rag.py import   # 正式导入（按来源更新，保留文档编号和未成功重抓的资料）
    python byau_import_rag.py verify   # 导入后本地检索实测

说明:
- 不走 RAG.add_document（它每次全量重建索引，1200 页会 O(N^2) 慢），
  而是直接批量写 knowledge / knowledge_chunks 表，索引由主应用重启时一次性重建。
- 幂等：按来源 URL 更新已存在文档；未成功抓取的旧资料不删除。
- 仅接纳 extraction_version=2 且 content_status=fulltext 的新抓取正文。
"""
import hashlib
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # campus-agent/
BACKEND = os.path.join(BASE, "backend")
OUT = os.path.join(BASE, "output")
sys.path.insert(0, BACKEND)

from db import get_conn, now  # noqa: E402
from rag import RAG, chunk_text  # noqa: E402

STATE = os.path.join(OUT, "byau_crawl_state.json")
PHONE = os.path.join(OUT, "八一农垦大学部门电话簿.md")

MIN_CHARS = 50
EXCLUDE_EXT = (".xls", ".doc", ".docx", ".pdf", ".ppt", ".properties", ".psp", ".js", ".css")
KTYPE_WEB = "官网爬取"
KTYPE_PHONE = "电话簿"


def norm_url(u):
    u = str(u).strip().lower()
    if u.startswith(("官网:", "电话簿:")):
        u = u.split(":", 1)[1]
    u = re.sub(r"^https?://", "", u)
    return re.sub(r"^www\.", "", u)


def load_pages():
    with open(STATE, encoding="utf-8") as f:
        data = json.load(f)
    raw = len(data)
    pages = {}
    for url, v in data.items():
        if v.get("extraction_version") != 2 or v.get("content_status") != "fulltext" or v.get("status") != 200:
            continue
        if str(url).lower().endswith(EXCLUDE_EXT):
            continue
        text = (v.get("text") or "").strip()
        if (v.get("chars") or 0) < MIN_CHARS or len(text) < MIN_CHARS:
            continue
        key = norm_url(url)
        old = pages.get(key)
        if old is None or len(text) > len(old["text"]):
            pages[key] = {
                "url": url,
                "title": (v.get("title") or "").strip(),
                "group": (v.get("group") or "").strip(),
                "text": text,
            }
    # 内容指纹去重（http/https 双版本、重复抓取）
    uniq, seen = [], set()
    for p in pages.values():
        fp = hashlib.md5(p["text"].encode("utf-8")).hexdigest()[:16]
        if fp in seen:
            continue
        seen.add(fp)
        uniq.append(p)
    return raw, len(pages), uniq


def build_docs(uniq):
    docs = []
    for p in uniq:
        title = p["title"] or (p["url"].rstrip("/").rsplit("/", 1)[-1] or "无标题页面")
        if p["group"] and not title.startswith(f"[{p['group']}]"):
            title = f"[{p['group']}] {title}"
        docs.append((title, f"官网:{p['url']}", p["text"], KTYPE_WEB))
    if os.path.exists(PHONE):
        phone_text = open(PHONE, encoding="utf-8").read().strip()
        if phone_text:
            docs.append(("八一农垦大学部门电话簿", "电话簿:output/八一农垦大学部门电话簿.md", phone_text, KTYPE_PHONE))
    return docs


def cmd_stats():
    raw, kept, uniq = load_pages()
    print(f"原始页面: {raw}")
    print(f"过滤后(>={MIN_CHARS}字, 去附件): {kept}")
    print(f"内容指纹去重后: {len(uniq)}")
    print(f"正文总字数: 约 {sum(len(p['text']) for p in uniq)} 字")
    docs = build_docs(uniq)
    n_chunks = 0
    for title, source, content, ktype in docs:
        n_chunks += len(chunk_text(content))
    print(f"预计文档: {len(docs)}  预计知识块: {n_chunks}")
    ktypes = {}
    for d in docs:
        ktypes[d[3]] = ktypes.get(d[3], 0) + 1
    print("分布:", ktypes)


def cmd_import():
    raw, kept, uniq = load_pages()
    docs = build_docs(uniq)
    conn = get_conn()
    existing = {}
    for row in conn.execute("SELECT id, source, content, ktype FROM knowledge WHERE ktype IN (?,?)", (KTYPE_WEB, KTYPE_PHONE)):
        existing[(row["ktype"], norm_url(row["source"]))] = row
    ts = now()
    added = updated = unchanged = total_chunks = 0
    with conn:
        for title, source, content, ktype in docs:
            old = existing.get((ktype, norm_url(source)))
            if old and old["content"] == content:
                unchanged += 1
                continue
            if old:
                doc_id = old["id"]
                conn.execute("UPDATE knowledge SET title=?, source=?, content=?, ktype=? WHERE id=?",
                             (title, source, content, ktype, doc_id))
                conn.execute("DELETE FROM knowledge_chunks WHERE doc_id=?", (doc_id,))
                updated += 1
            else:
                cur = conn.execute("INSERT INTO knowledge(title, source, content, ktype, created_at) VALUES(?,?,?,?,?)",
                                   (title, source, content, ktype, ts))
                doc_id = cur.lastrowid
                added += 1
            chunks = chunk_text(content)
            conn.executemany("INSERT INTO knowledge_chunks(doc_id, chunk_index, content) VALUES(?,?,?)",
                             [(doc_id, i, ch) for i, ch in enumerate(chunks)])
            total_chunks += len(chunks)
    n = conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"]
    nc = conn.execute("SELECT COUNT(*) c FROM knowledge_chunks").fetchone()["c"]
    print(f"缓存 {raw} 页 -> 本轮完整正文 {kept} 页 -> 内容去重 {len(uniq)} 页")
    print(f"新增 {added} / 更新 {updated} / 未变化 {unchanged}；重建知识块 {total_chunks}")
    print(f"知识库总量: {n} 文档 / {nc} 知识块")


def cmd_verify():
    rag = RAG()  # 全量重建索引，模拟主应用启动后的状态
    qs = [
        "学生想请假应该找谁",
        "应用科技学院办公室电话",
        "学校资助中心电话",
        "学校有几个学院",
        "心理咨询中心电话多少",
    ]
    ok = 0
    for q in qs:
        hits = rag.search(q, top_k=3)
        print(f"\nQ: {q}")
        if not hits:
            print("  (无结果)")
            continue
        for h in hits:
            preview = h["content"][:80].replace("\n", " ")
            print(f"  [{h['score']}] {h['title']} ({h['ktype']}) :: {preview}")
        if hits and hits[0]["ktype"] in (KTYPE_WEB, KTYPE_PHONE):
            ok += 1
    print(f"\n命中官网/电话簿数据的查询: {ok}/{len(qs)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    {"stats": cmd_stats, "import": cmd_import, "verify": cmd_verify}[cmd]()

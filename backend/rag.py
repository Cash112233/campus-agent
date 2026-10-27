"""RAG 检索层：文档切块 + TF-IDF 向量检索（纯本地、离线可用）

设计说明：
- 采用字符二元组(bigram)+英文词的 TF-IDF 余弦相似度，对中文校园知识库检索效果良好，且零外部依赖；
- 若未来需要更强的语义检索，可将 TfidfIndex 替换为 embedding API + 向量库，接口保持不变。
"""
import math
import re
from collections import Counter

from db import get_conn, now
from retrieval_rules import compact, query_terms, is_relevant, source_meta, source_url


def active_school_id():
    """读取当前模板学校；导入期或测试期无法读取时回到原学校。"""
    try:
        from config import load_config
        return str(load_config().get("current_school_id") or "hlby")
    except Exception:
        return "hlby"


def tokenize(text):
    """中文按字符二元组切分，英文/数字按词切分"""
    if not text:
        return []
    text = str(text).lower()
    ascii_tokens = re.findall(r"[a-z0-9]+", text)
    cjk = re.sub(r"[a-z0-9\s\W_]", "", text)
    bigrams = [cjk[i:i + 2] for i in range(max(0, len(cjk) - 1))]
    return ascii_tokens + bigrams + list(cjk)


class TfidfIndex:
    """轻量 TF-IDF 索引，支持增删与余弦相似度检索"""

    def __init__(self):
        self.docs = {}  # key -> {"tf": Counter, "len": int, "text": str, "meta": dict}

    def add(self, key, text, meta=None):
        toks = tokenize(text)
        if not toks:
            return
        self.docs[key] = {"tf": Counter(toks), "len": len(toks), "text": text, "meta": meta or {}}

    def remove(self, key):
        self.docs.pop(key, None)

    def search(self, query, top_k=5, predicate=None):
        qtokens = tokenize(query)
        if not qtokens or not self.docs:
            return []
        N = len(self.docs)
        df = Counter()
        for d in self.docs.values():
            for t in d["tf"]:
                df[t] += 1

        def idf(t):
            return math.log((N + 1) / (df.get(t, 0) + 1)) + 1.0

        qcount = Counter(qtokens)
        qlen = len(qtokens)
        qvec = {t: (c / qlen) * idf(t) for t, c in qcount.items()}

        results = []
        for key, d in self.docs.items():
            if predicate is not None and not predicate(d):
                continue
            tf, L = d["tf"], d["len"]
            if L == 0:
                continue
            dnorm = 0.0
            for t, c in tf.items():
                w = (c / L) * idf(t)
                dnorm += w * w
            sim = 0.0
            for t, wq in qvec.items():
                if t in tf:
                    sim += wq * (tf[t] / L) * idf(t)
            if sim > 0:
                score = sim / (math.sqrt(dnorm) or 1.0)
                results.append({"key": key, "score": round(score, 4), "text": d["text"], "meta": d["meta"]})
        results.sort(key=lambda r: -r["score"])
        return results[:top_k]


def chunk_text(text, size=500, overlap=80):
    """按段落合并切块，长段落硬切分，块间保留重叠"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= size:
            cur = (cur + "\n" + p).strip()
        else:
            if cur:
                chunks.append(cur)
            while len(p) > size:
                chunks.append(p[:size])
                p = p[max(0, size - overlap):]
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or ([text.strip()] if text.strip() else [])


class RAG:
    """知识库管理：入库(切块) / 删除 / 检索"""

    def __init__(self):
        self.index = TfidfIndex()
        self.reload()

    def reload(self):
        self.index = TfidfIndex()
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT c.id chunk_id, c.content, k.id doc_id, k.title, k.source, k.ktype, k.created_at "
                "FROM knowledge_chunks c JOIN knowledge k ON k.id = c.doc_id "
                "WHERE k.school_id=? OR k.school_id='common'",
                (active_school_id(),),
            ).fetchall()
        except Exception as error:
            # 兼容外部测试或旧内存库尚未执行迁移的短暂窗口；正式启动会由 db.init_db 补齐字段。
            if "school_id" not in str(error):
                raise
            rows = conn.execute(
                "SELECT c.id chunk_id, c.content, k.id doc_id, k.title, k.source, k.ktype, k.created_at "
                "FROM knowledge_chunks c JOIN knowledge k ON k.id = c.doc_id"
            ).fetchall()
        for r in rows:
            # 无原文链接的旧整理号码簿仍可人工查看，不能充当校务政策依据。
            if r["ktype"] == "电话簿" and not source_url(r["source"]):
                continue
            self.index.add(
                r["chunk_id"],
                r["title"] + "\n" + r["content"],
                {"content": r["content"], "indexed_at": r["created_at"], "doc_id": r["doc_id"], "title": r["title"], "source": r["source"], "ktype": r["ktype"]},
            )

    def add_document(self, title, content, source="手动添加", ktype="文档"):
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO knowledge(title, source, content, ktype, school_id, created_at) VALUES(?,?,?,?,?,?)",
            (title, source, content, ktype, active_school_id(), now()),
        )
        doc_id = cur.lastrowid
        chunks = chunk_text(content)
        for i, ch in enumerate(chunks):
            conn.execute(
                "INSERT INTO knowledge_chunks(doc_id, chunk_index, content) VALUES(?,?,?)",
                (doc_id, i, ch),
            )
        conn.commit()
        self.reload()
        return doc_id, len(chunks)

    def delete_document(self, doc_id):
        conn = get_conn()
        conn.execute("DELETE FROM knowledge WHERE id=? AND (school_id=? OR school_id='common')", (doc_id, active_school_id()))
        conn.execute("DELETE FROM knowledge_chunks WHERE doc_id=? AND NOT EXISTS (SELECT 1 FROM knowledge WHERE knowledge.id=knowledge_chunks.doc_id)", (doc_id,))
        conn.commit()
        self.reload()

    def search(self, query, top_k=4):
        """标题加权、别称扩展与主题/意图约束；没有对应资料时返回空。"""
        if not str(query or "").strip() or top_k <= 0:
            return []
        q, groups, expanded = query_terms(query)
        res = self.index.search(expanded, top_k=len(self.index.docs), predicate=lambda d:
            is_relevant(q, groups, d["meta"].get("title"), d["meta"].get("content", "")))
        for r in res:
            title = compact(r["meta"].get("title", "").removeprefix("[官网]"))
            q_bigrams = {t for t in tokenize(q) if len(t) > 1}
            overlap = len(q_bigrams & set(tokenize(title))) / max(1, len(q_bigrams))
            r["score"] += 0.18 * overlap
            if title and (q == title or title in q):
                r["score"] += 0.3
        res.sort(key=lambda r: -r["score"])
        out, seen = [], set()
        conn = get_conn()
        for r in res:
            if r["score"] < 0.12:
                continue
            meta = r["meta"]
            doc_id = meta.get("doc_id")
            if doc_id in seen:
                continue
            seen.add(doc_id)
            content = meta.get("content", "")
            out.append({"doc_id": doc_id, "title": meta.get("title"), "source": meta.get("source"),
                        "ktype": meta.get("ktype"), "content": content,
                        **source_meta(meta, content), "score": round(r["score"], 4)})
            if len(out) >= top_k:
                break
        if out:
            for row in out:
                conn.execute("UPDATE knowledge SET hits = hits + 1 WHERE id=?", (row["doc_id"],))
            conn.commit()
        return out

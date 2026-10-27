"""长记忆系统：跨对话记住学生信息，支持检索、手动增删、对话后自动提炼"""
from db import get_conn, now
from rag import TfidfIndex


class MemoryManager:
    def __init__(self):
        self.index = TfidfIndex()
        self.reload()

    def reload(self):
        self.index = TfidfIndex()
        conn = get_conn()
        for r in conn.execute("SELECT id, content, category, source, created_at FROM memories").fetchall():
            self.index.add(r["id"], r["content"], {
                "category": r["category"], "source": r["source"], "created_at": r["created_at"],
            })

    def add(self, content, category="通用", source="系统"):
        content = (content or "").strip()
        if not content:
            return {"id": None, "duplicated": True}
        conn = get_conn()
        row = conn.execute("SELECT id FROM memories WHERE content=?", (content,)).fetchone()
        if row:
            return {"id": row["id"], "duplicated": True}
        cur = conn.execute(
            "INSERT INTO memories(content, category, source, created_at) VALUES(?,?,?,?)",
            (content, category, source, now()),
        )
        conn.commit()
        self.reload()
        return {"id": cur.lastrowid, "duplicated": False}

    def delete(self, mid):
        conn = get_conn()
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
        conn.commit()
        self.reload()
        return {"ok": True}

    def list_all(self):
        conn = get_conn()
        rows = conn.execute("SELECT id, content, category, source, created_at FROM memories ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def search(self, query, top_k=3):
        """按相关性检索记忆，返回记忆内容列表"""
        res = self.index.search(query, top_k=top_k)
        out = []
        for r in res:
            out.append({
                "content": r["text"],
                "category": r["meta"].get("category"),
                "source": r["meta"].get("source"),
                "created_at": r["meta"].get("created_at"),
                "score": r["score"],
            })
        return out

    # ---------- 记忆巩固：相似去重合并 ----------

    def consolidate(self, threshold=0.85):
        """合并高度相似的记忆（保留较新一条），防止记忆库膨胀失真。

        规则：按写入顺序两两比较 TF 余弦相似度，相似度 ≥ threshold 的对，
        保留较新（后写入）条目、删除较旧条目。返回删除明细与统计。
        """
        from collections import Counter
        from rag import tokenize

        conn = get_conn()
        rows = conn.execute(
            "SELECT id, content, category, created_at FROM memories ORDER BY id"
        ).fetchall()
        if len(rows) < 2:
            return {"removed_count": 0, "removed": [], "examined_pairs": 0, "kept": len(rows)}

        vecs = {}
        for r in rows:
            toks = tokenize(r["content"])
            if not toks:
                vecs[r["id"]] = {}
                continue
            cnt = Counter(toks)
            L = len(toks)
            vecs[r["id"]] = {t: c / L for t, c in cnt.items()}

        def _cos(a, b):
            keys = set(a) & set(b)
            if not keys:
                return 0.0
            dot = sum(a[k] * b[k] for k in keys)
            na = sum(v * v for v in a.values()) ** 0.5
            nb = sum(v * v for v in b.values()) ** 0.5
            return dot / (na * nb) if na and nb else 0.0

        removed_ids, removed_detail, pairs = set(), [], 0
        for i in range(len(rows)):
            if rows[i]["id"] in removed_ids:
                continue
            for j in range(i + 1, len(rows)):
                if rows[j]["id"] in removed_ids:
                    continue
                pairs += 1
                sim = _cos(vecs[rows[i]["id"]], vecs[rows[j]["id"]])
                if sim >= threshold:
                    conn.execute("DELETE FROM memories WHERE id=?", (rows[i]["id"],))
                    removed_ids.add(rows[i]["id"])
                    removed_detail.append({
                        "removed": rows[i]["content"],
                        "kept": rows[j]["content"],
                        "similarity": round(sim, 3),
                    })
                    break  # i 已被合并删除，跳出内层循环
        conn.commit()
        if removed_ids:
            self.reload()
        return {
            "removed_count": len(removed_detail),
            "removed": removed_detail,
            "examined_pairs": pairs,
            "kept": len(rows) - len(removed_ids),
        }

    # ---------- 对话后自动提炼 ----------

    def extract_from_conversation(self, conversation_id, llm):
        """调用大模型从对话中提炼关于学生的长期事实，去重入库"""
        conn = get_conn()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND content NOT LIKE '【已作废：示例数据】%' ORDER BY id", (conversation_id,)
        ).fetchall()
        if not rows:
            return []
        dialogue = "\n".join(f"{'学生' if r['role'] == 'user' else '助手'}：{r['content']}" for r in rows)
        prompt = (
            "你是记忆提炼引擎。请分析下面这段学生与校园助手的对话，提取值得长期记住的关于「该学生本人」的事实"
            "（如专业年级、课程安排、考试时间、偏好、目标计划、身体状况等）。\n"
            "要求：\n"
            "1. 只提取对话中有明确依据的内容，不要编造；\n"
            "2. 每条一句话，包含 category（学生背景/重要事项/偏好/通用）；\n"
            "3. 输出 JSON 数组，如 [{\"content\":\"...\",\"category\":\"...\"}]；没有可提取内容时输出 []。\n\n"
            f"对话内容：\n{dialogue}"
        )
        msg = llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
        data = _as_list(msg.content)
        added = []
        for item in data:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            r = self.add(content, str(item.get("category", "通用")).strip() or "通用", source="对话提炼")
            if not r.get("duplicated"):
                added.append(content)
        return added


def _as_list(text):
    from llm import parse_json
    data = parse_json(text)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        inner = data.get("memories") or data.get("items") or data.get("data")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []

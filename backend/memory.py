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

    def add(self, content, category="通用", source="系统", user_id=None):
        content = (content or "").strip()
        if not content:
            return {"id": None, "duplicated": True}
        conn = get_conn()
        if user_id is None:
            row = conn.execute("SELECT id FROM memories WHERE content=? AND user_id IS NULL", (content,)).fetchone()
        else:
            row = conn.execute("SELECT id FROM memories WHERE content=? AND user_id=?", (content, user_id)).fetchone()
        if row:
            return {"id": row["id"], "duplicated": True}
        cur = conn.execute(
            "INSERT INTO memories(content, category, source, user_id, created_at) VALUES(?,?,?,?,?)",
            (content, category, source, user_id, now()),
        )
        conn.commit()
        self.reload()
        return {"id": cur.lastrowid, "duplicated": False}

    def delete(self, mid, user_id=None):
        conn = get_conn()
        if user_id is None:
            conn.execute("DELETE FROM memories WHERE id=? AND user_id IS NULL", (mid,))
        else:
            conn.execute("DELETE FROM memories WHERE id=? AND user_id=?", (mid, user_id))
        conn.commit()
        self.reload()
        return {"ok": True}

    def list_all(self, user_id=None):
        conn = get_conn()
        if user_id is None:
            rows = conn.execute("SELECT id, content, category, source, created_at FROM memories WHERE user_id IS NULL ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT id, content, category, source, created_at FROM memories WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def search(self, query, top_k=3, user_id=None):
        """按相关性检索记忆，返回记忆内容列表"""
        if user_id is None:
            res = self.index.search(query, top_k=top_k)
        else:
            scoped = TfidfIndex()
            for row in get_conn().execute("SELECT id,content,category,source,created_at FROM memories WHERE user_id=?", (user_id,)).fetchall():
                scoped.add(row["id"], row["content"], {"category": row["category"], "source": row["source"], "created_at": row["created_at"]})
            res = scoped.search(query, top_k=top_k)
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

    def consolidate(self, threshold=0.85, user_id=None):
        """合并高度相似的记忆（保留较新一条），防止记忆库膨胀失真。

        规则：按写入顺序两两比较 TF 余弦相似度，相似度 ≥ threshold 的对，
        保留较新（后写入）条目、删除较旧条目。返回删除明细与统计。
        """
        from collections import Counter
        from rag import tokenize

        conn = get_conn()
        if user_id is None:
            rows = conn.execute("SELECT id, content, category, created_at FROM memories WHERE user_id IS NULL ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT id, content, category, created_at FROM memories WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
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
                    if user_id is None:
                        conn.execute("DELETE FROM memories WHERE id=? AND user_id IS NULL", (rows[i]["id"],))
                    else:
                        conn.execute("DELETE FROM memories WHERE id=? AND user_id=?", (rows[i]["id"], user_id))
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

    def extract_from_conversation(self, conversation_id, llm, user_id=None):
        """调用大模型从对话中提炼关于学生的长期事实，去重入库"""
        conn = get_conn()
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND content NOT LIKE '【已作废：示例数据】%' ORDER BY id", (conversation_id,)
        ).fetchall()
        if not rows:
            return []
        # 记忆只从学生自己的表达中提炼，避免把助手的猜测或模板话术写成学生事实。
        dialogue = "\n".join(f"学生：{r['content']}" for r in rows if r["role"] == "user")
        if not dialogue:
            return []
        prompt = (
            "你是记忆提炼引擎。请分析下面这段学生与校园助手的对话，提取值得长期记住的关于「该学生本人」的稳定事实"
            "（如专业年级、长期课程安排、确定的考试时间、稳定偏好、长期目标等）。\n"
            "要求：\n"
            "1. 只提取学生明确说过或确认过的内容，不要根据助手猜测或常识推断；\n"
            "2. 不要保存 API Key、密码、身份证号、银行卡号、访问令牌等敏感信息；\n"
            "3. 不要保存一次性问题、临时计划、尚未确定的猜测或泛泛的助手客套话；‘临时计划’只有在学生明确要求长期记住时才保留；\n"
            "4. 同一事实只保留一条；如果新内容明确纠正旧内容，保留新内容并在句子中说明已更新；\n"
            "5. 每条一句话，包含 category（学生背景/重要事项/偏好/通用）；\n"
            "6. 输出 JSON 数组，如 [{\"content\":\"...\",\"category\":\"...\"}]；没有可提取内容时输出 []。\n\n"
            f"对话内容：\n{dialogue}"
        )
        msg = llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
        data = _as_list(msg.content)
        added = []
        import re
        sensitive = re.compile(r"api[\s_-]?key|密码|身份证|银行卡|访问令牌|token|secret", re.I)
        allowed_categories = {"学生背景", "重要事项", "偏好", "通用"}
        for item in data[:20]:
            content = str(item.get("content", "")).strip()
            category = str(item.get("category", "通用")).strip() or "通用"
            if not content or len(content) < 6 or len(content) > 160 or sensitive.search(content):
                continue
            if category not in allowed_categories:
                category = "通用"
            r = self.add(content, category, source="对话提炼", user_id=user_id)
            if not r.get("duplicated"):
                added.append(content)
            if len(added) >= 10:
                break
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

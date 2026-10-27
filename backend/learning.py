"""自主学习引擎：对话后提炼问答知识对 → 人工审核 → 采纳入知识库（RAG）"""
from db import get_conn, now


class LearningEngine:
    def __init__(self, rag):
        self.rag = rag

    # ---------- 提炼 ----------

    def extract(self, conversation_id, llm):
        """从对话中提炼可沉淀的问答知识对（进入待审核状态）"""
        conn = get_conn()
        rows = conn.execute(
            "SELECT m.id, m.role, m.content, m.rating FROM messages m WHERE m.conversation_id=? AND m.content NOT LIKE '【已作废：示例数据】%' ORDER BY m.id",
            (conversation_id,),
        ).fetchall()
        if not rows:
            return []
        lines = []
        for r in rows:
            tag = "学生" if r["role"] == "user" else "助手"
            mark = "（该回答被学生点了踩）" if r["role"] == "assistant" and r["rating"] == -1 else ""
            lines.append(f"{tag}：{r['content']}{mark}")
        dialogue = "\n".join(lines)

        prompt = (
            "你是知识提炼引擎。请分析下面这段学生与校园助手的对话，提取出值得长期沉淀的「问答知识对」"
            "——以后其他学生问类似问题时可以直接复用的知识（校规、办事流程、校园常识、学习方法等）。\n"
            "要求：\n"
            "1. 只提取对话中有明确依据的内容，不要编造；被点踩的回答不要提取；\n"
            "2. 不要提取只与该学生个人相关的信息（那由记忆系统处理）；\n"
            "3. 每条包含 question（问题）、answer（答案）、note（备注：来源与适用范围，可为空）；\n"
            "4. 输出 JSON 数组，如 [{\"question\":\"...\",\"answer\":\"...\",\"note\":\"...\"}]；没有可提取内容时输出 []。\n\n"
            f"对话内容：\n{dialogue}"
        )
        msg = llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
        data = _as_list(msg.content)
        created = []
        for item in data:
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer", "")).strip()
            if not q or not a:
                continue
            # 去重：同问题已有待审/已采纳条目则跳过
            dup = conn.execute(
                "SELECT id FROM learned WHERE question=? AND status IN ('pending','approved')", (q,)
            ).fetchone()
            if dup:
                continue
            cur = conn.execute(
                "INSERT INTO learned(conversation_id, question, answer, note, created_at) VALUES(?,?,?,?,?)",
                (conversation_id, q, a, str(item.get("note", "")).strip(), now()),
            )
            conn.commit()
            created.append({"id": cur.lastrowid, "question": q})
        return created

    # ---------- 审核 ----------

    def approve(self, lid):
        """采纳：写入知识库并重建索引"""
        conn = get_conn()
        row = conn.execute("SELECT * FROM learned WHERE id=?", (lid,)).fetchone()
        if not row:
            return {"ok": False, "error": "条目不存在"}
        if row["status"] != "pending":
            return {"ok": False, "error": "该条目已处理过"}
        content = f"问：{row['question']}\n答：{row['answer']}"
        if row["note"]:
            content += f"\n备注：{row['note']}"
        doc_id, n_chunks = self.rag.add_document(
            title=row["question"][:50],
            content=content,
            source=f"学习沉淀(对话#{row['conversation_id']})",
            ktype="学习所得",
        )
        conn.execute("UPDATE learned SET status='approved' WHERE id=?", (lid,))
        conn.commit()
        return {"ok": True, "knowledge_id": doc_id, "chunks": n_chunks}

    def reject(self, lid):
        conn = get_conn()
        row = conn.execute("SELECT status FROM learned WHERE id=?", (lid,)).fetchone()
        if not row:
            return {"ok": False, "error": "条目不存在"}
        if row["status"] != "pending":
            return {"ok": False, "error": "该条目已处理过"}
        conn.execute("UPDATE learned SET status='rejected' WHERE id=?", (lid,))
        conn.commit()
        return {"ok": True}

    def list(self, status="pending"):
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, conversation_id, question, answer, note, status, created_at FROM learned "
            "WHERE status=? ORDER BY id DESC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 反馈统计 ----------

    @staticmethod
    def add_feedback(message_id, rating, comment=""):
        conn = get_conn()
        conn.execute(
            "INSERT INTO feedback(message_id, rating, comment, created_at) VALUES(?,?,?,?)",
            (message_id, rating, comment, now()),
        )
        conn.execute(
            "UPDATE messages SET rating=? WHERE id=?",
            (1 if rating == "up" else -1, message_id),
        )
        conn.commit()
        return {"ok": True}

    @staticmethod
    def stats():
        conn = get_conn()

        def cnt(sql):
            return conn.execute(sql).fetchone()["c"]

        return {
            "pending": cnt("SELECT COUNT(*) c FROM learned WHERE status='pending'"),
            "approved": cnt("SELECT COUNT(*) c FROM learned WHERE status='approved'"),
            "rejected": cnt("SELECT COUNT(*) c FROM learned WHERE status='rejected'"),
            "memories": cnt("SELECT COUNT(*) c FROM memories"),
            "knowledge_docs": cnt("SELECT COUNT(*) c FROM knowledge"),
            "conversations": cnt("SELECT COUNT(*) c FROM conversations"),
            "feedback_up": cnt("SELECT COUNT(*) c FROM feedback WHERE rating='up'"),
            "feedback_down": cnt("SELECT COUNT(*) c FROM feedback WHERE rating='down'"),
        }


def _as_list(text):
    from llm import parse_json
    data = parse_json(text)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        inner = data.get("knowledge") or data.get("items") or data.get("data")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    return []

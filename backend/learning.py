"""自主学习引擎：对话后提炼问答知识对 → 人工审核 → 采纳入知识库（RAG）"""
import json

from db import get_conn, now


def _active_school_id():
    try:
        from config import load_config
        return str(load_config().get("current_school_id") or "hlby")
    except Exception:
        return "hlby"


class LearningEngine:
    def __init__(self, rag):
        self.rag = rag

    def analyze_materials(self, document_ids, uploaded_title, uploaded_content, llm, question_count=5, school_id=None, user_id=None):
        """合并当前学校知识库资料与本次上传内容，生成考点、总结和自测题。"""
        conn = get_conn()
        ids = []
        for raw_id in document_ids or []:
            try:
                doc_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if doc_id > 0 and doc_id not in ids:
                ids.append(doc_id)
        ids = ids[:30]
        rows_by_id = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id,title,source,content,ktype FROM knowledge WHERE id IN ({marks}) AND (school_id=? OR school_id='common') AND (owner_user_id IS NULL OR owner_user_id=?)",
                (*ids, str(school_id or _active_school_id()), user_id),
            ).fetchall()
            rows_by_id = {int(row["id"]): row for row in rows}

        materials = []
        blocks = []
        total_chars = 0
        for doc_id in ids:
            row = rows_by_id.get(doc_id)
            if not row or not str(row["content"] or "").strip():
                continue
            title = str(row["title"] or f"资料 {doc_id}").strip()
            content = str(row["content"] or "")[:30000]
            materials.append({"id": doc_id, "title": title, "source": str(row["source"] or ""), "kind": "上传资料" if row["ktype"] == "学习资料" else "知识库"})
            blocks.append(f"【知识库资料：{title}】\n来源：{row['source'] or '未标注'}\n{content}")
            total_chars += len(content)

        upload = str(uploaded_content or "").strip()
        if upload:
            upload_title = str(uploaded_title or "上传资料").strip()[:120] or "上传资料"
            upload = upload[:30000]
            materials.append({"id": None, "title": upload_title, "source": "本次上传", "kind": "上传资料"})
            blocks.append(f"【用户上传资料：{upload_title}】\n来源：用户本次上传\n{upload}")
            total_chars += len(upload)

        if not blocks:
            raise ValueError("请先选择知识库资料或上传资料")

        question_count = max(1, min(20, int(question_count or 5)))
        material_text = "\n\n".join(blocks)
        if len(material_text) > 80000:
            material_text = material_text[:80000] + "\n【资料过长，后续内容未纳入本次分析】"
        prompt = (
            "你是严格依据资料工作的学习辅导引擎。请只根据下面提供的资料生成学习内容，不能使用常识补全，"
            "资料没有说明的内容要明确写‘资料未说明’。用户上传资料与知识库资料都要综合考虑，若内容冲突，"
            "请指出冲突并保留对应来源。\n"
            "请只输出 JSON 对象，不要 Markdown 代码围栏，结构如下：\n"
            '{"overview":"课程/资料总览", "key_points":[{"point":"考点", "explanation":"解释", "source":"资料标题"}], '
            '"questions":[{"type":"选择题/判断题/简答题", "question":"题目", "options":["A..."], '
            '"answer":"答案", "explanation":"解析", "source":"资料标题"}]}\n'
            f"要求：提炼 3-20 个重要考点；生成 {question_count} 道覆盖重点的自测题，题目和答案必须能在资料中找到依据；"
            "选择题提供 options，判断题和简答题 options 为空数组；每个考点和题目尽量标注资料标题。\n\n"
            f"资料：\n{material_text}"
        )
        response = llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        from llm import parse_json
        data = parse_json(getattr(response, "content", ""))
        if not isinstance(data, dict):
            raise RuntimeError("模型未返回可解析的学习分析结果")

        key_points = []
        for item in data.get("key_points", []):
            if not isinstance(item, dict) or not str(item.get("point") or "").strip():
                continue
            key_points.append({
                "point": str(item.get("point") or "").strip(),
                "explanation": str(item.get("explanation") or "资料未说明").strip(),
                "source": str(item.get("source") or "未标注").strip(),
            })
        questions = []
        for item in data.get("questions", [])[:question_count]:
            if not isinstance(item, dict) or not str(item.get("question") or "").strip():
                continue
            options = item.get("options") if isinstance(item.get("options"), list) else []
            questions.append({
                "type": str(item.get("type") or "简答题").strip(),
                "question": str(item.get("question") or "").strip(),
                "options": [str(option).strip() for option in options if str(option).strip()],
                "answer": str(item.get("answer") or "资料未说明").strip(),
                "explanation": str(item.get("explanation") or "资料未说明").strip(),
                "source": str(item.get("source") or "未标注").strip(),
            })
        return {
            "overview": str(data.get("overview") or "资料未提供总览").strip(),
            "key_points": key_points,
            "questions": questions,
            "materials": materials,
            "material_chars": total_chars,
            "question_count": len(questions),
        }

    # ---------- 提炼 ----------

    def extract(self, conversation_id, llm):
        """从对话中提炼可沉淀的问答知识对（进入待审核状态）"""
        conn = get_conn()
        rows = conn.execute(
            "SELECT m.id, m.role, m.content, m.rating, m.source_refs, m.tool_events FROM messages m WHERE m.conversation_id=? AND m.content NOT LIKE '【已作废：示例数据】%' ORDER BY m.id",
            (conversation_id,),
        ).fetchall()
        if not rows:
            return []
        lines = []
        for r in rows:
            tag = "学生" if r["role"] == "user" else "助手"
            mark = "（该回答被学生点了踩）" if r["role"] == "assistant" and r["rating"] == -1 else ""
            evidence = ""
            if r["role"] == "assistant":
                try:
                    refs = json.loads(r["source_refs"] or "[]")
                    ref_text = "；".join(
                        f"{ref.get('title', '未命名')}（{ref.get('source_url') or ref.get('source') or '未标注'}）"
                        for ref in refs[:8] if isinstance(ref, dict)
                    )
                    if ref_text:
                        evidence += f"\n来源参考：{ref_text}"
                except (TypeError, ValueError):
                    pass
                try:
                    events = json.loads(r["tool_events"] or "[]")
                    tool_names = "、".join(str(event.get("name")) for event in events[:8] if isinstance(event, dict) and event.get("name"))
                    if tool_names:
                        evidence += f"\n使用工具：{tool_names}"
                except (TypeError, ValueError):
                    pass
            lines.append(f"{tag}：{r['content']}{mark}{evidence}")
        dialogue = "\n".join(lines)

        prompt = (
            "你是知识提炼引擎。请分析下面这段学生与校园助手的对话，提取出值得长期沉淀的「问答知识对」"
            "——以后其他学生问类似问题时可以直接复用的知识（校规、办事流程、校园常识、学习方法等）。\n"
            "要求：\n"
            "1. 只提取对话中有明确依据、以后可复用的内容，不要编造；被点踩的回答、没有来源支持的猜测不要提取；\n"
            "2. 不要提取只与该学生个人相关的信息（那由记忆系统处理）；\n"
            "3. 不得提取只有‘好/可以/不知道’等低信息量回答；问题和答案都必须足够完整；\n"
            "4. 每条包含 question（问题）、answer（答案）、note（备注：来源、适用范围、可能冲突，可为空）；\n"
            "5. 输出 JSON 数组，如 [{\"question\":\"...\",\"answer\":\"...\",\"note\":\"...\"}]；没有可提取内容时输出 []。\n\n"
            f"对话内容：\n{dialogue}"
        )
        msg = llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
        data = _as_list(msg.content)
        created = []
        existing = {
            "".join(ch for ch in str(row["question"] or "").lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
            for row in conn.execute("SELECT question FROM learned WHERE status IN ('pending','approved')").fetchall()
        }
        for item in data[:20]:
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer", "")).strip()
            q_key = "".join(ch for ch in q.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
            if len(q_key) < 5 or len(a) < 8 or a in ("好", "可以", "不知道", "不清楚") or q_key in existing:
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
            existing.add(q_key)
            if len(created) >= 12:
                break
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

    def list(self, status="pending", school_id=None, user_id=None):
        conn = get_conn()
        if school_id:
            user_clause = " AND c.user_id=?" if user_id is not None else ""
            params = [status, school_id]
            if user_id is not None:
                params.append(user_id)
            rows = conn.execute(
                "SELECT l.id, l.conversation_id, l.question, l.answer, l.note, l.status, l.created_at "
                "FROM learned l JOIN conversations c ON c.id=l.conversation_id "
                f"WHERE l.status=? AND c.school_id=?{user_clause} ORDER BY l.id DESC",
                tuple(params),
            ).fetchall()
        else:
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
    def stats(school_id=None, user_id=None):
        conn = get_conn()

        def cnt(sql):
            return conn.execute(sql).fetchone()["c"]

        if school_id:
            user_clause = " AND c.user_id=?" if user_id is not None else ""
            user_params = (school_id, user_id) if user_id is not None else (school_id,)
            pending = conn.execute(
                "SELECT COUNT(*) c FROM learned l JOIN conversations c ON c.id=l.conversation_id "
                f"WHERE l.status='pending' AND c.school_id=?{user_clause}", user_params
            ).fetchone()["c"]
            approved = conn.execute(
                "SELECT COUNT(*) c FROM learned l JOIN conversations c ON c.id=l.conversation_id "
                f"WHERE l.status='approved' AND c.school_id=?{user_clause}", user_params
            ).fetchone()["c"]
            rejected = conn.execute(
                "SELECT COUNT(*) c FROM learned l JOIN conversations c ON c.id=l.conversation_id "
                f"WHERE l.status='rejected' AND c.school_id=?{user_clause}", user_params
            ).fetchone()["c"]
            knowledge_docs = conn.execute(
                "SELECT COUNT(*) c FROM knowledge WHERE ktype='学习资料' AND (school_id=? OR school_id='common') AND (owner_user_id IS NULL OR owner_user_id=?)", (school_id, user_id)
            ).fetchone()["c"]
            conversations = conn.execute(
                "SELECT COUNT(*) c FROM conversations WHERE school_id=?" + (" AND user_id=?" if user_id is not None else ""), user_params
            ).fetchone()["c"]
            feedback_up = conn.execute(
                "SELECT COUNT(*) c FROM feedback f JOIN messages m ON m.id=f.message_id "
                "JOIN conversations c ON c.id=m.conversation_id WHERE f.rating='up' AND c.school_id=?" + (" AND c.user_id=?" if user_id is not None else ""), user_params
            ).fetchone()["c"]
            feedback_down = conn.execute(
                "SELECT COUNT(*) c FROM feedback f JOIN messages m ON m.id=f.message_id "
                "JOIN conversations c ON c.id=m.conversation_id WHERE f.rating='down' AND c.school_id=?" + (" AND c.user_id=?" if user_id is not None else ""), user_params
            ).fetchone()["c"]
            return {
                "pending": pending, "approved": approved, "rejected": rejected,
                "memories": conn.execute("SELECT COUNT(*) c FROM memories WHERE user_id=?", (user_id,)).fetchone()["c"] if user_id is not None else cnt("SELECT COUNT(*) c FROM memories WHERE user_id IS NULL"),
                "knowledge_docs": knowledge_docs, "conversations": conversations,
                "feedback_up": feedback_up, "feedback_down": feedback_down,
            }

        return {
            "pending": cnt("SELECT COUNT(*) c FROM learned WHERE status='pending'"),
            "approved": cnt("SELECT COUNT(*) c FROM learned WHERE status='approved'"),
            "rejected": cnt("SELECT COUNT(*) c FROM learned WHERE status='rejected'"),
            "memories": cnt("SELECT COUNT(*) c FROM memories WHERE user_id IS NULL"),
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

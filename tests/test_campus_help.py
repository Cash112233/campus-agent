"""第一阶段：相关资料、办事联系人与来源留存的行为测试。"""
from pathlib import Path
import importlib.util
import json
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import db
from rag import RAG
from agent import Agent

class CampusHelpTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.patches = [patch("db.get_conn", return_value=self.conn), patch("rag.get_conn", return_value=self.conn), patch("agent.get_conn", return_value=self.conn)]
        for item in self.patches: item.start()
        db.init_db()
        self.rag = RAG()
    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.conn.close()
    def add(self, title, content, url="https://www.byau.edu.cn/example/page.htm"):
        return self.rag.add_document(title, content, "官网:" + url, "官网爬取")[0]
    def test_unrelated_campus_news_is_not_a_card_replacement_guide(self):
        self.add("校园植物活动", "校园植物科普活动在校园内进行，师生参观校园植物景观。")
        self.assertEqual(self.rag.search("校园卡补办"), [])
    def test_phone_directory_cannot_answer_library_opening_hours(self):
        self.add("电话查询", "图书馆 | 咨询服务部 | 6819375 | 图书馆三层中区")
        self.assertEqual(self.rag.search("图书馆几点开门"), [])
    def test_card_alias_and_title_retrieve_the_actual_guide(self):
        self.add("校园卡补办指南", "丢失后请先办理挂失，补办所需材料以窗口说明为准。")
        self.add("图书馆活动", "校园文化校园生活校园活动，欢迎同学参加。")
        hits = self.rag.search("饭卡丢了怎么补办")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["title"], "校园卡补办指南")
        self.assertEqual(hits[0].get("source_url"), "https://www.byau.edu.cn/example/page.htm")
        self.assertTrue(hits[0].get("indexed_at"))
    def test_title_match_ranks_the_actual_statute_first(self):
        self.add("高等学校章程制定办法", "制定章程的程序和章程管理的规定。")
        self.add("学校章程", "第一章：本校的办学宗旨与组织机构。")
        self.assertEqual(self.rag.search("学校章程")[0]["title"], "学校章程")
    def test_scoped_notice_is_not_applied_to_another_college(self):
        self.add("工程学院学生请假申请规定", "学生通过门户提出请假申请，需提供证明。")
        self.assertEqual(self.rag.search("应用科技学院请假流程"), [])
    def test_agent_can_use_official_contact_and_service_tools(self):
        import tools
        names = {row["function"]["name"] for row in tools.TOOL_SCHEMAS}
        self.assertIn("find_campus_contact", names)
        self.assertIn("get_campus_service", names)
        self.add("电话查询", "教务处 | 学籍科 | 6819092 | 主楼802", "https://www.byau.edu.cn/921/list.htm")
        previous = tools._ctx.copy()
        with patch("services.get_conn", return_value=self.conn):
            try:
                tools.set_context(self.rag, None)
                result = tools.run_tool("find_campus_contact", {"query": "学籍科"})
                self.assertEqual(result["results"][0]["phone"], "6819092")
                detail = tools.run_tool("get_campus_service", {"service_id": "campus-card"})
                self.assertEqual(detail["status"], "needs_sources")
            finally: tools._ctx.update(previous)

    def test_legacy_unsourced_phone_summary_cannot_supply_policy(self):
        self.rag.add_document("旧电话簿", "校园卡补卡需要15元，到模拟卡务窗口补办。", "电话簿:旧整理.md", "电话簿")
        self.assertEqual(self.rag.search("校园卡补办"), [])

    def test_service_detail_can_include_the_students_college_contacts(self):
        import services
        self.add("电话查询", "党委学生工作部 | 本科生管理科 | 6819046 | 主楼839\n应用科技学院 | 党委副书记 | 6819352 | 二教403", "https://www.byau.edu.cn/921/list.htm")
        self.assertIn("college", __import__('inspect').signature(services.service_detail).parameters)
        with patch("services.get_conn", return_value=self.conn):
            detail = services.service_detail("leave", self.rag, college="应用科技学院")
            self.assertEqual(detail["college_contacts"][0]["phone"], "6819352")
            self.assertIn("不等于审批人", detail["notice"])

    def test_contact_query_accepts_department_plus_role(self):
        import services
        self.add("电话查询", "教务处（教师教学发展中心） | 学籍科副科长 | 6819092 | 主楼802", "https://www.byau.edu.cn/921/list.htm")
        with patch("services.get_conn", return_value=self.conn):
            rows = services.list_contacts("教务处学籍科电话")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["phone"], "6819092")

    def test_college_names_are_not_matched_as_other_colleges_substrings(self):
        self.add("经济管理学院学生请假申请规定", "学生通过门户提出请假申请并提交材料。")
        self.assertEqual(self.rag.search("理学院请假流程"), [])

    def test_source_refs_survive_chat_storage(self):
        self.add("学校章程", "学校章程介绍本校办学宗旨和组织机构。")
        agent = Agent(self.rag, SimpleNamespace(search=lambda *a, **k: []), None, lambda: {"api_key": "test", "model": "test"})
        with patch("agent.LLMClient") as client:
            client.return_value.chat_stream.return_value = iter([{"type": "final", "message": SimpleNamespace(content="请参考学校章程原文。", tool_calls=None)}])
            events = list(agent.chat(None, "查询学校章程"))
        answer = next(x["data"] for x in events if x["type"] == "answer")
        self.assertTrue(answer.get("source_refs"))
        row = self.conn.execute("SELECT * FROM messages WHERE id=?", (answer["message_id"],)).fetchone()
        self.assertEqual(json.loads(row["source_refs"]), answer["source_refs"])
    def test_fetch_timestamp_is_not_a_publication_date(self):
        from retrieval_rules import source_meta
        meta = source_meta({"source": "官网:https://nic.byau.edu.cn/190/list.htm", "ktype": "官网爬取", "indexed_at": "2026-10-26"}, "资料采集时间：2026-10-26\n门户帮助正文")
        self.assertIsNone(meta["published_at"])

    def test_db_migrates_old_message_table_without_losing_history(self):
        self.conn.execute("DROP TABLE messages")
        self.conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, conversation_id INTEGER, role TEXT, content TEXT, tool_events TEXT, rating INTEGER, created_at TEXT)")
        self.conn.execute("INSERT INTO messages(id,content) VALUES(10,'原有真实对话')")
        self.conn.commit()
        db.init_db(); db.init_db()
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        self.assertIn("source_refs", columns)
        self.assertEqual(self.conn.execute("SELECT content FROM messages WHERE id=10").fetchone()[0], "原有真实对话")
    def test_service_directory_reads_official_rows_and_does_not_make_up_steps(self):
        self.assertTrue((ROOT / "backend/services.py").exists(), "需要办事导航实现")
        spec = importlib.util.spec_from_file_location("campus_services_test", ROOT / "backend/services.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.add("电话查询", "办 公 电 话 号 码 簿\n部 门 | 职 务 | 办公电话 | 办公地址\n教务处 | 学籍科 | 6819092 | 主楼802\n教务科 | 6819093 | 主楼802", "https://www.byau.edu.cn/921/list.htm")
        with patch.object(module, "get_conn", return_value=self.conn):
            contacts = module.list_contacts("学籍科")
            self.assertEqual(len(contacts), 1)
            self.assertEqual(contacts[0]["phone"], "6819092")
            self.assertEqual(contacts[0]["department"], "教务处")
            guide = module.service_detail("campus-card", self.rag)
            self.assertEqual(guide["references"], [])
            self.assertEqual(guide["status"], "needs_sources")
            self.assertNotIn("steps", guide)
        self.assertIsNone(module.service_detail("unknown", self.rag))

if __name__ == "__main__": unittest.main()

"""删除演示数据后的真实性回归：缺少接入时不能声称无课或已达标。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import db
import tools
from rag import RAG


class DemoRemovalTests(unittest.TestCase):
    def test_personal_tools_report_unavailable_without_real_data(self):
        cases = [
            ("query_course_schedule", {}),
            ("query_grades", {}),
            ("query_empty_classroom", {"weekday": "周一"}),
            ("detect_schedule_conflict", {"candidates": [{"name": "选修", "weekday": "周一", "time": "08:00-09:40"}]}),
            ("audit_credits", {"completed_courses": [{"name": "课程", "credit": 4, "category": "专业必修"}]}),
        ]
        for name, args in cases:
            with self.subTest(tool=name):
                result = tools.run_tool(name, args)
                self.assertEqual(result.get("available"), False)
                self.assertIn("未接入", result.get("message", ""))

    def test_calendar_and_notices_use_documents_instead_of_constants(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        with patch.object(db, "get_conn", return_value=conn), patch("rag.get_conn", return_value=conn):
            db.init_db()
            rag = RAG()
            rag.add_document("校历", "学期校历：考试安排请查看原文。", "官网:https://www.byau.edu.cn/919/list.htm", "官网爬取")
            rag.add_document("放假通知", "中秋节放假安排及补课通知。", "官网:https://www.byau.edu.cn/notice/page.htm", "官网爬取")
            old = tools._ctx.copy()
            try:
                tools.set_context(rag, None)
                cal = tools.query_academic_calendar()
                self.assertTrue(cal.get("results"))
                self.assertEqual(cal["results"][0]["title"], "校历")
                notices = tools.search_campus_notice("中秋放假")
                self.assertTrue(notices["matches"])
                self.assertEqual(notices["matches"][0]["title"], "放假通知")
                self.assertIn("byau.edu.cn", notices["matches"][0]["source"])
            finally:
                tools._ctx.update(old)
        conn.close()

    def test_invalidated_history_cannot_be_learned_again(self):
        import sqlite3
        from types import SimpleNamespace
        from memory import MemoryManager
        from learning import LearningEngine
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        with patch.object(db, "get_conn", return_value=conn), patch("memory.get_conn", return_value=conn), patch("learning.get_conn", return_value=conn):
            db.init_db()
            conn.executemany("INSERT INTO messages(conversation_id,role,content) VALUES(1,?,?)", [
                ("user", "我是应用科技学院学生"),
                ("assistant", "【已作废：示例数据】模拟课表陈刚A教学楼302"),
            ])
            conn.commit()
            prompts = []
            class FakeLLM:
                def chat(self, messages, **kwargs):
                    prompts.append(messages[0]["content"])
                    return SimpleNamespace(content="[]")
            MemoryManager().extract_from_conversation(1, FakeLLM())
            LearningEngine(None).extract(1, FakeLLM())
            self.assertEqual(len(prompts), 2)
            for prompt in prompts:
                self.assertNotIn("模拟课表陈刚", prompt)
                self.assertIn("应用科技学院学生", prompt)
        conn.close()

    def test_deadlines_do_not_invent_calendar_nodes(self):
        result = tools.countdown_deadlines([{"title": "提交论文", "date": "2099-01-01"}])
        self.assertEqual(result["calendar"], [])
        self.assertIn("校历", result.get("calendar_note", ""))
        self.assertEqual(result["items"][0]["title"], "提交论文")

    def test_brief_does_not_treat_missing_schedule_as_no_classes(self):
        old = tools._ctx.copy()
        try:
            tools.set_context(None, None)
            result = tools.daily_brief()
            self.assertEqual(result.get("schedule_available"), False)
            self.assertNotIn("course_count", result)
            self.assertNotIn("semester", result)
        finally:
            tools._ctx.update(old)


class MCPDemoRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_calendar_notices_and_deadlines_have_no_demo_data(self):
        import importlib.util
        import json
        spec = importlib.util.spec_from_file_location("campus_mcp_cleanup_test", Path(__file__).resolve().parents[1] / "mcp-server/server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cal = json.loads(await module.campus_query_academic_calendar())
        self.assertFalse(cal.get("available", True))
        notices = json.loads(await module.campus_search_notice(module.NoticeInput(keyword="奖学金")))
        self.assertFalse(notices.get("available", True))
        self.assertEqual(notices["matches"], [])
        deadlines = json.loads(await module.campus_countdown_deadlines(module.DeadlineInput(items=[module.DeadlineItem(title="提交论文", date="2099-01-01")])))
        self.assertEqual(deadlines["calendar"], [])
        self.assertIn("未接入", deadlines.get("calendar_note", ""))


if __name__ == "__main__":
    unittest.main()

"""用户资料导入：完整性、上下文和重复更新。"""
import importlib
from pathlib import Path
import sqlite3
import struct
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import db
from rag import RAG

class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module("material_import")
    def test_word_piece_table_obeys_main_text_length_and_unicode(self):
        word = bytearray(1024)
        struct.pack_into("<H", word, 0, 0xa5ec)
        struct.pack_into("<H", word, 32, 14)
        struct.pack_into("<H", word, 62, 22)
        struct.pack_into("<I", word, 76, 4)
        struct.pack_into("<H", word, 152, 93)
        struct.pack_into("<II", word, 154+33*8, 0, 21)
        word[700:710] = "请假规定尾".encode("utf-16le")
        plc = struct.pack("<II", 0, 5) + struct.pack("<HIH",0,700,0)
        text, details = self.m.word_main_text(bytes(word), b"\x02"+struct.pack("<I",len(plc))+plc)
        self.assertEqual(text, "请假规定")
        self.assertEqual(details["main_characters"],4)
    def test_menu_chunks_preserve_hall_window_headers_prices_and_notes(self):
        rows = [f"| 菜品{i} | {i}.5 | {i+2}.5 |" for i in range(100)]
        text = "# 二食堂一楼\n> 价格单位：元。加量栏对应大碗。\n## 1. 面馆\n> 免费加面。\n### 汤面\n| 品名 | 标准 | 加量 |\n|---|---|---|\n"+"\n".join(rows)
        docs = self.m.menu_documents(text,"二食堂一楼菜单价格汇总.md")
        chunks = [ch for d in docs for ch in d["chunks"]]
        self.assertGreater(len(chunks),1)
        for row in rows: self.assertTrue(any(row in ch for ch in chunks))
        for ch in chunks:
            for s in ["二食堂一楼", "面馆", "汤面", "价格单位", "免费加面", "| 品名 | 标准 | 加量 |"]: self.assertIn(s,ch)
    def test_handbook_sections_do_not_merge_distinct_regulations(self):
        text = "黑龙江八一农垦大学\n本科生手册\n学生请假管理办法\n第一章 总则\n第一条 学生请假应办理申请。\n第二条 请假材料说明。\n学生考试管理规定\n第一章 总则\n第一条 考试安排。"
        docs = self.m.handbook_documents(text, "本科生手册--20241020.doc")
        leave = [d for d in docs if "学生请假管理办法" in d["title"]]
        self.assertTrue(leave)
        self.assertIn("第二条", "\n".join(d["content"] for d in leave))
        self.assertNotIn("考试安排", "\n".join(d["content"] for d in leave))
        self.assertTrue(all("20241020" in d["content"] for d in docs))
    def test_upsert_replaces_only_matching_source_and_preserves_id(self):
        con=sqlite3.connect(":memory:"); con.row_factory=sqlite3.Row
        with patch("db.get_conn",return_value=con): db.init_db()
        d={"title":"食堂一楼面馆","content":"原价","chunks":["原价"],"ktype":"食堂资料","source":"用户提供:test.md#section-1"}
        self.m.upsert_documents(con,[d]); before=con.execute("select id from knowledge").fetchone()[0]
        d.update(content="新价",chunks=["新价"]); self.m.upsert_documents(con,[d])
        self.assertEqual(con.execute("select id,content from knowledge").fetchone()[:],(before,"新价"))
        self.assertEqual(con.execute("select count(*) from knowledge_chunks").fetchone()[0],1)
        con.close()
    def test_handbook_without_numbered_articles_remains_retrievable_in_full(self):
        lines=[f"借阅说明{i}："+"借阅规则。"*20 for i in range(60)]
        docs=self.m.handbook_documents("学生图书借阅规定\n"+"\n".join(lines),"本科生手册--20241020.doc")
        self.assertGreater(len(docs),2)
        for line in lines: self.assertTrue(any(line in d['content'] for d in docs))
        self.assertLess(max(len(ch) for d in docs for ch in d['chunks']),2200)
    def test_short_menu_keeps_price_table_with_its_trailing_note(self):
        text='# 一食堂\n## 一楼\n### 套餐饭\n| 套餐 | 价格 |\n|---|---|\n| 五元套餐 | 5元 |\n\n> 开餐时间 11:00-12:30。'
        docs=self.m.menu_documents(text,'一食堂.md')
        self.assertEqual(len(docs[0]['chunks']),1)
        self.assertIn('5元',docs[0]['chunks'][0])
        self.assertIn('11:00',docs[0]['chunks'][0])
    def test_dining_hall_and_floor_are_not_cross_matched(self):
        from retrieval_rules import query_terms,is_relevant
        q,g,_=query_terms("二食堂三楼牛肉面多少钱")
        self.assertFalse(is_relevant(q,g,"一食堂三楼牛肉面","牛肉面12元"))
        self.assertFalse(is_relevant(q,g,"二食堂二楼牛肉面","牛肉面12元"))
        self.assertTrue(is_relevant(q,g,"二食堂三楼牛肉面","牛肉面12元"))
    def test_handbook_is_labeled_uploaded_and_available_in_service_navigation(self):
        import services
        from retrieval_rules import source_meta
        self.assertIn("用户提供",source_meta({"source":"用户提供:手册.doc","ktype":"本科生手册"},"")["source_kind"])
        con=sqlite3.connect(":memory:");con.row_factory=sqlite3.Row
        with patch("db.get_conn",return_value=con),patch("rag.get_conn",return_value=con),patch("services.get_conn",return_value=con):
            db.init_db();r=RAG();r.add_document("本科生请假管理办法","请假应提交申请办理审批。","用户提供:手册.doc","本科生手册")
            self.assertTrue(services.service_detail("leave",r)["references"])
        con.close()

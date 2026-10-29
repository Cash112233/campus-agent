"""单一对话入口：配置与管理卡片无需模型，且只持久化卡片类型。"""
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import db
from agent import Agent


class ChatPanelTests(unittest.TestCase):
    def setUp(self):
        self.panels=importlib.import_module('chat_panels')
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.patches=[patch('db.get_conn',return_value=self.conn),patch('agent.get_conn',return_value=self.conn)]
        for p in self.patches:p.start()
        db.init_db()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.conn.close()
    def test_explicit_management_requests_open_the_right_inline_card(self):
        for query,panel in [('办事导航','services'),('查看知识库','knowledge'),('补充知识库','knowledge'),('完善知识库','knowledge'),('看看你记住了我什么','memory'),('审核学习内容','learning'),('修改设置','settings'),('切换学校','settings'),('学校模板','settings'),('更换学校','settings')]:
            with self.subTest(query=query):self.assertEqual(self.panels.match_panel_request(query),panel)
        for query in ['请假怎么办','知识库里转专业有什么条件','你还记得我在哪个学院吗','中午吃什么']:
            self.assertIsNone(self.panels.match_panel_request(query))
    def test_all_five_panels_can_be_opened_without_a_key_and_survive_reload(self):
        agent=Agent(None,None,None,lambda:{})
        with patch('agent.LLMClient') as llm:
            for q,panel in [('办事导航','services'),('查看知识库','knowledge'),('查看记忆','memory'),('学习中心','learning'),('修改设置','settings')]:
                events=list(agent.chat(None,q))
                answer=next(e['data'] for e in events if e['type']=='answer')
                row=self.conn.execute('SELECT * FROM messages WHERE id=?',(answer['message_id'],)).fetchone()
                stored=json.loads(row['tool_events'])
                self.assertEqual(stored[0]['result']['ui_panel'],panel)
                self.assertNotIn('api_key',row['tool_events'])
            llm.assert_not_called()
    def test_panel_tool_rejects_unknown_panels(self):
        import tools
        self.assertEqual(tools.run_tool('open_chat_panel',{'panel':'settings'})['ui_panel'],'settings')
        with self.assertRaises(ValueError):tools.run_tool('open_chat_panel',{'panel':'arbitrary-html'})

    def test_school_template_request_focuses_school_picker(self):
        agent=Agent(None,None,None,lambda:{})
        events=list(agent.chat(None,'切换学校'))
        result=next(e['data']['result'] for e in events if e['type']=='tool_result')
        self.assertEqual(result['ui_panel'],'settings')
        self.assertEqual(result['focus'],'school')

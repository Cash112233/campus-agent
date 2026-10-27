"""从真实来源随机挑选正餐，聊天入口保证数量并保留来源。"""
import importlib
import json
from pathlib import Path
import random
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import db
from agent import Agent


class DiningTests(unittest.TestCase):
    def setUp(self):
        self.d = importlib.import_module('dining')
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.patches = [patch('db.get_conn', return_value=self.conn), patch('agent.get_conn', return_value=self.conn), patch('dining.get_conn', return_value=self.conn)]
        for p in self.patches: p.start()
        db.init_db()
        for n in range(12):
            hall = '一食堂' if n < 6 else '二食堂'
            self.add(f'[食堂] {hall}一楼 / {n+1}. 面馆{n}', f'| 菜品 | 价格 | 菜品 | 价格 |\n|---|---|---|---|\n| 牛肉面{n} | 12 | 鸡腿饭{n} | 15 |')

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.conn.close()

    def add(self, title, content, kind='食堂资料'):
        cur = self.conn.execute('INSERT INTO knowledge(title,content,ktype,source,created_at) VALUES(?,?,?,?,?)', (title,content,kind,'用户提供:菜单.md#'+title,'2026-10-27'))
        self.conn.commit()
        return cur.lastrowid

    def test_generic_food_intents_do_not_capture_unrelated_questions(self):
        for q in ['中午吃什么', '推荐一下吃什么', '今天晚饭吃啥好？', '推荐一下食堂吃的', '午饭推荐几个']:
            self.assertTrue(self.d.is_food_request(q),q)
        for q in ['推荐一本书', '杨枝甘露多少钱', '食堂营业时间', '不要推荐吃什么，先查通知', '感冒吃什么药', '推荐一下今晚吃什么药']:
            self.assertFalse(self.d.is_food_request(q),q)

    def test_random_three_to_five_unique_dishes_with_actual_sources(self):
        variants=set()
        for seed in range(15):
            result=self.d.recommend_food('中午吃什么',rng=random.Random(seed))
            rows=result['results']
            self.assertIn(len(rows),[3,4,5])
            self.assertEqual(len({r['dish'] for r in rows}),len(rows))
            self.assertEqual(len({r['location'] for r in rows}),len(rows))
            for r in rows:
                doc=self.conn.execute('SELECT * FROM knowledge WHERE id=?',(r['doc_id'],)).fetchone()
                self.assertIn(r['dish'],doc['content'])
                self.assertEqual(r['source'],doc['source'])
            variants.add(tuple(r['dish'] for r in rows))
        self.assertGreater(len(variants),1)

    def test_location_and_budget_constraints_are_respected(self):
        rows=self.d.recommend_food('二食堂一楼，12元以内，中午吃什么',rng=random.Random(4))['results']
        self.assertGreaterEqual(len(rows),3)
        self.assertTrue(all('二食堂一楼' in r['location'] for r in rows))
        self.assertTrue(all('12' in r['price'] for r in rows))

    def test_multiple_prices_keep_original_portion_headers(self):
        self.add('[食堂] 二食堂二楼 / 1. 牛肉饭', '| 菜品 | 价格 | 加量 |\n|---|---|---|\n| 牛肉饭 | 10 | 13 |')
        rows=self.d.recommend_food('二食堂二楼吃什么',rng=random.Random(1))['results']
        self.assertEqual(len(rows),1)
        self.assertIn('10',rows[0]['price']);self.assertIn('13',rows[0]['price']);self.assertIn('加量',rows[0]['price'])

    def test_drinks_addons_and_unsourced_examples_are_not_lunch(self):
        self.add('[食堂] 二食堂二楼 / 1. 水吧', '| 菜品 | 价格 |\n|---|---|\n| 奶茶 | 9 |')
        self.add('[食堂] 二食堂二楼 / 2. 面馆 / 加料', '| 菜品 | 价格 |\n|---|---|\n| 鸡腿 | 5 |')
        self.add('演示牛肉饭','| 菜品 | 价格 |\n|---|---|\n| 示例牛肉饭 | 1 |','示例')
        result=self.d.recommend_food('二食堂二楼吃什么',rng=random.Random(1))
        self.assertEqual(result['results'],[])
        self.assertIn('没有',self.d.format_recommendations(result))

    def test_chat_routes_without_llm_and_persists_sources_and_followup(self):
        agent=Agent(None,None,None,lambda:{})
        with patch('agent.LLMClient') as llm:
            events=list(agent.chat(None,'中午吃什么'))
            cid=next(e['data'] for e in events if e['type']=='conversation')
            answer=next(e['data'] for e in events if e['type']=='answer')
            first=next(e['data']['result'] for e in events if e['type']=='tool_result')['results']
            second_events=list(agent.chat(cid,'换一批'))
            second=next(e['data']['result'] for e in second_events if e['type']=='tool_result')['results']
            llm.assert_not_called()
        self.assertTrue(set(r['dish'] for r in first).isdisjoint(r['dish'] for r in second))
        self.assertEqual(len(answer['source_refs']),len(first))
        stored=self.conn.execute('SELECT source_refs FROM messages WHERE id=?',(answer['message_id'],)).fetchone()[0]
        self.assertEqual(json.loads(stored),answer['source_refs'])

    def test_food_tool_is_available_to_the_agent(self):
        import tools
        self.assertIn('recommend_campus_food',{row['function']['name'] for row in tools.TOOL_SCHEMAS})
        result=tools.run_tool('recommend_campus_food',{'query':'中午吃什么'})
        self.assertIn(result['count'],[3,4,5])

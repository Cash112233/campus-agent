"""官网更新只替换对应来源，保留其他已入库资料与文档编号。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch, mock_open

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'output'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import byau_import_rag as importer


class ImportTests(unittest.TestCase):
    def test_http_and_https_source_keys_are_equivalent(self):
        self.assertEqual(importer.norm_url('官网:http://www.byau.edu.cn/a.htm'),
                         importer.norm_url('官网:https://www.byau.edu.cn/a.htm'))

    def test_only_refreshed_fulltext_is_eligible_for_import(self):
        data = {
            'https://www.byau.edu.cn/a.htm': {'text': '完整正文。'*30, 'chars':150,
                'extraction_version':2, 'content_status':'fulltext', 'status':200},
            'https://www.byau.edu.cn/b.htm': {'text': '旧摘要。'*30, 'chars':120},
        }
        with patch('builtins.open', mock_open(read_data=json.dumps(data))):
            _, _, pages = importer.load_pages()
        self.assertEqual([p['url'] for p in pages], ['https://www.byau.edu.cn/a.htm'])

    def test_update_preserves_ids_and_unfetched_documents(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE knowledge(id INTEGER PRIMARY KEY, title TEXT, source TEXT,
                content TEXT, ktype TEXT, created_at TEXT, hits INTEGER DEFAULT 0);
            CREATE TABLE knowledge_chunks(id INTEGER PRIMARY KEY, doc_id INTEGER,
                chunk_index INTEGER, content TEXT);
            INSERT INTO knowledge VALUES(7,'旧文章','官网:https://www.byau.edu.cn/a.htm',
                '旧摘要','官网爬取','旧时间',3);
            INSERT INTO knowledge VALUES(8,'未重新抓取的文章','官网:https://www.byau.edu.cn/b.htm',
                '应保留内容','官网爬取','旧时间',2);
            INSERT INTO knowledge_chunks VALUES(1,7,0,'旧摘要');
            INSERT INTO knowledge_chunks VALUES(2,8,0,'应保留内容');
        ''')
        updated = [('新文章','官网:https://www.byau.edu.cn/a.htm','完整正文。'*30,'官网爬取')]
        with patch.object(importer, 'get_conn', return_value=conn), \
             patch.object(importer, 'load_pages', return_value=(1,1,[])), \
             patch.object(importer, 'build_docs', return_value=updated), redirect_stdout(io.StringIO()):
            importer.cmd_import()
        row = conn.execute('SELECT content FROM knowledge WHERE id=7').fetchone()
        self.assertIsNotNone(row, '更新后应保留原文档编号')
        self.assertEqual(row[0], '完整正文。'*30)
        self.assertEqual(conn.execute('SELECT hits FROM knowledge WHERE id=7').fetchone()[0], 3)
        self.assertEqual(conn.execute('SELECT content FROM knowledge WHERE id=8').fetchone()[0], '应保留内容')
        self.assertEqual(conn.execute('SELECT count(*) FROM knowledge_chunks WHERE doc_id=7').fetchone()[0], 1)
        conn.close()

"""官网正文提取和链接范围回归测试（不访问网络）。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'output'))
import byau_crawler as crawler


class ExtractionTests(unittest.TestCase):
    def test_full_article_wins_over_description(self):
        opening = '学校举办校园服务说明会。' * 60
        page = ('<html><head><meta name="description" content="' + opening[:500] + '"></head>'
                '<body><nav>导航噪声</nav><div class="wp_articlecontent">'
                '<p>' + opening + '</p><div><p>正文最后一段必须完整保留。</p></div>'
                '</div><footer>友情链接噪声</footer></body></html>')
        text = crawler.extract_text(page)
        self.assertIn('正文最后一段必须完整保留。', text)
        self.assertGreater(len(text), 500)
        self.assertNotIn('友情链接噪声', text)
        self.assertNotIn('导航噪声', text)

    def test_phone_table_preserves_cell_boundaries(self):
        page = ('<div class="wp_articlecontent"><table><tr><th>部门</th><th>电话</th></tr>'
                '<tr><td>教务科</td><td>6819093</td></tr></table></div>')
        text = crawler.extract_text(page)
        self.assertIn('教务科 | 6819093', text)
        self.assertIn('部门 | 电话', text)

    def test_title_keeps_punctuation_and_date(self):
        self.assertEqual(crawler.extract_title('<title>2026-2027学年教学安排—通知</title>'),
                         '2026-2027学年教学安排—通知')

    def test_urls_are_canonical_and_non_pages_are_excluded(self):
        self.assertEqual(crawler.normalize('http://www.byau.edu.cn/896/list.htm#top'),
                         'https://www.byau.edu.cn/896/list.htm')
        self.assertIsNone(crawler.normalize('/file.xlsx'))
        self.assertIsNone(crawler.normalize('https://example.com/page.htm'))
        self.assertIsNone(crawler.normalize('/_visitcount?articleId=123'))


class RefreshTests(unittest.TestCase):
    def test_cached_pages_are_refetched_and_updated(self):
        article = 'https://www.byau.edu.cn/2026/1022/c906a153760/page.htm'
        old = {crawler.BASE: {'text': '旧首页'}, article: {'text': '旧摘要'}}
        page = '<title>新正文</title><div class="wp_articlecontent">' + '完整正文。' * 40 + '</div>'
        with patch.object(crawler, 'visited', old), patch.object(crawler, 'queue', deque()), \
             patch.object(crawler, 'MAX_PAGES', 2), patch.object(crawler, 'DELAY', 0), \
             patch.object(crawler, 'load_state'), patch.object(crawler, 'save_state'), \
             patch.object(crawler, '_knowledge_urls', return_value=[], create=True), \
             patch.object(crawler, 'fetch', return_value=(200, page)) as fetch:
            crawler.main()
            self.assertEqual(fetch.call_count, 2)
            self.assertIn('完整正文。', crawler.visited[article]['text'])
            self.assertEqual(crawler.visited[article].get('extraction_version'), 2)


if __name__ == '__main__':
    unittest.main()

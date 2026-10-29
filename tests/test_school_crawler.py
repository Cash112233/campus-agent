"""通用学校官网爬虫与按学校入库测试，不访问真实网络。"""
import sqlite3
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db
import school_crawler


class SchoolCrawlerTests(unittest.TestCase):
    def test_urls_stay_on_the_official_host_and_skip_attachments(self):
        base = "https://www.example.edu.cn/"
        self.assertEqual(school_crawler.normalize_url("/jwc/notice.htm#top", base), "https://www.example.edu.cn/jwc/notice.htm")
        self.assertEqual(school_crawler.normalize_url("https://news.example.edu.cn/a.html", base), "https://news.example.edu.cn/a.html")
        self.assertIsNone(school_crawler.normalize_url("https://evil.example.com/a.html", base))
        self.assertIsNone(school_crawler.normalize_url("/files/handbook.pdf", base))

    def test_crawl_selects_useful_pages_and_discovers_links(self):
        base = "https://www.example.edu.cn/"
        pages = {
            base: '<title>学校首页</title><main><a href="/jwc/notice.htm">教务通知</a></main>',
            "https://www.example.edu.cn/jwc/notice.htm": (
                '<title>关于考试安排的通知</title><article>'
                + "教务处发布考试安排通知，学生可查看校历、报名和成绩查询说明。" * 8
                + "</article>"
            ),
        }
        def fetcher(url, timeout):
            return (200, pages[url])
        report = school_crawler.crawl_site(base, max_pages=5, delay=0, fetcher=fetcher)
        self.assertEqual(report["visited"], 2)
        self.assertEqual(report["selected"], 1)
        self.assertIn("考试安排", report["pages"][0]["title"])
        self.assertIn("教务", report["pages"][0]["keywords"])

    def test_import_is_idempotent_and_keeps_school_scope(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        with patch("db.get_conn", return_value=conn):
            db.init_db()
            page = {"url": "https://www.example.edu.cn/jwc/notice.htm", "title": "考试安排通知", "content": "教务处考试安排通知。" * 80}
            first = school_crawler.import_pages([page], "hlj-001")
            second = school_crawler.import_pages([page], "hlj-001")
            self.assertEqual(first["added"], 1)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM knowledge WHERE school_id='hlj-001'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM knowledge WHERE school_id='hlby'").fetchone()[0], 0)
        conn.close()

    def test_import_requires_a_configured_website(self):
        cfg = {"current_school_id": "hlj-001", "custom_profiles": [], "profile_overrides": {}}
        with self.assertRaisesRegex(ValueError, "尚未配置官网"):
            school_crawler.crawl_and_import("hlj-001", cfg=cfg, fetcher=lambda *_: (200, ""))


if __name__ == "__main__":
    unittest.main()

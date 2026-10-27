"""PDF 附件发现与文字提取测试，使用含 PDF 依赖的运行时执行。"""
import io
import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'output'))
import byau_crawler as crawler


class PdfTests(unittest.TestCase):
    def test_embedded_pdf_links_are_resolved_and_limited_to_official_host(self):
        finder = getattr(crawler, 'pdf_links', None)
        self.assertIsNotNone(finder, '需要发现 iframe 和脚本中的 PDF 地址')
        html = ('<script>viewer({src:"/_upload/calendar.pdf"})</script>'
                '<a href="/_upload/calendar.pdf">校历</a>'
                '<iframe src="https://example.com/other.pdf"></iframe>')
        self.assertEqual(finder(html, crawler.BASE), ['https://www.byau.edu.cn/_upload/calendar.pdf'])

    @unittest.skipUnless(importlib.util.find_spec('pypdf') and importlib.util.find_spec('reportlab'),
                         'PDF 解析测试需要 pypdf 和 reportlab；可用 Codex 内置 Python 执行')
    def test_pdf_extraction_reads_last_page(self):
        extractor = getattr(crawler, 'extract_pdf', None)
        self.assertIsNotNone(extractor, '需要提取 PDF 全部页面的文字')
        from reportlab.pdfgen.canvas import Canvas
        buffer = io.BytesIO()
        canvas = Canvas(buffer)
        canvas.drawString(72, 720, 'Campus first page')
        canvas.showPage()
        canvas.drawString(72, 720, 'Campus last page')
        canvas.save()
        text = extractor(buffer.getvalue())
        self.assertIn('Campus first page', text)
        self.assertIn('Campus last page', text)

    @unittest.skipUnless(importlib.util.find_spec('pypdf') and importlib.util.find_spec('reportlab'),
                         'PDF 表格测试需要 pypdf 和 reportlab')
    def test_pdf_keeps_columns_separated(self):
        from reportlab.pdfgen.canvas import Canvas
        buffer = io.BytesIO()
        canvas = Canvas(buffer)
        canvas.drawString(72, 720, 'Department')
        canvas.drawString(300, 720, 'Phone')
        canvas.save()
        self.assertRegex(crawler.extract_pdf(buffer.getvalue()), r'Department {2,}Phone')


if __name__ == '__main__':
    unittest.main()

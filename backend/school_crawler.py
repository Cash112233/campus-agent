"""通用学校官网爬虫：抓取可用于校园问答的正文并按学校写入知识库。

默认只访问学校官网及其子域名，限速、限制页数，并按通知、教务、办事、
规章、联系方式、校园生活等关键词筛选内容。脚本默认只预览，传入 --import
后才写入当前项目的 SQLite 知识库。
"""
import argparse
import hashlib
import html
import ipaddress
import json
import re
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import campus_profiles
import config
import db
from rag import chunk_text

MIN_CHARS = 120
DEFAULT_MAX_PAGES = 80
DEFAULT_DELAY = 0.35
DEFAULT_TIMEOUT = 15
USER_AGENT = "CampusAgentSchoolCrawler/1.0 (+校园问答资料采集)"
KEYWORDS = (
    "通知", "公告", "教务", "教学", "学籍", "选课", "考试", "成绩", "校历", "规章", "制度",
    "办事", "服务", "联系", "电话", "部门", "学院", "招生", "资助", "奖学金", "就业", "图书馆",
    "后勤", "宿舍", "食堂", "校医院", "心理", "保卫", "校园卡", "网络", "缴费", "转专业", "请假",
)
BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "tr", "br", "hr"}
SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "nav", "header", "footer", "aside"}
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".zip", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf")


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(html.unescape(attrs["href"]))
        if tag in SKIP_TAGS:
            self._skip += 1
        if not self._skip and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in SKIP_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if not self._skip and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def extract_title(document):
    match = re.search(r"<title[^>]*>(.*?)</title>", document or "", re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip() if match else ""


def extract_page(document):
    parser = _PageParser()
    parser.feed(document or "")
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    return extract_title(document), text, parser.links


def normalize_url(url, base_url, allowed_host=None):
    if not url or str(url).strip().lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    absolute = urljoin(base_url, str(url).strip())
    parsed = urlparse(absolute)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = (allowed_host or urlparse(base_url).hostname or "").lower().rstrip(".")
    # 官网抓取不应访问本机、内网或链路本地地址，避免管理员误配造成 SSRF。
    if host in {"localhost", "localhost.localdomain"}:
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    allowed_root = allowed[4:] if allowed.startswith("www.") else allowed
    if parsed.scheme not in ("http", "https") or not host or not (host == allowed_root or host.endswith("." + allowed_root)):
        return None
    path = parsed.path or "/"
    if path.lower().endswith(SKIP_EXTENSIONS):
        return None
    # 去掉查询参数和锚点，避免同一篇文章重复入队；页面路径仍然保留。
    return urlunparse(("https" if parsed.scheme == "https" else "http", host, path, "", "", ""))


def page_score(title, text, url=""):
    haystack = f"{title}\n{text}".lower()
    matched = [word for word in KEYWORDS if word in haystack]
    score = len(set(matched))
    if any(word in title for word in KEYWORDS):
        score += 2
    if any(token in (url or "").lower() for token in ("notice", "article", "news", "jwc", "xxgk", "column")):
        score += 1
    return score, sorted(set(matched))


def fetch_page(url, timeout=DEFAULT_TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            content_type = (response.headers.get_content_type() if response.headers else "text/html")
            if content_type not in ("text/html", "application/xhtml+xml", "text/plain"):
                return response.status, ""
            head = raw[:800].decode("ascii", errors="ignore")
            charset = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
            encoding = charset.group(1) if charset else "utf-8"
            return response.status, raw.decode(encoding, errors="ignore")
    except Exception:
        return None, ""


def crawl_site(website, max_pages=DEFAULT_MAX_PAGES, delay=DEFAULT_DELAY, timeout=DEFAULT_TIMEOUT, fetcher=None):
    """抓取一个学校官网，返回页面正文和统计；fetcher 供测试注入。"""
    base = normalize_url(website, website)
    if not base:
        raise ValueError("学校官网必须是 http 或 https 链接")
    allowed_host = urlparse(base).hostname
    queue = deque([base])
    queued = {base}
    pages, seen_content = [], set()
    failed_requests = 0
    fetcher = fetcher or fetch_page
    max_pages = max(1, min(int(max_pages), 500))
    while queue and len(queued) <= max_pages:
        url = queue.popleft()
        status, document = fetcher(url, timeout=timeout)
        if status != 200 or not document:
            failed_requests += 1
        if status == 200 and document:
            title, text, links = extract_page(document)
            score, keywords = page_score(title, text, url)
            if len(text) >= MIN_CHARS and score > 0:
                fingerprint = hashlib.sha1(text.encode("utf-8")).hexdigest()
                if fingerprint not in seen_content:
                    seen_content.add(fingerprint)
                    pages.append({
                        "url": url, "title": title or "学校官网页面", "content": text[:30000],
                        "score": score, "keywords": keywords,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    })
            for link in links:
                target = normalize_url(link, url, allowed_host)
                if target and target not in queued and len(queued) < max_pages:
                    queued.add(target)
                    queue.append(target)
        if queue and delay > 0:
            time.sleep(max(0.0, float(delay)))
    return {
        "website": base, "visited": len(queued), "pages": pages,
        "selected": len(pages), "failed_requests": failed_requests,
        "failed_or_skipped": max(0, len(queued) - len(pages)),
    }


def import_pages(pages, school_id, conn=None):
    """按 source URL 幂等写入指定学校；不依赖当前选择的学校。"""
    school_id = str(school_id or "").strip()
    if not school_id:
        raise ValueError("缺少学校 ID")
    conn = conn or db.get_conn()
    added = updated = unchanged = chunks_count = 0
    with conn:
        for page in pages or []:
            content = str(page.get("content") or "").strip()
            url = str(page.get("url") or "").strip()
            if len(content) < MIN_CHARS or not url:
                continue
            source = "官网:" + url
            old = conn.execute("SELECT id, content FROM knowledge WHERE source=? AND school_id=?", (source, school_id)).fetchone()
            if old and old["content"] == content:
                unchanged += 1
                continue
            title = str(page.get("title") or "学校官网页面").strip()[:200]
            if old:
                doc_id = old["id"]
                conn.execute("UPDATE knowledge SET title=?, content=?, ktype=?, created_at=? WHERE id=?", (title, content, "官网爬取", db.now(), doc_id))
                conn.execute("DELETE FROM knowledge_chunks WHERE doc_id=?", (doc_id,))
                updated += 1
            else:
                doc_id = conn.execute("INSERT INTO knowledge(title, source, content, ktype, school_id, created_at) VALUES(?,?,?,?,?,?)", (title, source, content, "官网爬取", school_id, db.now())).lastrowid
                added += 1
            chunks = chunk_text(content)
            conn.executemany("INSERT INTO knowledge_chunks(doc_id, chunk_index, content) VALUES(?,?,?)", [(doc_id, index, chunk) for index, chunk in enumerate(chunks)])
            chunks_count += len(chunks)
    return {"added": added, "updated": updated, "unchanged": unchanged, "chunks": chunks_count}


def crawl_and_import(school_id, cfg=None, max_pages=DEFAULT_MAX_PAGES, delay=DEFAULT_DELAY, timeout=DEFAULT_TIMEOUT, fetcher=None, rag=None):
    cfg = cfg or config.load_config()
    profile = campus_profiles.get_profile(school_id, cfg)
    if not profile:
        raise ValueError("未找到这所学校模板")
    website = profile.get("website") or ""
    if not website:
        raise ValueError("当前学校尚未配置官网，请先保存学校官网")
    report = crawl_site(website, max_pages=max_pages, delay=delay, timeout=timeout, fetcher=fetcher)
    imported = import_pages(report["pages"], school_id)
    if rag is not None and str(cfg.get("current_school_id") or "hlby") == str(school_id):
        rag.reload()
    return {"school_id": school_id, "website": website, "visited": report["visited"], "selected": report["selected"], "failed_requests": report.get("failed_requests", 0), **imported}


def main(argv=None):
    parser = argparse.ArgumentParser(description="抓取学校官网可问答资料并按学校写入知识库")
    parser.add_argument("--school-id", default=None, help="学校模板 ID，默认当前学校")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--import", dest="do_import", action="store_true", help="确认将筛选结果写入知识库")
    args = parser.parse_args(argv)
    cfg = config.load_config()
    school_id = args.school_id or cfg.get("current_school_id") or "hlby"
    profile = campus_profiles.get_profile(school_id, cfg)
    if not profile or not profile.get("website"):
        raise SystemExit("当前学校没有可用官网，请先在学校模板中保存官网")
    report = crawl_site(profile["website"], args.max_pages, args.delay, args.timeout)
    summary = {"school_id": school_id, "website": profile["website"], "visited": report["visited"], "selected": report["selected"]}
    if args.do_import:
        summary.update(import_pages(report["pages"], school_id))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    main()

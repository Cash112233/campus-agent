"""批量抓取学校官网并写入各自知识库。

用法：
    python -X utf8 -B backend/crawl_all_schools.py --dry-run
    python -X utf8 -B backend/crawl_all_schools.py --import
    python -X utf8 -B backend/crawl_all_schools.py --school-id hlby --import

脚本不会把学校资料合并到一起；每页资料都按对应学校的 school_id 入库。
没有配置官网的学校会跳过，并在报告中列出。
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import campus_profiles
import config
import school_crawler

REPORT_PATH = Path(__file__).resolve().parents[1] / "output" / "school_crawl_report.json"


def select_profiles(cfg, school_id=None):
    profiles = campus_profiles.list_profiles(cfg)
    if school_id:
        profile = next((row for row in profiles if row["id"] == school_id), None)
        if not profile:
            raise ValueError("未找到这所学校模板")
        if not profile.get("website"):
            raise ValueError("当前学校尚未配置官网")
        return [profile]
    return [row for row in profiles if row.get("website")]


def run(cfg=None, school_id=None, max_pages=80, delay=0.35, timeout=15, do_import=False):
    cfg = cfg or config.load_config()
    profiles = select_profiles(cfg, school_id)
    all_profiles = campus_profiles.list_profiles(cfg)
    skipped = [row["id"] for row in all_profiles if not row.get("website")]
    results = []
    for profile in profiles:
        item = {"school_id": profile["id"], "school": profile["name"], "website": profile["website"]}
        try:
            report = school_crawler.crawl_site(profile["website"], max_pages=max_pages, delay=delay, timeout=timeout)
            item.update({"visited": report["visited"], "selected": report["selected"], "failed_requests": report.get("failed_requests", 0)})
            if do_import:
                item.update(school_crawler.import_pages(report["pages"], profile["id"]))
            item["ok"] = True
        except Exception as error:
            item.update({"ok": False, "error": str(error)})
        results.append(item)
    return {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "imported": bool(do_import),
        "total_profiles": len(all_profiles),
        "targeted": len(profiles),
        "skipped_without_website": skipped,
        "results": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="批量抓取已配置官网的学校资料并按学校写入知识库")
    parser.add_argument("--school-id", help="只抓取指定学校，例如 hlby")
    parser.add_argument("--max-pages", type=int, default=80, help="每所学校最多抓取页面数")
    parser.add_argument("--delay", type=float, default=0.35, help="每次请求间隔秒数")
    parser.add_argument("--timeout", type=int, default=15, help="单页请求超时秒数")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--import", dest="do_import", action="store_true", help="抓取后写入知识库")
    mode.add_argument("--dry-run", dest="do_import", action="store_false", help="只抓取统计，不写入知识库")
    parser.set_defaults(do_import=False)
    parser.add_argument("--report", default=str(REPORT_PATH), help="报告 JSON 输出路径")
    args = parser.parse_args(argv)
    report = run(config.load_config(), args.school_id, args.max_pages, args.delay, args.timeout, args.do_import)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()

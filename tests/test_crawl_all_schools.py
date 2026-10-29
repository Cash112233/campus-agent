import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import crawl_all_schools


class CrawlAllSchoolsTests(unittest.TestCase):
    def test_selects_only_profiles_with_websites(self):
        cfg = {
            "current_school_id": "hlby",
            "custom_profiles": [],
            "profile_overrides": {"hlby": {"website": "https://www.byau.edu.cn/"}},
        }
        profiles = crawl_all_schools.select_profiles(cfg)
        self.assertEqual([p["id"] for p in profiles], ["hlby"])

    def test_explicit_school_without_website_is_reported_for_later_configuration(self):
        cfg = {"custom_profiles": [], "profile_overrides": {}}
        with self.assertRaisesRegex(ValueError, "尚未配置官网"):
            crawl_all_schools.select_profiles(cfg, "hlj-001")


if __name__ == "__main__":
    unittest.main()

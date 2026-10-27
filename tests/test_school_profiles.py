import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import config
import campus_profiles
import db
from rag import RAG
from unittest.mock import patch
import sqlite3


class SchoolProfileTests(unittest.TestCase):
    def test_hlj_directory_is_ready_for_the_statewide_template(self):
        profiles = campus_profiles.list_profiles()
        self.assertGreaterEqual(len(profiles), 80)
        self.assertTrue(any(p["id"] == "hlby" for p in profiles))
        self.assertTrue(all(p["name"] and p["city"] for p in profiles))

    def test_apply_profile_changes_school_scoped_map_fields_and_preserves_secrets(self):
        original = {**config.DEFAULT_CONFIG, "api_key": "deepseek-secret", "amap_key": "amap-secret"}
        updated = campus_profiles.apply_profile(original, "hlby")
        self.assertEqual(updated["current_school_id"], "hlby")
        self.assertEqual(updated["amap_campus_name"], "黑龙江八一农垦大学")
        self.assertEqual(updated["amap_city"], "大庆市")
        self.assertEqual(updated["api_key"], "deepseek-secret")
        self.assertEqual(updated["amap_key"], "amap-secret")

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            campus_profiles.apply_profile(dict(config.DEFAULT_CONFIG), "does-not-exist")

    def test_map_settings_can_be_saved_back_to_the_selected_profile(self):
        cfg = {**config.DEFAULT_CONFIG, "current_school_id": "hlby"}
        updated = campus_profiles.update_profile_override(cfg, "hlby", {
            "campus_location": "125.168208,46.587304", "radius": 4200,
        })
        profile = campus_profiles.get_profile("hlby", updated)
        self.assertEqual(profile["campus_location"], "125.168208,46.587304")
        self.assertEqual(profile["radius"], 4200)

    def test_knowledge_retrieval_is_scoped_to_current_school(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cfg = {**config.DEFAULT_CONFIG, "current_school_id": "hlby"}
        with patch("db.get_conn", return_value=conn), patch("rag.get_conn", return_value=conn), patch("config.load_config", return_value=cfg):
            db.init_db()
            rag = RAG()
            rag.add_document("八一校规", "八一农垦大学请假规定", "校官网", "规定")
            cfg["current_school_id"] = "hlj-001"
            rag.reload()
            self.assertEqual(rag.search("请假规定"), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()

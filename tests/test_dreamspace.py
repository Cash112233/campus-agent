import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import dreamspace
import httpx


class DreamSpaceActivityTests(unittest.TestCase):
    def test_activity_questions_are_detected(self):
        self.assertTrue(dreamspace.is_activity_request("最近有什么活动可以报名"))
        self.assertTrue(dreamspace.is_activity_request("今天有讲座吗"))
        self.assertFalse(dreamspace.is_activity_request("本科生转专业需要什么材料"))

    def test_activity_keyword_ignores_personal_context(self):
        self.assertEqual(dreamspace.activity_keyword("我想看看我是经济管理学院的，有什么活动适合我"), "")
        self.assertEqual(dreamspace.activity_keyword("最近有哪些人工智能讲座"), "人工智能讲座")

    def test_normalize_list_response_keeps_live_activity_fields(self):
        payload = {
            "rows": [{
                "id": "42",
                "activityName": "校园讲座",
                "startDateStr": "2026-10-28 18:00:00",
                "endDateStr": "2099-10-28 20:00:00",
            }],
            "total": 1,
        }
        result = dreamspace.normalize_list_response(payload, keyword="讲座")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["id"], "42")
        self.assertEqual(result["results"][0]["title"], "校园讲座")
        self.assertEqual(result["results"][0]["source_url"], dreamspace.activity_url("42"))
        self.assertIn("fetched_at", result)

    def test_normalize_list_response_hides_ended_events(self):
        payload = {"rows": [
            {"id": "1", "activityName": "已结束", "startDateStr": "2020-01-01 00:00:00", "endDateStr": "2020-01-02 00:00:00"},
            {"id": "2", "activityName": "进行中", "startDateStr": "2026-01-01 00:00:00", "endDateStr": "2099-01-02 00:00:00"},
        ]}
        result = dreamspace.normalize_list_response(payload, keyword="")
        self.assertEqual([row["id"] for row in result["results"]], ["2"])

    def test_normalize_detail_removes_markup_and_exposes_source(self):
        payload = {"result": {
            "id": "42",
            "activityName": "校园讲座",
            "description": '<p>地点：图书馆报告厅</p><p><img src="x"></p>',
            "startDateStr": "2026-10-28 18:00:00",
            "endDateStr": "2026-10-28 20:00:00",
            "activityAbstract": "欢迎报名",
        }}
        item = dreamspace.normalize_detail_response(payload)
        self.assertEqual(item["title"], "校园讲座")
        self.assertIn("地点：图书馆报告厅", item["description"])
        self.assertNotIn("<p>", item["description"])
        self.assertEqual(item["source_url"], dreamspace.activity_url("42"))

    def test_tool_dispatches_activity_search_without_exposing_session(self):
        import tools
        with patch.object(tools.dreamspace, "search_activities", return_value={"available": True, "results": []}) as search:
            result = tools.run_tool("search_dreamspace_activities", {"query": "讲座"})
        search.assert_called_once()
        self.assertEqual(result["available"], True)

    def test_client_retries_transient_tls_transport_error(self):
        client = dreamspace.DreamSpaceClient()
        response = Mock()
        response.json.return_value = {"rows": [{"id": "9", "activityName": "稳定活动", "endDateStr": "2099-01-01 00:00:00"}]}
        response.raise_for_status.return_value = None
        with patch.object(client.client, "post", side_effect=[httpx.TransportError("EOF"), response]) as post:
            result = client.search("稳定活动", include_details=False)
        client.close()
        self.assertTrue(result["available"])
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()

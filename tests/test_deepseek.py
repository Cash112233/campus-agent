"""DeepSeek 接入回归测试；使用本机 HTTP transport，不请求远端 API。"""
import json
from pathlib import Path
import sys
import unittest

import httpx
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from config import DEFAULT_CONFIG
from llm import LLMClient


class DeepSeekTests(unittest.TestCase):
    def test_default_model_is_current_official_model(self):
        self.assertEqual(DEFAULT_CONFIG["model"], "deepseek-flash")

    def test_current_models_send_non_thinking_tool_requests(self):
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            with self.subTest(model=model):
                captured = []

                def respond(request):
                    captured.append(json.loads(request.content))
                    self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
                    self.assertEqual(request.headers["authorization"], "Bearer unit-test-key")
                    return httpx.Response(200, json={
                        "id": "test", "object": "chat.completion", "created": 0,
                        "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "正常"}}],
                    })

                llm = LLMClient({"api_key": "unit-test-key", "model": model})
                llm.client.close()
                llm.client = OpenAI(api_key="unit-test-key", base_url="https://api.deepseek.com",
                    http_client=httpx.Client(transport=httpx.MockTransport(respond)))
                try:
                    message = llm.chat([{"role": "user", "content": "测试"}], tools=[{
                        "type": "function", "function": {"name": "get_time",
                        "parameters": {"type": "object", "properties": {}}}}])
                    self.assertEqual(message.content, "正常")
                    self.assertEqual(captured[0]["model"], model)
                    self.assertEqual(captured[0].get("thinking"), {"type": "disabled"})
                    self.assertEqual(captured[0]["tools"][0]["function"]["name"], "get_time")
                finally:
                    llm.client.close()


if __name__ == "__main__":
    unittest.main()

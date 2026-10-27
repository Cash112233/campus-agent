"""LLM 客户端：DeepSeek（OpenAI 兼容协议），支持流式输出、工具调用聚合与自动重试"""
import json
import re
import time

from openai import OpenAI


def parse_json(text):
    """从模型输出中稳健地解析 JSON（容忍代码块围栏与前后缀文本）"""
    if not text:
        return None
    t = str(text).strip()
    t = re.sub(r"```(?:json)?", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    for start_ch, end_ch in (("[", "]"), ("{", "}")):
        i = t.find(start_ch)
        if i >= 0:
            j = t.rfind(end_ch)
            if j > i:
                try:
                    return json.loads(t[i:j + 1])
                except Exception:
                    continue
    return None


class _FunctionCall:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, cid, name, arguments):
        self.id = cid
        self.type = "function"
        self.function = _FunctionCall(name, arguments)

    def model_dump(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class StreamMessage:
    """流式聚合后的 assistant 消息（接口与 openai ChatCompletionMessage 兼容）"""

    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls or None


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(
            api_key=cfg.get("api_key") or "EMPTY",
            base_url=cfg.get("base_url") or "https://api.deepseek.com",
            timeout=120,
        )

    def _kwargs(self, messages, tools, temperature, extra=None):
        kwargs = {
            "model": self.cfg.get("model", "deepseek-flash"),
            "messages": messages,
            "temperature": self.cfg.get("temperature", 0.7) if temperature is None else temperature,
        }
        if tools:
            kwargs["tools"] = tools
        # 当前 Agent 未传回 reasoning_content，显式使用普通模式以兼容工具连续调用。
        if kwargs["model"] in ("deepseek-flash", "deepseek-v4-pro"):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if extra:
            kwargs.update(extra)
        return kwargs

    def chat(self, messages, tools=None, temperature=None):
        """非流式单轮，失败自动重试一次"""
        kwargs = self._kwargs(messages, tools, temperature)
        last_err = None
        for attempt in range(2):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                return resp.choices[0].message
            except Exception as e:
                last_err = e
                if attempt == 0:
                    time.sleep(1.2)
        raise last_err

    def chat_stream(self, messages, tools=None, temperature=None):
        """流式生成器：
        - 逐段 yield {"type": "delta", "text": str}
        - 结束时 yield {"type": "final", "message": StreamMessage}
        - 流式中途异常自动回退为非流式调用（内置重试），此时只发 final 保证内容完整不重复
        """
        kwargs = self._kwargs(messages, tools, temperature, extra={"stream": True})
        content_parts = []
        tool_acc = {}  # index -> {"id","name","arguments"}
        try:
            stream = self.client.chat.completions.create(**kwargs)
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    content_parts.append(delta.content)
                    yield {"type": "delta", "text": delta.content}
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index if tc.index is not None else 0
                        acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                acc["name"] += tc.function.name
                            if tc.function.arguments:
                                acc["arguments"] += tc.function.arguments
        except Exception:
            msg = self.chat(messages, tools=tools, temperature=temperature)
            yield {"type": "final", "message": msg}
            return

        tool_calls = [_ToolCall(v["id"], v["name"], v["arguments"])
                      for _, v in sorted(tool_acc.items())]
        yield {"type": "final", "message": StreamMessage("".join(content_parts), tool_calls)}

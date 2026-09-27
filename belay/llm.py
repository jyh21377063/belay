"""模型客户端：DeepSeek 的 Anthropic 兼容接口（https://api.deepseek.com/anthropic）。

与 Claude Code 走同一种协议，工具调用格式一致。DeepSeek 的兼容性要点：
  - thinking 支持（budget_tokens 被忽略），深度由 output_config.effort 控制；
  - 思考模式下发生工具调用时，后续请求必须原样带回 thinking 块，否则可能 400。
    所以这里保留 assistant 消息中的全部块（含 thinking 与 signature），不像 mini_claude 那样丢弃；
  - cache_control 被忽略（DeepSeek 按前缀自动缓存），因此不加缓存断点。

另外提供录制与回放：调试 runtime 时直接回放录下的回复，不花钱、结果可复现。
部分结构参考 mini_claude（MIT）：重试与退避、流式调用后取最终消息。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


@dataclass
class Usage:
    input_tokens: int = 0            # 未命中缓存的输入
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def context_tokens(self) -> int:
        """本次请求的完整输入长度（用于判断上下文是否过长）。"""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.output_tokens += other.output_tokens


@dataclass
class Response:
    content: list[dict]              # 规范化后的内容块（可直接放回 messages）
    stop_reason: str | None
    usage: Usage = field(default_factory=Usage)

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()


def normalize_block(block: dict) -> dict | None:
    """只保留协议需要的字段；未知类型丢弃。"""
    t = block.get("type")
    if t == "text":
        return {"type": "text", "text": block.get("text", "")}
    if t == "thinking":
        out = {"type": "thinking", "thinking": block.get("thinking", "")}
        if block.get("signature") is not None:
            out["signature"] = block["signature"]
        return out
    if t == "tool_use":
        return {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input") or {}}
    return None


def _usage_from(u) -> Usage:
    g = (lambda k: (u.get(k) if isinstance(u, dict) else getattr(u, k, None)) or 0)
    return Usage(g("input_tokens"), g("cache_read_input_tokens"), g("cache_creation_input_tokens"), g("output_tokens"))


class LLM:
    """真实的模型调用。"""

    def __init__(self, model: str, api_key: str, base_url: str = "https://api.deepseek.com/anthropic",
                 max_tokens: int = 64000, effort: str | None = "max", thinking: bool = True,
                 request_timeout: float = 600, max_retries: int = 8, retry_base: float = 2.0, record_path: str | Path | None = None,
                 log=None):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url, timeout=request_timeout, max_retries=0)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.thinking = thinking
        self.max_retries = max_retries
        self.retry_base = retry_base
        self.record_path = Path(record_path) if record_path else None
        self.log = log or (lambda msg: None)

    def _extra_body(self) -> dict:
        body: dict = {"thinking": {"type": "enabled" if self.thinking else "disabled"}}
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        return body

    async def call(self, system: str, tools: list[dict], messages: list[dict],
                   tool_choice: dict | None = None) -> Response:
        params = dict(model=self.model, max_tokens=self.max_tokens, system=system,
                      messages=messages, extra_body=self._extra_body())
        if tools:
            params["tools"] = tools
        if tool_choice:
            params["tool_choice"] = tool_choice

        for attempt in range(self.max_retries + 1):
            try:
                async with self.client.messages.stream(**params) as stream:
                    final = await stream.get_final_message()
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status = getattr(e, "status_code", None)
                retryable = status in RETRYABLE_STATUS or type(e).__name__ in (
                    "APIConnectionError", "APITimeoutError", "RemoteProtocolError", "ReadError")
                if attempt >= self.max_retries or not retryable:
                    raise
                delay = min(60.0, self.retry_base * 2 ** attempt) * (0.5 + random.random())
                self.log(f"模型调用失败（{status or type(e).__name__}），{delay:.0f}s 后第 {attempt + 1} 次重试")
                await asyncio.sleep(delay)

        content = [b for b in (normalize_block(x.model_dump()) for x in final.content) if b]
        resp = Response(content, final.stop_reason, _usage_from(final.usage))
        if self.record_path:
            with self.record_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "content": resp.content, "stop_reason": resp.stop_reason,
                                    "usage": resp.usage.__dict__}, ensure_ascii=False) + "\n")
        return resp


class ReplayLLM:
    """按顺序回放录制的回复，忽略请求内容。用于调试 runtime，不调用模型。"""

    def __init__(self, record_path: str | Path):
        lines = Path(record_path).read_text(encoding="utf-8").splitlines()
        self._items = [json.loads(x) for x in lines if x.strip()]
        self._i = 0

    async def call(self, system, tools, messages, tool_choice=None) -> Response:
        if self._i >= len(self._items):
            raise RuntimeError("回放记录已用完")
        item = self._items[self._i]
        self._i += 1
        return Response(item["content"], item.get("stop_reason"), Usage(**item.get("usage", {})))


class ScriptedLLM:
    """单元测试用：依次返回预先写好的内容块列表，并记录每次收到的请求。"""

    def __init__(self, script: list[list[dict]], context_tokens: list[int] | None = None):
        self.script = list(script)
        self.context_tokens = list(context_tokens or [])     # 每一步报告的输入长度，默认 1000
        self.requests: list[dict] = []

    async def call(self, system, tools, messages, tool_choice=None) -> Response:
        self.requests.append({"system": system, "tools": tools, "messages": json.loads(json.dumps(messages)),
                              "tool_choice": tool_choice})
        if not self.script:
            raise RuntimeError("脚本已用完")
        content = self.script.pop(0)
        stop = "tool_use" if any(b.get("type") == "tool_use" for b in content) else "end_turn"
        tokens = self.context_tokens.pop(0) if self.context_tokens else 1000
        return Response(content, stop, Usage(input_tokens=tokens, output_tokens=100))

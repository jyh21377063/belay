"""用本地的假服务器（Anthropic SSE 协议）检查 LLM.call：流式解析、thinking 块保留、
extra_body 透传（thinking / output_config）、429 重试、录制与回放。不需要网络和 API key。
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from belay.llm import LLM, ReplayLLM


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = [
    {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "model": "deepseek-flash",
                                          "content": [], "stop_reason": None, "stop_sequence": None,
                                          "usage": {"input_tokens": 50, "output_tokens": 1,
                                                    "cache_read_input_tokens": 1200}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "need to read"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1",
                                                                  "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"file_path\": "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"a.py\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]


class Handler(BaseHTTPRequestHandler):
    bodies: list = []
    fail_first = 0

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.bodies.append(body)
        if Handler.fail_first > 0:
            Handler.fail_first -= 1
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("retry-after", "0")
            self.end_headers()
            self.wfile.write(b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(sse(STREAM))

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    Handler.bodies = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_stream_thinking_extra_body_retry_and_replay(server, tmp_path):
    Handler.fail_first = 1
    rec = tmp_path / "rec.jsonl"
    llm = LLM("deepseek-flash", "k", base_url=server, max_tokens=1000, effort="max", retry_base=0, record_path=rec)
    resp = asyncio.run(llm.call("sys", [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}],
                                [{"role": "user", "content": "hi"}]))
    assert resp.content == [{"type": "thinking", "thinking": "need to read", "signature": "sig-1"},
                            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"file_path": "a.py"}}]
    assert resp.stop_reason == "tool_use"
    assert resp.usage.cache_read_tokens == 1200 and resp.usage.output_tokens == 42
    assert len(Handler.bodies) == 2                                    # 第一次 429，重试成功
    body = Handler.bodies[-1]
    assert body["thinking"] == {"type": "enabled"} and body["output_config"] == {"effort": "max"}
    assert body["stream"] is True and body["model"] == "deepseek-flash"

    replayed = asyncio.run(ReplayLLM(rec).call("x", [], []))
    assert replayed.content == resp.content and replayed.usage.output_tokens == 42

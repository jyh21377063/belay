"""worker 主循环：LocalEnv + ScriptedLLM，不需要容器和模型。"""
from __future__ import annotations

import asyncio
import json

from belay.env import LocalEnv
from belay.llm import ScriptedLLM
from belay.worker import Worker, WorkerConfig
from belay.worker.transcript import Transcript
from tests.conftest import tool_use


def run(coro):
    return asyncio.run(coro)


def test_worker_fixes_bug_and_submits(repo, tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("run")
    llm = ScriptedLLM([
        [{"type": "thinking", "thinking": "look first", "signature": "sig"},
         tool_use("t1", "read_file", file_path="pkg/mod.py"), tool_use("t2", "grep_search", pattern="add")],
        [tool_use("t3", "edit_file", file_path="pkg/mod.py", old_string="return a - b", new_string="return a + b")],
        [tool_use("t4", "bash", command="python -m pytest -q tests 2>&1 | tail -3")],
        [{"type": "text", "text": "done"}, tool_use("t5", "submit", summary="fixed add")],
    ])
    worker = Worker(llm, LocalEnv(str(repo)), transcript=Transcript(out_dir / "t.jsonl"))
    res = run(worker.run("Fix add() in pkg/mod.py"))
    assert res.status == "submitted" and res.summary == "fixed add" and res.turns == 4
    assert "return a + b" in (repo / "pkg" / "mod.py").read_text()
    # thinking 块原样带回，tool_result 与 tool_use 一一配对
    second = llm.requests[1]["messages"]
    assert second[1]["content"][0] == {"type": "thinking", "thinking": "look first", "signature": "sig"}
    assert [b["tool_use_id"] for b in second[2]["content"]] == ["t1", "t2"]
    assert "passed" in llm.requests[3]["messages"][-1]["content"][0]["content"]
    types = [json.loads(x)["type"] for x in (out_dir / "t.jsonl").read_text().splitlines()]
    assert types[0] == "start" and types[-1] == "end" and types.count("assistant") == 4


def test_worker_stops_without_tool_calls_and_reports_tool_errors(repo):
    llm = ScriptedLLM([[tool_use("a", "edit_file", file_path="README.md", old_string="demo", new_string="x")],
                       [{"type": "text", "text": "giving up"}]])
    res = run(Worker(llm, LocalEnv(str(repo))).run("task"))
    assert res.status == "no_tool_call"
    result_block = llm.requests[1]["messages"][-1]["content"][0]
    assert result_block["is_error"] and "before editing" in result_block["content"]


def test_worker_reset_writes_handoff_and_rebuilds_context(repo):
    llm = ScriptedLLM([
        [tool_use("a", "read_file", file_path="pkg/mod.py")],
        [{"type": "text", "text": "HANDOFF: add() subtracts; fix it next."}],
        [tool_use("b", "edit_file", file_path="pkg/mod.py", old_string="a - b", new_string="a + b")],
        [tool_use("c", "read_file", file_path="pkg/mod.py")],
        [tool_use("d", "edit_file", file_path="pkg/mod.py", old_string="a - b", new_string="a + b")],
        [tool_use("e", "submit", summary="ok")],
    ], context_tokens=[9000])                    # 只有第一步超过阈值
    worker = Worker(llm, LocalEnv(str(repo)), config=WorkerConfig(reset_tokens=5000))
    res = run(worker.run("Fix add()"))
    assert res.status == "submitted" and res.resets >= 1
    handoff_req = llm.requests[1]
    assert handoff_req["tool_choice"] == {"type": "none"}
    assert "handoff note" in handoff_req["messages"][-1]["content"][-1]["text"]
    rebuilt = llm.requests[2]["messages"]
    assert len(rebuilt) == 1 and "HANDOFF: add() subtracts" in rebuilt[0]["content"] and "<task>" in rebuilt[0]["content"]
    # 重建后必须重新读取才能编辑
    assert "before editing" in llm.requests[3]["messages"][-1]["content"][0]["content"]

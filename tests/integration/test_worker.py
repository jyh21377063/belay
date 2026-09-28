"""worker 主循环：LocalEnv + ScriptedLLM，不需要容器和模型。"""
from __future__ import annotations

import asyncio
import json

from belay.env import LocalEnv
from belay.llm import ScriptedLLM
from belay.tools import DEFAULT_TOOLS, get_tools
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


# ---- 只读探索子 agent -----------------------------------------------------------------

def test_explore_subagent_reports_and_stays_read_only(repo, tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    llm = ScriptedLLM([
        [tool_use("p1", "explore", description="find add", prompt="Where is add() defined and tested?")],
        [tool_use("c1", "grep_search", pattern="def add"),
         tool_use("c2", "bash", command="echo hacked > pkg/x.txt"),
         tool_use("c3", "edit_file", file_path="pkg/mod.py", old_string="a", new_string="b")],
        [{"type": "text", "text": "add() is in pkg/mod.py:1 and tested in tests/test_mod.py:3"}],
        [tool_use("p2", "submit", summary="done")],
    ])
    worker = Worker(llm, LocalEnv(str(repo)), transcript=Transcript(out / "t.jsonl"))
    res = run(worker.run("Fix add()"))
    assert res.status == "submitted"
    assert "explore" in llm.requests[0]["system"]                        # 主 worker 的提示里有用法说明

    child = llm.requests[1]
    assert "read-only exploration agent" in child["system"]
    assert sorted(t["name"] for t in child["tools"]) == ["bash", "grep_search", "list_files", "read_file"]
    assert child["messages"][0]["content"].startswith("<question>")
    results = llm.requests[2]["messages"][-1]["content"]
    assert "mod.py:1" in results[0]["content"]
    assert results[1]["is_error"] and "read-only" in results[1]["content"]
    assert results[2]["is_error"] and "unknown tool" in results[2]["content"]
    assert not (repo / "pkg" / "x.txt").exists()

    report = llm.requests[3]["messages"][-1]["content"][0]
    assert not report["is_error"] and "tests/test_mod.py:3" in report["content"]
    assert res.usage.input_tokens == 4000                                # 父 2 次 + 子 2 次，子 agent 用量计入
    assert (out / "t-explore-1.jsonl").exists()
    assert any(e.get("category") == "read_only" and e.get("source") == "explore-1" for e in res.events)


def test_parallel_explorers(repo, tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    llm = ScriptedLLM([
        [tool_use("p1", "explore", description="a", prompt="Question A"),
         tool_use("p2", "explore", description="b", prompt="Question B")],
        [{"type": "text", "text": "REPORT"}],
        [{"type": "text", "text": "REPORT"}],
        [tool_use("p3", "submit", summary="done")],
    ])
    res = run(Worker(llm, LocalEnv(str(repo)), transcript=Transcript(out / "t.jsonl")).run("task"))
    assert res.status == "submitted"
    results = llm.requests[-1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["p1", "p2"] and all(r["content"] == "REPORT" for r in results)
    assert (out / "t-explore-1.jsonl").exists() and (out / "t-explore-2.jsonl").exists()


def test_explorer_wraps_up_at_turn_limit(repo):
    llm = ScriptedLLM([
        [tool_use("p1", "explore", description="x", prompt="Survey the package")],
        [tool_use("c1", "list_files", pattern="**/*.py")],
        [{"type": "text", "text": "Partial report: three python files."}],
        [tool_use("p2", "submit", summary="done")],
    ])
    worker = Worker(llm, LocalEnv(str(repo)), config=WorkerConfig(explore_max_turns=1))
    run(worker.run("task"))
    wrapup = llm.requests[2]
    assert wrapup["tool_choice"] == {"type": "none"} and "limit" in wrapup["messages"][-1]["content"][-1]["text"]
    assert "Partial report" in llm.requests[3]["messages"][-1]["content"][0]["content"]


def test_explorer_worktree_change_is_flagged(repo):
    llm = ScriptedLLM([
        [tool_use("p1", "explore", description="x", prompt="Look around")],
        [tool_use("c1", "bash", command="python3 -c \"open('pkg/new.txt','w').write('x')\"")],   # 绕过了正则
        [{"type": "text", "text": "done looking"}],
        [tool_use("p2", "submit", summary="done")],
    ])
    res = run(Worker(llm, LocalEnv(str(repo))).run("task"))
    report = llm.requests[3]["messages"][-1]["content"][0]["content"]
    assert "working tree changed" in report
    assert any(e["kind"] == "explore_modified_worktree" for e in res.events)


def test_explore_can_be_disabled(repo):
    llm = ScriptedLLM([[tool_use("p1", "submit", summary="ok")]])
    tools = get_tools([n for n in DEFAULT_TOOLS if n != "explore"])
    run(Worker(llm, LocalEnv(str(repo)), tools=tools).run("task"))
    assert "explore" not in [t["name"] for t in llm.requests[0]["tools"]]
    assert "use explore" not in llm.requests[0]["system"]


# ---- 上下文重建、任务清单提醒、系统提示 ------------------------------------------------------

def test_rebuild_carries_todos_and_full_diff(repo):
    llm = ScriptedLLM([
        [tool_use("a", "read_file", file_path="pkg/mod.py")],
        [tool_use("b", "edit_file", file_path="pkg/mod.py", old_string="a - b", new_string="a + b"),
         tool_use("c", "todo_write", todos=[{"content": "Fix add()", "status": "completed"},
                                            {"content": "Run the tests", "status": "in_progress"}])],
        [{"type": "text", "text": "1. Task status: add() fixed, tests not run yet."}],
        [tool_use("d", "submit", summary="ok")],
    ], context_tokens=[1000, 9000])                          # 第二步超过重建阈值
    worker = Worker(llm, LocalEnv(str(repo)), config=WorkerConfig(reset_tokens=5000))
    res = run(worker.run("Fix add()"))
    assert res.status == "submitted" and res.resets == 1
    assert "Code map" in llm.requests[2]["messages"][-1]["content"][-1]["text"]      # 交接提示是新的五段结构
    rebuilt = llm.requests[3]["messages"][0]["content"]
    assert "add() fixed, tests not run yet" in rebuilt
    assert "<todo_list>\n[x] Fix add()\n[~] Run the tests\n</todo_list>" in rebuilt
    assert "+    return a + b" in rebuilt and "-    return a - b" in rebuilt              # 带上了完整 diff


def test_first_message_has_no_full_diff(repo):
    (repo / "pkg" / "mod.py").write_text("changed before the run\n")
    llm = ScriptedLLM([[tool_use("a", "submit", summary="ok")]])
    run(Worker(llm, LocalEnv(str(repo))).run("task"))
    first = llm.requests[0]["messages"][0]["content"]
    assert "git status --short" in first and "git --no-pager diff'" not in first


def test_todo_reminder_after_quiet_turns(repo):
    llm = ScriptedLLM([
        [tool_use("t", "todo_write", todos=[{"content": "x", "status": "in_progress"}])],
        [tool_use("a", "bash", command="true")],
        [tool_use("b", "bash", command="true")],
        [tool_use("c", "submit", summary="ok")],
    ])
    run(Worker(llm, LocalEnv(str(repo)), config=WorkerConfig(todo_reminder_turns=2)).run("task"))
    texts = [[b.get("text", "") for b in r["messages"][-1]["content"] if b.get("type") == "text"]
             for r in llm.requests[1:]]
    assert texts[0] == [] and texts[1] == []                                          # 刚更新过，不提醒
    assert "todo list has not been updated" in texts[2][0]


def test_system_prompt_states_environment_rules(repo):
    llm = ScriptedLLM([[tool_use("a", "submit", summary="ok")]])
    run(Worker(llm, LocalEnv(str(repo)), config=WorkerConfig(extra_rules="Never touch tests.")).run("task"))
    system = llm.requests[0]["system"]
    for phrase in ["fresh shell at the repository root", "no network access", "Do not commit",
                   "check every requirement", "report what actually happened", "# Additional rules\nNever touch tests."]:
        assert phrase in system, phrase

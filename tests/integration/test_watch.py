"""eval.watch：Claude Code 的 stream-json、自研 worker 的 transcript.jsonl、Belay 的会话 / 复核轨迹与合并链。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from belay.env import LocalEnv
from belay.llm import ScriptedLLM
from belay.worker import Worker, WorkerConfig
from belay.worker.transcript import Transcript
from eval import watch
from tests.conftest import tool_use


def trial_dir(root: Path) -> Path:
    d = root / "run-x" / "swe_evo" / "demo" / "1" / "pier" / "agent" / "t1" / "agent"
    d.mkdir(parents=True)
    return d


def test_watch_renders_worker_transcript(repo, tmp_path, capsys):
    d = trial_dir(tmp_path)
    llm = ScriptedLLM([
        [tool_use("e", "explore", description="find add", prompt="Where is add()?")],
        [{"type": "text", "text": "mod.py:1"}],
        [tool_use("a", "read_file", file_path="pkg/mod.py"),
         tool_use("t", "todo_write", todos=[{"content": "fix add", "status": "in_progress"}])],
        [tool_use("g", "bash", command="git commit -am wip")],
        [{"type": "text", "text": "HANDOFF NOTE"}],
        [tool_use("s", "submit", summary="Fixed add(); tests not run.")],
    ], context_tokens=[1000, 1000, 1000, 9000])
    worker = Worker(llm, LocalEnv(str(repo)), config=WorkerConfig(reset_tokens=5000),
                    transcript=Transcript(d / "transcript.jsonl"))
    asyncio.run(worker.run("Fix add()"))

    assert watch.find_log(str(tmp_path)) == d / "transcript.jsonl"          # 不会选中子 agent 的轨迹
    watch.main([str(tmp_path), "--no-follow"])
    out = capsys.readouterr().out
    for phrase in ["🔍 explore#1 开始：find add", "todo_write: 0/1 完成，进行中：fix add", "🚫 越界 git_write / audit",
                   "🔄 上下文重建 #1：HANDOFF NOTE", "submit: Fixed add(); tests not run.", "agent 结束：submitted",
                   "swe_evo/demo #1", "重建 1 · explore 1 · 越界 1"]:
        assert phrase in out, phrase
    assert "💬 HANDOFF NOTE" not in out                                       # 交接说明不当作普通回复
    assert f"第 {worker.turns} 轮" in out                                      # 轮数与 worker 一致


def test_watch_still_renders_claude_code_logs(tmp_path, capsys):
    d = trial_dir(tmp_path)
    events = [
        {"type": "system", "subtype": "init", "model": "deepseek-flash", "cwd": "/testbed"},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": "Looking at the code."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/testbed/a.py"}},
            {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                {"content": "x", "status": "completed", "activeForm": "x"}]}}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}},
        {"type": "result", "subtype": "success", "num_turns": 1, "duration_ms": 60000,
         "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    (d / "claude-code.txt").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    watch.main([str(d / "claude-code.txt"), "--no-follow"])
    out = capsys.readouterr().out
    for phrase in ["会话开始  model=deepseek-flash", "💬 Looking at the code.", "🔧 Edit: /testbed/a.py",
                   "TodoWrite: 1/1 完成", "agent 结束：success", "改过 1 个文件"]:
        assert phrase in out, phrase


def test_watch_shows_belay_merge_chain_and_reviewer(tmp_path, capsys):
    from tests.integration.test_belay_run import (ADD_SUB, FIX_ADD, PLANNER, READ, SUBMIT, TASK, Harness, call)
    h = Harness(tmp_path)
    run = h.make(ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB), call(SUBMIT)]))
    assert asyncio.run(run.start(TASK)).status == "DONE"
    d = h.run_dir()
    vid = run.rt.graph.checkpoints[run.rt.graph.run.delivered].review

    watch.main([str(d / "sessions" / "S1.jsonl"), "--no-follow"])            # 会话轨迹里穿插合并链
    out = capsys.readouterr().out
    for phrase in ["📦 合并请求 A", f"🔎 复核 {vid} 开始", f"🧑‍⚖️ 复核 {vid} 结论：建议合并",
                   "规则校验通过", "🟢 合并点 #", "需求 R3 → done（E1），由复核者", "📦 交付合并点", "合并链：链头 #"]:
        assert phrase in out, phrase

    watch.main([str(d / "events.jsonl"), "--chain", "--no-follow"])          # 只看合并链
    out = capsys.readouterr().out
    assert "运行开始 Belay v8" in out and "需求 R2 → done（E3），由测试" in out and "已交付 #" in out and "🔧" not in out

    watch.main([str(d / "reviews" / f"{vid}.jsonl"), "--no-follow"])        # 复核者的轨迹
    out = capsys.readouterr().out
    assert "🔧 verdict: 建议合并" in out and "复核会话结束" in out and "接下来是评分" not in out

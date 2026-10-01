"""FlatAgent 的端到端检查：用本地子进程模拟 Pier 的 environment，走完 setup → run → 导出补丁。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pier")

from belay.llm import ScriptedLLM                                    # noqa: E402
from eval.agents.belay_agent import BelayAgent                      # noqa: E402
from eval.agents.flat_agent import FlatAgent                        # noqa: E402


class FakePierEnvironment:
    """只实现 FlatAgent 与 PatchCaptureMixin 用到的接口。"""

    def __init__(self, workdir: Path, agent_dir: Path):
        self.workdir = workdir
        self.env_paths = SimpleNamespace(agent_dir=str(agent_dir), verifier_dir=str(agent_dir / "verifier"))
        self.capabilities = SimpleNamespace(mounted=True)
        self.calls: list[str] = []

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.calls.append(command)
        proc = await asyncio.create_subprocess_shell(command, cwd=cwd or self.workdir, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return SimpleNamespace(stdout=out.decode(), stderr=err.decode(), return_code=proc.returncode)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run("git init -q && git add -A && git -c user.email=a@b -c user.name=t commit -qm init",
                   shell=True, cwd=repo, check=True)
    state = tmp_path / "capture"
    monkeypatch.setattr("eval.agents.patch_capture.STATE", str(state))     # 不写 /opt
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    return repo, agent_dir


SCRIPT = [
    [{"type": "tool_use", "id": "1", "name": "read_file", "input": {"file_path": "calc.py"}}],
    [{"type": "tool_use", "id": "2", "name": "edit_file",
      "input": {"file_path": "calc.py", "old_string": "a - b", "new_string": "a + b"}}],
    [{"type": "tool_use", "id": "3", "name": "bash", "input": {"command": "git stash list"}}],
    [{"type": "tool_use", "id": "4", "name": "submit", "input": {"summary": "fixed"}}],
]


def test_flat_agent_end_to_end(setup):
    repo, agent_dir = setup
    env = FakePierEnvironment(repo, agent_dir)
    agent = FlatAgent(logs_dir=agent_dir, model_name="deepseek-flash", extra_env={"DEEPSEEK_API_KEY": "x"})
    agent._make_llm = lambda: ScriptedLLM([list(s) for s in SCRIPT])
    context = SimpleNamespace(metadata=None)

    async def go():
        await agent.setup(env)
        await agent.run("Fix add()", env, context)
    asyncio.run(go())

    patch = (agent_dir / "patch.diff").read_text()
    assert "-    return a - b" in patch and "+    return a + b" in patch
    assert context.metadata["worker_status"] == "submitted"
    assert context.metadata["violations"] == {"git_write": 1}        # B 组：只记录，不拦截
    assert context.n_agent_steps == 4 and context.n_output_tokens == 400
    assert (agent_dir / "transcript.jsonl").exists()


TASK = "Fix add() in calc.py so that it returns the sum of its arguments."
PLAN = {"requirements": [{"id": "R1", "quote": TASK, "summary": "fix add"}],
        "tasks": [{"id": "T1", "title": "Fix add", "links": ["R1"]}]}
PLANNER = [{"type": "text", "text": json.dumps(PLAN)}]
QUIET = {"reviewer": False, "labeler": False, "diagnoser": False}       # 这些后台 LLM 调用会打乱脚本的顺序


def tu(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


WORKER = [[tu("1", "claim", task="T1"), tu("2", "read_file", file_path="calc.py")],
          [tu("3", "edit_file", file_path="calc.py", old_string="a - b", new_string="a + b")],
          [tu("4", "bash", command="git stash list")],
          [tu("5", "ready_for_review", task="T1")],
          [{"type": "text", "text": "done"}]]


def belay_agent(agent_dir, **kw):
    tmp = agent_dir.parent
    return BelayAgent(logs_dir=agent_dir, model_name="deepseek-flash", extra_env={"DEEPSEEK_API_KEY": "x"},
                      state_dir=str(tmp / "belay-state"), runtime={**QUIET, **kw.pop("runtime", {})}, **kw)


def test_belay_agent_without_gate_runs_strict_and_delivers(setup):
    repo, agent_dir = setup
    env = FakePierEnvironment(repo, agent_dir)
    agent = belay_agent(agent_dir)
    llm = ScriptedLLM([PLANNER] + [list(s) for s in WORKER])
    agent._make_llm = lambda: llm
    context = SimpleNamespace(metadata=None)

    async def go():
        await agent.setup(env)                             # 没有任务原文：准备留到 run() 里做
        await agent.run(TASK, env, context)
    asyncio.run(go())
    denied = llm.requests[4]["messages"][-1]["content"][0]
    assert denied["is_error"] and "Git write" in denied["content"]          # Belay 组：越界直接拒绝
    meta = context.metadata
    assert meta["belay_status"] == "DONE" and meta["delivered_level"] == "confirmed", meta
    assert meta["prepared_in_setup"] is False and context.n_agent_steps == 5
    patch = (agent_dir / "patch.diff").read_text()
    assert "+    return a + b" in patch
    for name in ("ledger.json", "events.jsonl", "setup.json", "deliverable.diff"):
        assert (agent_dir / "belay" / name).exists(), name


def test_belay_agent_prepares_in_setup_and_verifies_with_the_gate(setup):
    repo, agent_dir = setup
    (repo / "tests").mkdir()
    (repo / "tests/test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
                                             "\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    subprocess.run("git add -A && git -c user.email=a@b -c user.name=t commit -qm tests", shell=True, cwd=repo,
                   check=True)
    env = FakePierEnvironment(repo, agent_dir)
    gate = {"workdir": str(repo), "test_cmd": "python -m pytest -rA -p no:cacheprovider tests", "timeout_sec": 120}
    agent = belay_agent(agent_dir, gate_spec=json.dumps(gate), task_instruction=TASK)
    llm = ScriptedLLM([PLANNER] + [list(s) for s in WORKER])
    agent._make_llm = lambda: llm
    context = SimpleNamespace(metadata=None)

    async def go():
        await agent.setup(env)
        assert len(llm.requests) == 1                      # 规划在 setup 里做完，不占预算
        setup_info = json.loads((agent_dir / "belay" / "setup.json").read_text())
        assert setup_info["prepared"] and setup_info["guard_checks"] == 1 and setup_info["isolation"]["valid"]
        await agent.run(TASK, env, context)
    asyncio.run(go())
    events = [json.loads(x) for x in (agent_dir / "belay" / "events.jsonl").read_text().splitlines()]
    types = [e["type"] for e in events]
    assert "clock_started" in types and types.index("clock_started") < types.index("session_started")
    assert context.metadata["belay_status"] == "DONE" and context.metadata["prepared_in_setup"]
    patch = (agent_dir / "patch.diff").read_text()
    assert "+    return a + b" in patch and "tests/" not in patch


def test_belay_agent_cancelled_by_the_harness_delivers_the_confirmed_checkpoint(setup):
    repo, agent_dir = setup
    env = FakePierEnvironment(repo, agent_dir)
    agent = belay_agent(agent_dir)
    script = [PLANNER, WORKER[0], WORKER[1], [tu("c", "checkpoint", summary="fixed")],
              [tu("e2", "edit_file", file_path="calc.py", old_string="a + b", new_string="a * b")],
              [tu("s", "bash", command="sleep 30")]]
    llm = ScriptedLLM([list(s) for s in script])
    agent._make_llm = lambda: llm
    context = SimpleNamespace(metadata=None)

    async def go():
        await agent.setup(env)
        task = asyncio.create_task(agent.run(TASK, env, context))
        for _ in range(200):                               # 等到 worker 开始跑那个长命令
            await asyncio.sleep(0.1)
            if len(llm.requests) >= 6:
                break
        await asyncio.sleep(1.0)
        task.cancel()                                      # 评测框架超时
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert context.metadata["worker_status"] == "cancelled"
    assert context.metadata["belay_emergency_checkpoint"] is not None
    patch = (agent_dir / "patch.diff").read_text()
    assert "+    return a + b" in patch and "a * b" not in patch          # 交付的是确认过的存档，不是半成品

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


def test_belay_agent_is_strict_and_delivers_the_integration_branch(setup):
    repo, agent_dir = setup
    env = FakePierEnvironment(repo, agent_dir)
    tmp = agent_dir.parent
    paths = {"state": str(tmp / "belay-state"), "bin": str(tmp / "belay-bin"), "dev_jobs": str(tmp / "belay-jobs")}
    agent = BelayAgent(logs_dir=agent_dir, model_name="deepseek-flash", extra_env={"DEEPSEEK_API_KEY": "x"}, workers=1,
                       runtime={"isolation": False, "tick_sec": 0.5, "requirement_planner": "rules"},
                       paths=paths)
    llm = ScriptedLLM([list(s) for s in SCRIPT])
    agent._make_llm = lambda: llm
    context = SimpleNamespace(metadata=None)

    async def go():
        await agent.setup(env)
        await agent.run("Fix add()", env, context)
    asyncio.run(go())
    assert context.metadata["violations"] == {"git_write": 1}
    denied = llm.requests[3]["messages"][-1]["content"][0]
    assert denied["is_error"] and "Git write" in denied["content"]
    # 没有测试配置：提交直接合并；worker 结束后 runtime 把工作区作为最终候选，交付集成分支 HEAD
    assert context.metadata["belay_status"] == "DONE" and context.metadata["merges"] == 1
    patch = (agent_dir / "patch.diff").read_text()
    assert "+    return a + b" in patch
    assert (agent_dir / "belay" / "ledger.json").exists() and (agent_dir / "belay" / "events.jsonl").exists()


def test_belay_agent_splits_requirements_during_setup(setup):
    """runner 在 setup 前传入任务原文时，需求拆解在 setup 里完成（不占预算），run() 不再重拆。"""
    repo, agent_dir = setup
    env = FakePierEnvironment(repo, agent_dir)
    tmp = agent_dir.parent
    paths = {"state": str(tmp / "belay-state"), "bin": str(tmp / "belay-bin"), "dev_jobs": str(tmp / "belay-jobs")}
    split = {"requirements": [{"statement": "add() returns the sum of its arguments.", "quotes": ["Fix add()"],
                               "kind": "change", "section": ""}]}
    plan_calls = [[{"type": "text", "text": json.dumps(split)}],
                  [{"type": "text", "text": json.dumps({"ok": True, "issues": []})}]]
    agent = BelayAgent(logs_dir=agent_dir, model_name="deepseek-flash", extra_env={"DEEPSEEK_API_KEY": "x"}, workers=1,
                       runtime={"isolation": False, "tick_sec": 0.5}, paths=paths, task_instruction="Fix add()")
    llm = ScriptedLLM(plan_calls + [list(s) for s in SCRIPT])
    agent._make_llm = lambda: llm
    context = SimpleNamespace(metadata=None)

    async def go():
        await agent.setup(env)
        assert len(llm.requests) == 2                      # 拆 + 审，都在 setup 里
        await agent.run("Fix add()", env, context)
    asyncio.run(go())
    plan = json.loads((agent_dir / "belay" / "requirements.json").read_text())
    assert plan["source"] == "llm" and plan["requirements"][0]["text"] == "add() returns the sum of its arguments."
    assert plan["requirements"][0]["quotes"] == ["Fix add()"]
    assert (agent_dir / "belay" / "planner.jsonl").exists()
    assert "add() returns the sum" in llm.requests[2]["messages"][0]["content"]   # worker 看到的需求来自拆解
    assert context.metadata["belay_status"] == "DONE"

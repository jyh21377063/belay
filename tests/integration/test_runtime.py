"""runtime 的集成测试：LocalEnv + ScriptedLLM，不需要容器和模型。

本机目录充当"容器"：影子仓库、作业目录、runner.py 都在临时目录里；测试命令用当前解释器的 pytest。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from belay.config import RuntimeConfig, RuntimePaths
from belay.env import LocalEnv
from belay.llm import ScriptedLLM
from belay.runtime.bootstrap import pytest_spec, setup_container
from belay.runtime.gitops import ShadowRepo
from belay.runtime.jobs import JobRunner, JobSpec
from belay.runtime.orchestrator import Orchestrator
from belay.tools import get_belay_tools
from belay.worker import Worker, WorkerConfig
from belay.worker.transcript import Transcript
from tests.conftest import tool_use

CALC = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TESTS = ("from pkg.calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
         "def test_mul():\n    assert mul(2, 3) == 6\n")
# PYTHONDONTWRITEBYTECODE：测试里同一秒内两次修改同一个文件，避免 import 到过期的 .pyc
SPEC = {"prelude": f"export PATH={os.path.dirname(sys.executable)}:$PATH PYTHONDONTWRITEBYTECODE=1", "commands": [],
        "test_cmd": "python -m pytest -rA -p no:cacheprovider tests/test_calc.py", "parser": "parse_log_pytest",
        "timeout_sec": 300}
TASK = """The repository contains a small calculator package.
<release_notes>
### Features
- Add a `sub(a, b)` function to `pkg.calc`
</release_notes>"""


def run(coro):
    return asyncio.run(coro)


def make_repo(path: Path) -> Path:
    (path / "pkg").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "pkg" / "__init__.py").write_text("")
    (path / "pkg" / "calc.py").write_text(CALC)
    (path / "tests" / "test_calc.py").write_text(TESTS)
    subprocess.run("git init -q && git add -A && git -c user.email=a@b -c user.name=t commit -qm init",
                   shell=True, cwd=path, check=True)
    return path


def local_paths(tmp: Path) -> RuntimePaths:
    return RuntimePaths(state=str(tmp / "state"), bin=str(tmp / "bin"), dev_jobs=str(tmp / "devjobs"))


def setup(tmp: Path, **cfg):
    repo = make_repo(tmp / "repo")
    paths = local_paths(tmp)
    env = LocalEnv(str(repo))
    rc = RuntimeConfig(isolation=False, test_author=False, **cfg)
    info = run(setup_container(env, str(repo), SPEC, rc, paths, log=lambda m: None))
    return repo, paths, env, rc, info


def test_setup_records_baseline(tmp_path):
    repo, paths, env, rc, info = setup(tmp_path)
    assert info.gate_available, info.notes
    assert info.baseline == {"tests/test_calc.py::test_add": "PASS", "tests/test_calc.py::test_mul": "PASS"}
    assert info.test_files == ["tests/test_calc.py"] and info.base_commit
    left = subprocess.run("git status --porcelain", shell=True, cwd=repo, capture_output=True, text=True).stdout
    assert [x for x in left.splitlines() if "__pycache__" not in x] == []   # 基线没有在工作区留下东西


def test_strip_tests_and_gate_on_candidate_tree_restores_workspace(tmp_path):
    repo, paths, env, rc, info = setup(tmp_path)
    (repo / "pkg" / "calc.py").write_text(CALC.replace("a * b", "a + b"))            # 回归
    worker_test = TESTS.replace("mul(2, 3) == 6", "mul(2, 3) == 5")                   # 改测试迁就实现
    (repo / "tests" / "test_calc.py").write_text(worker_test)
    (repo / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n")
    shadow = ShadowRepo(env, paths)
    tree = run(shadow.snapshot(str(repo), "w1"))
    cand, dropped = run(shadow.strip_tests(info.base_tree, tree))
    assert sorted(dropped) == ["tests/test_calc.py", "tests/test_new.py"]
    assert run(shadow.changed(info.base_tree, cand)) == ["pkg/calc.py"]

    runner = JobRunner(env)
    js = JobSpec("G1", f"{paths.gate_jobs}/G1", str(repo), 300, runner=paths.runner,
                 spec=pytest_spec(SPEC, str(repo), tree=cand, git_dir=paths.git_dir))
    out = run(_launch_and_wait(runner, js))
    assert out.state == "DONE", out.result
    assert out.result["tests"]["tests/test_calc.py::test_mul"] == "FAILED"          # 门禁跑的是原始测试
    assert (repo / "tests" / "test_calc.py").read_text() == worker_test             # 工作区原样恢复
    assert (repo / "tests" / "test_new.py").exists()


async def _launch_and_wait(runner, js):
    await runner.launch(js)
    return await runner.wait(js, time.time())


def test_orchestrator_rejects_regression_then_merges_and_delivers_head(tmp_path):
    repo, paths, env, rc, info = setup(tmp_path, tick_sec=0.5)
    llm = ScriptedLLM([
        [tool_use("a1", "read_file", file_path="pkg/calc.py")],
        [tool_use("a2", "edit_file", file_path="pkg/calc.py", old_string="    return a * b\n",
                  new_string="    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"),
         tool_use("a3", "write_file", file_path="tests/test_new.py", content="def test_x():\n    assert True\n")],
        [tool_use("a4", "submit", summary="sub added", final=False)],
        [tool_use("a5", "edit_file", file_path="pkg/calc.py", old_string="    return a + b\n\n\ndef sub",
                  new_string="    return a * b\n\n\ndef sub")],
        [tool_use("a6", "run_check")],
        [tool_use("a7", "wait", job_ids=["J2"])],
        [tool_use("a8", "submit", summary="done", final=True)],
    ])
    out = tmp_path / "out"

    def factory(work_id, runtime, task_refresh, deadline):
        return Worker(llm, env, tools=get_belay_tools(), config=WorkerConfig(deadline=deadline),
                      transcript=Transcript(out / "transcript.jsonl"), runtime=runtime, work_id=work_id,
                      task_refresh=task_refresh)

    async def go():
        orch = Orchestrator(cfg=rc, paths=paths, setup=info, spec=SPEC, instruction=TASK, root_env=env,
                            agent_env=env, llm=llm, worker_factory=factory, out_dir=out, budget_sec=600,
                            log=lambda m: None)
        status = await asyncio.wait_for(orch.run(), 120)
        await orch.shutdown(deliver=True)
        return status, orch

    status, orch = run(go())
    assert status == "DONE", orch.final_reason
    rejected = llm.requests[3]["messages"][-1]["content"][0]["content"]
    assert "rejected" in rejected and "test_mul" in rejected
    waited = llm.requests[6]["messages"][-1]["content"][0]["content"]
    assert "2 tests ran, 2 passed" in waited
    finished = json.loads((out / "ledger.json").read_text())
    assert finished["status"] == "DONE" and finished["merges"] == 1
    assert [r["id"] for r in finished["requirements"]] == ["R1"]
    # 交付 = 集成分支 HEAD：有 sub、mul 正确；测试路径下的新文件不在交付物里
    calc = (repo / "pkg" / "calc.py").read_text()
    assert "def sub" in calc and "return a * b" in calc
    assert not (repo / "tests" / "test_new.py").exists()
    types = [json.loads(line)["type"] for line in (out / "events.jsonl").read_text().splitlines()]
    assert "gate_rejected" in types and "merged" in types and "finish" in types


class RoutingLLM:
    """按系统提示把调用分给不同的脚本：worker、Test Author、Reviewer 并发运行时顺序不确定。"""

    def __init__(self, worker: ScriptedLLM, author: ScriptedLLM, reviewer: ScriptedLLM):
        self.worker, self.author, self.reviewer = worker, author, reviewer

    async def call(self, system, tools, messages, tool_choice=None):
        target = (self.author if "TEST AUTHOR" in system else self.reviewer if "REVIEWER" in system
                  else self.worker)
        return await target.call(system, tools, messages, tool_choice)


TASK_M4 = """The repository contains a small calculator package.
<release_notes>
### Features
- Add a `sub(a, b)` function to `pkg.calc`
### Breaking changes
- `mul(a, b)` now returns `a * b + 1`
</release_notes>"""

AUTHORED = {
    "R1": """```python
import pkg.calc as calc


def test_sub():
    assert hasattr(calc, "sub")
    assert calc.sub(5, 3) == 2
```
Checks the new sub().""",
    "R2": """```python
from pkg.calc import mul


def test_mul_adds_one():
    assert mul(2, 3) == 7
```
Checks the new mul().""",
}


class AuthorLLM:
    """Test Author 按需求并发运行：按任务里的需求 id 分给各自的脚本。"""

    def __init__(self, answers: dict[str, str]):
        self.scripts = {rid: ScriptedLLM([[{"type": "text", "text": text}]]) for rid, text in answers.items()}

    async def call(self, system, tools, messages, tool_choice=None):
        first = json.dumps(messages[0]["content"])
        rid = next(r for r in self.scripts if f'id=\\"{r}\\"' in first)
        return await self.scripts[rid].call(system, tools, messages, tool_choice)


def test_acceptance_tests_gate_the_final_submission_and_reports_waive_old_tests(tmp_path):
    repo = make_repo(tmp_path / "repo")
    paths = local_paths(tmp_path)
    env = LocalEnv(str(repo))
    rc = RuntimeConfig(isolation=False, tick_sec=0.5)
    info = run(setup_container(env, str(repo), SPEC, rc, paths, log=lambda m: None))
    assert info.test_author_available, info.notes
    worker = ScriptedLLM([
        [tool_use("w1", "read_file", file_path="pkg/calc.py")],
        [tool_use("w2", "edit_file", file_path="pkg/calc.py", old_string="    return a * b\n",
                  new_string="    return a * b + 1\n\n\ndef sub(a, b):\n    return a - b\n")],
        [tool_use("w3", "report_conflict", kind="test_conflict", requirement="R2",
                  checks=["tests/test_calc.py::test_mul"], reason="R2 changes what mul returns")],
        [tool_use("w4", "submit", summary="done", final=True)],
    ])
    reviewer = ScriptedLLM([[{"type": "text", "text": json.dumps({
        "decision": "approve", "quote": "`mul(a, b)` now returns `a * b + 1`", "reason": "explicit change"})}]])
    llm = RoutingLLM(worker, AuthorLLM(AUTHORED), reviewer)
    out = tmp_path / "out"

    def factory(work_id, runtime, task_refresh, deadline):
        return Worker(llm, env, tools=get_belay_tools(), config=WorkerConfig(deadline=deadline),
                      transcript=Transcript(out / "transcript.jsonl"), runtime=runtime, work_id=work_id,
                      task_refresh=task_refresh)

    async def go():
        orch = Orchestrator(cfg=rc, paths=paths, setup=info, spec=SPEC, instruction=TASK_M4, root_env=env,
                            agent_env=env, llm=llm, worker_factory=factory, out_dir=out, budget_sec=600,
                            log=lambda m: None)
        status = await asyncio.wait_for(orch.run(), 120)
        await orch.shutdown(deliver=True)
        return status, orch

    status, orch = run(go())
    ledger = json.loads((out / "ledger.json").read_text())
    assert status == "DONE", (orch.final_reason, ledger)
    # 两条需求都由开工时写好的验收测试证明；test_mul 的失败经申诉豁免，不挡合并
    assert {r["id"]: r["status"] for r in ledger["requirements"]} == {"R1": "SUPPORTED", "R2": "SUPPORTED"}
    report_reply = worker.requests[3]["messages"][-1]["content"][0]["content"]
    assert "approved" in report_reply
    types = [json.loads(line)["type"] for line in (out / "events.jsonl").read_text().splitlines()]
    assert types.count("test_requested") == 2 and "merged" in types
    calc = (repo / "pkg" / "calc.py").read_text()
    assert "def sub" in calc and "a * b + 1" in calc
    assert (out / "reviews.jsonl").exists() and list(out.glob("test_author-T1-*.jsonl"))

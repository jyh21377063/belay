"""长程场景的端到端测试（LocalEnv + ScriptedLLM + 真实 git / pytest，不需要容器和模型）。

  模块 A  验证槽位：验证进行中 worker 修改同一文件不丢；导入隔离（破坏探针、sys.path 映射、基线双跑）；抢占
  模块 B  自动快照与后台存档（在 test_belay_run 里也覆盖）
  模块 D  被拒信息：原因、failure_log、按门的口径复现、定位与只撤销这一段
  模块 G  runtime 重启时重新接上仍在运行的作业；删掉影子仓库与工作区后 resume --rebuild
  模块 H  到软阈值后等 step_done 再交接；恢复后开场带步骤列表与部分改动
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from belay.core.config import BelayConfig
from belay.core.invariants import check, check_log
from belay.core.model import Job
from belay.core.reduce import replay
from belay.env import LocalEnv
from belay.llm import ScriptedLLM
from belay.runtime.driver import BelayRun, RunSettings
from belay.runtime.runtime import Runtime
from belay.runtime.store import EventStore
from belay.runtime.verifier import RunnerVerifier, VerifierSpec
from tests.integration.test_belay_run import (ADD, ADD_SUB, BLOCK_T2, FIX_ADD, MUL, PLANNER, READ, SPEC, TASK,
                                              assert_t2_blocked, call, first_message, results, say, tu)

MOD = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
TESTS = ("from pkg.mod import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
         "def test_mul():\n    assert mul(2, 3) == 6\n")


def make_repo(d: Path, pkg_root: str = "", tests: str = TESTS, extra: dict | None = None) -> None:
    root = d / pkg_root if pkg_root else d
    (root / "pkg").mkdir(parents=True)
    (d / "tests").mkdir(exist_ok=True)
    (root / "pkg/__init__.py").write_text("")
    (root / "pkg/mod.py").write_text(MOD)
    (d / "tests/test_mod.py").write_text(tests)
    for rel, text in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    subprocess.run("git init -q && git add -A && git -c user.email=a@b -c user.name=t commit -qm init", shell=True,
                   cwd=d, check=True)


class H:
    def __init__(self, tmp: Path, cfg: BelayConfig | None = None, spec: VerifierSpec = SPEC, budget: float = 600,
                 repo_kw: dict | None = None, **settings):
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.repo.mkdir()
        make_repo(self.repo, **(repo_kw or {}))
        self.cfg = cfg or BelayConfig()
        self.spec = spec
        kw = dict(run_dir=str(tmp / "run"), budget_sec=budget, git_dir=str(tmp / "state/git"),
                  jobs_dir=str(tmp / "state/jobs"), tick_sec=0.2, crash_backoff_sec=0.0, retry_backoff_sec=0.0)
        kw.update(settings)
        self.settings = RunSettings(**kw)
        self.logs: list[str] = []

    def make(self, llm, cls=BelayRun, **kw) -> BelayRun:
        return cls(llm, LocalEnv(str(self.repo)), self.settings, self.cfg, self.spec,
                   aux_llm=kw.pop("aux", ScriptedLLM([])), log=self.logs.append, **kw)

    def events(self):
        store = EventStore(self.settings.run_dir)
        try:
            return store.events()
        finally:
            store.close()

    def verify_log(self, run: BelayRun):
        events = self.events()
        g = replay(events)
        assert g == run.rt.graph
        assert not check(g) and not check_log(events)
        return events


async def setup_only(h: H) -> BelayRun:
    """只做准备阶段（基线 + 规划），用来检查导入隔离。"""
    run = h.make(ScriptedLLM([PLANNER]))
    run.rt = Runtime(run.store, run.cfg, clock=run.clock, log=run.log)
    run._wire()
    await run._setup(TASK, "setup")
    await run.rt.close()
    return run


# ======================================================================== 模块 A：导入隔离

def test_isolation_is_valid_for_a_plain_repo(tmp_path):
    h = H(tmp_path)
    run = asyncio.run(setup_only(h))
    g = run.rt.graph
    assert g.isolation["valid"] is True and not g.degraded
    assert g.isolation["probe"]["inconclusive"] is False                 # 找到了被测试导入的源文件并破坏了它
    assert g.baseline == {ADD: "fail", MUL: "pass"}
    wheres = sorted(j.where for j in g.jobs.values() if j.purpose == "baseline")
    assert wheres == ["slot", "workspace"]                               # 基线双跑：工作区一次，槽位一次


def _site_prelude(tmp: Path, target: str) -> str:
    """模拟 pip install -e：一个 site 目录里的 .pth 把工作区里的某个目录加到 sys.path 末尾。"""
    site = tmp / "site"
    pth = tmp / "pth"
    site.mkdir()
    pth.mkdir()
    (pth / "editable.pth").write_text(target + "\n")
    (site / "sitecustomize.py").write_text(f"import site\nsite.addsitedir({str(pth)!r})\n")
    return f"export PYTHONPATH={site}${{PYTHONPATH:+:$PYTHONPATH}}"


def test_mapped_sys_path_isolates_a_lib_layout(tmp_path):
    repo = tmp_path / "repo"
    spec = VerifierSpec(test_cmd="python -m pytest -rA -p no:cacheprovider tests", timeout_sec=120,
                        prelude=_site_prelude(tmp_path, str(repo / "lib")))
    h = H(tmp_path, spec=spec, repo_kw={"pkg_root": "lib"})
    run = asyncio.run(setup_only(h))
    assert "lib" in run.verifier.pythonpath_rel                          # 工作区的 sys.path 映射到槽位
    assert run.rt.graph.isolation["valid"] is True


def test_probe_catches_imports_that_resolve_to_the_workspace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    spec = VerifierSpec(test_cmd="python -m pytest -rA -p no:cacheprovider tests", timeout_sec=120,
                        prelude=_site_prelude(tmp_path, str(repo / "lib")))
    h = H(tmp_path, spec=spec, repo_kw={"pkg_root": "lib"})
    monkeypatch.setattr(RunnerVerifier, "map_sys_path", lambda self, sp: ["."])   # 不做映射：只放根目录
    run = asyncio.run(setup_only(h))
    g = run.rt.graph
    assert g.isolation["valid"] is False and g.degraded
    assert "resolves to" in g.isolation["reason"] or "did not break" in g.isolation["reason"]
    assert sorted(j.tag for j in g.jobs.values() if j.purpose == "baseline") == \
        ["baseline#slot", "baseline#ws", "baseline#ws2"]                  # 降级：基线用工作区上的两次


def test_baseline_dual_run_detects_a_missing_dependency(tmp_path):
    reads_outside = ("import os\nfrom pkg.mod import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                     "def test_mul():\n    assert mul(2, 3) == 6\n\n\ndef test_data():\n"
                     "    here = os.path.dirname(os.path.abspath(__file__))\n"
                     "    assert os.path.exists(os.path.join(here, '..', '..', 'outside.txt'))\n")
    (tmp_path / "outside.txt").write_text("data")
    h = H(tmp_path, repo_kw={"tests": reads_outside})
    run = asyncio.run(setup_only(h))
    g = run.rt.graph
    assert g.degraded and g.isolation["diff"] == 1 and "test_data" in g.isolation["reason"]
    assert g.baseline["tests/test_mod.py::test_data"] == "pass"            # 守护集合来自工作区上的两次


def test_degraded_mode_has_no_background_verification(tmp_path):
    (tmp_path / "outside.txt").write_text("data")
    reads_outside = TESTS + ("\n\ndef test_data():\n    import os\n    here = os.path.dirname(os.path.abspath(__file__))\n"
                             "    assert os.path.exists(os.path.join(here, '..', '..', 'outside.txt'))\n")
    h = H(tmp_path, repo_kw={"tests": reads_outside})
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                       call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    assert g.degraded
    assert_t2_blocked(run, res)
    assert all(a.lane == "fg" for a in g.attempts.values())             # 没有后台存档
    assert all(j.where in ("workspace", "live") for j in g.jobs.values() if j.purpose != "baseline")
    h.verify_log(run)


# ======================================================================== 模块 A：验证与编辑并发

def test_worker_edits_during_verification_are_not_lost(tmp_path):
    slow = TESTS.replace("def test_add():", "import time\n\n\ndef test_add():\n    time.sleep(2)")
    h = H(tmp_path, repo_kw={"tests": slow}, deliver_checkout=False)
    edit2 = tu("e2", "edit_file", file_path="pkg/mod.py", old_string="def mul(a, b):",
               new_string="def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):")
    todos = tu("t", "todo_write", todos=[{"content": "fix add", "status": "in_progress"},
                                         {"content": "add sub", "status": "pending"}])
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ, todos), call(FIX_ADD),
                       call(tu("sd", "step_done", summary="add fixed")),             # 步骤锚点进后台验证
                       call(tu("s", "bash", command="sleep 0.5")), call(edit2),       # 后台验证正在跑
                       call(tu("w", "bash", command="sleep 3")), call(BLOCK_T2),
                       call(tu("3", "ready_for_review", task="T1")), say("done")])
    run = h.make(llm)
    asyncio.run(run.start(TASK))
    text = (h.repo / "pkg/mod.py").read_text()
    assert "return a + b" in text and "def sub(a, b)" in text               # worker 的改动没有被覆盖
    g = run.rt.graph
    first = g.checkpoints[1]
    shown = subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "show", f"{first.tree}:pkg/mod.py"],
                           capture_output=True, text=True).stdout
    assert "return a + b" in shown and "def sub" not in shown             # 结果记在它验证的那棵树上
    h.verify_log(run)


# ======================================================================== 模块 A：可抢占的优先级队列

def test_preemption_lets_the_waiting_checkpoint_go_first(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run_tests.sh").write_text("sleep ${SLEEP:-3}; echo PASSED tests/test_x.py::test_a\n")
    subprocess.run("git init -q && git add -A && git -c user.email=a@b -c user.name=t commit -qm init", shell=True,
                   cwd=repo, check=True)
    env = LocalEnv(str(repo))
    spec = VerifierSpec(test_cmd="bash run_tests.sh", timeout_sec=60)

    async def main():
        from belay.runtime.gitops import ShadowRepo
        git = str(tmp_path / "git")
        shadow = ShadowRepo(env, git, str(repo))
        _commit, tree = await shadow.init()
        v = RunnerVerifier(env, spec, str(repo), git, str(tmp_path / "jobs"), slots=1, poll_sec=0.1)
        prio = {"J1": 4, "J2": 2}
        v.priority_of = lambda job: prio[job.id]
        preempted = []

        async def on_pre(jid):
            preempted.append(jid)
        v.on_preempt = on_pre
        done_order = []

        async def go(jid, delay):
            await asyncio.sleep(delay)
            out = await v.run(Job(jid, jid, tree, None, "verify"))
            done_order.append((jid, out.state, out.results))
        await asyncio.gather(go("J1", 0), go("J2", 1.0))
        return done_order, preempted

    order, preempted = asyncio.run(main())
    assert [x[0] for x in order] == ["J2", "J1"] and preempted == ["J1"]
    assert all(x[1] == "finished" and x[2] == {"tests/test_x.py::test_a": "PASSED"} for x in order)


# ======================================================================== 模块 D：被拒信息补全与规则定位

def test_rejection_reasons_failure_log_gate_check_and_revert_change(tmp_path):
    h = H(tmp_path, BelayConfig(confirm_regressions=False))
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(break_mul),
                       call(tu("c", "checkpoint", summary="try")),
                       call(tu("f", "failure_log", test=MUL)),
                       call(tu("g", "run_check", tests=[MUL], as_gate=True)), call(tu("w", "wait", jobs=["J4"])),
                       call(tu("r", "revert_change", located="L1#0")), call(READ), call(FIX_ADD),
                       call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    out = results(llm)
    rej = out[3]
    assert "Checkpoint rejected" in rej and "assert" in rej                     # D1：失败原因
    assert "first failed at" in rej and "revert_change(located=\"L1#0\")" in rej  # D3：定位随拒绝消息返回
    assert "def test_mul" in out[4] and "assert 5 == 6" in out[4]               # failure_log：traceback 段落
    assert "as the gate runs it" in out[5] and "REGRESSIONS" in out[6]           # D2：按门的口径复现
    assert "Reverted the change" in out[7]
    assert "return a * b" in out[8]                                             # D4：只撤销了那一段
    assert_t2_blocked(run, res)
    g = run.rt.graph
    loc = g.locates["L1"]
    assert loc.results and loc.results[0]["exact"] and "pkg/mod.py" in json.dumps(loc.results[0]["files"])
    h.verify_log(run)


# ======================================================================== 模块 G：重新接上、容器重建

class CrashDuringJob(BelayRun):
    main: asyncio.Task | None = None

    async def _eff_launch_job(self, job: str) -> None:
        j = self.rt.graph.jobs[job]
        if j.purpose == "verify":
            await self.verifier.setup()
            slot = self.verifier.slots[0]
            await self.verifier.launch(j, self.verifier.runner_spec(j, slot, 4))   # 作业进程已经起来了
            await asyncio.sleep(0.5)
            self.main.cancel()
            await asyncio.sleep(3600)
        await super()._eff_launch_job(job)


def _phase1(h: H, cls, script, **attrs):
    async def phase1():
        run = h.make(ScriptedLLM(script), cls)
        for k, v in attrs.items():
            setattr(run, k, v)
        run.main = asyncio.create_task(run.start(TASK))
        with pytest.raises(asyncio.CancelledError):
            await run.main
        for t in list(run._bg) + list(run.rt._tasks):
            t.cancel()
        run.store.close()
    asyncio.run(phase1())


def test_runtime_restart_reattaches_a_running_job(tmp_path):
    slow = TESTS.replace("def test_add():", "import time\n\n\ndef test_add():\n    time.sleep(6)")
    h = H(tmp_path, repo_kw={"tests": slow})
    todos = tu("t", "todo_write", todos=[{"content": "fix add", "status": "in_progress"}])
    _phase1(h, CrashDuringJob, [PLANNER, call(tu("1", "claim", task="T1"), READ, todos), call(FIX_ADD),
                                call(tu("sd", "step_done", summary="add fixed")),   # 步骤锚点的验证作业
                                call(tu("s", "bash", command="sleep 3"))])
    assert not (tmp_path / "state/jobs/J3/done").exists()                     # 作业在 runtime 死后仍在跑
    llm2 = ScriptedLLM([call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm2)
    res = asyncio.run(run.resume())
    assert_t2_blocked(run, res)
    events = h.verify_log(run)
    rec = next(e for e in events if e.type == "runtime_recovered")
    assert run.rt.graph.sessions["S1"].resumes == ("replay",)                   # 会话也原样接上
    assert any(j.get("reattached") for j in rec.get("reconciled")["jobs"])        # 重新接上，而不是重跑
    verify = [e for e in events if e.type == "job_started" and e.get("purpose") == "verify"]
    assert len({e.get("key") for e in verify}) == len(verify)
    assert not [e for e in events if e.type == "job_finished" and e.get("state") == "unknown"]


class CrashAfterMirror(ScriptedLLM):
    def __init__(self, script, at: int, run_ref: list):
        super().__init__(script)
        self.at = at
        self.n = 0
        self.run_ref = run_ref

    async def call(self, system, tools, messages, tool_choice=None):
        self.n += 1
        if self.n == self.at:
            run = self.run_ref[0]
            await asyncio.sleep(1.0)                                # 让后台的 bundle 导出完成
            await run.mirror("test")
            run.main.cancel()
            await asyncio.sleep(3600)
        return await super().call(system, tools, messages, tool_choice)


def test_rebuild_from_bundles_after_losing_the_container_state(tmp_path):
    h = H(tmp_path, BelayConfig(snapshot_bash_every=1))
    ref: list = []
    script = [PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
              call(tu("c", "checkpoint", summary="add fixed")), call(ADD_SUB), call(tu("x", "bash", command="true"))]

    async def phase1():
        llm = CrashAfterMirror(script, at=7, run_ref=ref)
        run = h.make(llm)
        ref.append(run)
        run.main = asyncio.create_task(run.start(TASK))
        with pytest.raises(asyncio.CancelledError):
            await run.main
        for t in list(run._bg) + list(run.rt._tasks):
            t.cancel()
        g = run.rt.graph
        run.store.close()
        return g
    g1 = asyncio.run(phase1())
    assert (tmp_path / "run" / "git" / "1.bundle").exists()
    # 容器没了：影子仓库、验证目录、工作区都回到原始镜像的样子
    subprocess.run(f"rm -rf {tmp_path / 'state'}", shell=True, check=True)
    subprocess.run("git checkout -q -- . && git clean -qfdx", shell=True, cwd=h.repo, check=True)
    assert "return a - b" in (h.repo / "pkg/mod.py").read_text()
    llm2 = ScriptedLLM([call(READ), call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm2)
    h.settings.deliver_checkout = False
    res = asyncio.run(run.resume(rebuild=True))
    g = run.rt.graph
    assert g.run.rebuilds == 1
    events = h.verify_log(run)
    rec = next(e for e in events if e.type == "runtime_recovered")
    assert rec.get("rebuilt") is True
    ref_head = subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "rev-parse", "refs/heads/belay"],
                              capture_output=True, text=True).stdout.strip()
    assert ref_head == g.head_cp.commit
    kept = [s for s in g1.snapshots.values() if not g.snapshots[s.n].lost]
    assert kept and all(subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "cat-file", "-e", s.commit]).returncode
                        == 0 for s in kept)
    text = (h.repo / "pkg/mod.py").read_text()
    assert "return a + b" in text and "def sub(a, b)" in text               # 工作区 = 最新一张已导出快照
    assert_t2_blocked(run, res)


# ======================================================================== 模块 H：交接落在步骤边界

def test_soft_threshold_hands_off_at_the_next_step_done(tmp_path):
    cfg = BelayConfig(l2_tokens=5000, l4_tokens=10 ** 9, l1_trigger_tokens=10 ** 9)
    h = H(tmp_path, cfg)
    todos = tu("t", "todo_write", todos=[{"content": "fix add", "status": "in_progress"},
                                         {"content": "add sub", "status": "pending"}])
    script = [PLANNER, call(tu("1", "claim", task="T1"), todos), call(READ), call(FIX_ADD),
              call(tu("sd", "step_done", summary="add fixed")),
              say("Next I add sub."),                                             # 交接摘要
              call(ADD_SUB), call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")]
    llm = ScriptedLLM(script, context_tokens=[1000, 1000, 6000, 6000, 6000, 1000, 1000, 1000, 1000, 1000])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    assert g.sessions["S1"].end_reason == "handoff"
    assert not [c for c in g.compactions if c.level in (2, 3)]                  # 软阈值后没有做 L2
    assert g.steps["T1.1"].status == "anchored" and g.steps["T1.1"].summary == "add fixed"
    opening2 = first_message(llm.requests[6])
    assert "T1.1 fix add — done" in opening2 and "T1.2 add sub  <- current step" in opening2
    tr = [json.loads(x) for x in open(g.sessions["S1"].transcript) if x.strip()]
    assert any(r["type"] == "handoff" and r.get("at_step_boundary") for r in tr)
    assert_t2_blocked(run, res)
    h.verify_log(run)


class KillMidStep(ScriptedLLM):
    def __init__(self, script, at: int, ref: list):
        super().__init__(script)
        self.at, self.n, self.ref = at, 0, ref

    async def call(self, system, tools, messages, tool_choice=None):
        self.n += 1
        if self.n == self.at:
            self.ref[0].main.cancel()
            await asyncio.sleep(3600)
        return await super().call(system, tools, messages, tool_choice)


def test_killed_mid_step_resumes_with_steps_and_partial_diff(tmp_path):
    cfg = BelayConfig(resume_max_downtime_sec=0)                              # 停机太久：不原样接上，开新会话
    h = H(tmp_path, cfg)
    todos = tu("t", "todo_write", todos=[{"content": "read code", "status": "in_progress"},
                                         {"content": "fix add", "status": "pending"},
                                         {"content": "add sub", "status": "pending"}])
    ref: list = []
    script = [PLANNER, call(tu("1", "claim", task="T1"), todos), call(READ),
              call(tu("sd", "step_done", summary="read it")), call(FIX_ADD), say("never reached")]

    async def phase1():
        run = h.make(KillMidStep(script, at=6, ref=ref))
        ref.append(run)
        run.main = asyncio.create_task(run.start(TASK))
        with pytest.raises(asyncio.CancelledError):
            await run.main
        for t in list(run._bg) + list(run.rt._tasks):
            t.cancel()
        run.store.close()
    asyncio.run(phase1())
    llm2 = ScriptedLLM([call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm2)
    res = asyncio.run(run.resume())
    g = run.rt.graph
    assert g.sessions["S1"].end_reason == "runtime_crash" and g.sessions["S2"].reason == "recover"
    opening = first_message(llm2.requests[0])
    assert "T1.1 read code — done" in opening and "T1.2 fix add  <- current step" in opening
    assert "+    return a + b" in opening                                    # 部分改动：保留在工作区，交还给模型
    assert "Your last actions before the interruption" in opening
    assert "Files of the current step (re-read by the harness)" in opening
    assert_t2_blocked(run, res)
    h.verify_log(run)


# ======================================================================== 模块 E、F：诊断者与复查者（LLM 只能解释、只能收紧）

class RoleLLM:
    """按系统提示区分角色的假模型：复查者第一次说没做完，诊断者给出结构化结论，标签给一行说明。"""

    def __init__(self):
        self.calls: list[str] = []
        self.reviews = 0

    async def call(self, system, tools, messages, tool_choice=None):
        from belay.llm import Response
        from belay.runtime import prompts as PR
        body = json.dumps(messages)[:200000]
        if system == PR.REVIEW_SYSTEM:
            self.calls.append("review")
            self.reviews += 1
            text = json.dumps({"implemented": "no" if self.reviews == 1 else "yes",
                               "missing": ["sub() is not defined"], "evidence": []})
        elif system == PR.DIAGNOSE_SYSTEM:
            self.calls.append("diagnose")
            assert "test_mul" in body and "Located change" in body              # 输入来自图：测试源码、定位出的 diff
            text = json.dumps({"suspects": [{"file": "pkg/mod.py", "hunk": "@@", "confidence": 0.9,
                                             "reason": "mul now adds"}],
                               "intentional": {"likely": True, "requirement": "R1", "quote": "not in the task"},
                               "suggestion": "restore a * b", "flaky_suspect": False})
        elif system == PR.LABEL_SYSTEM:
            self.calls.append("label")
            text = "fixed add"
        else:
            self.calls.append("other")
            text = "summary"
        return Response([{"type": "text", "text": text}], "end_turn")


def test_diagnoser_and_reviewer_only_explain_or_tighten(tmp_path):
    h = H(tmp_path, BelayConfig(confirm_regressions=False))
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    fix_mul = tu("fm", "edit_file", file_path="pkg/mod.py", old_string="return a + b + 0", new_string="return a * b")
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(break_mul),
                       call(tu("c", "checkpoint")), call(tu("w", "bash", command="sleep 1")), call(READ),
                       call(fix_mul), call(FIX_ADD), call(tu("3", "ready_for_review", task="T1")),
                       call(tu("4", "claim", task="T2")), call(tu("6", "ready_for_review", task="T2")),
                       call(tu("w2", "bash", command="sleep 1")),
                       call(tu("7", "claim", task="T2")), call(tu("r2", "read_file", file_path="pkg/mod.py")),
                       call(ADD_SUB), call(tu("8", "ready_for_review", task="T2")), say("done")])
    aux = RoleLLM()
    run = h.make(llm, aux=aux)
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    d = next(iter(g.diagnoses.values()))
    assert d.status == "recorded" and d.result["intentional"]["likely"] is False   # 引文不在原文里：丢弃这一项
    notices = json.dumps([r["messages"][-1]["content"] for r in llm.requests])
    assert "Diagnosis of tests/test_mod.py::test_mul" in notices and "restore a * b" in notices
    assert "A reviewer reopened T2: sub() is not defined" in notices
    t2 = g.tasks["T2"]
    assert t2.status == "done_unverified" and t2.review_reopens == 1 and aux.reviews == 1   # 第二次声明做完不再复查
    events = h.verify_log(run)
    llm_events = [e for e in events if e.source == "llm" and e.type not in ("plan_proposed", "task_added",
                                                                            "task_split", "compacted")]
    assert {e.type for e in llm_events} <= {"diagnosis_recorded", "review_recorded", "checkpoint_labeled"}
    assert res.status == "DONE"



def test_bundles_are_consolidated_and_restore_every_checkpoint(tmp_path):
    """G3：增量 bundle 够数后合并成一份完整的；只用宿主机上的 bundle 就能还原全部快照与里程碑存档，
    自动存档的提交可以从快照的树原样重做。"""
    h = H(tmp_path, BelayConfig(mirror_consolidate=2, mirror_every=1, snapshot_bash_every=1))
    edit = lambda i, a, b: tu(f"e{i}", "edit_file", file_path="pkg/mod.py", old_string=a, new_string=b)  # noqa: E731
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                       call(tu("c1", "checkpoint", summary="one")),
                       call(edit(2, "def mul(a, b):", "def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):")),
                       call(tu("c2", "checkpoint", summary="two")),
                       call(edit(3, "def mul(a, b):", "def neg(a):\n    return -a\n\n\ndef mul(a, b):")),
                       call(tu("c3", "checkpoint", summary="three")),
                       call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert_t2_blocked(run, res)
    meta = EventStore(h.settings.run_dir).get_meta("mirror")
    files = sorted(p.name for p in (tmp_path / "run" / "git").glob("*.bundle"))
    assert any(f.endswith("-full.bundle") for f in files) and files == sorted(meta["bundles"])
    assert len(files) < meta["seq"]                                           # 旧的增量文件已删除
    # 与重建相同：先从原始代码重建 0 号基线（bundle 以它为前提），再按顺序 unbundle
    orig = tmp_path / "orig"
    subprocess.run(["git", "clone", "-q", str(h.repo), str(orig)], check=True)    # 工作区的改动没有提交过
    fresh = tmp_path / "fresh.git"
    from belay.runtime.gitops import ShadowRepo
    base_commit, _tree = asyncio.run(ShadowRepo(LocalEnv(str(orig)), str(fresh), str(orig)).init())
    g = run.rt.graph
    assert base_commit == g.checkpoints[0].commit
    for name in meta["bundles"]:
        subprocess.run(["git", f"--git-dir={fresh}", "bundle", "unbundle", str(tmp_path / "run" / "git" / name)],
                       check=True, capture_output=True)

    def has(sha):
        return subprocess.run(["git", f"--git-dir={fresh}", "cat-file", "-e", sha]).returncode == 0
    assert all(has(s.commit) for s in g.snapshots.values())
    assert all(has(c.tree) for c in g.checkpoints.values() if c.id > 0)
    assert all(has(c.commit) for c in g.checkpoints.values() if c.id > 0)


# ======================================================================== 评测接入：准备在预算之外

def test_prepare_then_run_starts_the_budget_clock_late(tmp_path):
    """评测框架的 setup 阶段做准备（时钟停在 1000），run 阶段才开始计时（真实时钟）：截止时间从 run 开始算。"""
    h = H(tmp_path, budget=600)

    async def prepare():
        run = BelayRun(ScriptedLLM([PLANNER]), LocalEnv(str(h.repo)), h.settings, h.cfg, h.spec,
                       aux_llm=ScriptedLLM([]), clock=lambda: 1000.0, log=h.logs.append)
        await run.prepare(TASK)
        run.store.close()

    async def run_phase():
        llm = ScriptedLLM([call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                           call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
        run = BelayRun(llm, LocalEnv(str(h.repo)), h.settings, h.cfg, h.spec, aux_llm=ScriptedLLM([]),
                       log=h.logs.append)
        assert run.prepared(TASK) and not run.prepared(TASK + " more")
        return run, await run.run_prepared()
    asyncio.run(prepare())
    run, res = asyncio.run(run_phase())
    events = h.verify_log(run)
    types = [e.type for e in events]
    started = next(e for e in events if e.type == "run_started")
    clock_ev = next(e for e in events if e.type == "clock_started")
    assert started.get("deadline_t") == 1600.0 and clock_ev.get("deadline_t") > 1600.0 + 10 ** 6
    assert types.index("clock_started") < types.index("session_started")
    assert_t2_blocked(run, res)

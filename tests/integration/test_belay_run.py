"""端到端：LocalEnv + ScriptedLLM + 真实的 git 与 pytest。不需要容器和模型。

覆盖：首次开场（build_context）→ 认领 → 验证后 CAS 推进存档 → 证据判定完成 → 交付最近的存档；
回归被拒、测试改动不交付；会话结束不等于运行结束；L2 / L3 压缩与 L4 交接；worker 崩溃；runtime 崩溃后的对账
（CAS 前 / CAS 后 / 作业丢失）；截止时交付最近的存档；回退与开发检查。
每个场景结束后都检查：事件库重放 == 实时的图，日志满足来源纪律。
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from belay.core.config import BelayConfig
from belay.core.invariants import check, check_log
from belay.core.reduce import replay
from belay.env import LocalEnv
from belay.llm import ScriptedLLM
from belay.runtime.driver import BelayRun, RunSettings
from belay.runtime.store import EventStore
from belay.runtime.verifier import VerifierSpec

ADD, MUL = "tests/test_mod.py::test_add", "tests/test_mod.py::test_mul"
TASK = ("Fix add in pkg/mod.py so that add(1, 2) returns 3.\n"
        "Add a function sub(a, b) to pkg/mod.py that returns a minus b.")
PLAN = {"requirements": [{"id": "R1", "quote": "Fix add in pkg/mod.py so that add(1, 2) returns 3.", "summary": "fix add"},
                         {"id": "R2", "quote": "Add a function sub(a, b) to pkg/mod.py that returns a minus b.",
                          "summary": "add sub"}],
        "tasks": [{"id": "T1", "title": "Fix add", "links": ["R1"], "checks": [ADD]},
                  {"id": "T2", "title": "Add sub", "links": ["R2"]}]}
MOD = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
SPEC = VerifierSpec(test_cmd="python -m pytest -rA -p no:cacheprovider tests", timeout_sec=120)


def tu(i: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def call(*uses) -> list[dict]:
    return list(uses)


def say(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


PLANNER = say(json.dumps(PLAN))
READ = tu("r", "read_file", file_path="pkg/mod.py")
FIX_ADD = tu("fa", "edit_file", file_path="pkg/mod.py", old_string="return a - b", new_string="return a + b")
ADD_SUB = tu("as", "edit_file", file_path="pkg/mod.py", old_string="def mul(a, b):",
             new_string="def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):")
BLOCK_T2 = tu("b2", "report_blocked", task="T2", kind="insufficient_info", reason="spec unclear")


def make_repo(d: Path) -> None:
    (d / "pkg").mkdir(parents=True)
    (d / "tests").mkdir()
    (d / "pkg/__init__.py").write_text("")
    (d / "pkg/mod.py").write_text(MOD)
    (d / "tests/test_mod.py").write_text("from pkg.mod import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
                                         "\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    subprocess.run("git init -q && git add -A && git -c user.email=a@b -c user.name=t commit -qm init", shell=True,
                   cwd=d, check=True)


class Harness:
    def __init__(self, tmp: Path, cfg: BelayConfig | None = None, budget: float = 600, **settings):
        self.tmp = tmp
        self.repo = tmp / "repo"
        make_repo(self.repo)
        self.cfg = cfg or BelayConfig()
        kw = dict(run_dir=str(tmp / "run"), budget_sec=budget, git_dir=str(tmp / "state/git"),
                  jobs_dir=str(tmp / "state/jobs"), tick_sec=0.2, crash_backoff_sec=0.0)
        kw.update(settings)
        self.settings = RunSettings(**kw)
        self.logs: list[str] = []

    def make(self, llm, cls=BelayRun, aux=None) -> BelayRun:
        """aux：诊断者、复查者、标签用的模型（默认没有脚本：这些调用失败，只记为 failed，不影响 worker 的脚本）。"""
        return cls(llm, LocalEnv(str(self.repo)), self.settings, self.cfg, SPEC, aux_llm=aux or ScriptedLLM([]),
                   log=self.logs.append)

    def events(self):
        store = EventStore(self.settings.run_dir)
        try:
            return store.events()
        finally:
            store.close()

    def verify_log(self, run: BelayRun):
        events = self.events()
        g = replay(events)
        assert g == run.rt.graph, "replaying the event store must rebuild the live graph"
        assert not check(g) and not check_log(events)
        return events

    def run_dir(self) -> Path:
        return Path(self.settings.run_dir)


def results(llm: ScriptedLLM) -> list[str]:
    out = []
    for r in llm.requests:
        m = r["messages"][-1]
        if m["role"] == "user" and isinstance(m["content"], list):
            out += [str(b["content"]) for b in m["content"] if b.get("type") == "tool_result"]
    return out


def first_message(req: dict) -> str:
    c = req["messages"][0]["content"]
    return c if isinstance(c, str) else json.dumps(c)


# ======================================================================== 正常路径

def test_happy_path_done(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                       call(tu("3", "ready_for_review", task="T1")), call(tu("4", "claim", task="T2")), call(ADD_SUB),
                       call(tu("6", "ready_for_review", task="T2")), say("All tasks done.")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE" and res.checkpoint == 2
    g = run.rt.graph
    assert g.tasks["T1"].status == "done" and g.tasks["T1"].verified
    assert g.tasks["T2"].status == "done_unverified"
    assert g.baseline == {ADD: "fail", MUL: "pass"}
    opening = first_message(llm.requests[1])
    assert "<task>" in opening and "Suggested order" in opening and "- R1 fix add" in opening
    assert "Requirements: not_started 2" in opening
    out = results(llm)
    assert "T1 is DONE: all its checks passed on checkpoint 1" in out[3]
    assert "done_unverified" in out[6]
    patch = (h.run_dir() / "deliverable.diff").read_text()
    assert "+    return a + b" in patch and "+def sub(a, b):" in patch
    assert (h.run_dir() / "checkpoints/1.diff").exists() and (h.run_dir() / "ledger.md").exists()
    events = h.verify_log(run)
    assert [e.type for e in events].count("checkpoint_created") == 3        # 0、1（自动）、2（review）
    assert events[-1].type == "delivered" and events[-1].get("status") == "DONE"
    assert g.checkpoints[1].kind == "auto" and g.checkpoints[2].kind == "review"
    assert g.checkpoints[2].level == "confirmed" and events[-1].get("level") == "confirmed"
    assert (h.run_dir() / "git" / "1.bundle").exists()                     # G3：影子仓库对象导出到宿主机


# ======================================================================== 回归被拒；测试改动不交付

def test_regression_rejected_and_test_changes_are_not_delivered(tmp_path):
    h = Harness(tmp_path)
    cheat = tu("c", "write_file", file_path="tests/test_mod.py",
               content="from pkg.mod import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                       "def test_mul():\n    assert mul(2, 3) == 5\n")
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    fix_mul = tu("fm", "edit_file", file_path="pkg/mod.py", old_string="return a + b + 0", new_string="return a * b")
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ, tu("rt", "read_file", file_path="tests/test_mod.py")),
                       call(FIX_ADD), call(break_mul), call(cheat),
                       call(tu("5", "ready_for_review", task="T1")), call(fix_mul),
                       call(tu("7", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    out = results(llm)
    rejected = next(o for o in out if "Checkpoint rejected" in o)
    assert MUL in rejected and "reopened (checkpoint_rejected)" in rejected
    assert "tests/test_mod.py" in rejected                          # 说明测试改动没有进入候选
    assert res.status == "DONE"
    patch = (h.run_dir() / "deliverable.diff").read_text()
    assert "tests/test_mod.py" not in patch and "+    return a + b" in patch
    assert "tests/test_mod.py" in (h.run_dir() / "worktree.diff").read_text()
    g = run.rt.graph
    assert g.tasks["T2"].status == "blocked" and g.tasks["T1"].status == "done"
    assert "assert mul(2, 3) == 6" in (h.repo / "tests/test_mod.py").read_text()   # 交付时工作区 = 链头
    h.verify_log(run)


# ======================================================================== 会话结束 ≠ 运行结束

def test_session_end_is_not_run_end(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                       say("I think I'm done for now."),                         # 会话 1 结束，T1 还没交
                       call(tu("2", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    g = run.rt.graph
    assert [s.end_reason for s in g.sessions.values()] == ["done", "done"]
    assert g.sessions["S2"].reason == "restart"
    events = h.verify_log(run)
    # 编辑之后 runtime 自动拍快照、后台存档（worker 不需要记得存档）
    att = [e for e in events if e.type == "checkpoint_attempted"]
    assert att[0].get("trigger") == "auto" and att[0].get("lane") == "bg"
    opening2 = first_message(llm.requests[4])
    assert "continuing work in a new session" in opening2
    assert "### T1 [active] Fix add" in opening2 and "return a + b" in opening2


def test_repeated_idle_sessions_end_the_run_incomplete(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1")), say("stop"), say("stop"), say("stop")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "INCOMPLETE" and res.checkpoint == 0
    g = run.rt.graph
    assert len(g.sessions) == 2 and g.stalls[-1].kind == "sessions_no_progress"
    h.verify_log(run)


# ======================================================================== 压缩与交接

def test_l2_compaction_replaces_old_conversation_with_graph(tmp_path):
    cfg = BelayConfig(l2_tokens=5000, l4_tokens=10 ** 9, l2_keep_recent_tokens=200, l3_mode="off",
                      l1_trigger_tokens=10 ** 9)
    h = Harness(tmp_path, cfg)
    script = [PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
              call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")]
    llm = ScriptedLLM(script, context_tokens=[1000, 1000, 6000, 1000, 1000, 1000])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    req = llm.requests[3]                                                   # 压缩之后的第一次调用
    first = first_message(req)
    assert "earlier conversation in this session was replaced" in first
    assert "Recently modified files (re-read by the harness)" in first and "return a + b" in first
    assert req["messages"][1]["role"] == "assistant"
    ev = [e for e in h.verify_log(run) if e.type == "compacted"]
    assert ev and ev[0].get("level") == 2 and ev[0].get("before") >= 5000


def test_l3_summary_goes_into_the_graph(tmp_path):
    cfg = BelayConfig(l2_tokens=5000, l4_tokens=10 ** 9, l2_keep_recent_tokens=200, l3_mode="always",
                      l1_trigger_tokens=10 ** 9)
    h = Harness(tmp_path, cfg)
    script = [PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
              say("Decided to fix add in place; tried a wrapper first but callers import add directly."),
              call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")]
    llm = ScriptedLLM(script, context_tokens=[1000, 1000, 6000, 1000, 1000, 1000, 1000])
    run = h.make(llm)
    asyncio.run(run.start(TASK))
    assert llm.requests[3]["tool_choice"] == {"type": "none"}                 # L3 摘要调用不许用工具
    after = first_message(llm.requests[4])
    assert "callers import add directly" in after and "model-written summary" in after
    ev = [e for e in h.verify_log(run) if e.type == "compacted"]
    assert ev[0].get("level") == 3 and ev[0].source == "llm" and "wrapper" in ev[0].get("summary")


def test_l4_handoff_keeps_lease_notes_and_summary(tmp_path):
    cfg = BelayConfig(l2_tokens=10 ** 8, l4_tokens=5000, l1_trigger_tokens=10 ** 9)
    h = Harness(tmp_path, cfg)
    script = [PLANNER,
              call(tu("1", "claim", task="T1"), tu("n", "note", text="add() is used by mul tests too; keep signature")),
              call(READ), call(FIX_ADD),                                      # 这一步报告上下文 6000 → 交接
              say("Was about to run ready_for_review on T1."),                 # 交接摘要（L4）
              call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")]
    llm = ScriptedLLM(script, context_tokens=[1000, 1000, 1000, 6000, 1000, 1000, 1000, 1000])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    g = run.rt.graph
    assert g.sessions["S1"].end_reason == "handoff" and g.sessions["S2"].reason == "handoff"
    claims = [e for e in h.verify_log(run) if e.type == "task_claimed"]
    assert len(claims) == 1                                                  # 新会话不需要重新认领
    opening2 = first_message(llm.requests[5])
    assert "keep signature" in opening2 and "(self-reported)" in opening2
    assert "about to run ready_for_review" in opening2
    assert "### T1 [active]" in opening2 and "+    return a + b" in opening2      # 部分改动随恢复点交还


# ======================================================================== worker 崩溃

class CrashingLLM(ScriptedLLM):
    def __init__(self, script, crash_at: int, **kw):
        super().__init__(script, **kw)
        self.crash_at = crash_at
        self.n = 0

    async def call(self, system, tools, messages, tool_choice=None):
        self.n += 1
        if self.n == self.crash_at:
            raise ConnectionError("model API unreachable after retries")
        return await super().call(system, tools, messages, tool_choice)


def test_model_failure_is_retried_in_memory(tmp_path):
    """G2：模型接口多次重试仍失败，runtime 还在 → 同一个会话用原来的消息再调用一次，不开新会话。"""
    h = Harness(tmp_path)
    llm = CrashingLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                       call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")], crash_at=4)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    g = run.rt.graph
    assert len(g.sessions) == 1 and g.sessions["S1"].resumes == ("memory",)
    retried = llm.requests[3]["messages"]
    assert retried == llm.requests[2]["messages"][:len(retried)] or len(retried) >= len(llm.requests[2]["messages"])
    h.verify_log(run)


class ContextErrorLLM(CrashingLLM):
    async def call(self, system, tools, messages, tool_choice=None):
        self.n += 1
        if self.n == self.crash_at:
            e = RuntimeError("prompt is too long for the context window")
            e.status_code = 400
            raise e
        return await ScriptedLLM.call(self, system, tools, messages, tool_choice)


def test_context_problem_starts_a_new_session(tmp_path):
    h = Harness(tmp_path)
    llm = ContextErrorLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                           call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")], crash_at=4)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    g = run.rt.graph
    assert g.sessions["S1"].end_reason == "crash" and "context window" in g.sessions["S1"].error
    assert g.sessions["S2"].reason == "crash" and not g.sessions["S1"].resumes
    h.verify_log(run)


# ======================================================================== runtime 崩溃与对账

class CrashBeforeCAS(BelayRun):
    """checkpoint_advancing 已写入日志，CAS 还没做（或做完了还没记 created）时 runtime 进程死掉。"""
    cas_first = False
    main: asyncio.Task | None = None

    async def _eff_advance_ref(self, attempt: str) -> None:
        if self.cas_first:
            a = self.rt.graph.attempts[attempt]
            commit = await self.repo.commit(a.tree, a.parent_commit, self.commit_message(attempt), a.date)
            assert await self.repo.cas(commit, a.parent_commit)
        self.main.cancel()
        await asyncio.sleep(3600)


class LoseJobs(BelayRun):
    main: asyncio.Task | None = None

    async def _eff_launch_job(self, job: str) -> None:
        if self.rt.graph.jobs[job].purpose == "verify":
            self.main.cancel()                    # 作业既没启动也没有完成标记：恢复时只能记为 unknown 并重跑
            await asyncio.sleep(3600)
        await super()._eff_launch_job(job)


def _crash_then_resume(h: Harness, cls, script1, script2, **attrs):
    async def phase1():
        run = h.make(ScriptedLLM(script1), cls)
        for k, v in attrs.items():
            setattr(run, k, v)
        run.main = asyncio.create_task(run.start(TASK))
        with pytest.raises(asyncio.CancelledError):
            await run.main
        for t in list(run._bg) + list(run.rt._tasks):
            t.cancel()
        run.store.close()

    asyncio.run(phase1())
    llm2 = ScriptedLLM(script2)
    run2 = h.make(llm2)
    res = asyncio.run(run2.resume())
    return run2, res, llm2


@pytest.mark.parametrize("cas_first", [False, True])
def test_runtime_crash_around_cas_is_reconciled(tmp_path, cas_first):
    h = Harness(tmp_path)
    # runtime 在后台自动存档推进 CAS 的时候死掉；恢复后原样接上会话（读盘重放），被打断的编辑记为“效果未知”
    s1 = [PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD)]
    s2 = [call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")]
    run, res, llm2 = _crash_then_resume(h, CrashBeforeCAS, s1, s2, cas_first=cas_first)
    assert res.status == "DONE" and res.checkpoint == 1
    events = h.verify_log(run)
    types = [e.type for e in events]
    assert types.count("checkpoint_advancing") == 1 and types.count("checkpoint_created") == 2     # 0 与 1，只创建一次
    assert "runtime_recovered" in types
    g = run.rt.graph
    assert g.sessions["S1"].resumes == ("replay",) and len(g.sessions) == 1   # 同一个会话接着做
    assert g.tasks["T1"].status == "done"                                     # 恢复后证据判定照常进行
    ref = subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "rev-parse", "refs/heads/belay"],
                         capture_output=True, text=True).stdout.strip()
    assert ref == g.checkpoints[1].commit
    parents = subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "rev-list", "--count", ref],
                             capture_output=True, text=True).stdout.strip()
    assert parents == "2"                                                     # 基线 + 一个存档，没有重复合并
    first = llm2.requests[0]["messages"]                                      # 重放的对话 + 恢复点提醒
    assert first[0]["content"].startswith("You are starting work")
    tail = json.dumps(first[-1]["content"])
    assert "interrupted" in tail and "resumed with your conversation intact" in tail


def test_runtime_crash_with_a_lost_job(tmp_path):
    h = Harness(tmp_path)
    s1 = [PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD)]
    s2 = [call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")]
    run, res, _ = _crash_then_resume(h, LoseJobs, s1, s2)
    assert res.status == "DONE" and res.checkpoint == 1
    events = h.verify_log(run)
    lost = [e for e in events if e.type == "job_finished" and e.get("state") == "unknown"]
    assert len(lost) == 1
    verify_jobs = [e for e in events if e.type == "job_started" and e.get("purpose") == "verify"]
    assert len(verify_jobs) == 2 and verify_jobs[0].get("key") == verify_jobs[1].get("key")   # 同一个键重跑


# ======================================================================== 截止

def test_deadline_delivers_the_latest_checkpoint(tmp_path):
    cfg = BelayConfig(reserve_min_sec=4, reserve_max_frac=0.5, reserve_extra_sec=0)
    h = Harness(tmp_path, cfg, budget=12, stop_grace_sec=2, finalize_grace_sec=20)
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    sleeps = [call(tu(f"s{i}", "bash", command="sleep 0.5")) for i in range(60)]
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(FIX_ADD),
                       call(tu("c", "checkpoint", summary="add fixed")), call(break_mul)] + sleeps)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "INCOMPLETE" and res.checkpoint == 1
    events = h.verify_log(run)
    types = [e.type for e in events]
    assert "deadline_reserve" in types
    g = run.rt.graph
    last = sorted(g.attempts.values(), key=lambda a: a.created_seq)[-1]
    assert last.trigger == "deadline" and last.tier == "full" and last.status == "rejected"
    assert g.sessions["S1"].end_reason == "deadline"
    patch = (h.run_dir() / "deliverable.diff").read_text()
    assert "+    return a + b\n" in patch and "a + b + 0" not in patch       # 未验证的进度不交付
    assert "return a * b" in (h.repo / "pkg/mod.py").read_text()


# ======================================================================== 回退、开发检查

def test_run_check_wait_and_rollback(tmp_path):
    h = Harness(tmp_path)
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    llm = ScriptedLLM([PLANNER, call(tu("1", "claim", task="T1"), READ), call(break_mul),
                       call(tu("rc", "run_check")), call(tu("w", "wait", jobs=["J4"])),
                       call(tu("rb", "rollback")), call(READ), call(FIX_ADD),
                       call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    out = results(llm)
    assert "Started job J4" in out[3]                                       # J3 是后台自动存档的验证作业
    assert "REGRESSIONS" in out[4] and MUL in out[4]
    assert "restored to checkpoint 0" in out[5]
    assert "return a * b" in out[6] and "return a - b" in out[6]            # 回退后文件回到原样
    assert res.status == "DONE"
    h.verify_log(run)


# ======================================================================== 卡死检测、停滞 → 重新规划

class HangingLLM(ScriptedLLM):
    def __init__(self, script, hang_at: int, **kw):
        super().__init__(script, **kw)
        self.hang_at = hang_at
        self.n = 0

    async def call(self, system, tools, messages, tool_choice=None):
        self.n += 1
        if self.n == self.hang_at:
            await asyncio.sleep(3600)                  # 模型调用挂住，没有任何工具调用
        return await super().call(system, tools, messages, tool_choice)


def test_stuck_worker_is_restarted_but_long_tool_calls_are_not_stuck(tmp_path):
    h = Harness(tmp_path, BelayConfig(idle_timeout_sec=1.0))
    llm = HangingLLM([PLANNER, call(tu("1", "claim", task="T1"), READ),
                      call(tu("s", "bash", command="sleep 2")),                 # 工具调用进行中：不算卡死
                      call(READ), call(FIX_ADD),                                # 第 4 次调用挂住；新会话从这里继续
                      call(tu("3", "ready_for_review", task="T1")), call(BLOCK_T2), say("done")], hang_at=4)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    g = run.rt.graph
    assert g.sessions["S1"].end_reason == "stuck" and g.sessions["S1"].turns == 2
    assert g.sessions["S2"].reason == "restart"
    h.verify_log(run)


def test_stall_escalates_to_replan_and_split(tmp_path):
    cfg = BelayConfig(stall_no_progress_sec=1.0, stall_same_failure=3, confirm_regressions=False,
                      auto_checkpoint=False, locate=False)
    h = Harness(tmp_path, cfg)
    split = {"children": [{"title": "Fix add core", "links": ["R1"]}, {"title": "Fix add edge cases", "links": ["R1"]}]}
    planner = ScriptedLLM([PLANNER, say(json.dumps(split))])
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    fix_mul = tu("fm", "edit_file", file_path="pkg/mod.py", old_string="return a + b + 0", new_string="return a * b")
    ck = lambda i: call(tu(f"c{i}", "checkpoint"))                               # noqa: E731
    worker = ScriptedLLM([call(tu("1", "claim", task="T1"), READ), call(tu("s0", "bash", command="sleep 1.5")),
                          call(FIX_ADD), call(break_mul), ck(1), ck(2), ck(3),
                          call(tu("s1", "bash", command="sleep 1")), call(tu("s2", "bash", command="sleep 1")),
                          call(fix_mul), call(tu("a", "claim", task="T3")), call(tu("r3", "ready_for_review", task="T3")),
                          call(tu("b", "claim", task="T4")), call(tu("r4", "ready_for_review", task="T4")),
                          call(BLOCK_T2), say("done")])
    run = BelayRun(worker, LocalEnv(str(h.repo)), h.settings, cfg, SPEC, planner_llm=planner, aux_llm=ScriptedLLM([]),
                   log=h.logs.append)
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    kinds = [(s.kind, s.action) for s in g.stalls]
    assert ("no_progress", "hint") in kinds and ("repeated_failure", "replan") in kinds
    assert g.tasks["T1"].status == "split" and g.tasks["T1"].children == ("T3", "T4")
    assert res.status == "DONE"
    notices = " ".join(json.dumps(r["messages"][-1]["content"]) for r in worker.requests)
    assert "rejected for the same reason" in notices and "was split into T3, T4" in notices
    assert not re.search(r"\d+ min\b|minutes|time budget|[Tt]ime left", notices)   # 时间不进给模型的文字
    h.verify_log(run)

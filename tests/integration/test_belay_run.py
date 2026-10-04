"""端到端：LocalEnv + ScriptedLLM + 真实的 git 与 pytest。不需要容器和模型（复核者、诊断者用 fakes.FakeAux）。

覆盖：首次开场（build_context）→ 后台合并请求（回归门 + 复核者）、需求随检查项（E3）与复核者的判定记下 →
submit（请求立即复核：合并并判定、交还缺失项或接受）→ 交付合并链的链头；复核者失败时的重试与降级；
回归被拒、测试改动不交付；模型停下不调工具时先追问、再当作提交；会话结束不等于运行结束；L2 / L3 压缩与 L4 交接；
worker 崩溃；runtime 崩溃后的对账（CAS 前 / CAS 后 / 作业丢失）；截止时交付链头。
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
from belay.core.invariants import check, check_log, llm_effects
from belay.core.reduce import replay
from belay.env import LocalEnv
from belay.llm import ScriptedLLM
from belay.runtime.driver import BelayRun, RunSettings
from belay.runtime.store import EventStore
from belay.runtime.verifier import VerifierSpec
from tests.integration.fakes import FakeAux, oracle

ADD, MUL = "tests/test_mod.py::test_add", "tests/test_mod.py::test_mul"
TASK = ("# Changes\n"
        "Fix add in pkg/mod.py so that add(1, 2) returns 3.\n"
        "Add a function sub(a, b) to pkg/mod.py that returns a minus b.")
PLAN = {"requirements": [{"id": "H", "kind": "context", "quote": "# Changes", "summary": "heading"},
                         {"id": "R1", "quote": "Fix add in pkg/mod.py so that add(1, 2) returns 3.",
                          "summary": "fix add", "checks": [ADD]},
                         {"id": "R2", "quote": "Add a function sub(a, b) to pkg/mod.py that returns a minus b.",
                          "summary": "add sub"}]}
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
SUBMIT = tu("sm", "submit", summary="fixed add and added sub")
BLOCK_SUB = tu("b2", "submit", summary="fixed add; sub is unclear",
              blocked=[{"requirement": "R3", "kind": "insufficient_info", "reason": "spec unclear"}])


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

    def make(self, llm, cls=BelayRun, aux=None, spec: VerifierSpec | None = None) -> BelayRun:
        """aux：复核者与诊断者用的模型（默认 FakeAux：看复核目录里的快照下结论的 oracle）。"""
        self.aux = aux if aux is not None else FakeAux()
        return cls(llm, LocalEnv(str(self.repo)), self.settings, self.cfg, SPEC if spec is None else spec,
                   aux_llm=self.aux, log=self.logs.append)

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
        assert not check(g) and not check_log(events) and not llm_effects(events)
        return events

    def run_dir(self) -> Path:
        return Path(self.settings.run_dir)


def assert_sub_blocked(run: BelayRun, res) -> None:
    """脚本里 R3（加 sub）以 insufficient_info 受阻、R2 做完时：复核者认可受阻，运行记为 DONE；交付的是链头，
    它是复核过的合并点。"""
    g = run.rt.graph
    assert res.status == "DONE", (res.status, g.run.status_reasons)
    r3 = g.requirements["R3"]
    assert r3.status == "blocked" and r3.by == "review" and r3.blocked_kind == "insufficient_info"
    assert g.requirements["R2"].status == "done" and g.requirements["R2"].level == "E3"
    assert g.run.delivered == g.head and g.checkpoints[g.head].review is not None


def results(llm: ScriptedLLM) -> list[str]:
    out = []
    for r in llm.requests:
        m = r["messages"][-1]
        if m["role"] == "user" and isinstance(m["content"], list):
            out += [str(b["content"]) for b in m["content"] if b.get("type") == "tool_result"]
    return out


def tool_outputs(run: BelayRun, name: str) -> list[str]:
    """从会话轨迹里取某个工具的全部输出（提交被接受时会话随即结束，结果不会再发给模型）。"""
    out = []
    for sess in sorted(run.rt.graph.sessions.values(), key=lambda s: s.n):
        with open(sess.transcript, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r.get("type") == "tool_result":
                    out += [str(x["output"]) for x in r["results"] if x["name"] == name]
    return out


def first_message(req: dict) -> str:
    c = req["messages"][0]["content"]
    return c if isinstance(c, str) else json.dumps(c)


# ======================================================================== 正常路径

def test_happy_path_done(tmp_path):
    h = Harness(tmp_path)
    todos = tu("t", "todo_write", todos=[{"content": "fix add (R2)", "status": "in_progress"},
                                         {"content": "add sub (R3)", "status": "pending"}])
    llm = ScriptedLLM([PLANNER, call(READ, todos), call(FIX_ADD), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE", run.rt.graph.run.status_reasons
    g = run.rt.graph
    assert (g.requirements["R2"].status, g.requirements["R2"].level, g.requirements["R2"].by) == ("done", "E3",
                                                                                                    "checks")
    assert (g.requirements["R3"].status, g.requirements["R3"].level, g.requirements["R3"].by) == ("done", "E1",
                                                                                                    "review")
    assert g.requirements["R1"].kind == "context"
    assert g.baseline == {ADD: "fail", MUL: "pass"}
    req1 = llm.requests[1]
    tools = [t["name"] for t in req1["tools"]]
    assert "submit" in tools and "waive_check" not in tools and len(tools) == 12
    opening = first_message(req1)
    assert "<task>" in opening and "- R2 fix add" in opening and "- R1 heading" not in opening
    assert "open 2" in opening and "call submit" in opening and "reviewer" in opening
    out = tool_outputs(run, "submit")
    assert len(out) == 1 and "accepted" in out[0] and "Done (E3, tests): R2" in out[0] and "R3" in out[0]
    patch = (h.run_dir() / "deliverable.diff").read_text()
    assert "+    return a + b" in patch and "+def sub(a, b):" in patch
    events = h.verify_log(run)
    assert events[-1].type == "delivered" and events[-1].get("status") == "DONE"
    assert any(e.type == "merge_requested" and e.get("lane") == "bg" for e in events)   # 编辑后后台请求合并
    delivered = g.checkpoints[g.run.delivered]
    assert delivered.review is not None and delivered.label == "change reviewed by the oracle"
    assert g.sessions["S1"].end_reason == "submitted" and len(g.sessions) == 1
    assert (h.run_dir() / "git" / "1.bundle").exists()
    assert (h.run_dir() / "reviews" / f"{delivered.review}.jsonl").exists()      # 复核会话的轨迹
    msg = subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "log", "-1", "--format=%B",
                          "refs/belay/delivered"], capture_output=True, text=True).stdout
    assert "change reviewed by the oracle" in msg                                # 交付点有标签，提交说明来自复核者
    L = json.loads((h.run_dir() / "ledger.json").read_text())
    assert L["categories"]["done-E3"] == 1 and L["categories"]["done-E1"] == 1
    assert L["submits"][-1]["status"] == "accepted"
    opening = h.aux.openings[-1]
    assert "## Regression gate" in opening and "def sub" in opening and "Submit summary: fixed add" in opening
    import argparse
    import contextlib
    import io
    from belay.cli import _handoff
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):                                       # 随时导出交接上下文
        _handoff(argparse.Namespace(run_dir=str(h.run_dir()), worker=None))
    assert "<task>" in buf.getvalue() and "Latest merge point" in buf.getvalue() and "Done (E3): R2" in buf.getvalue()


def test_reviewer_returns_what_is_missing_then_accepts(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(SUBMIT), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    out = results(llm)
    returned = next(o for o in out if "still open" in o)
    assert "R3" in returned and "sub() is not defined" in returned and "define sub(a, b)" in returned
    g = run.rt.graph
    assert g.requirements["R3"].status == "done" and g.requirements["R3"].level == "E1"
    assert [s.status for s in g.submits.values()] == ["returned", "accepted"]
    events = h.verify_log(run)
    assert {e.type for e in events if e.source == "llm"} <= {"plan_proposed", "merge_reviewed", "diagnosis_recorded",
                                                            "compacted"}


def test_reviewer_runs_commands_and_cites_them_as_e2(tmp_path):
    def policy(opening, review_dir):
        v = oracle(opening, review_dir)
        for r in v["requirements"]:
            if r["status"] == "done":
                r.update(level="E2", runs=["X1"])
        return v
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm, aux=FakeAux(policy, run_first="python -c 'from pkg.mod import sub; print(sub(5, 3))'"))
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    g = run.rt.graph
    r3 = g.requirements["R3"]
    assert r3.level == "E2" and r3.runs == ("X1",)
    v = g.reviews[r3.review]
    assert v.runs[0]["id"] == "X1" and v.runs[0]["rc"] == 0
    tr = (h.run_dir() / "reviews" / f"{v.id}.jsonl").read_text()
    assert "[run X1] exit code 0\\n2" in tr                                # 输出写进复核会话的轨迹
    h.verify_log(run)


def test_a_reviewer_without_a_verdict_is_retried_then_the_gate_alone_decides(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm, aux=FakeAux(lambda opening, d: "I could not decide."))
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    fg = [v for v in g.reviews.values() if v.trigger == "submit"]
    assert [v.status for v in fg] == ["failed", "failed"] and fg[1].retry_of == fg[0].id
    assert g.checkpoints[g.head].review is None                                   # 复核者不可用：只按回归门合并
    assert g.requirements["R3"].level == "E0" and g.requirements["R3"].by == "self_report"
    assert res.status == "INCOMPLETE" and any("only self-reported" in x for x in g.run.status_reasons)
    assert "regression gate only" in tool_outputs(run, "submit")[-1]
    assert json.loads((h.run_dir() / "ledger.json").read_text())["category_requirements"]["self-reported"] == ["R3"]
    h.verify_log(run)


def test_without_tests_the_reviewer_runs_the_code_and_the_score_must_not_drop(tmp_path):
    """LHTB 的情形：没有测试配置（没有回归门）。复核者在复核目录里运行程序（E2）、按任务自测分数；分数下降的快照不合并。"""
    def policy(opening, review_dir):
        v = oracle(opening, review_dir)
        mod = (review_dir / "pkg" / "mod.py").read_text()
        score = (0.5 if "return a + b" in mod else 0.0) + (0.5 if "def sub(" in mod else 0.0)
        if "a + b + 0" in mod:
            score = 0.1                                                    # 改坏了 mul：分数下降
        if "return a + b\n" in mod:
            v["requirements"].append({"id": "R2", "status": "done", "level": "E2"})
        for r in v["requirements"]:
            if r["status"] == "done":
                r.update(level="E2", runs=["X1"])
        return {**v, "score": score, "score_note": "python score.py"}
    h = Harness(tmp_path, BelayConfig(merge_min_interval_sec=0))
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    fix_mul = tu("fm", "edit_file", file_path="pkg/mod.py", old_string="return a + b + 0", new_string="return a * b")
    wait = lambda i: call(tu(f"w{i}", "bash", command="sleep 1.5"))      # noqa: E731
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), wait(1), call(break_mul), wait(2), call(fix_mul),
                       call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm, aux=FakeAux(policy, run_first="python -c 'import pkg.mod'"), spec=VerifierSpec())
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    assert not g.baseline and not [j for j in g.jobs.values() if j.purpose != "baseline"]   # 没有回归门作业
    assert res.status == "DONE", g.run.status_reasons
    assert g.checkpoints[g.head].score == 1.0 and g.requirements["R3"].level == "E2"
    dropped = [a for a in g.attempts.values() if a.reason == "review" and "score dropped" in a.detail]
    assert dropped                                                          # 改坏 mul 的那一版没有合并
    scores = [c.score for c in reversed([g.checkpoints[i] for i in g.checkpoints if i]) if c.score is not None]
    assert scores == sorted(scores, reverse=True)                           # 合并链上的分数不下降
    L = json.loads((h.run_dir() / "ledger.json").read_text())
    assert L["gate_available"] is False and L["delivered_score"] == 1.0
    assert "a + b + 0" not in (h.run_dir() / "deliverable.diff").read_text()
    h.verify_log(run)


# ======================================================================== 回归被拒；测试改动不交付

def test_regression_rejected_and_test_changes_are_not_delivered(tmp_path):
    h = Harness(tmp_path, BelayConfig(locate_wait_sec=5))
    cheat = tu("c", "write_file", file_path="tests/test_mod.py",
               content="from pkg.mod import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                       "def test_mul():\n    assert mul(2, 3) == 5\n")
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    fix_mul = tu("fm", "edit_file", file_path="pkg/mod.py", old_string="return a + b + 0", new_string="return a * b")
    llm = ScriptedLLM([PLANNER, call(READ, tu("rt", "read_file", file_path="tests/test_mod.py")),
                       call(FIX_ADD), call(break_mul), call(cheat), call(SUBMIT), call(fix_mul), call(BLOCK_SUB)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    out = results(llm)
    rejected = next(o for o in out if "did not pass the regression gate" in o)
    assert MUL in rejected and "tests/test_mod.py" in rejected                # 测试改动没有进入候选
    assert_sub_blocked(run, res)
    patch = (h.run_dir() / "deliverable.diff").read_text()
    assert "tests/test_mod.py" not in patch and "+    return a + b" in patch
    assert "tests/test_mod.py" in (h.run_dir() / "worktree.diff").read_text()
    g = run.rt.graph
    rej = [a for a in g.attempts.values() if a.trigger == "submit" and a.status == "rejected"]
    assert rej and rej[0].reason == "regression" and not rej[0].reviews     # 回归门没过：不开复核
    assert "assert mul(2, 3) == 6" in (h.repo / "tests/test_mod.py").read_text()   # 交付时工作区 = 交付点
    reminders = [json.dumps(r["messages"][-1]["content"]) for r in llm.requests]
    assert sum("Keeping a todo list" in x for x in reminders) == 1       # 第一次改文件、还没有 todo：提醒一次
    h.verify_log(run)


# ======================================================================== 跑完测试：提醒“做完了就勾掉”

def test_running_tests_with_an_item_in_progress_nudges_to_tick_it(tmp_path):
    h = Harness(tmp_path)
    todos = tu("t", "todo_write", todos=[{"content": "fix add (R2)", "status": "in_progress"},
                                         {"content": "add sub (R3)", "status": "pending"}])
    pytest_ = tu("pt", "bash", command="python -m pytest -q -p no:cacheprovider tests")
    tick = tu("t2", "todo_write", todos=[{"content": "fix add (R2)", "status": "completed"},
                                         {"content": "add sub (R3)", "status": "in_progress"}])
    llm = ScriptedLLM([PLANNER, call(todos), call(READ), call(tu("rt", "read_file", file_path="tests/test_mod.py")),
                       call(FIX_ADD), call(pytest_), call(tick), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE", run.rt.graph.run.status_reasons
    last = [json.dumps(r["messages"][-1]["content"]) for r in llm.requests]
    hits = [i for i, x in enumerate(last) if "You just ran tests" in x]
    assert len(hits) == 1 and '"tool_use_id": "pt"' in last[hits[0]]       # 只在跑完测试的那一轮之后
    assert "fix add (R2)" in last[hits[0]] and "reviews and merges" in last[hits[0]]
    g = run.rt.graph
    p = next(t for t in g.todos.values() if t.title == "fix add (R2)")
    assert p.status == "anchored" and p.checkpoint is not None              # 提醒之后勾掉的条目进了合并链
    h.verify_log(run)


# ======================================================================== 停下不调用工具：先追问，再当作提交

def test_stopping_is_nudged_then_treated_as_a_submit(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB), say("All done."), say("Yes, all done.")])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    nudge = json.dumps(llm.requests[5]["messages"][-1]["content"])
    assert "call submit" in nudge
    g = run.rt.graph
    sub = next(iter(g.submits.values()))
    assert sub.implicit and sub.status == "accepted" and sub.summary == "Yes, all done."
    assert g.sessions["S1"].end_reason == "submitted"
    h.verify_log(run)


def test_an_implicit_submit_that_is_returned_continues_the_session(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(READ), say("done"), say("done"), call(FIX_ADD), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "DONE"
    reply = json.dumps(llm.requests[4]["messages"][-1]["content"])
    assert "treated that as a submit" in reply and "R2" in reply and ADD in reply   # 检查没过：交还
    assert "sub() is not defined" in reply                                      # 只判定的复核：R3 没做
    g = run.rt.graph
    assert [s.implicit for s in g.submits.values()] == [True, False] and len(g.sessions) == 1
    first = next(iter(g.submits.values()))
    assert first.attempt is None and g.reviews[first.review].trigger == "judge"
    h.verify_log(run)


# ======================================================================== 会话结束 ≠ 运行结束

def test_session_end_is_not_run_end(tmp_path):
    h = Harness(tmp_path, BelayConfig(nudge_on_stop=False))
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD),
                       say("I think I'm done for now."),                         # 会话 1 结束，没有提交
                       call(BLOCK_SUB)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert_sub_blocked(run, res)
    g = run.rt.graph
    assert [s.end_reason for s in g.sessions.values()] == ["done", "submitted"]
    assert g.sessions["S2"].reason == "restart"
    h.verify_log(run)
    opening2 = first_message(llm.requests[4])
    assert "continuing work in a new session" in opening2
    assert "return a + b" in opening2 and "Files you were changing" in opening2


def test_repeated_idle_sessions_end_the_run_incomplete(tmp_path):
    h = Harness(tmp_path, BelayConfig(nudge_on_stop=False))
    llm = ScriptedLLM([PLANNER, say("stop"), say("stop"), say("stop")])
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
    script = [PLANNER, call(READ), call(FIX_ADD), call(tu("w", "bash", command="true")), call(BLOCK_SUB)]
    llm = ScriptedLLM(script, context_tokens=[1000, 1000, 6000, 1000, 1000])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert_sub_blocked(run, res)
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
    script = [PLANNER, call(READ), call(FIX_ADD),
              say("Decided to fix add in place; tried a wrapper first but callers import add directly."),
              call(tu("w", "bash", command="true")), call(BLOCK_SUB)]
    llm = ScriptedLLM(script, context_tokens=[1000, 1000, 6000, 1000, 1000, 1000])
    run = h.make(llm)
    asyncio.run(run.start(TASK))
    assert llm.requests[3]["tool_choice"] == {"type": "none"}                 # L3 摘要调用不许用工具
    after = first_message(llm.requests[4])
    assert "callers import add directly" in after and "model-written summary" in after
    ev = [e for e in h.verify_log(run) if e.type == "compacted"]
    assert ev[0].get("level") == 3 and ev[0].source == "llm" and "wrapper" in ev[0].get("summary")


def test_l4_handoff_keeps_todos_summary_and_partial_changes(tmp_path):
    cfg = BelayConfig(l2_tokens=10 ** 8, l4_tokens=5000, l1_trigger_tokens=10 ** 9)
    h = Harness(tmp_path, cfg)
    todos = tu("t", "todo_write", todos=[{"content": "fix add in pkg/mod.py", "status": "in_progress"},
                                         {"content": "add sub", "status": "pending"}])
    script = [PLANNER, call(todos), call(READ), call(FIX_ADD),                  # 这一步报告上下文 6000 → 交接
              say("Was about to run the tests; add() is used by mul tests too, keep its signature."),   # 交接摘要
              call(BLOCK_SUB)]
    llm = ScriptedLLM(script, context_tokens=[1000, 1000, 1000, 6000, 1000, 1000])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert_sub_blocked(run, res)
    g = run.rt.graph
    assert g.sessions["S1"].end_reason == "handoff" and g.sessions["S2"].reason == "handoff"
    opening2 = first_message(llm.requests[5])
    assert "keep its signature" in opening2 and "model-written" in opening2
    assert "[~] fix add in pkg/mod.py" in opening2 and "[ ] add sub" in opening2
    assert "+    return a + b" in opening2 or "Done (E3): R2" in opening2
    assert "Files you were changing (re-read by the harness)" in opening2
    h.verify_log(run)


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
    llm = CrashingLLM([PLANNER, call(READ), call(FIX_ADD), call(BLOCK_SUB)], crash_at=4)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert_sub_blocked(run, res)
    g = run.rt.graph
    assert len(g.sessions) == 1 and g.sessions["S1"].resumes == ("memory",)
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
    llm = ContextErrorLLM([PLANNER, call(READ), call(FIX_ADD), call(BLOCK_SUB)], crash_at=4)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert_sub_blocked(run, res)
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
        if self.rt.graph.jobs[job].purpose == "gate":
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


SLEEPS = [call(tu(f"z{i}", "bash", command="sleep 1")) for i in range(10)]


@pytest.mark.parametrize("cas_first", [False, True])
def test_runtime_crash_around_cas_is_reconciled(tmp_path, cas_first):
    h = Harness(tmp_path)
    # runtime 在后台验证最新快照、推进 CAS 的时候死掉；恢复后原样接上会话（读盘重放），被打断的调用记为“效果未知”
    s1 = [PLANNER, call(READ), call(FIX_ADD)] + SLEEPS
    s2 = [call(BLOCK_SUB)]
    run, res, llm2 = _crash_then_resume(h, CrashBeforeCAS, s1, s2, cas_first=cas_first)
    assert_sub_blocked(run, res)
    assert res.checkpoint == 1
    events = h.verify_log(run)
    types = [e.type for e in events]
    assert types.count("merge_advancing") == 1 and types.count("merged") == 2     # 0 与 1，只创建一次
    assert "runtime_recovered" in types
    g = run.rt.graph
    assert g.sessions["S1"].resumes == ("replay",) and len(g.sessions) == 1   # 同一个会话接着做
    assert g.requirements["R2"].level == "E3"                                 # 恢复后需求照常判定
    ref = subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "rev-parse", "refs/heads/belay"],
                         capture_output=True, text=True).stdout.strip()
    assert ref == g.checkpoints[1].commit
    parents = subprocess.run(["git", f"--git-dir={h.settings.git_dir}", "rev-list", "--count", ref],
                             capture_output=True, text=True).stdout.strip()
    assert parents == "2"                                                     # 基线 + 一个合并点，没有重复合并
    first = llm2.requests[0]["messages"]                                      # 重放的对话 + 恢复点提醒
    assert first[0]["content"].startswith("You are starting work")
    tail = json.dumps(first[-1]["content"])
    assert "resumed with your conversation intact" in tail


def test_runtime_crash_with_a_lost_job(tmp_path):
    h = Harness(tmp_path)
    s1 = [PLANNER, call(READ), call(FIX_ADD)] + SLEEPS
    s2 = [call(BLOCK_SUB)]
    run, res, _ = _crash_then_resume(h, LoseJobs, s1, s2)
    assert_sub_blocked(run, res)
    assert res.checkpoint == 1
    events = h.verify_log(run)
    lost = [e for e in events if e.type == "job_finished" and e.get("state") == "unknown"]
    assert len(lost) == 1
    verify_jobs = [e for e in events if e.type == "job_started" and e.get("purpose") == "gate"]
    assert len(verify_jobs) == 2 and verify_jobs[0].get("key") == verify_jobs[1].get("key")   # 同一个键重跑


# ======================================================================== 截止

def test_deadline_delivers_the_head_of_the_merge_chain(tmp_path):
    cfg = BelayConfig(reserve_min_sec=4, reserve_max_frac=0.5, reserve_extra_sec=0)
    h = Harness(tmp_path, cfg, budget=14, stop_grace_sec=2, finalize_grace_sec=20)
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    sleeps = [call(tu(f"s{i}", "bash", command="sleep 0.5")) for i in range(60)]
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(tu("w", "bash", command="sleep 2")),
                       call(break_mul)] + sleeps)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert res.status == "INCOMPLETE" and res.checkpoint == 1               # 没有提交，但后台合并的照常交付
    events = h.verify_log(run)
    types = [e.type for e in events]
    assert "deadline_reserve" in types
    g = run.rt.graph
    last = sorted(g.attempts.values(), key=lambda a: a.created_seq)[-1]
    assert last.trigger == "deadline" and last.selection is None and last.status == "rejected"
    assert g.sessions["S1"].end_reason == "deadline"
    assert g.requirements["R2"].level == "E3"                                # 没人声明，检查项照样判定
    patch = (h.run_dir() / "deliverable.diff").read_text()
    assert "+    return a + b\n" in patch and "a + b + 0" not in patch       # 未验证的进度不交付
    assert "return a * b" in (h.repo / "pkg/mod.py").read_text()


# ======================================================================== board

def test_board_shows_the_checklist(tmp_path):
    h = Harness(tmp_path, BelayConfig(background="off"))
    llm = ScriptedLLM([PLANNER, call(tu("b", "board")), call(tu("b2", "board", requirement="R2")), call(BLOCK_SUB),
                       call(READ), call(FIX_ADD), call(BLOCK_SUB)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    out = results(llm)
    assert "Requirements: open 2" in out[0] and "R2 [open] fix add" in out[0]
    assert "Task text:" in out[1] and ADD in out[1]
    assert "still open" in out[2] and ADD in out[2]
    assert_sub_blocked(run, res)


# ======================================================================== 卡死检测、反复被拒的提示

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
    llm = HangingLLM([PLANNER, call(READ),
                      call(tu("s", "bash", command="sleep 2")),                 # 工具调用进行中：不算卡死
                      call(READ), call(FIX_ADD),                                # 第 4 次调用挂住；新会话从这里继续
                      call(BLOCK_SUB)], hang_at=4)
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    assert_sub_blocked(run, res)
    g = run.rt.graph
    assert g.sessions["S1"].end_reason == "stuck" and g.sessions["S1"].turns == 2
    assert g.sessions["S2"].reason == "restart"
    h.verify_log(run)


def test_repeated_rejected_submits_get_a_hint_without_time(tmp_path):
    cfg = BelayConfig(stall_no_progress_sec=1.0, stall_same_failure=3, confirm_regressions=False, locate=False)
    h = Harness(tmp_path, cfg)
    break_mul = tu("bm", "edit_file", file_path="pkg/mod.py", old_string="return a * b", new_string="return a + b + 0")
    fix_mul = tu("fm", "edit_file", file_path="pkg/mod.py", old_string="return a + b + 0", new_string="return a * b")
    sub = lambda i: call(tu(f"u{i}", "submit", summary="try"))                   # noqa: E731
    llm = ScriptedLLM([PLANNER, call(READ), call(tu("s0", "bash", command="sleep 1.5")), call(FIX_ADD),
                       call(break_mul), sub(1), sub(2), sub(3), call(tu("s1", "bash", command="sleep 1")),
                       call(fix_mul), call(BLOCK_SUB)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    kinds = [(s.kind, s.action) for s in g.stalls]
    assert ("no_progress", "hint") in kinds and ("repeated_failure", "hint") in kinds
    assert_sub_blocked(run, res)
    notices = " ".join(json.dumps(r["messages"][-1]["content"]) for r in llm.requests)
    assert "rejected for the same reason" in notices
    assert not re.search(r"\d+ min\b|minutes|time budget|[Tt]ime left", notices)   # 时间不进给模型的文字
    h.verify_log(run)


# ======================================================================== 改进阶段（见 test_polish_run.py，它用到下面两个）

SUB_DOC = tu("sd", "edit_file", file_path="pkg/mod.py", old_string="def sub(a, b):\n    return a - b",
             new_string="def sub(a, b):\n    \"\"\"Return a minus b.\"\"\"\n    return a - b")
SUBMIT2 = tu("sm2", "submit", summary="documented sub")


def test_finalize_mode_reviewer_sees_no_improvement_fields(tmp_path):
    h = Harness(tmp_path)
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm)
    asyncio.run(run.start(TASK))
    verdict_tool = next(t for t in h.aux.requests[-1]["tools"] if t["name"] == "verdict")
    assert "new_improvements" not in verdict_tool["input_schema"]["properties"]
    assert all("## Improvements" not in o for o in h.aux.openings)

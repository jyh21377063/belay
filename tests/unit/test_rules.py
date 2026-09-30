"""状态转换规则（纯函数，经由模拟器驱动；每个事务之后都检查不变量）。"""
from __future__ import annotations

import pytest

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import build_context
from belay.core.invariants import check_log
from belay.core.model import ACTIVE, BLOCKED, DONE, DONE_UNVERIFIED, OPEN
from belay.core.rules import Rejected
from belay.core.suggest import suggest
from tests.sim import Sim

ADD, MUL, Z = "tests/test_mod.py::test_add", "tests/test_mod.py::test_mul", "tests/test_other.py::test_z"
BASE = {ADD: "FAILED", MUL: "PASSED", Z: "PASSED"}
TASK = ("Fix the add function so that it returns the sum.\n"
        "Also make mul handle negative numbers correctly.\n"
        "Document the new behaviour in the module docstring please.")
PLAN = {"requirements": [{"id": "a", "quote": "Fix the add function so that it returns the sum.", "summary": "add"},
                         {"id": "b", "quote": "Also make mul handle negative numbers correctly.", "summary": "mul"},
                         {"id": "c", "quote": "Document the new behaviour in the module docstring please.",
                          "summary": "docs"}],
        "tasks": [{"id": "x", "title": "fix add", "links": ["a"], "checks": [ADD]},
                  {"id": "y", "title": "fix mul", "links": ["b"], "blocked_by": ["x"]},
                  {"id": "z", "title": "docs", "links": ["c"]}]}


def sim(**kw) -> Sim:
    s = Sim(BASE, **kw)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


# ======================================================================== 准备

def test_setup_baseline_and_plan():
    s = sim()
    g = s.g
    assert g.baseline == {ADD: "fail", MUL: "pass", Z: "pass"}
    assert g.frozen and set(g.requirements) == {"R1", "R2", "R3"}
    assert g.tasks["T1"].checks == (ADD,) and g.tasks["T2"].blocked_by == ("T1",)
    assert g.head == 0 and g.checkpoints[0].tree == "t0"


# ======================================================================== 认领与租约

def test_claim_rules():
    s = sim()
    with pytest.raises(Rejected, match="blocked by unfinished"):
        s.do(R.claim, "w1", "T2")
    assert s.do(R.claim, "w1", "T1") == "claimed"
    assert s.do(R.claim, "w1", "T1") == "already"                       # 幂等，不写事件
    assert s.g.leases["T1"].worker == "w1" and s.g.tasks["T1"].status == ACTIVE
    claimed = [e for e in s.log if e.type == "task_claimed"][-1]
    assert claimed.get("suggested_rank") == 1                           # 记录是否按建议认领
    s.do(R.release, "w1", "T1", "not now")
    assert s.g.tasks["T1"].status == OPEN and "T1" not in s.g.leases
    assert s.g.notes[-1].text.startswith("[released T1]")
    with pytest.raises(Rejected):
        s.do(R.release, "w1", "T1")


def test_lease_renewal_is_rate_limited_and_expiry_needs_no_session():
    cfg = BelayConfig(lease_ttl_sec=100)
    s = sim(cfg=cfg)
    s.do(R.claim, "w1", "T1")
    n = len(s.log)
    s.advance(10)
    s.do(R.heartbeat, "w1")
    assert len(s.log) == n                                               # 剩余 90 > 50：不写
    s.advance(45)
    s.do(R.heartbeat, "w1")
    assert s.log[-1].type == "lease_renewed" and s.g.leases["T1"].expires_t == s.now + 100
    s.advance(500)
    s.do(R.tick)
    assert "T1" in s.g.leases                                            # 会话还在：不过期
    s.do(R.end_session, "w1", "crash")
    s.do(R.tick)
    assert "T1" not in s.g.leases and s.g.tasks["T1"].status == OPEN
    s.do(R.start_session, "w1", "restart", {})


def test_session_start_renews_leases_for_handoff_to_self():
    cfg = BelayConfig(lease_ttl_sec=100)
    s = sim(cfg=cfg)
    s.do(R.claim, "w1", "T1")
    s.do(R.end_session, "w1", "handoff")
    s.advance(99)
    s.do(R.start_session, "w1", R.session_reason(s.g, "w1"), {})
    assert s.g.sessions["S2"].reason == "handoff"
    assert s.g.leases["T1"].expires_t == s.now + 100


# ======================================================================== 待验证 → 完成

def test_review_verified_done_then_unverified_done():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {ADD: "PASSED"})
    aid = s.do(R.request_review, "w1", "T1", s.obs("t1"))
    g = s.g
    assert g.attempts[aid].status == "created" and g.head == 1
    assert g.tasks["T1"].status == DONE and g.tasks["T1"].done_checkpoint == 1 and g.tasks["T1"].verified
    assert "T1" not in g.leases
    s.do(R.claim, "w1", "T2")
    s.world.define("t2", {ADD: "PASSED"})
    s.do(R.request_review, "w1", "T2", s.obs("t2"))
    assert s.g.tasks["T2"].status == DONE_UNVERIFIED and s.g.tasks["T2"].done_checkpoint == 2
    assert s.g.sessions["S1"].progress
    s.check_log()


def test_regression_rejects_checkpoint_and_reopens_task():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.world.define("bad", {ADD: "PASSED", MUL: "FAILED"})
    aid = s.do(R.request_review, "w1", "T1", s.obs("bad"))
    g = s.g
    a = g.attempts[aid]
    assert a.status == "rejected" and a.regressions == (f"{MUL} (FAILED)",)
    assert g.head == 0                                                   # 链不动
    t = g.tasks["T1"]
    assert t.status == ACTIVE and g.leases["T1"].worker == "w1"          # 重开，仍归原 worker
    assert t.reopen_reason == "checkpoint_rejected" and MUL in t.last_failure[0]
    assert g.wips["w1"].last_rejection["regressions"] == [f"{MUL} (FAILED)"]
    # 确认重跑（confirm）确实跑了，且标签不同于第一次验证
    assert [j.purpose for j in g.jobs.values()].count("confirm") == 1
    # 被重开的任务在建议里排第一
    s.do(R.release, "w1", "T1")
    assert suggest(s.g, "w1", s.now, s.cfg)[0].task == "T1"


def test_missing_or_skipped_guard_tests_are_regressions():
    s = sim()
    s.do(R.claim, "w1", "T3")
    s.world.define("skip", {MUL: "SKIPPED"})
    aid = s.do(R.request_checkpoint, "w1", s.obs("skip", files=[("pkg/mod.py", 1, 0)]), "worker")
    assert s.g.attempts[aid].status == "rejected"
    s.world.define("gone", {})
    s.world.trees["gone"] = {ADD: "FAILED", Z: "PASSED"}                 # MUL 漏跑
    aid = s.do(R.request_checkpoint, "w1", s.obs("gone", files=[("setup.py", 1, 0)]), "worker")
    assert s.g.attempts[aid].tier == "full"                              # setup.py → 全量
    assert s.g.attempts[aid].regressions == (f"{MUL} (MISSING)",)


def test_flaky_failure_confirmed_as_flaky_is_not_a_regression():
    s = sim()
    s.do(R.claim, "w1", "T3")
    s.world.define("t1", {})
    s.world.flaky_once.add(("t1", MUL))
    aid = s.do(R.request_checkpoint, "w1", s.obs("t1"), "worker")
    a = s.g.attempts[aid]
    assert a.status == "created" and a.flaky == (MUL,)


def test_evidence_failure_reopens_task():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {})                                            # ADD 仍失败，但没有回归
    s.do(R.request_review, "w1", "T1", s.obs("t1"))
    t = s.g.tasks["T1"]
    assert s.g.head == 1                                                 # 存档照样前进（没有回归）
    assert t.status == ACTIVE and t.reopen_reason == "evidence_failed" and ADD in t.last_failure[0]


def test_review_on_unchanged_tree_uses_head_and_runs_evidence():
    s = sim(auto_jobs=False)
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {ADD: "PASSED"})
    aid = s.do(R.request_checkpoint, "w1", s.obs("t1"), "worker")
    s.finish_job(s.g.attempts[aid].jobs[0])                              # related 验证：tests/test_mod.py
    assert s.g.head == 1
    assert s.do(R.request_review, "w1", "T1", s.obs("t1")) is None       # 树没变：直接在链头上判定
    assert s.g.tasks["T1"].status == DONE                                # 同一棵树上已有 ADD 的结果，不用再跑


def test_job_dedupe_and_unknown_job_rerun():
    s = sim(auto_jobs=False)
    j1 = s.do(R.ensure_job, "tx", ("tests/test_mod.py",), "evidence")
    j2 = s.do(R.ensure_job, "tx", ("tests/test_mod.py",), "evidence")
    assert j1 == j2
    s.finish_job(j1, state="unknown")
    running = [j for j in s.g.jobs.values() if j.state == "running" and j.tree == "tx"]
    assert len(running) == 1 and running[0].id != j1                     # 丢失的作业按同样的键重跑


def test_add_task_and_split_rules():
    s = sim()
    s.do(R.claim, "w1", "T1")
    with pytest.raises(Rejected, match="Unknown requirement"):
        s.do(R.add_task, "w1", "t", ["R9"])
    with pytest.raises(Rejected, match="Unknown check"):
        s.do(R.add_task, "w1", "t", ["R1"], checks=["tests/new_test.py::test_x"])
    tid = s.do(R.add_task, "w1", "refactor helper", ["R1"], blocked_by=["T1"])
    t = s.g.tasks[tid]
    assert t.origin == "self_report" and t.discovered_from == "T1"
    with pytest.raises(Rejected, match="do not cover"):
        s.do(R.split_task, "T3", [{"title": "a", "links": []}, {"title": "b", "links": []}])
    kids = s.do(R.split_task, "T3", [{"title": "a", "links": ["R3"]}, {"title": "b", "links": ["R3"]}])
    assert s.g.tasks["T3"].status == "split" and all(s.g.tasks[k].parent == "T3" for k in kids)


def test_report_blocked_and_conflict_requires_verbatim_quote():
    s = sim()
    s.do(R.claim, "w1", "T1")
    with pytest.raises(Rejected, match="verbatim"):
        s.do(R.report_blocked, "w1", "T1", "check_conflict", "test_mul contradicts", quote="mul must be negative")
    s.do(R.report_blocked, "w1", "T1", "check_conflict", "old test contradicts",
         quote="Fix the add function so that it returns the sum.")
    t = s.g.tasks["T1"]
    assert t.status == BLOCKED and "T1" not in s.g.leases and t.blocked_quote
    assert [e for e in s.log if e.type == "task_blocked"][0].source == "self_report"
    # 受阻的任务可以重新认领（先 reopen）
    s.do(R.claim, "w1", "T1")
    assert s.g.tasks["T1"].status == ACTIVE


def test_rollback_reopens_tasks_on_abandoned_checkpoints():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {ADD: "PASSED"})
    s.do(R.request_review, "w1", "T1", s.obs("t1"))
    s.do(R.claim, "w1", "T3")
    s.world.define("t2", {ADD: "PASSED"})
    s.do(R.request_checkpoint, "w1", s.obs("t2"), "worker")
    assert s.g.head == 2
    with pytest.raises(Rejected):
        s.do(R.rollback, "w1", 7)
    s.do(R.rollback, "w1", 0)
    g = s.g
    assert g.head == 0 and g.checkpoints[1].abandoned and g.checkpoints[2].abandoned
    assert g.tasks["T1"].status == OPEN and g.tasks["T1"].reopen_reason == "rolled_back"
    assert any(e.kind == "restore_workspace" and e.args["reset_ref"] for e in s.effects)


# ======================================================================== 时间、停滞、运行的结束

def test_deadline_reserve_and_reserve_suggestions():
    cfg = BelayConfig(reserve_min_sec=300)
    s = sim(cfg=cfg)
    s.advance(5400 - 299)
    s.do(R.tick)
    assert s.g.run.reserve and any(e.type == "deadline_reserve" for e in s.log)
    assert any(e.kind == "stop_workers" for e in s.effects)
    assert suggest(s.g, "w1", s.now, cfg) == []                          # 预留期不给建议，由 runtime 收尾
    assert R.next_step(s.g, "w1", s.now, cfg) == ("finalize", "deadline")


def test_stall_hint_then_replan_and_repeated_failure():
    cfg = BelayConfig(stall_no_progress_sec=600, reserve_min_sec=10)
    s = sim(cfg=cfg)
    s.do(R.claim, "w1", "T1")
    s.advance(601)
    s.do(R.tick)
    assert s.g.stalls[-1].kind == "no_progress" and s.g.stalls[-1].action == "hint"
    for i in range(3):
        s.world.define(f"bad{i}", {MUL: "FAILED"})
        s.do(R.request_checkpoint, "w1", s.obs(f"bad{i}"), "worker")
    s.do(R.tick)
    st = s.g.stalls[-1]
    assert st.kind == "repeated_failure" and st.action == "replan" and st.task == "T1"
    assert any(e.kind == "replan" and e.args["task"] == "T1" for e in s.effects)
    n = len(s.g.stalls)
    s.do(R.tick)
    assert len(s.g.stalls) == n                                          # 同一种停滞在进展之前只记一次


def test_session_end_is_not_run_end():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.do(R.end_session, "w1", "done")                                    # worker 想停，但还有活
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("start_session", "restart")
    s.do(R.start_session, "w1", "restart", {})
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "no_progress")   # 连续两个会话没有新证据


def test_run_done_requires_coverage_and_full_verification():
    s = sim()
    for tid, tree, over in (("T1", "t1", {ADD: "PASSED"}), ("T2", "t2", {ADD: "PASSED"})):
        s.do(R.claim, "w1", tid)
        s.world.define(tree, over)
        s.do(R.request_review, "w1", tid, s.obs(tree))
    s.do(R.claim, "w1", "T3")
    s.do(R.report_blocked, "w1", "T3", "insufficient_info", "which docstring?")
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    assert R.final_status(s.g) == "INCOMPLETE"                           # 链头还没有全量验证
    assert s.do(R.verify_head) is True                                    # 起了全量作业（模拟器里立即完成）
    assert s.do(R.verify_head) is False
    assert R.final_status(s.g) == "DONE"
    assert s.do(R.deliver, "complete") == "DONE" and s.g.run.delivered == 2
    s.check_log()


def test_crash_restarts_are_bounded():
    s = sim()
    s.do(R.claim, "w1", "T1")
    for _ in range(2):
        s.do(R.end_session, "w1", "crash")
        s.do(R.start_session, "w1", R.session_reason(s.g, "w1"), {})
    s.do(R.end_session, "w1", "crash")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "crashes")


def test_source_discipline_in_log():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {ADD: "PASSED"})
    s.do(R.request_review, "w1", "T1", s.obs("t1"))
    assert not check_log(s.log)
    assert all(e.source in ("rule", "observed") for e in s.log if e.type in ("task_done", "checkpoint_created"))
    forged = list(s.log) + [s.log[-1].__class__(len(s.log) + 1, 0, "task_done", "worker:w1", "self_report", {})]
    assert check_log(forged)


def test_build_context_after_rejection_mentions_facts_and_notes():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.do(R.note, "w1", "tried patching add via operator overloading; dead end")
    s.world.define("bad", {ADD: "PASSED", MUL: "FAILED"})
    s.do(R.request_review, "w1", "T1", s.obs("bad"))
    s.do(R.record_compaction, "w1", 3, 1000, 200, "Decided to change add() in place because callers rely on it.")
    s.do(R.end_session, "w1", "handoff")
    ctx = build_context(s.g, "w1", 50_000, s.now, s.cfg, mode="resume")
    text = ctx.text
    for needle in ("<task>", "R1 [in_progress]", "### T1 [active] fix add", "was rejected (regression)",
                   f"{MUL} (FAILED)", "already fail on the original code", "operator overloading",
                   "your own earlier notes", "Decided to change add()", "model-written summary", "Suggested order"):
        assert needle in text, needle
    assert [k for k, _ in ctx.sections][:3] == ["task", "requirements", "my_tasks"]

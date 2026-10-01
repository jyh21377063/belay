"""状态转换规则（纯函数，经由模拟器驱动；每个事务之后都检查不变量）。"""
from __future__ import annotations

import pytest

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import build_context
from belay.core.invariants import check_log, llm_effects
from belay.core.model import ACTIVE, BLOCKED, DONE, DONE_UNVERIFIED, OPEN
from belay.core.queries import delivery_checkpoint, resume_point, suspect
from belay.core.render import ledger, render_attempt
from belay.core.rules import Rejected
from belay.core.suggest import suggest
from belay.core.verify import related_units
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
MOD = [("pkg/mod.py", 1, 1)]
OTHER = [("pkg/other.py", 1, 1)]


def sim(**kw) -> Sim:
    s = Sim(BASE, **kw)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def manual(**kw) -> BelayConfig:
    """关掉后台自动存档，只看前台规则。"""
    return BelayConfig(auto_checkpoint=False, **kw)


# ======================================================================== 准备

def test_setup_baseline_and_plan():
    s = sim()
    g = s.g
    assert g.baseline == {ADD: "fail", MUL: "pass", Z: "pass"}
    assert g.frozen and set(g.requirements) == {"R1", "R2", "R3"}
    assert g.tasks["T1"].checks == (ADD,) and g.tasks["T2"].blocked_by == ("T1",)
    assert g.head == 0 and g.checkpoints[0].tree == "t0" and g.confirmed == 0
    assert not g.degraded and g.isolation["valid"] is True


# ======================================================================== 认领：当前焦点，依赖只是排序提示

def test_claim_is_focus_and_dependencies_are_only_hints():
    s = sim()
    assert s.do(R.claim, "w1", "T2") == "claimed"                       # 依赖未完成也能认领
    assert "T1" in R.claim_hint(s.g, "T2")
    assert s.g.tasks["T2"].claimed_head == 0 and s.g.leases["T2"].head == 0
    assert s.do(R.claim, "w1", "T2") == "already"
    s.do(R.release, "w1", "T2", "not now")
    assert s.g.tasks["T2"].status == OPEN and "T2" not in s.g.leases
    assert s.g.notes[-1].text.startswith("[released T2]") and s.g.notes[-1].task == "T2"
    order = [x.task for x in suggest(s.g, "w1", s.now, s.cfg)]
    assert order.index("T1") < order.index("T2")                        # 依赖未完成的排后
    assert "after T1" in suggest(s.g, "w1", s.now, s.cfg)[order.index("T2")].reason
    with pytest.raises(Rejected):
        s.do(R.release, "w1", "T2")


def test_no_lease_expiry_events_exist():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.do(R.end_session, "w1", "crash")
    s.advance(10 ** 6)
    s.do(R.tick)
    assert s.g.leases["T1"].worker == "w1"                               # 单 worker：持有没有时效
    assert not any(e.type.startswith("lease") for e in s.log)


# ======================================================================== 前台存档与 review

def test_review_verified_done_then_unverified_done():
    s = sim(cfg=manual())
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {ADD: "PASSED"})
    aid = s.review("T1", "t1")
    g = s.g
    assert g.attempts[aid].status == "created" and g.head == 1 and g.checkpoints[1].kind == "review"
    assert g.tasks["T1"].status == DONE and g.tasks["T1"].done_checkpoint == 1 and g.tasks["T1"].verified
    assert "T1" not in g.leases
    s.do(R.claim, "w1", "T2")
    s.world.define("t2", {ADD: "PASSED"})
    s.review("T2", "t2")
    assert s.g.tasks["T2"].status == DONE_UNVERIFIED and s.g.tasks["T2"].done_checkpoint == 2
    assert s.g.tasks["T2"].review == "running"                           # 没有检查项：进入复查
    assert s.g.sessions["S1"].progress
    s.check_log()


def test_regression_rejects_checkpoint_reopens_task_and_carries_reasons():
    s = sim(cfg=manual(locate=False))
    s.do(R.claim, "w1", "T1")
    s.world.define("bad", {ADD: "PASSED", MUL: "FAILED"})
    aid = s.review("T1", "bad", dropped=("tests/test_mod.py",))
    g = s.g
    a = g.attempts[aid]
    assert a.status == "rejected" and a.regressions == (f"{MUL} (FAILED)",)
    assert g.head == 0
    t = g.tasks["T1"]
    assert t.status == ACTIVE and g.leases["T1"].worker == "w1"
    assert t.reopen_reason == "checkpoint_rejected" and MUL in t.last_failure[0]
    assert g.wips["w1"].last_rejection["regressions"] == [f"{MUL} (FAILED)"]
    assert [j.purpose for j in g.jobs.values()].count("confirm") == 1
    text = render_attempt(g, aid, "T1")
    assert f"assert failure in {MUL}" in text                            # D1：失败原因贯通到拒绝消息
    assert "ran in their original version" in text and "tests/test_mod.py" in text   # D2：测试改动提示
    s.do(R.release, "w1", "T1")
    assert suggest(s.g, "w1", s.now, s.cfg)[0].task == "T1"


def test_missing_or_skipped_guard_tests_are_regressions():
    s = sim(cfg=manual())
    s.do(R.claim, "w1", "T3")
    s.world.define("skip", {MUL: "SKIPPED"})
    aid = s.checkpoint("skip", files=[("pkg/mod.py", 1, 0)])
    assert s.g.attempts[aid].status == "rejected"
    s.world.trees["gone"] = {ADD: "FAILED", Z: "PASSED"}                 # MUL 漏跑
    aid = s.checkpoint("gone", files=[("setup.py", 1, 0)])
    assert s.g.attempts[aid].tier == "full"
    assert s.g.attempts[aid].regressions == (f"{MUL} (MISSING)",)


def test_flaky_failure_confirmed_as_flaky_is_not_a_regression():
    s = sim(cfg=manual())
    s.do(R.claim, "w1", "T3")
    s.world.define("t1", {})
    s.world.flaky_once.add(("t1", MUL))
    aid = s.checkpoint("t1")
    a = s.g.attempts[aid]
    assert a.status == "created" and a.flaky == (MUL,)
    assert s.g.checkpoints[1].kind == "milestone" and s.g.checkpoints[1].tier == "related"


def test_evidence_failure_reopens_task():
    s = sim(cfg=manual())
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {})
    s.review("T1", "t1")
    t = s.g.tasks["T1"]
    assert s.g.head == 1
    assert t.status == ACTIVE and t.reopen_reason == "evidence_failed" and ADD in t.last_failure[0]


def test_review_on_unchanged_tree_uses_head_and_runs_evidence():
    s = sim(cfg=manual(), auto_jobs=False)
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {ADD: "PASSED"})
    aid = s.checkpoint("t1")
    s.finish_job(s.g.attempts[aid].jobs[0])
    assert s.g.head == 1
    n = s.snap("t1", reason="review")
    assert s.do(R.request_review, "w1", "T1", n) is None
    assert s.g.tasks["T1"].status == DONE


def test_precheck_failed_snapshot_is_on_the_timeline_but_never_queued():
    s = sim()
    s.do(R.claim, "w1", "T3")
    n = s.snap("broken", testable=False)
    assert s.g.snapshots[n].testable is False and not s.g.attempts                # 自动线不取
    s.world.define("t2", {})
    s.snap("t2")
    assert s.g.head == 1 and s.g.checkpoints[1].kind == "auto"
    n2 = s.snap("broken2", testable=False, reason="checkpoint")
    aid = s.do(R.request_checkpoint, "w1", n2, "worker")                          # 前台：直接给出原因
    assert s.g.attempts[aid].reason == "precheck" and not s.g.attempts[aid].jobs


def test_job_dedupe_and_unknown_job_rerun():
    s = sim(auto_jobs=False)
    j1 = s.do(R.ensure_job, "tx", ("tests/test_mod.py",), "evidence")
    j2 = s.do(R.ensure_job, "tx", ("tests/test_mod.py",), "evidence")
    assert j1 == j2
    s.finish_job(j1, state="unknown")
    running = [j for j in s.g.jobs.values() if j.state == "running" and j.tree == "tx"]
    assert len(running) == 1 and running[0].id != j1


# ======================================================================== 模块 B：自动快照与后台存档

def test_background_checkpoints_follow_snapshots():
    s = sim()
    s.do(R.claim, "w1", "T3")
    s.world.define("a1", {})
    n = s.snap("a1")
    g = s.g
    assert g.head == 1 and g.checkpoints[1].kind == "auto" and g.checkpoints[1].snapshot == n
    assert g.checkpoints[1].level == "provisional" or g.checkpoints[1].level == "confirmed"
    assert s.snap("a1") == n                                             # 同样的树不再记
    s.world.define("a2", {MUL: "FAILED"})
    s.snap("a2")
    assert s.g.head == 1 and s.g.wips["w1"].last_rejection is None      # 后台被拒不通知 worker
    assert not s.g.sessions["S1"].progress or s.g.checkpoints[1].level == "confirmed"


@pytest.mark.parametrize("order", ["old_first", "new_first"])
def test_new_snapshot_wins_in_both_orders(order):
    s = sim(auto_jobs=False)
    s.do(R.claim, "w1", "T3")
    s.world.define("old", {})
    s.world.define("new", {})
    s.snap("old")
    bg = next(a for a in s.g.attempts.values() if a.lane == "bg")
    fg = s.checkpoint("new")
    fg_job = s.g.attempts[fg].jobs[0]
    bg_job = s.g.attempts[bg.id].jobs[0]
    if order == "old_first":
        s.finish_job(bg_job)
        assert s.g.head == 1 and s.g.checkpoints[1].snapshot == bg.snapshot
        s.finish_job(fg_job)
        assert s.g.head == 2 and s.g.checkpoints[2].parent == 1
    else:
        s.finish_job(fg_job)
        assert s.g.head == 1
        assert s.g.attempts[bg.id].status == "superseded"
    snaps = [c.snapshot for c in sorted(s.g.checkpoints.values(), key=lambda c: c.id)]
    assert snaps == sorted(snaps)
    assert s.g.checkpoints[s.g.head].tree == "new"


def test_rollback_cancels_background_attempt():
    s = sim(auto_jobs=False)
    s.do(R.claim, "w1", "T3")
    s.world.define("a1", {})
    s.snap("a1")
    bg = next(a for a in s.g.attempts.values() if a.lane == "bg")
    assert bg.status == "pending"
    to = s.do(R.rollback, "w1")
    assert to == 0 and s.g.attempts[bg.id].status == "superseded" and s.g.epoch == 1
    assert any(e.kind == "cancel_orphans" for e in s.effects)


def test_only_auto_checkpoints_do_not_count_as_progress():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    s.world.base = dict(BASE)
    for i in range(3):
        s.world.define(f"w{i}", {MUL: "FAILED"} if i == 2 else {})
    s.cfg = s.cfg.with_(stall_no_progress_sec=100)
    s.snap("w0")
    assert s.g.checkpoints[1].kind == "auto"
    s.do(R.end_session, "w1", "done")
    assert not s.g.sessions["S1"].progress or s.g.checkpoints[1].level == "confirmed"


def test_stall_still_detected_with_auto_checkpoints_only():
    cfg = BelayConfig(stall_no_progress_sec=100, checkpoint_tier="related")
    s = Sim({ADD: "FAILED", MUL: "PASSED", Z: "PASSED"}, cfg=cfg, auto_jobs=False)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    s.do(R.claim, "w1", "T3")
    s.world.define("w0", {})
    s.snap("w0")
    bg = next(a for a in s.g.attempts.values() if a.lane == "bg")
    s.finish_job(bg.jobs[0])                                              # 只跑 related：暂存点
    promote = [j for j in s.g.jobs.values() if j.purpose == "promote"]
    assert s.g.checkpoints[1].kind == "auto" and promote                 # 后台提升已排队，但还没跑完
    s.advance(101)
    s.do(R.tick)
    assert s.g.stalls and s.g.stalls[-1].kind == "no_progress"


# ======================================================================== 模块 C：两级存档链

def _bg(s: Sim):
    return next(a for a in s.g.attempts.values() if a.lane == "bg" and a.status == "pending")


def _promote(s: Sim):
    return next(j for j in s.g.jobs.values() if j.purpose == "promote" and j.state == "running")


def test_promotion_skips_older_provisional_points():
    s = sim(auto_jobs=False)
    s.do(R.claim, "w1", "T3")
    for t in ("p1", "p2", "p3"):
        s.world.define(t, {})
    s.snap("p1")
    s.finish_job(_bg(s).jobs[0])
    p1 = _promote(s)
    assert p1.tree == "p1" and s.g.checkpoints[1].level == "provisional"
    for t in ("p2", "p3"):
        s.snap(t)
        s.finish_job(_bg(s).jobs[0])
    assert s.g.head == 3 and _promote(s).id == p1.id                      # 同一时刻只提升一个
    s.finish_job(p1.id)
    assert s.g.confirmed == 1 and _promote(s).tree == "p3"               # 更老的暂存点（p2）跳过
    s.finish_job(_promote(s).id)
    assert s.g.confirmed == 3 and s.g.checkpoints[2].level == "provisional"


def test_demotion_marks_suspects_and_delivery_falls_back():
    s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    s.world.define("p1", {})
    s.snap("p1")
    s.finish_job(_bg(s).jobs[0])
    s.finish_job(_promote(s).id)
    assert s.g.confirmed == 1
    # 第二个暂存点：related 档位（pkg/mod.py → tests/test_mod.py）漏检了 Z
    s.world.define("p2", {Z: "FAILED"})
    s.snap("p2")
    s.finish_job(_bg(s).jobs[0])
    prom = _promote(s)
    s.world.define("p3", {Z: "FAILED", ADD: "PASSED"})
    s.snap("p3")
    s.finish_job(_bg(s).jobs[0])
    assert s.g.head == 3 and prom.tree == "p2"
    s.finish_job(prom.id)
    g = s.g
    assert g.checkpoints[2].demoted and g.confirmed == 1 and suspect(g, 3)
    assert delivery_checkpoint(g, s.cfg) == 1
    assert _promote(s).tree == "p3"                                       # 降级点之后的暂存点照常提升


def test_demotion_recheck_fixed_or_still_failing():
    for fixed in (True, False):
        s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False))
        s.do(R.claim, "w1", "T3")
        s.world.define("d1", {Z: "FAILED"})
        s.snap("d1")
        bg = next(a for a in s.g.attempts.values() if a.lane == "bg")
        s.finish_job(bg.jobs[0])
        s.world.define("d2", {} if fixed else {Z: "FAILED"})
        s.snap("d2")
        promote = next(j for j in s.g.jobs.values() if j.purpose == "promote")
        s.finish_job(promote.id)
        assert s.g.checkpoints[1].demoted
        recheck = next(j for j in s.g.jobs.values() if j.purpose == "recheck")
        assert recheck.tree == "d2" and recheck.selection == ("tests/test_other.py",)
        s.finish_job(recheck.id)
        if fixed:
            assert not s.g.persistent                                     # 已经修复：只记录
        else:
            assert s.g.persistent[Z].trigger == "demoted"
            assert any(l.trigger == "demoted" for l in s.g.locates.values())


def test_delivery_consistency_done_after_the_delivered_point():
    s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    s.world.define("u1", {Z: "FAILED"})
    n = s.snap("u1", reason="review")
    s.do(R.request_review, "w1", "T3", n)
    a = next(a for a in s.g.attempts.values() if a.lane == "fg")
    s.finish_job(a.jobs[0])
    assert s.g.tasks["T3"].status == DONE_UNVERIFIED and s.g.head == 1
    promote = next(j for j in s.g.jobs.values() if j.purpose == "promote")
    s.finish_job(promote.id)
    assert s.g.checkpoints[1].demoted and s.g.confirmed == 0
    for tid in ("T1", "T2"):
        s.do(R.claim, "w1", tid)
        s.do(R.report_blocked, "w1", tid, "environment", "no db")
    s.do(R.end_session, "w1", "done")
    while s.pending_jobs:
        s.finish_job(s.pending_jobs[0])
    status = s.do(R.deliver, "complete")
    assert status == "INCOMPLETE" and s.g.run.delivered == 0
    L = ledger(s.g)
    assert L["not_delivered"] == ["T3"] and L["categories"]["done-not-delivered"] == 1
    assert s.log[-1].get("not_delivered") == ["T3"]


def test_deliver_unconfirmed_policy():
    for policy, expected in ((False, 0), (True, 1)):
        s = sim(auto_jobs=False, cfg=manual(deliver_unconfirmed=policy))
        s.do(R.claim, "w1", "T3")
        s.world.define("q1", {})
        aid = s.checkpoint("q1")
        s.finish_job(s.g.attempts[aid].jobs[0])
        assert delivery_checkpoint(s.g, s.cfg) == expected
        s.do(R.deliver, "deadline")
        assert s.g.run.deliver_unconfirmed is policy


def test_relation_learned_from_located_demotion():
    s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    s.world.define("r1", {})
    s.snap("r1")
    s.finish_job(next(a for a in s.g.attempts.values() if a.lane == "bg").jobs[0])
    s.world.define("r2", {Z: "FAILED"})
    s.snap("r2")
    s.finish_job(next(a for a in s.g.attempts.values() if a.lane == "bg" and a.status == "pending").jobs[0])
    for j in [j for j in s.g.jobs.values() if j.purpose == "promote" and j.state == "running"]:
        s.finish_job(j.id)
    while s.pending_jobs:
        s.finish_job(s.pending_jobs[0])
    assert s.g.checkpoints[2].demoted
    assert ("pkg/mod.py", "tests/test_other.py") in s.g.relations
    sel, _ = related_units(["pkg/mod.py"], ["tests/test_mod.py", "tests/test_other.py"], s.g.relations)
    assert "tests/test_other.py" in sel


# ======================================================================== 模块 D：快照二分

def _timeline(s: Sim, statuses: list[str]) -> list[int]:
    """在不触发后台验证的情况下造一条时间线：每张快照一棵树，Z 的结果按 statuses。"""
    ns = []
    for i, st in enumerate(statuses):
        tree = f"tl{i}"
        s.world.define(tree, {} if st == "P" else ({Z: "ERROR"} if st == "U" else {Z: "FAILED"}))
        if st == "U":
            s.world.trees[tree] = {}
        ns.append(s.snap(tree, files=OTHER))
    return ns


def locate_sim(statuses, **cfg):
    s = sim(auto_jobs=True, cfg=BelayConfig(auto_checkpoint=False, confirm_regressions=False, **cfg))
    s.do(R.claim, "w1", "T3")
    ns = _timeline(s, statuses)
    loc = s.do(R.start_locate, [Z], {"tree": f"tl{len(statuses) - 1}", "snapshot": ns[-1]}, "rejected")
    return s, ns, s.g.locates[loc]


def test_bisect_finds_the_first_bad_snapshot():
    s, ns, loc = locate_sim(["P"] * 5 + ["F"] * 6)
    rec = loc.results[0]
    assert rec["exact"] and rec["bad"]["id"] == ns[5] and rec["good"]["id"] == ns[4]
    steps = [j for j in s.g.jobs.values() if j.locate == loc.id]
    assert len(steps) <= 5


def test_bisect_skips_untestable_midpoints_and_caps_steps():
    s, ns, loc = locate_sim(["P", "P", "U", "U", "F", "F", "F"])
    rec = loc.results[0]
    assert rec["bad"]["id"] == ns[4] and rec["good"]["id"] == ns[1] and not rec["exact"]
    s, ns, loc = locate_sim(["P"] + ["F"] * 30, locate_max_steps=2)
    assert not loc.results[0]["exact"] and len([j for j in s.g.jobs.values() if j.locate == loc.id]) == 2


def test_bisect_with_transient_failure_reports_the_last_transition():
    s = sim(cfg=BelayConfig(auto_checkpoint=False, confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    ns = _timeline(s, ["P", "F", "P", "P", "F", "F"])
    for n in ns[:3]:                                                     # 图里已有 s1–s3 的结果（一过性失败）
        s.do(R.ensure_job, s.g.snapshots[n].tree, ("tests/test_other.py",), "verify")
    loc = s.do(R.start_locate, [Z], {"tree": "tl5", "snapshot": ns[-1]}, "rejected")
    rec = s.g.locates[loc].results[0]
    assert rec["good"]["id"] == ns[3] and rec["bad"]["id"] == ns[4]


def test_bisect_across_rollback_uses_the_rollback_target():
    s = sim(cfg=BelayConfig(auto_checkpoint=False, confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    s.world.define("k1", {})
    s.checkpoint("k1", files=OTHER)
    _timeline(s, ["F", "F"])                                              # 旧段里的失败
    s.do(R.rollback, "w1", 1)
    s.world.define("e1", {})
    n1 = s.snap("e1", files=OTHER)
    s.world.define("e2", {Z: "FAILED"})
    n2 = s.snap("e2", files=OTHER)
    loc = s.do(R.start_locate, [Z], {"tree": "e2", "snapshot": n2}, "rejected")
    L = s.g.locates[loc]
    assert L.lower == 1 and L.epoch == 1
    rec = L.results[0]
    assert rec["good"]["id"] == n1 and rec["bad"]["id"] == n2


def test_rejection_triggers_locate_and_diagnosis():
    s = sim(cfg=manual(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    s.world.define("g1", {})
    s.snap("g1", files=OTHER)
    s.world.define("g2", {Z: "FAILED"})
    aid = s.checkpoint("g2", files=OTHER)
    assert s.g.attempts[aid].status == "rejected"
    loc = next(iter(s.g.locates.values()))
    assert loc.trigger == "rejected" and loc.results
    d = next(iter(s.g.diagnoses.values()))
    assert d.locate == loc.id and d.status == "requested"
    s.do(R.record_diagnosis, d.id, {"suspects": [], "intentional": {"likely": True, "quote": "made up text"},
                                    "suggestion": "x"})
    assert s.g.diagnoses[d.id].result["intentional"]["likely"] is False   # 引文校验不过：丢弃这一项
    s.world.define("g3", {Z: "FAILED", ADD: "PASSED"})
    s.checkpoint("g3", files=OTHER)
    rep = [d for d in s.g.diagnoses.values() if d.trigger == "repeated"]
    assert rep and rep[0].previous == d.id                                # 同一签名第二次被拒：再诊断
    assert not llm_effects(s.log)


# ======================================================================== 模块 E：持续性回归

def test_persistent_regression_after_k_snapshots_but_not_transient():
    s = sim(cfg=BelayConfig(confirm_regressions=False, persist_k=3))
    s.do(R.claim, "w1", "T3")
    for i, st in enumerate(["P", "F", "P"]):                              # 一过性失败：不触发
        s.world.define(f"x{i}", {} if st == "P" else {Z: "FAILED"})
        s.snap(f"x{i}", files=OTHER)
    assert not s.g.persistent
    for i in range(3):
        s.world.define(f"y{i}", {Z: "FAILED"})
        s.snap(f"y{i}", files=OTHER)
    assert Z in s.g.persistent and s.g.persistent[Z].trigger == "background"
    assert any(l.trigger == "persistent" for l in s.g.locates.values())


# ======================================================================== 其他请求

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
    s.do(R.claim, "w1", "T1")
    assert s.g.tasks["T1"].status == ACTIVE


def test_rollback_defaults_to_the_latest_milestone():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T1")
    s.world.define("t1", {ADD: "PASSED"})
    s.review("T1", "t1")                                                  # 1：review 里程碑
    s.do(R.claim, "w1", "T3")
    s.world.define("t2", {ADD: "PASSED"})
    s.snap("t2")                                                          # 2：自动存档
    assert s.g.head == 2 and s.g.checkpoints[2].kind == "auto"
    with pytest.raises(Rejected):
        s.do(R.rollback, "w1", 7)
    assert s.do(R.rollback, "w1") == 1                                    # 默认退到最近的里程碑
    assert s.g.checkpoints[2].abandoned and s.g.tasks["T1"].status == DONE
    s.do(R.rollback, "w1", 0)
    g = s.g
    assert g.head == 0 and g.tasks["T1"].status == OPEN and g.tasks["T1"].reopen_reason == "rolled_back"
    assert g.epoch == 2 and g.confirmed == 0
    assert any(e.kind == "restore_workspace" and e.args["reset_ref"] for e in s.effects)


# ======================================================================== 模块 H：步骤与恢复点

def _step(s: Sim, tree: str, summary: str = "") -> str:
    n = s.snap(tree, reason="step_done")
    return s.do(R.step_done, "w1", n, summary)


def test_steps_are_planned_from_todos_and_anchored_by_checkpoints():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    todos = [{"content": "read docs", "status": "in_progress"}, {"content": "write docstring", "status": "pending"}]
    s.do(R.plan_steps, "w1", todos)
    assert [(x.id, x.status) for x in sorted(s.g.steps.values(), key=lambda x: x.n)] == \
        [("T3.1", "active"), ("T3.2", "planned")]
    n = len(s.log)
    s.do(R.plan_steps, "w1", todos)
    assert len(s.log) == n                                               # 没有变化：不写事件
    s.world.define("st1", {})
    sid = _step(s, "st1", "read the docs")
    assert sid == "T3.1" and s.g.steps["T3.1"].status == "anchored"      # 后台验证通过 → 锚定
    rp = resume_point(s.g, "w1")
    assert rp["step"] == "T3.2" and rp["base"] == s.g.steps["T3.1"].checkpoint and rp["anchored"] == ["T3.1"]
    assert s.g.sessions["S1"].progress


def test_anchor_rejected_then_contained_by_a_later_checkpoint():
    s = sim(cfg=BelayConfig(confirm_regressions=False, locate=False))
    s.do(R.claim, "w1", "T3")
    s.do(R.plan_steps, "w1", [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}])
    s.world.define("sb", {MUL: "FAILED"})
    _step(s, "sb")
    st = s.g.steps["T3.1"]
    assert st.status == "declared"
    assert s.g.wips["w1"].last_rejection["kind"] == "step"               # 锚点被拒要通知
    s.world.define("sc", {})
    s.checkpoint("sc")
    assert s.g.steps["T3.1"].status == "anchored"                        # 后续存档包含了锚点


def test_interleaved_tasks_and_rollback_invalidates_steps():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    s.do(R.plan_steps, "w1", [{"content": "doc 1", "status": "in_progress"}, {"content": "doc 2", "status": "pending"}])
    s.world.define("i1", {})
    _step(s, "i1")
    s.do(R.claim, "w1", "T1")                                            # 交错：去做 T1
    s.do(R.plan_steps, "w1", [{"content": "fix", "status": "in_progress"}])
    s.world.define("i2", {})
    _step(s, "i2")
    assert s.g.steps["T3.1"].status == "anchored" and s.g.steps["T1.1"].status == "anchored"
    s.do(R.rollback, "w1", s.g.steps["T3.1"].checkpoint)
    assert s.g.steps["T1.1"].status == "active" and s.g.steps["T1.1"].anchor_snapshot is None
    assert s.g.steps["T3.1"].status == "anchored"
    s.world.define("i3", {})
    s.snap("i3")
    assert s.g.steps["T1.1"].status == "active"                          # 新段的存档不会把失效的步骤判成 anchored


def test_todo_completion_counts_as_step_done():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    s.do(R.claim, "w1", "T3")
    todos = [{"content": "one", "status": "completed"}, {"content": "two", "status": "in_progress"}]
    assert R.newly_completed(s.g, "w1", todos)
    s.world.define("td", {})
    n = s.snap("td", reason="step_done")
    s.do(R.plan_steps, "w1", todos, n)
    assert s.g.steps["T3.1"].status == "anchored" and s.g.steps["T3.2"].status == "active"
    assert not R.newly_completed(s.g, "w1", todos)


def test_no_focus_todos_become_a_note():
    s = sim()
    s.do(R.plan_steps, "w1", [{"content": "look around", "status": "pending"}])
    assert s.g.notes[-1].kind == "todos" and not s.g.steps


# ======================================================================== 模块 F：复查

def _done_unverified(s: Sim, tid: str, tree: str) -> None:
    s.do(R.claim, "w1", tid)
    s.world.define(tree, {})
    s.review(tid, tree)


def test_review_no_reopens_yes_keeps_and_only_once():
    s = sim(cfg=manual())
    _done_unverified(s, "T3", "r1")
    assert s.g.tasks["T3"].review == "running"
    s.do(R.record_review, "T3", "done", {"implemented": "no", "missing": ["docstring for mul"]})
    t = s.g.tasks["T3"]
    assert t.status == OPEN and t.reopen_reason == "review_missing" and t.review_reopens == 1
    assert "docstring for mul" in t.last_failure[0]
    s.do(R.claim, "w1", "T3")
    s.world.define("r2", {})
    s.review("T3", "r2")
    assert s.g.tasks["T3"].status == DONE_UNVERIFIED and s.g.tasks["T3"].review is None   # 第二次不再复查
    _done_unverified(s, "T2", "r3")
    s.do(R.record_review, "T2", "done", {"implemented": "yes"})
    assert s.g.tasks["T2"].status == DONE_UNVERIFIED and s.g.tasks["T2"].review == "yes"
    assert all(e.type != "task_done" for e in s.log if e.source == "llm")
    assert not llm_effects(s.log)


def test_next_step_waits_for_reviews_and_deadline_does_not_reopen():
    s = sim(cfg=manual())
    _done_unverified(s, "T3", "n1")
    for tid in ("T1", "T2"):
        s.do(R.claim, "w1", tid)
        s.do(R.report_blocked, "w1", tid, "insufficient_info", "unclear")
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("review", "final")
    s.do(R.request_final_reviews)
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("wait", "review in progress")
    s.do(R.record_review, "T1", "blocked", {"reading": "add returns a+b"})
    assert s.g.tasks["T1"].status == OPEN and s.g.tasks["T1"].reopen_reason == "review_reading"
    s.do(R.record_review, "T2", "blocked", {})
    s.advance(5400)
    s.do(R.tick)
    s.do(R.record_review, "T3", "done", {"implemented": "no"})
    assert s.g.tasks["T3"].status == DONE_UNVERIFIED                      # 截止：只进账本


# ======================================================================== 时间、停滞、运行的结束

def test_deadline_reserve():
    cfg = BelayConfig(reserve_min_sec=300)
    s = sim(cfg=cfg)
    s.advance(5400 - 299)
    s.do(R.tick)
    assert s.g.run.reserve and any(e.kind == "stop_workers" for e in s.effects)
    assert suggest(s.g, "w1", s.now, cfg) == []
    assert R.next_step(s.g, "w1", s.now, cfg) == ("finalize", "deadline")


def test_stall_hint_then_replan_and_repeated_failure():
    cfg = BelayConfig(stall_no_progress_sec=600, reserve_min_sec=10, auto_checkpoint=False, locate=False)
    s = sim(cfg=cfg)
    s.do(R.claim, "w1", "T1")
    s.advance(601)
    s.do(R.tick)
    assert s.g.stalls[-1].kind == "no_progress" and s.g.stalls[-1].action == "hint"
    for i in range(3):
        s.world.define(f"bad{i}", {MUL: "FAILED"})
        s.checkpoint(f"bad{i}")
    s.do(R.tick)
    st = s.g.stalls[-1]
    assert st.kind == "repeated_failure" and st.action == "replan" and st.task == "T1"
    assert any(e.kind == "replan" and e.args["task"] == "T1" for e in s.effects)
    n = len(s.g.stalls)
    s.do(R.tick)
    assert len(s.g.stalls) == n


def test_session_end_is_not_run_end():
    s = sim()
    s.do(R.claim, "w1", "T1")
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("start_session", "restart")
    s.do(R.start_session, "w1", "restart", {})
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("resume_session", "S2")
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "no_progress")


def test_run_done_requires_resolved_tasks_and_a_confirmed_delivery():
    s = sim(cfg=manual(reviewer=False))
    for tid, tree in (("T1", "t1"), ("T2", "t2")):
        s.do(R.claim, "w1", tid)
        s.world.define(tree, {ADD: "PASSED"})
        s.review(tid, tree)
    s.do(R.claim, "w1", "T3")
    s.do(R.report_blocked, "w1", "T3", "environment", "no docs tool")
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    s.do(R.begin_finalize, "complete")
    assert s.do(R.promote_now) is False                                   # 模拟器里全量立即完成 → 提升
    assert s.g.checkpoints[2].level == "confirmed" and s.g.confirmed == 2
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
    s.review("T1", "t1")
    assert not check_log(s.log)
    assert all(e.source in ("rule", "observed") for e in s.log
               if e.type in ("task_done", "checkpoint_created", "checkpoint_confirmed", "step_anchored"))
    forged = list(s.log) + [s.log[-1].__class__(len(s.log) + 1, 0, "task_done", "worker:w1", "self_report", {})]
    assert check_log(forged)


def test_build_context_after_rejection_mentions_facts_and_notes():
    s = sim(cfg=manual(locate=False))
    s.do(R.claim, "w1", "T1")
    s.do(R.note, "w1", "tried patching add via operator overloading; dead end")
    s.world.define("bad", {ADD: "PASSED", MUL: "FAILED"})
    s.review("T1", "bad")
    s.do(R.record_compaction, "w1", 3, 1000, 200, "Decided to change add() in place because callers rely on it.")
    s.do(R.end_session, "w1", "handoff")
    ctx = build_context(s.g, "w1", 50_000, s.now, s.cfg, mode="resume")
    text = ctx.text
    for needle in ("<task>", "- R1 add", "### T1 [active] fix add", "rejected checkpoint attempt",
                   f"{MUL} (FAILED)", "already fail on the original code", "operator overloading",
                   "(self-reported)", "Decided to change add()", "model-written", "Suggested order"):
        assert needle in text, needle
    assert [k for k, _ in ctx.sections][:4] == ["task", "requirements", "focus", "pending"]

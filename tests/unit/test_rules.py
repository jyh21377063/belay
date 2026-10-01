"""状态转换规则（纯函数，经由模拟器驱动；每个事务之后都检查不变量）。"""
from __future__ import annotations

import pytest

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import build_context
from belay.core.invariants import check_log, llm_effects
from belay.core.model import REQ_BLOCKED, REQ_OPEN, REQ_SUBMITTED, REQ_VERIFIED
from belay.core.queries import delivery_checkpoint, resume_point, suspect
from belay.core.render import ledger, ledger_markdown, render_board, render_submit
from belay.core.rules import Rejected
from belay.core.verify import related_units
from tests.sim import Sim

ADD, MUL, Z = "tests/test_mod.py::test_add", "tests/test_mod.py::test_mul", "tests/test_other.py::test_z"
BASE = {ADD: "FAILED", MUL: "PASSED", Z: "PASSED"}
TASK = ("# Notes for version 2.0\n"
        "Fix the add function so that it returns the sum.\n"
        "Also make mul handle negative numbers correctly.\n"
        "Document the new behaviour in the module docstring please.")
PLAN = {"requirements": [{"id": "a", "quote": "Fix the add function so that it returns the sum.", "summary": "add",
                          "checks": [ADD]},
                         {"id": "b", "quote": "Also make mul handle negative numbers correctly.", "summary": "mul",
                          "checks": [MUL]},                               # 基线上本来就通过：不是证据
                         {"id": "c", "quote": "Document the new behaviour in the module docstring please.",
                          "summary": "docs"}]}
MOD = [("pkg/mod.py", 1, 1)]
OTHER = [("pkg/other.py", 1, 1)]


def sim(**kw) -> Sim:
    s = Sim(BASE, **kw)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def fg(**kw) -> BelayConfig:
    """关掉后台验证，只看前台（submit）规则。"""
    return BelayConfig(background="off", **kw)


def reqs(s: Sim) -> dict:
    return {r.id: r.status for r in s.g.requirements.values()}


# ======================================================================== 准备

def test_setup_baseline_and_plan():
    s = sim()
    g = s.g
    assert g.baseline == {ADD: "fail", MUL: "pass", Z: "pass"}
    assert g.frozen and set(g.requirements) == {"R1", "R2", "R3"}
    assert g.requirements["R1"].checks == (ADD,) and g.requirements["R2"].checks == (MUL,)
    assert all(r.kind == "actionable" and r.status == REQ_OPEN for r in g.requirements.values())
    assert g.head == 0 and g.checkpoints[0].tree == "t0" and g.confirmed == 0
    assert not g.degraded and g.isolation["valid"] is True


# ======================================================================== 后台：持续验证最新快照，不需要任何声明

def test_background_verifies_the_latest_snapshot_and_requirements_follow_their_checks():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    s.world.define("w1", {})
    s.snap("w1")
    g = s.g
    assert g.head == 1 and g.checkpoints[1].kind == "auto" and g.checkpoints[1].trigger == "auto"
    assert reqs(s) == {"R1": REQ_OPEN, "R2": REQ_OPEN, "R3": REQ_OPEN}
    assert not g.sessions["S1"].progress                                 # 存档本身不算进展
    s.world.define("w2", {ADD: "PASSED"})
    s.snap("w2")
    g = s.g
    assert g.head == 2 and g.requirements["R1"].status == REQ_VERIFIED and g.requirements["R1"].checkpoint == 2
    assert g.requirements["R2"].status == REQ_OPEN                       # MUL 在基线上就通过：证明不了 R2
    assert g.sessions["S1"].progress                                     # 需求验证通过算进展
    sel = g.attempts["A2"].selection
    assert "tests/test_mod.py" in sel                                    # 证据检查随尝试一起跑
    s.check_log()


def test_only_the_latest_snapshot_is_verified_and_tried_trees_are_not_retried():
    s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False))
    for t in ("a1", "a2", "a3"):
        s.world.define(t, {})
    s.snap("a1")
    a1 = next(a for a in s.g.attempts.values() if a.lane == "bg")
    s.snap("a2")
    s.snap("a3")
    assert len(s.g.attempts) == 1                                        # 同一时刻最多一个后台尝试
    s.finish_job(a1.jobs[0])
    nxt = [a for a in s.g.attempts.values() if a.status == "pending"]
    assert [a.tree for a in nxt] == ["a3"]                               # 跳过 a2：最新的胜出
    s.world.define("bad", {MUL: "FAILED"})
    s.snap("bad")
    s.finish_job(nxt[0].jobs[0])
    bad = next(a for a in s.g.attempts.values() if a.tree == "bad")
    s.finish_job(bad.jobs[0])
    assert s.g.attempts[bad.id].status == "rejected" and s.g.head == 2
    n = len(s.g.attempts)
    s.do(R.schedule_background)
    assert len(s.g.attempts) == n                                        # 被拒的树不再重试，等新的改动


def test_untestable_latest_falls_back_to_the_previous_testable_snapshot():
    s = sim(auto_jobs=False)
    s.world.define("ok", {})
    s.do(R.record_snapshot, "w1", R.SnapObs("ok", "ok", files=tuple(MOD)), "writes")
    first = next(iter(s.g.attempts.values()))
    s.snap("broken", testable=False)
    s.finish_job(first.jobs[0])
    assert s.g.head == 1 and len(s.g.attempts) == 1                      # 不可测的快照不进队列


def test_degraded_mode_and_handoff_mode_only_verify_handoffs():
    for kw in ({"isolation": {"valid": False, "reason": "x"}}, {}):
        cfg = BelayConfig() if kw else BelayConfig(background="handoff")
        s = Sim(BASE, cfg=cfg)
        s.setup(TASK, PLAN, **kw)
        s.do(R.start_session, "w1", "first", {})
        s.world.define("h1", {})
        s.snap("h1")
        assert not s.g.attempts
        s.snap("h1", reason="session_end")                               # 树没变也记一张：否则这个节点会漏验
        assert s.g.head == 1 and s.g.checkpoints[1].kind == "handoff"


def test_background_rejections_never_escalate():
    """后台验证被拒是常态（中间态测不过）：链头不动，不通知、不定位、不诊断、不算停滞。"""
    s = sim(cfg=BelayConfig(confirm_regressions=False, stall_same_failure=3))
    for i in range(4):
        s.world.define(f"y{i}", {Z: f"FAILED"} if i % 2 == 0 else {Z: "ERROR"})
        s.snap(f"y{i}", files=OTHER)
    g = s.g
    rejected = [a for a in g.attempts.values() if a.status == "rejected"]
    assert len(rejected) == 4 and all(a.lane == "bg" for a in rejected) and g.head == 0
    assert g.wips["w1"].last_rejection is None
    assert not g.persistent and not g.locates and not g.diagnoses
    s.do(R.tick)
    assert not any(x.kind == "repeated_failure" for x in s.g.stalls)


@pytest.mark.parametrize("order", ["bg_first", "fg_first"])
def test_new_snapshot_wins_between_background_and_submit(order):
    s = sim(auto_jobs=False, cfg=BelayConfig(reviewer=False))
    s.world.define("old", {})
    s.world.define("new", {})
    s.snap("old")
    bg = next(a for a in s.g.attempts.values() if a.lane == "bg")
    sid = s.submit("new")
    fa = s.g.submits[sid].attempt
    fg_job, bg_job = s.g.attempts[fa].jobs[0], s.g.attempts[bg.id].jobs[0]
    if order == "bg_first":
        s.finish_job(bg_job)
        assert s.g.head == 1 and s.g.checkpoints[1].snapshot == bg.snapshot
        s.finish_job(fg_job)
        assert s.g.head == 2 and s.g.checkpoints[2].parent == 1 and s.g.checkpoints[2].kind == "submit"
    else:
        s.finish_job(fg_job)
        assert s.g.head == 1
        assert s.g.attempts[bg.id].status == "superseded"
    assert s.g.checkpoints[s.g.head].tree == "new"
    assert s.g.submits[sid].status == "accepted" or s.g.submits[sid].open


# ======================================================================== 提交：判定需求、复查收紧、接受或交还清单

def test_submit_classifies_requirements_reviews_and_returns_the_list():
    s = sim(cfg=fg())
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1", summary="fixed add, mul and docs")
    g = s.g
    sub = g.submits[sid]
    assert sub.checkpoint == 1 and g.checkpoints[1].kind == "submit" and g.checkpoints[1].label.startswith("fixed")
    assert reqs(s) == {"R1": REQ_VERIFIED, "R2": REQ_SUBMITTED, "R3": REQ_SUBMITTED}
    assert sub.status == "reviewing" and s.running_reviews() == ["V1"]
    assert g.reviews["V1"].requirements == ("R2", "R3") and g.reviews["V1"].submit == sid
    assert R.next_step(g, "w1", s.now, s.cfg) == ("resume_session", "S1")
    s.review("V1", {"R2": {"implemented": "yes"}, "R3": {"implemented": "partial", "missing": ["docstring"]}})
    g = s.g
    assert g.submits[sid].status == "returned" and g.submits[sid].open == ("R3",)
    r3 = g.requirements["R3"]
    assert r3.status == REQ_OPEN and r3.reopen_reason == "review_missing" and r3.last_failure == ("docstring",)
    assert r3.review_reopens == 1 and g.requirements["R2"].review == "yes"
    text = render_submit(g, sid)
    assert "not accepted yet" in text and "R3" in text and "docstring" in text
    s.world.define("t2", {ADD: "PASSED"})
    sid2 = s.submit("t2")
    g = s.g
    assert g.submits[sid2].status == "accepted"                          # 第二次不再复查（上限 1 次）
    assert g.requirements["R3"].status == REQ_SUBMITTED and g.requirements["R3"].review is None
    assert "accepted" in render_submit(g, sid2)
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    assert not llm_effects(s.log)
    s.check_log()


def test_submit_regression_is_rejected_with_reasons_locate_and_diagnosis():
    s = sim(cfg=fg(confirm_regressions=False))
    s.world.define("g1", {})
    s.snap("g1", files=OTHER)
    s.world.define("bad", {ADD: "PASSED", Z: "FAILED"})
    sid = s.submit("bad", files=OTHER, dropped=("tests/test_other.py",))
    g = s.g
    sub = g.submits[sid]
    assert sub.status == "rejected" and sub.reason == "regression" and g.head == 0
    assert reqs(s) == {"R1": REQ_OPEN, "R2": REQ_OPEN, "R3": REQ_OPEN}   # 被拒：什么都没记下
    assert g.wips["w1"].last_rejection["regressions"] == [f"{Z} (FAILED)"]
    text = render_submit(g, sid)
    assert f"assert failure in {Z}" in text and "ran in their original version" in text
    loc = next(iter(g.locates.values()))
    assert loc.trigger == "rejected" and loc.results
    d = next(iter(g.diagnoses.values()))
    assert d.locate == loc.id
    s.do(R.record_diagnosis, d.id, {"suspects": [], "intentional": {"likely": True, "quote": "made up text"},
                                    "suggestion": "x"})
    assert s.g.diagnoses[d.id].result["intentional"]["likely"] is False   # 引文校验不过：丢弃这一项
    s.world.define("bad2", {Z: "FAILED", MUL: "PASSED"})
    s.submit("bad2", files=OTHER)
    rep = [x for x in s.g.diagnoses.values() if x.trigger == "repeated"]
    assert rep and rep[0].previous == d.id                                # 同一签名第二次被拒：再诊断
    assert not llm_effects(s.log)


def test_missing_or_skipped_guard_tests_are_regressions():
    s = sim(cfg=fg())
    s.world.define("skip", {MUL: "SKIPPED"})
    sid = s.submit("skip", files=[("pkg/mod.py", 1, 0)])
    assert s.g.submits[sid].status == "rejected"
    s.world.trees["gone"] = {ADD: "FAILED", Z: "PASSED"}                 # MUL 漏跑
    sid = s.submit("gone", files=[("setup.py", 1, 0)])
    a = s.g.attempts[s.g.submits[sid].attempt]
    assert a.tier == "full" and a.regressions == (f"{MUL} (MISSING)",)


def test_flaky_failure_confirmed_as_flaky_is_not_a_regression():
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {})
    s.world.flaky_once.add(("t1", MUL))
    sid = s.submit("t1")
    a = s.g.attempts[s.g.submits[sid].attempt]
    assert a.status == "created" and a.flaky == (MUL,) and s.g.checkpoints[1].tier == "related"


def test_evidence_failure_keeps_the_requirement_open_with_the_failing_checks():
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {})
    sid = s.submit("t1")
    g = s.g
    assert g.submits[sid].status == "returned" and g.submits[sid].open == ("R1",)
    assert g.requirements["R1"].status == REQ_OPEN and ADD in g.requirements["R1"].last_failure[0]
    assert g.submits[sid].failing == {"R1": [f"{ADD} (FAILED)"]}
    assert "R1" in render_submit(g, sid) and ADD in render_submit(g, sid)


def test_submit_on_the_head_tree_uses_the_head_and_waits_for_evidence():
    s = sim(auto_jobs=False, cfg=BelayConfig(reviewer=False))
    s.world.define("t1", {ADD: "PASSED"})
    s.snap("t1", files=OTHER)                                            # 不选 tests/test_mod.py 的改动
    bg = next(iter(s.g.attempts.values()))
    s.finish_job(bg.jobs[0])
    assert s.g.head == 1
    assert "tests/test_mod.py" in bg.selection                           # 证据检查也被选上
    n = s.snap("t1", files=OTHER, reason="submit")
    sid = s.do(R.request_submit, "w1", n, "done")
    g = s.g
    assert g.submits[sid].checkpoint == 1 and g.checkpoints[1].kind == "submit"
    assert g.submits[sid].status == "accepted"


def test_submit_precheck_failure_is_rejected_at_once():
    s = sim(cfg=fg())
    sid = s.submit("broken", testable=False)
    sub = s.g.submits[sid]
    assert sub.status == "rejected" and sub.reason == "precheck"
    assert "do not compile" in render_submit(s.g, sid)
    with pytest.raises(Rejected, match="still being checked"):
        s2 = sim(cfg=fg(), auto_jobs=False)
        s2.world.define("t1", {})
        s2.submit("t1")
        s2.submit("t1")


def test_blocked_list_is_validated_and_check_conflict_needs_a_verbatim_quote():
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {ADD: "PASSED"})
    with pytest.raises(Rejected, match="kind must be"):
        s.submit("t1", blocked=[{"requirement": "R3", "kind": "lazy", "reason": "x"}])
    with pytest.raises(Rejected, match="not a requirement"):
        s.submit("t1", blocked=[{"requirement": "R9", "kind": "environment", "reason": "x"}])
    with pytest.raises(Rejected, match="verbatim"):
        s.submit("t1", blocked=[{"requirement": "R2", "kind": "check_conflict", "reason": "x", "quote": "nope"}])
    sid = s.submit("t1", blocked=[{"requirement": "R2", "kind": "check_conflict", "reason": "old test",
                                   "quote": "make mul handle negative numbers correctly"},
                                  {"requirement": "R3", "kind": "environment", "reason": "no docs tool"}])
    g = s.g
    assert g.submits[sid].status == "accepted"
    assert reqs(s) == {"R1": REQ_VERIFIED, "R2": REQ_BLOCKED, "R3": REQ_BLOCKED}
    assert g.requirements["R2"].blocked_quote and g.requirements["R3"].blocked_kind == "environment"


def test_baseline_passing_checks_are_never_evidence_even_when_they_pass():
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {ADD: "PASSED"})
    s.submit("t1")
    r2 = s.g.requirements["R2"]
    assert r2.status == REQ_SUBMITTED and r2.checkpoint == 1             # 自述，不是 verified
    assert ledger(s.g)["category_requirements"]["self-reported"] == ["R2", "R3"]


def test_review_batches_blocked_reading_and_reviewer_off():
    s = sim(cfg=fg(review_batch=1))
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1", blocked=[{"requirement": "R3", "kind": "insufficient_info", "reason": "which docstring?"}])
    g = s.g
    assert [(v.phase, v.requirements) for v in g.reviews.values()] == [("done", ("R2",)), ("blocked", ("R3",))]
    s.review("V1", {"R2": {"implemented": "yes"}})
    assert s.g.submits[sid].status == "reviewing"                        # 还有一批在复查
    s.review("V2", {"R3": {"reading": "document add and mul in pkg/mod.py"}})
    g = s.g
    assert g.requirements["R3"].status == REQ_OPEN and g.requirements["R3"].reopen_reason == "review_reading"
    assert g.submits[sid].status == "returned"
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1")
    assert s.g.submits[sid].status == "accepted" and not s.g.reviews


def test_reviewer_failure_does_not_reopen_and_deadline_only_records():
    s = sim(cfg=fg())
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1")
    s.review("V1", {})                                                   # 复查者失败：什么都不重开
    assert s.g.submits[sid].status == "accepted"
    assert {s.g.requirements[r].review for r in ("R2", "R3")} == {"failed"}
    s = sim(cfg=fg())
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1")
    s.advance(5400)
    s.do(R.tick)
    s.review("V1", {"R2": {"implemented": "no"}, "R3": {"implemented": "yes"}})
    g = s.g
    assert g.requirements["R2"].status == REQ_SUBMITTED and g.requirements["R2"].review == "no"   # 截止：只进账本
    s.do(R.begin_finalize, "deadline")
    s.do(R.promote_now)
    assert s.do(R.deliver, "deadline") == "INCOMPLETE"
    assert "the reviewer found R2 incomplete" in s.g.run.status_reasons


# ======================================================================== todo：运行级步骤

def test_todos_are_mirrored_completed_anchored_and_label_checkpoints():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    todos = [{"content": "read the code", "status": "in_progress"},
             {"content": "fix add (R1)", "status": "pending"}]
    s.do(R.update_todos, "w1", todos)
    g = s.g
    assert [(t.id, t.status, t.requirements) for t in sorted(g.todos.values(), key=lambda t: t.n)] == \
        [("P1", "in_progress", ()), ("P2", "pending", ("R1",))]
    n = len(s.log)
    s.do(R.update_todos, "w1", todos)
    assert len(s.log) == n                                               # 没有变化：不写事件
    done = [{"content": "read the code", "status": "completed"}, {"content": "fix add (R1)", "status": "in_progress"}]
    assert R.newly_completed(s.g, done)
    s.world.define("td", {})
    n = s.snap("td", reason="todo")
    assert s.do(R.update_todos, "w1", done, n) == ["P1"]
    g = s.g
    assert g.todos["P1"].status == "anchored" and g.todos["P2"].status == "in_progress"
    cp = g.checkpoints[g.todos["P1"].checkpoint]
    assert cp.kind == "todo" and cp.label == "read the code"
    assert not R.newly_completed(s.g, done) and g.sessions["S1"].progress
    assert resume_point(g, "w1") == {"base": g.head, "partial": n, "todo": "P2"}
    s.do(R.update_todos, "w1", [{"content": "something else", "status": "pending"}])
    assert set(s.g.todos) == {"P1", "P3"}                                # 删掉没完成的；完成的留着


def test_todo_completed_on_a_rejected_snapshot_is_anchored_by_a_later_checkpoint():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    s.do(R.update_todos, "w1", [{"content": "a", "status": "in_progress"}])
    s.world.define("sb", {MUL: "FAILED"})
    n = s.snap("sb", reason="todo")
    s.do(R.update_todos, "w1", [{"content": "a", "status": "completed"}], n)
    assert s.g.todos["P1"].status == "completed" and s.g.wips["w1"].last_rejection is None
    s.world.define("sc", {})
    s.snap("sc")
    assert s.g.todos["P1"].status == "anchored"


def test_rollback_reopens_requirements_and_invalidates_todos():
    s = sim(cfg=BelayConfig(confirm_regressions=False, reviewer=False))
    s.world.define("k1", {})
    s.snap("k1")
    s.do(R.update_todos, "w1", [{"content": "fix add", "status": "in_progress"}])
    s.world.define("k2", {ADD: "PASSED"})
    n = s.snap("k2", reason="todo")
    s.do(R.update_todos, "w1", [{"content": "fix add", "status": "completed"}], n)
    assert s.g.requirements["R1"].status == REQ_VERIFIED and s.g.todos["P1"].status == "anchored"
    with pytest.raises(Rejected):
        s.do(R.rollback, "w1", 7)
    s.do(R.rollback, "w1", 1)
    g = s.g
    assert g.head == 1 and g.epoch == 1 and g.requirements["R1"].status == REQ_OPEN
    assert g.requirements["R1"].reopen_reason == "rolled_back" and g.todos["P1"].status == "in_progress"
    assert any(e.kind == "restore_workspace" and e.args["reset_ref"] for e in s.effects)


# ======================================================================== 模块 C：两级存档链

def _bg(s: Sim):
    return next(a for a in s.g.attempts.values() if a.lane == "bg" and a.status == "pending")


def _promote(s: Sim):
    return next(j for j in s.g.jobs.values() if j.purpose == "promote" and j.state == "running")


def test_promotion_skips_older_provisional_points():
    s = sim(auto_jobs=False)
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
    assert not any(j.purpose == "recheck" for j in g.jobs.values())       # 后台存档被降级：不追查、不通知
    assert not g.persistent and not g.locates


def test_demotion_of_a_submit_rechecks_fixed_or_still_failing():
    for fixed in (True, False):
        s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False, reviewer=False))
        s.world.define("d1", {Z: "FAILED"})
        sid = s.submit("d1")                                              # worker 提交的存档才追查
        s.finish_job(s.g.attempts[s.g.submits[sid].attempt].jobs[0])
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


def test_delivery_consistency_finished_after_the_delivered_point():
    s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False, reviewer=False, background="off"))
    s.world.define("u1", {Z: "FAILED", ADD: "PASSED"})
    sid = s.submit("u1")
    s.finish_job(s.g.attempts[s.g.submits[sid].attempt].jobs[0])
    assert s.g.submits[sid].status == "accepted" and s.g.head == 1
    promote = next(j for j in s.g.jobs.values() if j.purpose == "promote")
    s.finish_job(promote.id)
    assert s.g.checkpoints[1].demoted and s.g.confirmed == 0
    status = s.do(R.deliver, "complete")
    assert status == "INCOMPLETE" and s.g.run.delivered == 0
    L = ledger(s.g)
    assert L["not_delivered"] == ["R1", "R2", "R3"] and L["categories"]["done-not-delivered"] == 3
    assert s.log[-1].get("not_delivered") == ["R1", "R2", "R3"]


def test_deliver_unconfirmed_policy():
    for policy, expected in ((False, 0), (True, 1)):
        s = sim(auto_jobs=False, cfg=fg(deliver_unconfirmed=policy, reviewer=False))
        s.world.define("q1", {})
        sid = s.submit("q1")
        s.finish_job(s.g.attempts[s.g.submits[sid].attempt].jobs[0])
        assert delivery_checkpoint(s.g, s.cfg) == expected
        s.do(R.deliver, "deadline")
        assert s.g.run.deliver_unconfirmed is policy


def test_relation_learned_from_located_demotion():
    s = sim(auto_jobs=False, cfg=BelayConfig(confirm_regressions=False, reviewer=False))
    s.world.define("r1", {})
    s.snap("r1")
    s.finish_job(_bg(s).jobs[0])
    s.world.define("r2", {Z: "FAILED"})
    sid = s.submit("r2")
    s.finish_job(s.g.attempts[s.g.submits[sid].attempt].jobs[0])
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
    """造一条时间线：每张快照一棵树，Z 的结果按 statuses（P 通过 / F 失败 / U 跑不出结果）。"""
    ns = []
    for i, st in enumerate(statuses):
        tree = f"tl{i}"
        s.world.define(tree, {} if st == "P" else ({Z: "ERROR"} if st == "U" else {Z: "FAILED"}))
        if st == "U":
            s.world.trees[tree] = {}
        ns.append(s.snap(tree, files=OTHER))
    return ns


def locate_sim(statuses, **cfg):
    s = sim(auto_jobs=True, cfg=fg(confirm_regressions=False, **cfg))
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


def test_bisect_uses_background_results_and_reports_the_last_transition():
    s = sim(cfg=BelayConfig(confirm_regressions=False))
    ns = _timeline(s, ["P", "F", "P", "P", "F", "F"])                    # 后台验证过其中一些快照（一过性失败）
    loc = s.do(R.start_locate, [Z], {"tree": "tl5", "snapshot": ns[-1]}, "rejected")
    rec = s.g.locates[loc].results[0]
    assert rec["good"]["id"] == ns[3] and rec["bad"]["id"] == ns[4]


def test_bisect_across_rollback_uses_the_rollback_target():
    s = sim(cfg=fg(confirm_regressions=False, reviewer=False))
    s.world.define("k1", {})
    s.submit("k1", files=OTHER)
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


# ======================================================================== 回归门豁免

MUL_QUOTE = "make mul handle negative numbers correctly"


def test_waiver_takes_a_contradicted_test_out_of_the_gate():
    s = sim(cfg=fg(locate=False, reviewer=False))
    with pytest.raises(Rejected, match="not seen it fail"):              # 不能预先豁免
        s.do(R.waive_checks, "w1", [MUL], MUL_QUOTE, "the old test asserts the old sign")
    s.world.define("neg", {MUL: "FAILED", ADD: "PASSED"})
    sid = s.submit("neg")
    assert s.g.submits[sid].status == "rejected" and s.g.head == 0
    with pytest.raises(Rejected, match="verbatim"):
        s.do(R.waive_checks, "w1", [MUL], "mul must change", "the old test asserts the old sign")
    with pytest.raises(Rejected, match="not in the regression gate"):    # 原始代码上就失败的不在门里
        s.do(R.waive_checks, "w1", [ADD], MUL_QUOTE, "x")
    with pytest.raises(Rejected, match="Unknown requirement"):
        s.do(R.waive_checks, "w1", [MUL], MUL_QUOTE, "x", "R9")
    assert s.do(R.waive_checks, "w1", [MUL], MUL_QUOTE, "the old test asserts the old sign", "R2") == [MUL]
    w = s.g.waived[MUL]
    assert w.requirement == "R2" and w.worker == "w1" and w.quote == MUL_QUOTE
    sid = s.submit("neg")                                                # 同一棵树再提交：复用结果，门里已没有 MUL
    assert s.g.submits[sid].status == "accepted" and s.g.head == 1
    L = ledger(s.g)
    assert L["guard_checks"] == 1 and [x["test"] for x in L["waived"]] == [MUL]
    assert "Waived regression checks" in ledger_markdown(s.g)
    assert "1 waived" in build_context(s.g, "w1", 24_000, s.now, s.cfg, mode="resume").text
    with pytest.raises(Rejected, match="already waived"):
        s.do(R.waive_checks, "w1", [MUL], MUL_QUOTE, "again")
    s.check_log()


def test_waivers_can_be_disabled_and_are_capped():
    for kw, msg in (({"waivers": False}, "disabled"), ({"waive_max_tests": 0}, "At most 0")):
        s = sim(cfg=fg(locate=False, **kw))
        s.world.define("neg", {MUL: "FAILED"})
        s.submit("neg")
        with pytest.raises(Rejected, match=msg):
            s.do(R.waive_checks, "w1", [MUL], MUL_QUOTE, "x")
        assert not s.g.waived


# ======================================================================== 时间、停滞、运行的结束

def test_deadline_reserve():
    cfg = BelayConfig(reserve_min_sec=300)
    s = sim(cfg=cfg)
    s.advance(5400 - 299)
    s.do(R.tick)
    assert s.g.run.reserve and any(e.kind == "stop_workers" for e in s.effects)
    assert R.next_step(s.g, "w1", s.now, cfg) == ("finalize", "deadline")


def test_stall_hint_and_repeated_rejected_submits():
    cfg = BelayConfig(stall_no_progress_sec=600, reserve_min_sec=10, locate=False, background="off")
    s = sim(cfg=cfg)
    s.advance(601)
    s.do(R.tick)
    assert s.g.stalls[-1].kind == "no_progress" and s.g.stalls[-1].action == "hint"
    for i in range(3):
        s.world.define(f"bad{i}", {MUL: "FAILED"})
        s.submit(f"bad{i}")
    s.do(R.tick)
    st = s.g.stalls[-1]
    assert st.kind == "repeated_failure" and st.action == "hint" and "3 submits" in st.detail
    n = len(s.g.stalls)
    s.do(R.tick)
    assert len(s.g.stalls) == n


def test_session_end_is_not_run_end_and_submits_decide():
    s = sim(cfg=fg(), auto_jobs=False)
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("start_session", "restart")
    s.do(R.start_session, "w1", "restart", {})
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("resume_session", "S2")
    s.world.define("t1", {ADD: "PASSED"})
    s.submit("t1")
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("wait", "checkpoint in progress")
    while s.pending_jobs:
        s.finish_job(s.pending_jobs[0])
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("wait", "submit in progress")     # 复查进行中
    s.review("V1", {"R2": {"implemented": "yes"}, "R3": {"implemented": "yes"}})
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")


def test_sessions_without_progress_stop_the_run():
    s = sim()
    s.do(R.end_session, "w1", "done")
    s.do(R.start_session, "w1", "restart", {})
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "no_progress")


def test_run_done_requires_satisfied_requirements_and_a_confirmed_delivery():
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {ADD: "PASSED"})
    s.submit("t1")
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    s.do(R.begin_finalize, "complete")
    assert s.do(R.promote_now) is False                                   # 模拟器里全量立即完成 → 提升
    assert s.g.checkpoints[1].level == "confirmed" and s.g.confirmed == 1
    assert s.do(R.deliver, "complete") == "DONE" and s.g.run.delivered == 1
    assert s.g.run.status_reasons == () and ledger(s.g)["status_reasons"] == []
    s.check_log()


def test_blocked_requirement_is_incomplete_with_reasons():
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {ADD: "PASSED"})
    s.submit("t1", blocked=[{"requirement": "R3", "kind": "environment", "reason": "no docs tool"}])
    s.do(R.begin_finalize, "complete")
    s.do(R.promote_now)
    assert s.do(R.deliver, "complete") == "INCOMPLETE"
    assert s.g.run.status_reasons == ("R3 is blocked (environment)",)
    assert "not DONE because R3 is blocked" in ledger_markdown(s.g)


def test_unfinished_requirements_and_nothing_delivered():
    s = sim(cfg=fg())
    s.do(R.begin_finalize, "deadline")
    assert s.do(R.deliver, "deadline") == "INCOMPLETE"
    assert s.g.run.status_reasons[0] == "unfinished: R1–R3"


def test_crash_restarts_are_bounded():
    s = sim()
    for _ in range(2):
        s.do(R.end_session, "w1", "crash")
        s.do(R.start_session, "w1", R.session_reason(s.g, "w1"), {})
    s.do(R.end_session, "w1", "crash")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "crashes")


def test_source_discipline_in_log():
    s = sim(cfg=BelayConfig(reviewer=False))
    s.world.define("t1", {ADD: "PASSED"})
    s.snap("t1")
    s.submit("t1")
    assert not check_log(s.log)
    assert all(e.source in ("rule", "observed") for e in s.log
               if e.type in ("requirement_verified", "checkpoint_created", "checkpoint_confirmed", "todo_anchored"))
    assert all(e.source == "self_report" for e in s.log if e.type in ("requirement_submitted", "todos_updated"))
    forged = list(s.log) + [s.log[-1].__class__(len(s.log) + 1, 0, "requirement_verified", "worker:w1",
                                                "self_report", {})]
    assert check_log(forged)


def test_board_and_context_after_a_rejected_submit():
    s = sim(cfg=fg(locate=False))
    s.do(R.update_todos, "w1", [{"content": "fix add in pkg/mod.py", "status": "in_progress"}])
    s.world.define("bad", {ADD: "PASSED", MUL: "FAILED"})
    s.submit("bad")
    s.do(R.record_compaction, "w1", 3, 1000, 200, "Decided to change add() in place because callers rely on it.")
    s.do(R.end_session, "w1", "handoff")
    ctx = build_context(s.g, "w1", 50_000, s.now, s.cfg, mode="resume", blobs={"partial_diff": "+ return a + b"})
    text = ctx.text
    for needle in ("<task>", "- R1 add", "Your last submit was rejected", f"{MUL} (FAILED)",
                   "already fail on the original code", "[~] fix add in pkg/mod.py", "Decided to change add()",
                   "model-written", "+ return a + b", "call submit", "Checklist: open 3"):
        assert needle in text, needle
    assert "Suggested" not in text and "claim" not in text
    keys = [k for k, _ in ctx.sections]
    assert keys[:3] == ["task", "requirements", "pending"] and keys[-1] == "next"
    board = render_board(s.g, "w1", s.now, s.cfg)
    assert "Last submit U1: rejected" in board and "R1 [open]" in board

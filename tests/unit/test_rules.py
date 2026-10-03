"""状态转换规则（纯函数，经由模拟器驱动；每个事务之后都检查不变量）。"""
from __future__ import annotations

import pytest

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import build_context
from belay.core.invariants import check_log, llm_effects
from belay.core.model import REQ_BLOCKED, REQ_DONE, REQ_OPEN
from belay.core.queries import delivery_checkpoint, resume_point
from belay.core.render import ledger, ledger_markdown, render_board, render_submit
from belay.core.rules import Rejected
from tests.sim import APPROVE, MANUAL, Sim, judge

ADD, MUL, Z = "tests/test_mod.py::test_add", "tests/test_mod.py::test_mul", "tests/test_other.py::test_z"
BASE = {ADD: "FAILED", MUL: "PASSED", Z: "PASSED"}
TASK = ("# Notes for version 2.0\n"
        "Fix the add function so that it returns the sum.\n"
        "Also make mul handle negative numbers correctly.\n"
        "Document the new behaviour in the module docstring please.")
PLAN = {"requirements": [{"id": "a", "quote": "Fix the add function so that it returns the sum.", "summary": "add",
                          "checks": [ADD], "acceptance": "tests/test_mod.py::test_add passes"},
                         {"id": "b", "quote": "Also make mul handle negative numbers correctly.", "summary": "mul",
                          "checks": [MUL]},                               # 基线上本来就通过：不是证据
                         {"id": "c", "quote": "Document the new behaviour in the module docstring please.",
                          "summary": "docs", "acceptance": "read the module docstring"}]}
MOD = [("pkg/mod.py", 1, 1)]
OTHER = [("pkg/other.py", 1, 1)]
MUL_QUOTE = "make mul handle negative numbers correctly"
RUN = [{"id": "X1", "cmd": "python -c 'import pkg'", "rc": 0}]


def sim(cfg: BelayConfig | None = None, reviewer=APPROVE, **kw) -> Sim:
    s = Sim(BASE, cfg=cfg or BelayConfig(merge_min_interval_sec=0, merge_todo_interval_sec=0), reviewer=reviewer,
            **kw)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def fg(**kw) -> BelayConfig:
    """关掉后台合并，只看前台（submit）规则。"""
    return BelayConfig(background="off", **kw)


def bgc(**kw) -> BelayConfig:
    kw.setdefault("merge_min_interval_sec", 0)
    kw.setdefault("merge_todo_interval_sec", 0)
    return BelayConfig(**kw)


def reqs(s: Sim) -> dict:
    return {r.id: (r.status, r.level) for r in s.g.requirements.values()}


def last_review(s: Sim):
    return max(s.g.reviews.values(), key=lambda v: v.seq)


# ======================================================================== 准备

def test_setup_baseline_and_plan():
    s = sim()
    g = s.g
    assert g.baseline == {ADD: "fail", MUL: "pass", Z: "pass"}
    assert g.frozen and set(g.requirements) == {"R1", "R2", "R3"}
    assert g.requirements["R1"].checks == (ADD,) and g.requirements["R1"].acceptance.startswith("tests/test_mod")
    assert all(r.status == REQ_OPEN and r.level is None for r in g.requirements.values())
    assert g.head == 0 and g.checkpoints[0].tree == "t0" and g.run.version == 8
    assert not g.degraded


def test_v7_logs_are_refused_with_a_clear_error():
    from belay.core.events import Event, EventError
    from belay.core.reduce import apply
    from belay.core.model import Graph
    old = Event(1, 0.0, "run_started", "runtime", "rule", {"run_id": "r", "task": "t", "budget_sec": 1,
                                                            "deadline_t": 1, "workers": ["w1"]})
    with pytest.raises(EventError, match="before Belay v8"):
        apply(Graph(), old)
    with pytest.raises(EventError, match="v7 code"):
        apply(Graph(), Event(1, 0.0, "checkpoint_created", "runtime", "observed", {}))


# ======================================================================== 后台：回归门 → 复核 → 合并点

def test_background_merge_runs_the_gate_then_the_reviewer():
    s = sim(reviewer=judge(True, {"R3": {"status": "done", "level": "E2", "runs": ["X1"],
                                         "evidence": ["the docstring mentions sums"]}}, summary="fix add, docs"))
    s.world.define("w1", {ADD: "PASSED"})
    s.snap("w1")
    g = s.g
    a = next(iter(g.attempts.values()))
    assert a.lane == "bg" and a.trigger == "auto" and a.selection is None          # 全量回归门
    assert g.jobs[a.jobs[0]].purpose == "gate"
    v = g.reviews["V1"]
    assert v.attempt == a.id and v.status == "decided" and v.focus == ("R1", "R2", "R3")
    assert g.head == 1 and g.checkpoints[1].review == "V1" and g.checkpoints[1].label == "fix add, docs"
    assert reqs(s) == {"R1": (REQ_DONE, "E3"), "R2": (REQ_OPEN, None), "R3": (REQ_DONE, "E2")}
    assert g.requirements["R1"].by == "checks" and g.requirements["R1"].tests == (ADD,)
    assert g.requirements["R3"].by == "review" and g.requirements["R3"].runs == ("X1",)
    assert g.sessions["S1"].progress                                       # 需求完成算进展；合并本身不算
    assert [e.type for e in s.log if e.type in ("merge_reviewed", "review_decided")] == \
        ["merge_reviewed", "review_decided"]
    assert not llm_effects(s.log)
    s.check_log()


def test_merge_itself_is_not_progress():
    s = sim()
    s.world.define("w1", {})
    s.snap("w1")
    assert s.g.head == 1 and not s.g.sessions["S1"].progress


def test_background_reviews_are_throttled_but_handoffs_and_todos_are_not():
    cfg = BelayConfig(merge_min_interval_sec=600, merge_todo_interval_sec=180)
    s = sim(cfg=cfg)
    for t in ("a1", "a2", "a3", "a4"):
        s.world.define(t, {})
    s.snap("a1")
    assert s.g.head == 1
    s.snap("a2")
    assert len(s.g.attempts) == 1                                          # 间隔内不发起后台合并请求
    s.advance(599)
    s.tick()
    assert len(s.g.attempts) == 1
    s.advance(2)
    s.tick()
    assert s.g.head == 2                                                   # 到了间隔：时钟触发
    s.do(R.update_todos, "w1", [{"content": "x", "status": "in_progress"}])
    n = s.snap("a3", reason="todo")
    s.do(R.update_todos, "w1", [{"content": "x", "status": "completed"}], n)
    assert s.g.head == 2
    s.advance(181)
    s.tick()
    assert s.g.head == 3 and s.g.checkpoints[3].trigger == "todo"          # 勾掉 todo：更短的间隔
    s.snap("a4", reason="handoff")
    assert s.g.head == 4 and s.g.checkpoints[4].trigger == "handoff"       # 交接不受间隔限制


def test_gate_only_rejections_do_not_count_against_the_interval():
    s = sim(cfg=BelayConfig(merge_min_interval_sec=600, confirm_regressions=False))
    s.world.define("bad", {MUL: "FAILED"})
    s.snap("bad")
    assert not s.g.reviews and s.g.head == 0                               # 回归门拒绝：没有复核
    s.world.define("ok", {})
    s.snap("ok")
    assert s.g.head == 1                                                   # 马上可以再请求


def test_only_the_latest_snapshot_is_merged_and_tried_trees_are_not_retried():
    s = sim(auto_jobs=False, cfg=bgc(confirm_regressions=False))
    for t in ("a1", "a2", "a3"):
        s.world.define(t, {})
    s.snap("a1")
    a1 = next(a for a in s.g.attempts.values() if a.lane == "bg")
    s.snap("a2")
    s.snap("a3")
    assert len(s.g.attempts) == 1
    s.finish_job(a1.jobs[0])
    nxt = [a for a in s.g.attempts.values() if a.status == "pending"]
    assert [a.tree for a in nxt] == ["a3"]                                 # 跳过 a2：最新的胜出
    s.world.define("bad", {MUL: "FAILED"})
    s.snap("bad")
    s.finish_job(nxt[0].jobs[0])
    bad = next(a for a in s.g.attempts.values() if a.tree == "bad")
    s.finish_job(bad.jobs[0])
    assert s.g.attempts[bad.id].status == "rejected" and s.g.head == 2
    n = len(s.g.attempts)
    s.do(R.schedule_background)
    assert len(s.g.attempts) == n


def test_untestable_latest_falls_back_to_the_previous_testable_snapshot():
    s = sim(auto_jobs=False)
    s.world.define("ok", {})
    s.do(R.record_snapshot, "w1", R.SnapObs("ok", "ok", files=tuple(MOD)), "writes")
    first = next(iter(s.g.attempts.values()))
    s.snap("broken", testable=False)
    s.finish_job(first.jobs[0])
    assert s.g.head == 1 and len(s.g.attempts) == 1


def test_degraded_mode_and_handoff_mode_only_merge_at_handoffs():
    for kw in ({"isolation": {"valid": False, "reason": "x"}}, {}):
        cfg = bgc() if kw else bgc(background="handoff")
        s = Sim(BASE, cfg=cfg)
        s.setup(TASK, PLAN, **kw)
        s.do(R.start_session, "w1", "first", {})
        s.world.define("h1", {})
        s.snap("h1")
        assert not s.g.attempts
        s.snap("h1", reason="session_end")
        assert s.g.head == 1 and s.g.checkpoints[1].trigger == "handoff"


def test_background_gate_rejections_escalate_only_when_the_same_regression_persists():
    s = sim(cfg=bgc(confirm_regressions=False, stall_same_failure=3))
    s.world.define("y0", {Z: "FAILED"})
    s.snap("y0", files=OTHER)
    g = s.g
    assert [a.status for a in g.attempts.values()] == ["rejected"] and g.head == 0
    assert g.wips["w1"].last_rejection is None
    assert not g.persistent and not g.locates and not g.diagnoses
    s.world.define("y1", {Z: "ERROR"})
    s.snap("y1", files=OTHER)
    g = s.g
    assert set(g.persistent) == {Z} and g.persistent[Z].trigger == "background"
    assert [l.trigger for l in g.locates.values()] == ["background"]
    assert [(d.trigger, d.status) for d in g.diagnoses.values()] == [("background", "requested")]
    assert "two background snapshots in a row" in build_context(g, "w1", 50_000, s.now, s.cfg, mode="resume").text
    for i in (2, 3):
        s.world.define(f"y{i}", {Z: "FAILED"})
        s.snap(f"y{i}", files=OTHER)
    assert len(s.g.locates) == 1 and len([e for e in s.log if e.type == "persistent_regression"]) == 1
    s.check_log()


# ======================================================================== 后台：边界快照（勾掉 todo、交接）优先

def boundary_sim(**kw) -> Sim:
    """复核手动给结论、作业手动完成：用来摆出“合并进行中又勾掉 todo”这类交错。兜底 900 s、勾掉 todo 60 s。"""
    kw.setdefault("merge_min_interval_sec", 900)
    kw.setdefault("merge_todo_interval_sec", 60)
    kw.setdefault("confirm_regressions", False)
    return sim(cfg=BelayConfig(**kw), reviewer=MANUAL, auto_jobs=False)


def tick_todo(s: Sim, tree: str, done: list[str], doing: str | None = None, overrides: dict | None = None,
              testable: bool = True) -> int:
    """worker 勾掉 todo：driver 先强制拍一张锚点快照，再把列表镜像到图上。"""
    s.world.define(tree, overrides or {})
    n = s.snap(tree, reason="todo", testable=testable)
    todos = [{"content": t, "status": "completed"} for t in done]
    if doing:
        todos.append({"content": doing, "status": "in_progress"})
    s.do(R.update_todos, "w1", todos, n)
    return n


def edit(s: Sim, tree: str, overrides: dict | None = None, **kw) -> int:
    s.world.define(tree, overrides or {})
    return s.snap(tree, **kw)


def bg_open(s: Sim):
    return next((a for a in s.g.attempts.values() if a.lane == "bg" and a.status in ("pending", "advancing")), None)


def finish(s: Sim, merge: bool = True) -> None:
    """跑完进行中的后台请求（只跑它自己的作业）：回归门 → 复核结论。"""
    aid = bg_open(s).id
    while True:
        running = [j for j in s.g.attempts[aid].jobs if s.g.jobs[j].state == "running"]
        if not running:
            break
        s.finish_job(running[0])
    vid = s.running_review()
    if vid is not None:
        s.review(vid, judge(merge, feedback="" if merge else "the parser is half done")(s, s.g.reviews[vid]))


def bg_requests(s: Sim) -> list[tuple[str, str]]:
    return [(a.tree, a.trigger) for a in sorted(s.g.attempts.values(), key=lambda a: a.created_seq)
            if a.lane == "bg"]


def test_todo_anchor_is_merged_even_after_the_worker_kept_editing():
    s = boundary_sim()
    edit(s, "a1")
    assert bg_requests(s) == [("a1", "auto")]                              # 第一次：没有可比的复核，不节流
    tick_todo(s, "t1", ["parse the header"], "parse the body")              # a1 还在合并
    edit(s, "half1")
    edit(s, "half2")                                                       # worker 接着改：中间态
    assert len(s.g.attempts) == 1                                          # 进行中的不抢占、不排队
    finish(s)
    assert s.g.head == 1
    s.advance(61)
    s.tick()
    assert bg_requests(s)[-1] == ("t1", "todo")                            # 选勾掉 todo 的那张，不是最新的 half2
    assert bg_open(s).summary == "parse the header"
    finish(s)
    g = s.g
    assert g.checkpoints[g.head].tree == "t1" and g.todos["P1"].status == "anchored"
    s.advance(61)
    s.tick()
    assert bg_open(s) is None                                              # half2 只能等兜底间隔
    s.advance(900)
    s.tick()
    assert bg_requests(s)[-1] == ("half2", "auto")
    s.check_log()


def test_todos_ticked_during_a_merge_coalesce_into_one_request_for_the_newest():
    s = boundary_sim()
    edit(s, "a1")
    tick_todo(s, "t1", ["header"], "body")
    edit(s, "mid")
    tick_todo(s, "t2", ["header", "body"], "footer")
    edit(s, "mid2")
    tick_todo(s, "t3", ["header", "body", "footer"])
    edit(s, "after")
    finish(s)
    s.advance(61)
    s.tick()
    reqs_ = bg_requests(s)
    assert reqs_ == [("a1", "auto"), ("t3", "todo")]                       # 攒下的三个合成一次：t3 包含 t1、t2
    assert bg_open(s).summary == "header; body; footer"
    finish(s)
    g = s.g
    assert {t.title: t.status for t in g.todos.values()} == {"header": "anchored", "body": "anchored",
                                                              "footer": "anchored"}
    assert all(g.todos[t].checkpoint == g.head for t in g.todos)
    s.check_log()


def test_a_newer_todo_never_preempts_a_running_background_review():
    s = boundary_sim()
    edit(s, "a1")
    finish(s)
    s.advance(61)
    tick_todo(s, "t1", ["one"], "two")
    a = bg_open(s)
    assert (a.tree, a.trigger) == ("t1", "todo")
    for jid in a.jobs:
        s.finish_job(jid)
    vid = s.running_review()
    assert vid is not None
    for i in range(3):                                                     # worker 勾 todo 比复核快
        s.advance(120)
        tick_todo(s, f"t{i + 2}", ["one", "two"] + [f"x{k}" for k in range(i + 1)], f"x{i + 1}")
        s.tick()
    assert s.running_review() == vid and bg_open(s).id == a.id
    assert not [e for e in s.log if e.type in ("merge_superseded", "review_cancelled")]
    s.review(vid, APPROVE(s, s.g.reviews[vid]))
    assert s.g.checkpoints[s.g.head].tree == "t1"                          # 链头前进了：没有饥饿
    s.tick()
    assert bg_requests(s)[-1] == ("t4", "todo")                            # 复核早已超过 60 s：马上接最新的 todo
    s.check_log()


def test_submit_still_preempts_background_and_older_todos_are_not_revisited():
    s = boundary_sim()
    edit(s, "a1")
    finish(s)
    s.advance(61)
    tick_todo(s, "t1", ["one"], "two")
    bg = bg_open(s)
    for jid in bg.jobs:
        s.finish_job(jid)
    bg_review = s.running_review()
    tick_todo(s, "t2", ["one", "two"], "three")
    s.world.define("sub", {})
    sid = s.submit("sub")
    g = s.g
    assert g.attempts[bg.id].status == "superseded" and g.reviews[bg_review].status == "cancelled"
    fg_a = next(a for a in g.attempts.values() if a.lane == "fg")
    assert fg_a.tree == "sub" and bg_open(s) is None                       # t2 早于 submit 的快照：归前台
    for jid in fg_a.jobs:
        s.finish_job(jid)
    s.review(s.running_review(), APPROVE(s, s.g.reviews[s.running_review()]))
    g = s.g
    assert g.checkpoints[g.head].tree == "sub" and g.submits[sid].status != "pending"
    assert {t.title: t.status for t in g.todos.values()} == {"one": "anchored", "two": "anchored",
                                                              "three": "in_progress"}
    s.advance(61)
    s.tick()
    assert bg_open(s) is None and [t for t, _ in bg_requests(s)].count("t2") == 0
    tick_todo(s, "t3", ["one", "two", "three"])
    assert bg_requests(s)[-1] == ("t3", "todo")                            # submit 之后的 todo 照常触发
    s.check_log()


def test_auto_is_only_a_fallback_after_a_long_stretch_without_todos():
    s = boundary_sim()
    edit(s, "a1")
    finish(s)
    edit(s, "w1")
    s.advance(600)
    s.tick()
    assert bg_open(s) is None                                              # 旧的 600 s 不再触发
    edit(s, "w2")
    s.advance(299)
    s.tick()
    assert bg_open(s) is None
    s.advance(2)
    s.tick()
    assert bg_requests(s)[-1] == ("w2", "auto")                            # 兜底：最新快照
    finish(s)
    edit(s, "w3")
    s.advance(100)
    tick_todo(s, "t1", ["done thing"])
    assert bg_requests(s)[-1] == ("t1", "todo")                            # 兜底计时没到，勾掉 todo 照样马上合并
    s.check_log()


def test_todo_interval_only_guards_against_back_to_back_ticks():
    s = boundary_sim()
    edit(s, "a1")
    finish(s)
    s.advance(10)
    tick_todo(s, "t1", ["trivial"])
    assert bg_open(s) is None                                              # 距上一次复核开始不到 60 s
    edit(s, "later")
    s.advance(51)
    s.tick()
    assert bg_requests(s)[-1] == ("t1", "todo")                            # 时钟补发，选的仍是锚点
    s.check_log()


def test_rejected_todo_snapshot_is_not_retried_and_older_anchors_are_not_revisited():
    s = boundary_sim()
    edit(s, "a1")
    tick_todo(s, "t1", ["one"], "two")
    tick_todo(s, "t2", ["one", "two"], "three")
    finish(s)
    s.advance(61)
    s.tick()
    assert bg_requests(s)[-1] == ("t2", "todo")
    finish(s, merge=False)                                                 # 复核不批准 t2
    g = s.g
    assert g.head == 1 and {t.title: t.status for t in g.todos.values()}["one"] == "completed"
    s.advance(61)
    s.tick()
    assert bg_open(s) is None                                              # 不回退去试 t1（反馈已给 worker）
    tick_todo(s, "t3", ["one", "two", "three"])
    assert bg_requests(s)[-1] == ("t3", "todo")
    finish(s)
    assert all(t.status == "anchored" for t in s.g.todos.values())        # t3 包含 t1、t2 的锚点
    s.check_log()


def test_gate_rejected_todo_does_not_block_the_next_one_and_untestable_anchor_falls_back():
    s = boundary_sim()
    edit(s, "a1")
    finish(s)
    s.advance(61)
    tick_todo(s, "t1", ["one"], "two", overrides={MUL: "FAILED"})
    finish(s)
    t1 = next(a for a in s.g.attempts.values() if a.tree == "t1")
    assert t1.status == "rejected" and t1.reason == "regression" and s.g.todos["P1"].status == "completed"
    tick_todo(s, "t2", ["one", "two"], "three")                            # 被回归门拒绝的锚点不挡后面的 todo
    assert bg_requests(s)[-1] == ("t2", "todo")
    finish(s)
    assert s.g.todos["P1"].status == "anchored"                            # t2 包含 t1 的锚点
    s.advance(61)
    tick_todo(s, "t3", ["one", "two", "three"], "four")
    a = bg_open(s)
    for jid in a.jobs:
        s.finish_job(jid)
    tick_todo(s, "t4", ["one", "two", "three", "four"], "five", testable=False)   # 编译不过
    s.review(s.running_review(), APPROVE(s, s.g.reviews[s.running_review()]))
    tick_todo(s, "t5", ["one", "two", "three", "four", "five"], testable=False)
    s.advance(61)
    s.tick()
    assert bg_open(s) is None                                              # t4、t5 都不可测，链头已含 t3
    s.check_log()


def test_untestable_newest_todo_falls_back_to_the_previous_anchor():
    s = boundary_sim()
    edit(s, "a1")
    tick_todo(s, "t1", ["one"], "two")
    tick_todo(s, "t2", ["one", "two"], "three", testable=False)
    edit(s, "after")
    finish(s)
    s.advance(61)
    s.tick()
    assert bg_requests(s)[-1] == ("t1", "todo") and bg_open(s).summary == "one"
    s.check_log()


def test_a_revert_snapshot_is_a_barrier_for_older_anchors():
    s = boundary_sim()
    edit(s, "a1")
    tick_todo(s, "t1", ["one"], "two", overrides={Z: "FAILED"})            # 带着坏改动勾掉了 todo
    edit(s, "rv", reason="revert")                                         # worker 撤回了定位到的坏改动
    edit(s, "after")
    finish(s)
    s.advance(61)
    s.tick()
    assert bg_open(s) is None                                              # 不合并 revert 之前的锚点
    s.advance(900)
    s.tick()
    assert bg_requests(s)[-1] == ("after", "auto")
    s.check_log()


def test_handoff_is_a_boundary_too_and_the_newest_boundary_wins():
    s = boundary_sim()
    edit(s, "a1")
    tick_todo(s, "t1", ["one"], "two")
    edit(s, "h1", reason="handoff")
    edit(s, "mid")
    finish(s)
    assert bg_requests(s)[-1] == ("h1", "handoff")                         # 交接不受间隔限制，且比 t1 新
    finish(s)
    assert s.g.todos["P1"].status == "anchored"
    s.advance(5)
    edit(s, "h2", reason="handoff")
    tick_todo(s, "t2", ["one", "two"])
    s.tick()
    assert bg_requests(s)[-1] == ("h2", "handoff")                         # 间隔内：交接马上合并
    finish(s)
    s.advance(61)
    s.tick()
    assert bg_requests(s)[-1] == ("t2", "todo")                            # 更新的 t2 随后照常
    s.check_log()


def test_auto_does_not_fall_back_to_an_older_intermediate_state():
    s = boundary_sim()
    edit(s, "a1")
    finish(s)
    edit(s, "x")
    s.snap("a1")                                                           # worker 又改回了链头
    s.advance(901)
    s.tick()
    assert bg_open(s) is None                                              # 不合并中间的 x
    s.check_log()


def test_handoff_mode_ignores_todo_anchors():
    s = boundary_sim(background="handoff")
    edit(s, "a1")
    tick_todo(s, "t1", ["one"])
    s.advance(2000)
    s.tick()
    assert not s.g.attempts
    edit(s, "h1", reason="session_end")
    assert bg_requests(s) == [("h1", "handoff")]


def test_without_a_reviewer_todos_still_win_over_newer_edits():
    s = sim(cfg=bgc(reviewer=False, confirm_regressions=False), auto_jobs=False)
    edit(s, "a1")
    tick_todo(s, "t1", ["one"], "two")
    edit(s, "mid", {MUL: "FAILED"})                                        # 中间态还带着回归
    finish(s)
    assert bg_requests(s)[-1] == ("t1", "todo")
    finish(s)
    assert s.g.checkpoints[s.g.head].tree == "t1"
    s.check_log()


# ======================================================================== 复核者：证据等级、单调、分数

def test_evidence_levels_are_validated_and_downgraded():
    verdict = judge(True, {
        "R1": {"status": "done", "level": "E3", "tests": [ADD]},           # 原始代码上失败、现在通过：E3
        "R2": {"status": "done", "level": "E3", "tests": [MUL]},           # 原始代码上就通过：证明不了 → E2 → E1
        "R3": {"status": "done", "level": "E2", "runs": ["X9"]},           # 引用了不存在的命令 → E1
    })
    s = sim(reviewer=verdict, cfg=bgc(), runs=RUN)
    s.world.define("w1", {ADD: "PASSED"})
    s.snap("w1")
    assert reqs(s) == {"R1": (REQ_DONE, "E3"), "R2": (REQ_DONE, "E1"), "R3": (REQ_DONE, "E1")}
    s = sim(reviewer=judge(True, {"R3": {"status": "done", "level": "E0"}}))
    s.world.define("w1", {})
    s.snap("w1")
    r3 = s.g.requirements["R3"]
    assert r3.status == REQ_OPEN and r3.judgement == "partial" and "own claim" in r3.missing[-1]
    s = sim(reviewer=judge(True, {"R3": {"status": "done", "level": "E2", "runs": ["X1"]}}), runs=[])
    s.world.define("w1", {})
    s.snap("w1")
    assert reqs(s)["R3"] == (REQ_DONE, "E1")                               # 这次复核没有执行任何命令


def test_done_requirements_are_sticky_and_breaking_one_blocks_the_merge():
    s = sim(reviewer=judge(True, {"R3": ("done", "E1")}))
    s.world.define("w1", {})
    s.snap("w1")
    assert reqs(s)["R3"] == (REQ_DONE, "E1")
    # 只凭阅读说“没做完”：不改变已完成的需求，照常合并
    s.reviewer = judge(True, {"R3": {"status": "partial", "level": "E1", "missing": ["no example"]}})
    s.world.define("w2", {})
    s.snap("w2")
    assert s.g.head == 2 and reqs(s)["R3"] == (REQ_DONE, "E1")
    assert any("by reading only" in n for n in last_review(s).decision["notes"])
    # 有运行证据、且是这次改动弄坏的：不合并
    s.reviewer = judge(True, {"R3": {"status": "not_done", "level": "E2", "runs": ["X1"], "regressed": True,
                                     "missing": ["the docstring was deleted"]}})
    s.world.define("w3", {})
    s.snap("w3")
    a = max(s.g.attempts.values(), key=lambda a: a.created_seq)
    assert a.status == "rejected" and a.reason == "review" and s.g.head == 2
    assert "this change breaks it" in a.detail and reqs(s)["R3"] == (REQ_DONE, "E1")
    assert s.g.wips["w1"].last_rejection["reason"] == "review"             # 复核不通过：告诉 worker
    # 有运行证据、但不是这次改动弄坏的（重新评估）：合并，需求退回
    s.reviewer = judge(True, {"R3": {"status": "partial", "level": "E2", "runs": ["X1"], "missing": ["mul"]}})
    s.world.define("w4", {})
    s.snap("w4")
    r3 = s.g.requirements["R3"]
    assert s.g.head == 3 and r3.status == REQ_OPEN and r3.reason == "reassessed" and r3.missing == ("mul",)
    s.check_log()


def test_tests_behind_an_e3_requirement_must_keep_passing():
    s = sim(cfg=bgc(confirm_regressions=False))
    s.world.define("w1", {ADD: "PASSED"})
    s.snap("w1")
    assert reqs(s)["R1"] == (REQ_DONE, "E3")
    s.world.define("w2", {})                                               # ADD 又失败了
    sid = s.submit("w2")
    sub = s.g.submits[sid]
    assert sub.status == "rejected" and sub.reason == "requirement_regression" and s.g.head == 1
    assert "R1 was done" in render_submit(s.g, sid)
    assert any(l.trigger == "rejected" and ADD in l.tests for l in s.g.locates.values())


def test_score_never_drops_along_the_merge_chain():
    s = sim(reviewer=judge(True, score=0.50, score_note="python bench.py"), runs=RUN)
    s.world.define("w1", {})
    s.snap("w1")
    assert s.g.checkpoints[1].score == 0.5 and s.g.checkpoints[1].score_note == "python bench.py"
    s.reviewer = judge(True, score=0.40)
    s.world.define("w2", {})
    s.snap("w2")
    a = max(s.g.attempts.values(), key=lambda a: a.created_seq)
    assert a.status == "rejected" and "score dropped from 0.5" in a.detail and s.g.head == 1
    s.reviewer = judge(True, score=0.495)                                  # 容差之内（测量噪声）
    s.world.define("w3", {})
    s.snap("w3")
    assert s.g.head == 2
    s.reviewer = judge(True, score=None)                                   # 没测分数：门槛不清零
    s.world.define("w4", {})
    s.snap("w4")
    s.reviewer = judge(True, score=0.3)
    s.world.define("w5", {})
    s.snap("w5")
    assert s.g.head == 3 and s.g.checkpoints[3].score is None
    assert delivery_checkpoint(s.g) == s.g.head
    s2 = sim(reviewer=judge(True, score=0.9), runs=[])
    s2.world.define("w1", {})
    s2.snap("w1")
    assert s2.g.checkpoints[1].score is None                              # 没有执行命令的分数不算


def test_reviewer_not_approving_tells_the_worker_and_repeats_raise_a_stall_hint():
    s = sim(reviewer=judge(False, reason="debug prints left in pkg/mod.py", feedback="remove the prints"),
            cfg=bgc(stall_same_failure=2))
    for i in range(2):
        s.world.define(f"n{i}", {})
        s.snap(f"n{i}")
    assert s.g.head == 0
    rej = s.g.wips["w1"].last_rejection
    assert rej["reason"] == "review" and "debug prints" in rej["detail"]
    text = build_context(s.g, "w1", 50_000, s.now, s.cfg, mode="resume").text
    assert "The reviewer did not merge your snapshot" in text and "remove the prints" in text
    s.tick()
    st = s.g.stalls[-1]
    assert st.kind == "review_rejections" and "2 merge requests in a row" in st.detail
    n = len(s.g.stalls)
    s.tick()
    assert len(s.g.stalls) == n


def test_reviewer_failure_is_retried_then_the_gate_alone_decides():
    s = sim(reviewer=MANUAL, cfg=fg())
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1")
    s.review("V1", None, failed=True)
    assert s.running_review() == "V2" and s.g.reviews["V2"].retry_of == "V1"
    s.review("V2", {"merge": True})                                       # 没有判定任何需求也可以合并
    g = s.g
    assert g.head == 1 and g.checkpoints[1].review == "V2" and g.submits[sid].status == "returned"
    s.world.define("t2", {ADD: "PASSED"})
    sid = s.submit("t2")
    s.review(s.running_review(), {"no": "verdict"})                       # 没说合不合并：失败
    s.review(s.running_review(), None, failed=True)
    g = s.g
    assert g.head == 2 and g.checkpoints[2].review is None                 # 复核者不可用：只按回归门合并
    assert g.submits[sid].status == "accepted"                             # 自述（E0）
    assert reqs(s)["R2"] == (REQ_DONE, "E0") and g.requirements["R2"].by == "self_report"
    assert s.do(R.deliver, "complete") == "INCOMPLETE"
    assert any("only self-reported" in x for x in s.g.run.status_reasons)
    s.check_log()


# ======================================================================== 提交：请求立即复核

def test_submit_supersedes_a_background_review_and_reuses_the_gate():
    s = sim(reviewer=MANUAL, cfg=bgc())
    s.world.define("t1", {ADD: "PASSED"})
    s.snap("t1")
    bg = next(iter(s.g.attempts.values()))
    assert s.running_review() == "V1" and s.g.reviews["V1"].attempt == bg.id
    sid = s.submit("t1", summary="all done")                               # 同一棵树
    g = s.g
    assert g.attempts[bg.id].status == "superseded" and g.reviews["V1"].status == "cancelled"
    assert "V1" in s.cancelled_reviews
    fa = g.submits[sid].attempt
    assert g.reviews["V2"].attempt == fa and g.reviews["V2"].trigger == "submit"
    assert len([j for j in g.jobs.values() if j.purpose == "gate"]) == 1   # 回归门的结果按树复用
    s.review("V2", judge(True, {"R2": ("done", "E1"), "R3": ("done", "E1")})(s, g.reviews["V2"]))
    assert s.g.submits[sid].status == "accepted" and s.g.head == 1
    assert "accepted" in render_submit(s.g, sid) and "Done (E3, tests): R1" in render_submit(s.g, sid)
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")


def test_submit_returns_what_is_missing_then_accepts():
    s = sim(cfg=fg(), reviewer=judge(True, {"R2": ("done", "E1"),
                                            "R3": {"status": "partial", "level": "E1", "missing": ["docstring"]}},
                                     feedback="write the docstring of add"))
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1", summary="fixed add, mul and docs")
    g = s.g
    assert g.submits[sid].status == "returned" and g.submits[sid].open == ("R3",)
    text = render_submit(g, sid)
    assert "merged as merge point 1" in text and "docstring" in text and "write the docstring of add" in text
    assert R.next_step(g, "w1", s.now, s.cfg) == ("resume_session", "S1")
    s.reviewer = judge(True, {"R3": ("done", "E1")})
    s.world.define("t2", {ADD: "PASSED"})
    sid2 = s.submit("t2")
    assert s.g.submits[sid2].status == "accepted"
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    s.do(R.begin_finalize, "complete")
    assert s.do(R.deliver, "complete") == "DONE" and s.g.run.delivered == 2
    s.check_log()


def test_submit_without_changes_asks_the_reviewer_only_for_what_was_not_judged():
    s = sim(cfg=bgc(), reviewer=judge(True, {"R2": ("done", "E1")}))
    s.world.define("t1", {ADD: "PASSED"})
    s.snap("t1")                                                           # 后台：R1（测试）、R2 完成，R3 没提到
    assert s.g.head == 1 and reqs(s)["R3"] == (REQ_OPEN, None)
    s.reviewer = judge(False, {"R3": {"status": "not_done", "level": "E1", "missing": ["no docstring"]}})
    sid = s.submit("t1")                                                   # 快照就是链头：只判定、不合并
    g = s.g
    v = g.reviews[g.submits[sid].review]
    assert v.trigger == "judge" and v.attempt is None and v.checkpoint == 1 and v.decision["merge"] is None
    assert g.submits[sid].status == "returned" and g.requirements["R3"].missing == ("no docstring",)
    assert "No new changes since merge point 1" in render_submit(g, sid)
    n = len(g.reviews)
    sid2 = s.submit("t1")                                                  # 同一棵树上都判过了：直接按账本回答
    assert s.g.submits[sid2].status == "returned" and len(s.g.reviews) == n


def test_blocked_declarations_are_judged_by_the_reviewer():
    blocked = [{"requirement": "R3", "kind": "insufficient_info", "reason": "which docstring?"}]
    s = sim(cfg=fg(), reviewer=judge(True, {"R2": ("done", "E1"),
                                            "R3": {"status": "blocked", "level": "E1",
                                                   "reason": "the task does not say which module"}}))
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1", blocked=blocked)
    g = s.g
    r3 = g.requirements["R3"]
    assert g.submits[sid].status == "accepted" and r3.status == REQ_BLOCKED and r3.blocked_kind == "insufficient_info"
    assert r3.by == "review"
    s.do(R.begin_finalize, "complete")
    assert s.do(R.deliver, "complete") == "DONE"                           # 受阻且复核者认可：算完成
    s = sim(cfg=fg(), reviewer=judge(True, {"R2": ("done", "E1"),
                                            "R3": {"status": "not_done", "level": "E1",
                                                   "missing": ["document add and mul in pkg/mod.py"]}}))
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1", blocked=blocked)
    r3 = s.g.requirements["R3"]
    assert s.g.submits[sid].status == "returned" and r3.status == REQ_OPEN and r3.reason == "blocked_not_accepted"
    assert "did not accept that it is blocked" in render_submit(s.g, sid)


def test_reviewer_off_records_self_reports():
    s = sim(cfg=fg(reviewer=False))
    s.world.define("t1", {})
    sid = s.submit("t1", blocked=[{"requirement": "R3", "kind": "environment", "reason": "no docs tool"}])
    g = s.g
    assert g.submits[sid].status == "returned" and g.submits[sid].open == ("R1",)    # 证据检查没过
    assert reqs(s) == {"R1": (REQ_OPEN, None), "R2": (REQ_DONE, "E0"), "R3": (REQ_BLOCKED, None)}
    assert ADD in g.requirements["R1"].missing[0] and not g.reviews
    s.world.define("t2", {ADD: "PASSED"})
    sid = s.submit("t2")
    assert s.g.submits[sid].status == "accepted" and reqs(s)["R1"] == (REQ_DONE, "E3")
    s.do(R.begin_finalize, "complete")
    assert s.do(R.deliver, "complete") == "INCOMPLETE"
    reasons = s.g.run.status_reasons
    assert any("only self-reported" in x and "R2" in x for x in reasons)
    assert any("R3 is blocked (environment, self-reported)" in x for x in reasons)
    assert ledger(s.g)["category_requirements"]["self-reported"] == ["R2"]


def test_submit_regression_is_rejected_with_reasons_locate_and_diagnosis():
    s = sim(cfg=fg(confirm_regressions=False))
    s.world.define("g1", {})
    s.snap("g1", files=OTHER)
    s.world.define("bad", {ADD: "PASSED", Z: "FAILED"})
    sid = s.submit("bad", files=OTHER, dropped=("tests/test_other.py",))
    g = s.g
    sub = g.submits[sid]
    assert sub.status == "rejected" and sub.reason == "regression" and g.head == 0 and not g.reviews
    assert reqs(s)["R1"] == (REQ_OPEN, None)
    text = render_submit(g, sid)
    assert f"assert failure in {Z}" in text and "ran in their original version" in text
    loc = next(iter(g.locates.values()))
    assert loc.trigger == "rejected" and loc.results
    d = next(iter(g.diagnoses.values()))
    s.do(R.record_diagnosis, d.id, {"suspects": [], "intentional": {"likely": True, "quote": "made up text"},
                                    "suggestion": "x"})
    assert s.g.diagnoses[d.id].result["intentional"]["likely"] is False
    s.world.define("bad2", {Z: "FAILED", MUL: "PASSED"})
    s.submit("bad2", files=OTHER)
    rep = [x for x in s.g.diagnoses.values() if x.trigger == "repeated"]
    assert rep and rep[0].previous == d.id
    assert not llm_effects(s.log)


def test_missing_or_skipped_guard_tests_are_regressions():
    s = sim(cfg=fg())
    s.world.define("skip", {MUL: "SKIPPED"})
    sid = s.submit("skip", files=[("pkg/mod.py", 1, 0)])
    assert s.g.submits[sid].status == "rejected"
    s.world.trees["gone"] = {ADD: "FAILED", Z: "PASSED"}
    sid = s.submit("gone", files=[("setup.py", 1, 0)])
    a = s.g.attempts[s.g.submits[sid].attempt]
    assert a.regressions == (f"{MUL} (MISSING)",)


def test_flaky_failure_confirmed_as_flaky_is_not_a_regression():
    s = sim(cfg=fg())
    s.world.define("t1", {})
    s.world.flaky_once.add(("t1", MUL))
    sid = s.submit("t1")
    a = s.g.attempts[s.g.submits[sid].attempt]
    assert a.status == "created" and a.flaky == (MUL,)


def test_submit_precheck_failure_is_rejected_at_once():
    s = sim(cfg=fg())
    sid = s.submit("broken", testable=False)
    sub = s.g.submits[sid]
    assert sub.status == "rejected" and sub.reason == "precheck" and not s.g.reviews
    assert "do not compile" in render_submit(s.g, sid)
    s2 = sim(cfg=fg(), auto_jobs=False)
    s2.world.define("t1", {})
    s2.submit("t1")
    with pytest.raises(Rejected, match="still being reviewed"):
        s2.submit("t1")


def test_blocked_list_and_waiver_proposals_are_validated():
    s = sim(cfg=fg())
    s.world.define("t1", {ADD: "PASSED"})
    with pytest.raises(Rejected, match="kind must be"):
        s.submit("t1", blocked=[{"requirement": "R3", "kind": "lazy", "reason": "x"}])
    with pytest.raises(Rejected, match="not a requirement"):
        s.submit("t1", blocked=[{"requirement": "R9", "kind": "environment", "reason": "x"}])
    with pytest.raises(Rejected, match="verbatim"):
        s.submit("t1", blocked=[{"requirement": "R2", "kind": "check_conflict", "reason": "x", "quote": "nope"}])
    with pytest.raises(Rejected, match="not in the regression gate"):
        s.submit("t1", waivers=[{"tests": [ADD], "quote": MUL_QUOTE, "reason": "x"}])
    with pytest.raises(Rejected, match="verbatim"):
        s.submit("t1", waivers=[{"tests": [MUL], "quote": "mul must change", "reason": "x"}])
    s3 = sim(cfg=fg(waivers=False))
    s3.world.define("t1", {})
    with pytest.raises(Rejected, match="disabled"):
        s3.submit("t1", waivers=[{"tests": [MUL], "quote": MUL_QUOTE, "reason": "x"}])


# ======================================================================== 回归门豁免：worker 提议，复核者裁决

def test_waivers_are_proposed_by_the_worker_and_granted_by_the_reviewer():
    waiver = {"tests": [MUL], "quote": MUL_QUOTE, "reason": "the old test asserts the old sign", "requirement": "R2"}
    s = sim(cfg=fg(locate=False), reviewer=MANUAL)
    s.world.define("neg", {MUL: "FAILED", ADD: "PASSED"})
    sid = s.submit("neg")
    assert s.g.submits[sid].status == "rejected" and not s.g.reviews       # 没有提议：直接拒绝，不开复核
    sid = s.submit("neg", waivers=[waiver])
    v = s.g.reviews[s.running_review()]
    assert v.gate["regressions"] == [f"{MUL} (FAILED)"]
    s.review(v.id, judge(True, {"R2": ("done", "E1")},
                         waivers=[{"tests": [MUL], "quote": "mul must change", "reason": "x"}])(s, v))
    a = s.g.attempts[s.g.submits[sid].attempt]
    assert a.status == "rejected" and a.reason == "review" and "not waived" in a.detail   # 引文不逐字：不采纳
    assert not s.g.waived
    sid = s.submit("neg", waivers=[waiver])
    v = s.g.reviews[s.running_review()]
    s.review(v.id, judge(True, {"R2": ("done", "E1")}, waivers=[waiver])(s, v))
    g = s.g
    assert g.submits[sid].status == "returned" and g.head == 1
    w = g.waived[MUL]
    assert w.review == v.id and w.requirement == "R2" and w.quote == MUL_QUOTE
    L = ledger(g)
    assert L["guard_checks"] == 1 and [x["test"] for x in L["waived"]] == [MUL] and L["waived"][0]["review"] == v.id
    assert "Waived regression checks" in ledger_markdown(g)
    assert "1 waived by the reviewer" in build_context(g, "w1", 24_000, s.now, s.cfg, mode="resume").text
    s.check_log()


def test_waivers_are_capped():
    s = sim(cfg=fg(locate=False, waive_max_tests=0), reviewer=MANUAL)
    s.world.define("neg", {MUL: "FAILED"})
    s.submit("neg", waivers=[{"tests": [MUL], "quote": MUL_QUOTE, "reason": "x"}])
    v = s.g.reviews[s.running_review()]
    s.review(v.id, judge(True, waivers=[{"tests": [MUL], "quote": MUL_QUOTE, "reason": "x"}])(s, v))
    assert not s.g.waived and any("at most 0" in n for n in s.g.reviews[v.id].decision["notes"])


# ======================================================================== 没有回归门（LHTB）

def test_without_tests_merges_rest_on_the_reviewer_and_the_score():
    s = Sim({}, cfg=bgc(), reviewer=judge(True, {"R3": {"status": "done", "level": "E2", "runs": ["X1"]}},
                                          score=0.3, score_note="python eval.py --metric f1"))
    s.setup(TASK, PLAN, verifier=False)
    s.do(R.start_session, "w1", "first", {})
    assert not s.g.baseline and s.g.requirements["R1"].checks == ()
    s.snap("w1")
    g = s.g
    a = next(iter(g.attempts.values()))
    assert a.selection == () and not g.jobs                               # 没有回归门作业
    assert g.head == 1 and g.checkpoints[1].score == 0.3 and reqs(s)["R3"] == (REQ_DONE, "E2")
    assert "No tests are available" in build_context(g, "w1", 24_000, s.now, s.cfg).text
    s.reviewer = judge(True, score=0.2)
    s.snap("w2")
    assert s.g.head == 1                                                   # 分数下降：不合并
    s.reviewer = judge(True, score=0.6)
    s.snap("w3")
    assert s.g.head == 2 and delivery_checkpoint(s.g) == 2
    assert "score 0.6" in ledger_markdown(s.g)


# ======================================================================== todo

def test_todos_are_mirrored_completed_and_anchored():
    s = sim(cfg=bgc(confirm_regressions=False))
    todos = [{"content": "read the code", "status": "in_progress"},
             {"content": "fix add (R1)", "status": "pending"}]
    s.do(R.update_todos, "w1", todos)
    g = s.g
    assert [(t.id, t.status, t.requirements) for t in sorted(g.todos.values(), key=lambda t: t.n)] == \
        [("P1", "in_progress", ()), ("P2", "pending", ("R1",))]
    n = len(s.log)
    s.do(R.update_todos, "w1", todos)
    assert len(s.log) == n
    done = [{"content": "read the code", "status": "completed"}, {"content": "fix add (R1)", "status": "in_progress"}]
    assert R.newly_completed(s.g, done)
    s.world.define("td", {})
    n = s.snap("td", reason="todo")
    assert s.do(R.update_todos, "w1", done, n) == ["P1"]
    g = s.g
    assert g.todos["P1"].status == "anchored" and g.todos["P2"].status == "in_progress"
    assert g.checkpoints[g.todos["P1"].checkpoint].trigger == "todo"
    assert all(r.status == REQ_OPEN for r in g.requirements.values())    # todo 只是线索：不改变需求状态
    assert resume_point(g, "w1") == {"base": g.head, "partial": n, "todo": "P2", "todos": ["P2"]}
    s.do(R.update_todos, "w1", [{"content": "something else", "status": "pending"}])
    assert set(s.g.todos) == {"P1", "P3"}


def test_todo_completed_on_a_rejected_snapshot_is_anchored_by_a_later_merge():
    s = sim(cfg=bgc(confirm_regressions=False))
    s.do(R.update_todos, "w1", [{"content": "a", "status": "in_progress"}])
    s.world.define("sb", {MUL: "FAILED"})
    n = s.snap("sb", reason="todo")
    s.do(R.update_todos, "w1", [{"content": "a", "status": "completed"}], n)
    assert s.g.todos["P1"].status == "completed"
    s.world.define("sc", {})
    s.snap("sc")
    assert s.g.todos["P1"].status == "anchored"


def test_rollback_reopens_requirements_and_invalidates_todos():
    s = sim(cfg=bgc(confirm_regressions=False, reviewer=False))
    s.world.define("k1", {})
    s.snap("k1")
    s.do(R.update_todos, "w1", [{"content": "fix add", "status": "in_progress"}])
    s.world.define("k2", {ADD: "PASSED"})
    n = s.snap("k2", reason="todo")
    s.do(R.update_todos, "w1", [{"content": "fix add", "status": "completed"}], n)
    assert reqs(s)["R1"] == (REQ_DONE, "E3") and s.g.todos["P1"].status == "anchored"
    with pytest.raises(Rejected):
        s.do(R.rollback, "w1", 7)
    s.do(R.rollback, "w1", 1)
    g = s.g
    assert g.head == 1 and g.epoch == 1 and g.requirements["R1"].status == REQ_OPEN
    assert g.requirements["R1"].reason == "rolled_back" and g.todos["P1"].status == "in_progress"
    assert any(e.kind == "restore_workspace" and e.args["reset_ref"] for e in s.effects)


# ======================================================================== 模块 D：快照二分

def _timeline(s: Sim, statuses: list[str]) -> list[int]:
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
    assert len([j for j in s.g.jobs.values() if j.locate == loc.id]) <= 5


def test_bisect_skips_untestable_midpoints_and_caps_steps():
    s, ns, loc = locate_sim(["P", "P", "U", "U", "F", "F", "F"])
    rec = loc.results[0]
    assert rec["bad"]["id"] == ns[4] and rec["good"]["id"] == ns[1] and not rec["exact"]
    s, ns, loc = locate_sim(["P"] + ["F"] * 30, locate_max_steps=2)
    assert not loc.results[0]["exact"] and len([j for j in s.g.jobs.values() if j.locate == loc.id]) == 2


def test_bisect_uses_background_results_and_reports_the_last_transition():
    s = sim(cfg=bgc(confirm_regressions=False))
    ns = _timeline(s, ["P", "F", "P", "P", "F", "F"])
    loc = s.do(R.start_locate, [Z], {"tree": "tl5", "snapshot": ns[-1]}, "rejected")
    rec = s.g.locates[loc].results[0]
    assert rec["good"]["id"] == ns[3] and rec["bad"]["id"] == ns[4]


def test_bisect_across_rollback_uses_the_rollback_target():
    s = sim(cfg=fg(confirm_regressions=False, reviewer=False))
    s.world.define("k1", {})
    s.submit("k1", files=OTHER)
    _timeline(s, ["F", "F"])
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


# ======================================================================== 时间、停滞、运行的结束

def test_deadline_reserve_includes_a_review():
    cfg = BelayConfig(reserve_min_sec=120, reserve_review_sec=300, reserve_extra_sec=0)
    s = sim(cfg=cfg)
    from belay.core.queries import reserve_sec
    assert reserve_sec(s.g, cfg) == pytest.approx(s.g.baseline_sec * 1.3 + 300)
    assert reserve_sec(s.g, cfg.with_(reviewer=False)) == 120
    s.advance(5400 - reserve_sec(s.g, cfg) + 1)
    s.tick()
    assert s.g.run.reserve and any(e.kind == "stop_workers" for e in s.effects)
    assert R.next_step(s.g, "w1", s.now, cfg) == ("finalize", "deadline")


def test_stall_hint_and_repeated_rejected_submits():
    cfg = BelayConfig(stall_no_progress_sec=600, reserve_min_sec=10, locate=False, background="off")
    s = sim(cfg=cfg)
    s.advance(601)
    s.tick()
    assert s.g.stalls[-1].kind == "no_progress" and s.g.stalls[-1].action == "hint"
    for i in range(3):
        s.world.define(f"bad{i}", {MUL: "FAILED"})
        s.submit(f"bad{i}")
    s.tick()
    st = s.g.stalls[-1]
    assert st.kind == "repeated_failure" and st.action == "hint" and "3 submits" in st.detail
    n = len(s.g.stalls)
    s.tick()
    assert len(s.g.stalls) == n


def test_session_end_is_not_run_end_and_submits_decide():
    s = sim(cfg=fg(), auto_jobs=False, reviewer=MANUAL)
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("start_session", "restart")
    s.do(R.start_session, "w1", "restart", {})
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("resume_session", "S2")
    s.world.define("t1", {ADD: "PASSED"})
    s.submit("t1")
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("wait", "merge in progress")
    while s.pending_jobs:
        s.finish_job(s.pending_jobs[0])
    assert s.running_review()
    s.review(s.running_review(), judge(True, {"R2": ("done", "E1"), "R3": ("done", "E1")})(s, last_review(s)))
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")


def test_sessions_without_progress_stop_the_run():
    s = sim()
    s.do(R.end_session, "w1", "done")
    s.do(R.start_session, "w1", "restart", {})
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "no_progress")


def test_finalize_cancels_the_background_review_and_delivers_the_head():
    s = sim(reviewer=MANUAL, cfg=bgc())
    s.world.define("t1", {ADD: "PASSED"})
    s.snap("t1")
    vid = s.running_review()
    s.do(R.begin_finalize, "deadline")
    assert s.g.reviews[vid].status == "cancelled" and vid in s.cancelled_reviews
    n = s.snap("t1", reason="deadline")
    aid = s.do(R.request_merge, "w1", n, "deadline")
    v2 = s.running_review()
    assert s.g.reviews[v2].trigger == "deadline" and s.g.reviews[v2].attempt == aid
    s.review(v2, judge(True, {"R2": ("done", "E1"), "R3": ("done", "E1")})(s, s.g.reviews[v2]))
    assert s.g.head == 1
    assert s.do(R.deliver, "deadline") == "DONE" and s.g.run.delivered == 1
    s.check_log()


def test_unreviewed_final_snapshot_is_not_delivered_when_time_runs_out():
    s = sim(reviewer=MANUAL, cfg=fg())
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1")
    s.review(s.running_review(), judge(True)(s, last_review(s)))
    assert s.g.head == 1 and s.g.submits[sid].status == "returned"
    s.do(R.begin_finalize, "deadline")
    s.world.define("t2", {ADD: "PASSED"})
    n = s.snap("t2", reason="deadline")
    s.do(R.request_merge, "w1", n, "deadline")
    assert s.running_review()                                              # 复核还没结束就到点了
    assert s.do(R.deliver, "deadline") == "INCOMPLETE"
    assert s.g.run.delivered == 1 and not s.running_review()


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
    s = sim(cfg=bgc(), reviewer=judge(True, {"R3": ("done", "E1")}))
    s.world.define("t1", {ADD: "PASSED"})
    s.snap("t1")
    s.submit("t1")
    assert not check_log(s.log) and not llm_effects(s.log)
    assert all(e.source in ("rule", "observed") for e in s.log
               if e.type in ("merged", "merge_advancing", "review_decided", "todo_anchored"))
    assert all(e.source == "llm" for e in s.log if e.type == "merge_reviewed")
    assert all(e.source == "rule" for e in s.log if e.type == "requirement_judged")
    Event = s.log[-1].__class__
    forged = list(s.log) + [Event(len(s.log) + 1, 0, "requirement_judged", "worker:w1", "self_report",
                                  {"requirement": "R1", "status": "done", "level": "E2", "by": "self_report"})]
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
                   "model-written", "+ return a + b", "call submit", "open 3", "Latest merge point"):
        assert needle in text, needle
    keys = [k for k, _ in ctx.sections]
    assert keys[:3] == ["task", "requirements", "pending"] and keys[-1] == "next"
    board = render_board(s.g, "w1", s.now, s.cfg)
    assert "Last submit U1: rejected" in board and "R1 [open]" in board
    assert "How it will be checked" in render_board(s.g, "w1", s.now, s.cfg, requirement="R1")


def test_without_tests_a_failing_reviewer_never_merges_a_background_snapshot():
    s = Sim({}, cfg=bgc(review_retries=0), reviewer=MANUAL)
    s.setup(TASK, PLAN, verifier=False)
    s.do(R.start_session, "w1", "first", {})
    s.snap("w1")
    s.review(s.running_review(), None, failed=True)
    a = next(iter(s.g.attempts.values()))
    assert a.status == "rejected" and a.reason == "review" and s.g.head == 0       # 没有回归门可以兜底
    sid = s.submit("w2")
    s.review(s.running_review(), None, failed=True)
    assert s.g.head == 1 and s.g.submits[sid].status == "accepted"                 # 自己提交的：按自述记下
    assert reqs(s)["R3"] == (REQ_DONE, "E0")


def test_finalize_hands_the_reviewer_to_a_waiting_submit():
    s = sim(reviewer=MANUAL, cfg=bgc())
    s.world.define("t1", {ADD: "PASSED"})
    s.snap("t1")
    bg_review = s.running_review()
    s.world.define("t2", {ADD: "PASSED"})
    n = s.snap("t2", reason="submit")
    # 一个已经在等复核者的 submit（快照与链头不同，回归门已过）：模拟在后台复核进行中到达
    s.do(R.request_submit, "w1", n, "done")
    assert s.g.reviews[bg_review].status == "cancelled"                     # submit 取代了后台复核
    fg_review = s.running_review()
    assert s.g.reviews[fg_review].trigger == "submit"
    s.do(R.begin_finalize, "deadline")
    assert s.running_review() == fg_review                                   # 前台的复核不受收尾影响


def test_only_a_declared_block_can_be_accepted():
    s = sim(cfg=fg(), reviewer=judge(True, {"R3": {"status": "blocked", "level": "E1", "reason": "no docs tool"}}))
    s.world.define("t1", {ADD: "PASSED"})
    sid = s.submit("t1")
    r3 = s.g.requirements["R3"]
    assert r3.status == REQ_OPEN and "cannot be done here" in r3.missing[-1]
    assert s.g.submits[sid].status == "returned"

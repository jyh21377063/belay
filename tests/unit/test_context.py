"""分层开场上下文与 board：只读图的纯函数（模块 I）。"""
from __future__ import annotations

import re

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import PROTECTED, build_context, resume_reminder
from belay.core.render import render_board
from tests.sim import MANUAL, Sim, judge

A, B = "tests/test_a.py::test_a", "tests/test_b.py::test_b"
BASE = {A: "FAILED", B: "PASSED"}
TASK = ("# Release 1.1\n"
        "Implement feature one in the core module now.\nImplement feature two on top of feature one.\n"
        "Implement feature three independently of the others.\nImplement feature four as a small addition.")
LINES = TASK.splitlines()[1:]
PLAN = {"requirements": [{"id": "h", "kind": "context", "quote": "# Release 1.1", "summary": "heading"}] +
        [{"id": f"r{i}", "quote": q, "summary": f"f{i}", "checks": [A] if i == 0 else []}
         for i, q in enumerate(LINES)]}


def sim(cfg=None) -> Sim:
    s = Sim(BASE, cfg=cfg or BelayConfig(background="off"))
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def test_sections_order_scenarios_and_sources():
    s = sim()
    first = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="first")
    keys = [k for k, _ in first.sections]
    assert keys[:2] == ["task", "requirements"] and "pending" not in keys and "away" not in keys
    assert "summary" not in keys and "todos" not in keys
    assert keys.index("progress") < keys.index("workspace") < keys.index("gate") < keys.index("next")
    assert "- R1 heading" not in first.text and "- R2 f0" in first.text      # context 需求不进清单
    assert "not on the checklist" in first.text and "call submit" in first.text
    s.do(R.update_todos, "w1", [{"content": "remember the edge case", "status": "in_progress"}])
    s.world.define("bad", {B: "FAILED"})
    s.submit("bad")
    s.do(R.record_compaction, "w1", 4, 1000, 0, "Next: wire feature two into the registry.")
    s.do(R.end_session, "w1", "handoff")
    away = [e for e in s.log if e.seq > s.g.sessions["S1"].ended_seq - 20]
    res = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="resume", away=away,
                        blobs={"partial_diff": "+feature_two()"})
    rk = [k for k, _ in res.sections]
    assert rk[:5] == ["task", "requirements", "pending", "progress", "todos"] and "away" in rk
    assert rk.index("summary") < rk.index("workspace") < rk.index("away") < rk.index("gate") and rk[-1] == "next"
    assert "observed by the harness" in res.text and "task statement, verbatim" in res.text
    assert "[~] remember the edge case" in res.text and "self-reported" in res.text
    assert "wire feature two" in res.text and "+feature_two()" in res.text
    comp = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="compaction", away=away)
    assert "away" not in [k for k, _ in comp.sections] and "pending" in [k for k, _ in comp.sections]
    rem = resume_reminder(s.g, "w1", s.now, s.cfg, away=away)
    assert "Requirement status" in rem and "Open problems" in rem and "<task>" not in rem


def test_prefix_is_stable_when_requirement_status_changes():
    s = sim(cfg=BelayConfig(background="off", reviewer=False))
    before = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="first").text
    s.world.define("t1", {A: "PASSED"})
    s.submit("t1", blocked=[{"requirement": "R4", "kind": "environment", "reason": "no db"}])
    after = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="first").text
    cut = before.index("## Requirement status")
    assert after[:cut] == before[:cut]                       # 任务原文 + 需求索引逐字不变（前缀缓存）
    assert "blocked" not in after[:cut] and "R4 [blocked, self-reported]" in after[cut:]


def test_judged_requirements_show_level_and_what_is_missing():
    s = Sim(BASE, cfg=BelayConfig(background="off"), reviewer=MANUAL)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    s.world.define("t1", {})
    s.submit("t1")
    vid = next(iter(s.g.reviews))
    s.review(vid, judge(True, {"R3": {"status": "partial", "level": "E1", "missing": ["feature two is not "
                                                                                       "registered"]},
                               "R4": ("done", "E1"), "R5": {"status": "done", "level": "E2", "runs": ["X1"]}})(
        s, s.g.reviews[vid]))
    text = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="resume").text
    sec = text.split("## Requirement status")[1].split("\n## ")[0]
    assert "R2 [open]" in sec
    assert "R3 [open, judged partial]" in sec and "feature two is not registered" in sec
    assert "Done (E2): R5" in sec and "Done (E1): R4" in sec


def test_big_graph_keeps_protected_sections_within_budget_and_folds_the_rest():
    lines = [f"Requirement line {i} asks for behaviour number {i} to be implemented." for i in range(300)]
    task = "\n".join(lines)
    plan = {"requirements": [{"id": f"r{i}", "quote": q, "summary": f"behaviour {i} " + "detail " * 30}
                             for i, q in enumerate(lines)]}
    cfg = BelayConfig(reviewer=False, background="off")
    s = Sim(BASE, cfg=cfg, check_each=False)
    s.setup(task, plan)
    s.do(R.start_session, "w1", "first", {})
    for i in range(100):
        s.do(R.record_compaction, "w1", 4, 1000, 0, f"summary {i} " + "text " * 400)
        s.do(R.end_session, "w1", "handoff")
        s.do(R.start_session, "w1", "handoff", {})
    s.do(R.update_todos, "w1", [{"content": f"item {i} " + "x" * 100, "status": "pending"} for i in range(60)])
    s.world.define("t1", {})
    s.submit("t1", blocked=[{"requirement": f"R{i}", "kind": "environment", "reason": "reason " * 20}
                            for i in range(1, 150)])
    ctx = build_context(s.g, "w1", cfg.opening_budget_tokens, s.now, cfg, mode="resume", away=s.log[-500:],
                        blobs={"partial_diff": "+x\n" * 20000})
    assert ctx.protected_tokens <= cfg.opening_budget_tokens
    assert ctx.tokens <= cfg.opening_budget_tokens + 1000
    for key in ctx.trimmed:
        assert key not in ("task", "requirements")
    assert "board(status=" in ctx.text or "folded" in ctx.text           # 被折叠的段带查询入口
    for k in PROTECTED:
        assert k in [x for x, _ in ctx.sections] or k in ("pending", "why")    # why 只在 fresh 开场


def test_board_filters_and_pagination():
    s = sim()
    out = render_board(s.g, "w1", s.now, s.cfg)
    assert "Requirements: open 4" in out and "board(requirement=" in out and "R1" not in out
    assert "R3 [open]" in render_board(s.g, "w1", s.now, s.cfg, status="open")
    detail = render_board(s.g, "w1", s.now, s.cfg, requirement="R2")
    assert "Task text:" in detail and A in detail and "Linked checks" in detail
    assert "context, not on the checklist" in render_board(s.g, "w1", s.now, s.cfg, requirement="R1")
    assert "fail on the original code" in render_board(s.g, "w1", s.now, s.cfg, view="failures")
    assert "Merge chain" in render_board(s.g, "w1", s.now, s.cfg, view="merges")
    assert "Requirements (all, 4)" in render_board(s.g, "w1", s.now, s.cfg, view="requirements")


def test_time_never_reaches_the_model():
    """剩余时间只由 runtime 用来收尾：开场上下文和 board 里都不出现时间，进入截止预留后也一样。"""
    s = sim()
    time_words = re.compile(r"\d+ min\b|minutes|time budget|[Tt]ime left|RESERVE|reserved")
    for _ in range(2):
        for mode in ("first", "resume", "compaction"):
            assert not time_words.search(build_context(s.g, "w1", 100_000, s.now, s.cfg, mode=mode).text)
        assert not time_words.search(render_board(s.g, "w1", s.now, s.cfg))
        s.advance(5400)
        s.do(R.tick)
    assert s.g.run.reserve

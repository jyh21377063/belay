"""调度建议与分层开场上下文：只读图的两个纯函数（模块 I）。"""
from __future__ import annotations

import re

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import PROTECTED, build_context, resume_reminder
from belay.core.render import render_board
from belay.core.suggest import suggest
from tests.sim import Sim

A, B = "tests/test_a.py::test_a", "tests/test_b.py::test_b"
BASE = {A: "FAILED", B: "PASSED"}
TASK = ("Implement feature one in the core module now.\nImplement feature two on top of feature one.\n"
        "Implement feature three independently of the others.\nImplement feature four as a small addition.")
PLAN = {"requirements": [{"id": f"r{i}", "quote": q, "summary": f"f{i}"} for i, q in enumerate(TASK.splitlines())],
        "tasks": [{"id": "t0", "title": "one", "links": ["r0"], "checks": [A]},
                  {"id": "t1", "title": "two", "links": ["r1"], "blocked_by": ["t0"]},
                  {"id": "t2", "title": "three", "links": ["r2"], "priority": 5},
                  {"id": "t3", "title": "four", "links": ["r3"], "blocked_by": ["t0"]}]}


def sim(cfg=None) -> Sim:
    s = Sim(BASE, cfg=cfg or BelayConfig(auto_checkpoint=False))
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def test_suggest_order_unlocks_then_priority_and_dependencies_last():
    s = sim()
    order = [x.task for x in suggest(s.g, "w1", s.now, s.cfg)]
    assert order == ["T1", "T3", "T2", "T4"]                 # 依赖未完成的排在最后（但仍可认领）
    assert "unblocks 2 task(s)" in suggest(s.g, "w1", s.now, s.cfg)[0].reason


def test_suggest_reopened_first_and_coverage():
    s = sim()
    s.do(R.claim, "w1", "T3")
    s.world.define("bad", {B: "FAILED"})
    s.review("T3", "bad", files=[("x/test_like.cfg", 1, 1)])
    ss = suggest(s.g, "w1", s.now, s.cfg)
    assert ss[0].task == "T3" and "reopened" in ss[0].reason
    assert suggest(s.g, "w1", s.now, s.cfg.with_(suggest=False)) == []


def test_sections_order_scenarios_and_sources():
    s = sim()
    s.do(R.claim, "w1", "T1")
    first = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="first")
    keys = [k for k, _ in first.sections]
    assert keys[:3] == ["task", "requirements", "focus"] and "pending" not in keys and "away" not in keys
    assert keys.index("workspace") < keys.index("progress") < keys.index("next") < keys.index("gate")
    s.do(R.note, "w1", "remember the edge case")
    s.world.define("bad", {B: "FAILED"})
    s.review("T1", "bad")
    s.do(R.end_session, "w1", "handoff")
    away = [e for e in s.log if e.seq > s.g.sessions["S1"].ended_seq - 20]
    res = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="resume", away=away)
    rk = [k for k, _ in res.sections]
    assert rk[:4] == ["task", "requirements", "focus", "pending"] and "away" in rk
    assert "observed by the harness" in res.text and "task statement, verbatim" in res.text
    assert "(self-reported)" in res.text and "remember the edge case" in res.text
    comp = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="compaction", away=away)
    assert "away" not in [k for k, _ in comp.sections] and "pending" in [k for k, _ in comp.sections]
    rem = resume_reminder(s.g, "w1", s.now, s.cfg, away=away)
    assert "Current focus" in rem and "Open problems" in rem and "<task>" not in rem


def test_prefix_is_stable_when_task_status_changes():
    s = sim()
    before = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="first").text
    s.do(R.claim, "w1", "T3")
    s.do(R.report_blocked, "w1", "T3", "environment", "no db")
    after = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="first").text
    cut = before.index("## Current focus")
    assert after[:cut] == before[:cut]                       # 任务原文 + 需求索引逐字不变（前缀缓存）
    assert "R3 [blocked]" not in after[:cut]


def test_notes_of_the_focus_task_from_all_sessions():
    s = sim()
    s.do(R.claim, "w1", "T1")
    for i in range(3):
        s.do(R.note, "w1", f"decision number {i}")
        s.do(R.end_session, "w1", "handoff")
        s.do(R.start_session, "w1", "handoff", {})
    text = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="resume").text
    for i in range(3):
        assert f"decision number {i}" in text


def test_big_graph_keeps_protected_sections_within_budget_and_folds_the_rest():
    lines = [f"Requirement line {i} asks for behaviour number {i} to be implemented." for i in range(300)]
    task = "\n".join(lines)
    plan = {"requirements": [{"id": f"r{i}", "quote": q, "summary": f"behaviour {i} " + "detail " * 30}
                             for i, q in enumerate(lines)],
            "tasks": [{"id": f"t{i}", "title": f"task {i} " + "words " * 10, "links": [f"r{i % 300}"]}
                      for i in range(1000)]}
    cfg = BelayConfig(auto_checkpoint=False, reviewer=False)
    s = Sim(BASE, cfg=cfg, check_each=False)
    s.setup(task, plan)
    s.do(R.start_session, "w1", "first", {})
    for i in range(100):
        s.do(R.note, "w1", f"note {i} " + "text " * 50)
        s.do(R.end_session, "w1", "handoff")
        s.do(R.start_session, "w1", "handoff", {})
    for tid in [f"T{i}" for i in range(1, 400)]:
        s.do(R.claim, "w1", tid)
        s.do(R.report_blocked, "w1", tid, "environment", "reason " * 20)
    s.do(R.claim, "w1", "T500")
    s.do(R.plan_steps, "w1", [{"content": f"step {i} " + "x" * 100, "status": "pending"} for i in range(40)])
    ctx = build_context(s.g, "w1", cfg.opening_budget_tokens, s.now, cfg, mode="resume", away=s.log[-500:])
    assert ctx.protected_tokens <= cfg.opening_budget_tokens
    assert ctx.tokens <= cfg.opening_budget_tokens + 1000
    for key in ctx.trimmed:
        assert key not in ("task", "requirements")
    assert "board(status=" in ctx.text                       # 被折叠的段带查询入口
    for k in PROTECTED:
        assert k in [x for x, _ in ctx.sections] or k == "pending"


def test_board_filters_and_pagination():
    s = sim()
    out = render_board(s.g, "w1", s.now, s.cfg)
    assert "Tasks:" in out and "board(requirement=" in out
    assert "T2 [open]" in render_board(s.g, "w1", s.now, s.cfg, status="open")
    assert "Task text:" in render_board(s.g, "w1", s.now, s.cfg, requirement="R1")
    assert "- R1 (task text)" in render_board(s.g, "w1", s.now, s.cfg, task="T1")
    assert "fail on the original code" in render_board(s.g, "w1", s.now, s.cfg, view="failures")
    assert "Checkpoint chain" in render_board(s.g, "w1", s.now, s.cfg, view="checkpoints")


def test_time_never_reaches_the_model():
    """剩余时间只由 runtime 用来收尾：开场上下文和 board 里都不出现时间，进入截止预留后也一样。"""
    s = sim()
    s.do(R.claim, "w1", "T1")
    time_words = re.compile(r"\d+ min\b|minutes|time budget|[Tt]ime left|RESERVE|reserved")
    for _ in range(2):
        for mode in ("first", "resume", "compaction"):
            assert not time_words.search(build_context(s.g, "w1", 100_000, s.now, s.cfg, mode=mode).text)
        assert not time_words.search(render_board(s.g, "w1", s.now, s.cfg))
        s.advance(5400)
        s.do(R.tick)
    assert s.g.run.reserve

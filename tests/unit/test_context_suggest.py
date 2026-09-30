"""调度建议与上下文构建：只读图的两个纯函数。"""
from __future__ import annotations

import re

from belay.core import rules as R
from belay.core.context import build_context
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
    s = Sim(BASE, cfg=cfg)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def test_suggest_order_unlocks_then_priority():
    s = sim()
    order = [x.task for x in suggest(s.g, "w1", s.now, s.cfg)]
    assert order == ["T1", "T3"]                     # T1 解锁 2 个下游；T3 只是优先级高
    assert "unblocks 2 task(s)" in suggest(s.g, "w1", s.now, s.cfg)[0].reason


def test_suggest_reopened_first_and_coverage():
    s = sim()
    s.do(R.claim, "w1", "T3")
    s.world.define("bad", {B: "FAILED"})
    s.do(R.request_review, "w1", "T3", s.obs("bad", files=[("x/test_like.cfg", 1, 1)]))   # 全量 → B 回归 → 重开
    ss = suggest(s.g, "w1", s.now, s.cfg)
    assert ss[0].task == "T3" and "reopened" in ss[0].reason
    assert suggest(s.g, "w1", s.now, s.cfg.with_(suggest=False)) == []


def test_build_context_trims_from_the_bottom_and_keeps_the_first_three():
    s = sim()
    s.do(R.claim, "w1", "T1")
    for i in range(30):
        s.do(R.note, "w1", f"note number {i} " + "blah " * 40)
    s.do(R.end_session, "w1", "handoff")
    full = build_context(s.g, "w1", 100_000, s.now, s.cfg, mode="resume")
    keys = [k for k, _ in full.sections]
    assert keys[:3] == ["task", "requirements", "my_tasks"] and "notes" in keys and "suggestions" in keys
    assert keys.index("workspace") < keys.index("known_failures") < keys.index("notes") < keys.index("suggestions")
    small = build_context(s.g, "w1", 700, s.now, s.cfg, mode="resume")
    kept = [k for k, _ in small.sections]
    assert kept[:3] == ["task", "requirements", "my_tasks"]
    assert "suggestions" in small.dropped or "suggestions" in small.trimmed
    assert "notes" not in kept or "notes" in small.trimmed
    tiny = build_context(s.g, "w1", 10, s.now, s.cfg, mode="resume")
    assert [k for k, _ in tiny.sections] == ["task", "requirements", "my_tasks"]      # 前三段永远不裁
    assert "<task>" in tiny.text and "### T1" in tiny.text


def test_build_context_includes_wip_diff_from_blobs_and_marks_sources():
    s = sim()
    s.do(R.claim, "w1", "T1")
    obs = R.TreeObs("t9", "t9raw", (("pkg/a.py", 3, 1),), ("tests/test_a.py",), "/blobs/d1.diff")
    s.do(R.record_wip, "w1", obs)
    ctx = build_context(s.g, "w1", 50_000, s.now, s.cfg, blobs={"/blobs/d1.diff": "+ new line in a.py"},
                        mode="compaction")
    t = ctx.text
    assert "pkg/a.py (+3 -1)" in t and "+ new line in a.py" in t and "tests/test_a.py" in t
    assert "observed by the harness" in t and "rebuilt by the harness" in t


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

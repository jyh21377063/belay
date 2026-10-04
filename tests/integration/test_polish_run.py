"""端到端：after_accept=polish（换新会话进入 POLISH）与打转换人。LocalEnv + ScriptedLLM + 真实的 git 与 pytest。

每个场景结束后都检查：事件库重放 == 实时的图，日志满足来源纪律。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from belay.core.config import BelayConfig
from belay.core.context import VERIFY_LINE
from belay.llm import ScriptedLLM
from tests.integration.fakes import FakeAux, oracle
from tests.integration.test_belay_run import (ADD_SUB, FIX_ADD, PLANNER, READ, SUBMIT, SUBMIT2, TASK, Harness, call,
                                              first_message, say, tool_outputs, tu)

CHECK_SUB = "python -c 'from pkg.mod import sub; assert sub(5, 3) == 2'"
ADD_SUB_BAD = tu("asb", "edit_file", file_path="pkg/mod.py", old_string="def mul(a, b):",
                 new_string="def sub(a, b):\n    return b - a\n\n\ndef mul(a, b):")
FIX_SUB = tu("fs", "edit_file", file_path="pkg/mod.py", old_string="return b - a", new_string="return a - b")


def sub_ok(review_dir: Path) -> bool:
    try:
        return "def sub(a, b):\n    return a - b" in (review_dir / "pkg" / "mod.py").read_text()
    except OSError:
        return False


def auditor(opening: str, review_dir: Path) -> dict:
    """读代码时 sub 存在就判完成（E1）；复审时跑过 CHECK_SUB（X1）：运算顺序反了就以 E2 退回。"""
    v = oracle(opening, review_dir)
    ok = sub_ok(review_dir)
    if "## Audit" in opening:
        v["requirements"] = [{"id": "R3", "status": "done" if ok else "not_done", "level": "E2", "runs": ["X1"],
                              "missing": [] if ok else [f"sub(5, 3) is not 2: {CHECK_SUB}"]}]
    elif ok:
        for r in v["requirements"]:
            if r["id"] == "R3" and r["status"] == "done":
                r.update(level="E2", runs=["X1"])
    return v


def summary_index(llm: ScriptedLLM) -> int:
    return next(i for i, r in enumerate(llm.requests) if r.get("tool_choice") == {"type": "none"})


def test_verify_polish_hands_over_to_a_new_session_that_fixes_the_audited_gap(tmp_path):
    cfg = BelayConfig(after_accept="polish", new_session_min_sec=60)
    h = Harness(tmp_path, cfg=cfg)
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB_BAD), call(SUBMIT),
                       say("Checked add with pytest. Least sure about sub: I guessed the operand order."),
                       call(FIX_SUB), call(SUBMIT2)])
    run = h.make(llm, aux=FakeAux(auditor, run_first=CHECK_SUB))
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    assert res.status == "DONE", g.run.status_reasons
    assert g.run.polish_mode == "verify" and g.run.improve_closed.startswith("the audit found no gap")
    assert [(s.reason, s.end_reason) for s in sorted(g.sessions.values(), key=lambda s: s.n)] == \
        [("first", "phase"), ("phase", "submitted")]
    audits = [v for v in g.reviews.values() if v.trigger == "verify"]
    assert len(audits) == 2
    r3 = g.requirements["R3"]
    assert r3.status == "done" and r3.level == "E2" and any(x[2] == "reassessed" for x in r3.history)
    # 第一个会话的 submit：复审跑出了缺口，回复里带着复核者的命令与输出
    out = tool_outputs(run, "submit")
    assert "still open" in out[0] and "R3" in out[0] and "sub(5, 3) is not 2" in out[0]
    assert "exit code 1" in out[0] and "AssertionError" in out[0]
    # 交接摘要（不许用工具）进了图；新会话的开场：POLISH 的说明、摘要、预读整个任务改动最多的文件
    i = summary_index(llm)
    opening = first_message(llm.requests[i + 1])
    assert opening.startswith("You are starting a new session: every requirement on the checklist has been accepted")
    assert VERIFY_LINE in opening and "guessed the operand order" in opening
    assert "Files the delivered version changes relative to the original code" in opening
    assert "Files the delivered version changes most (re-read by the harness)" in opening
    assert "return b - a" in opening                                       # 预读的 pkg/mod.py
    # 复核者的复审开场：VERIFY 的说明；需求阶段的复核开场里没有改进 / 复审的说明
    audit_openings = [o for o in h.aux.openings if "## Audit" in o]
    assert len(audit_openings) == 2 and "Reopen a requirement only on a gap you have shown" in audit_openings[0]
    first_reviews = h.aux.openings[:h.aux.openings.index(audit_openings[0])]
    assert first_reviews and all("## Improvements" not in o and "audit" not in o.lower() for o in first_reviews)
    for req in h.aux.requests:
        props = next(t for t in req["tools"] if t["name"] == "verdict")["input_schema"]["properties"]
        assert "new_improvements" not in props and "blockers" in props
    assert "return a - b" in (h.run_dir() / "deliverable.diff").read_text()
    h.verify_log(run)


def test_polish_needs_time_for_a_new_session(tmp_path):
    h = Harness(tmp_path, cfg=BelayConfig(after_accept="polish"))          # 预算 600 秒：不够开新会话
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB), call(SUBMIT)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    assert res.status == "DONE" and not g.run.improving and len(g.sessions) == 1
    h.verify_log(run)


def test_a_requirement_failing_on_every_submit_is_handed_to_a_fresh_session(tmp_path):
    cfg = BelayConfig(new_session_min_sec=60)
    h = Harness(tmp_path, cfg=cfg)
    resubmit = lambda i: call(tu(f"u{i}", "submit", summary="R3 is done too"))     # noqa: E731
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(SUBMIT), resubmit(2), resubmit(3),
                       say("Tried submitting R3 as is; the reviewer says sub() is not defined."),
                       call(READ), call(ADD_SUB), call(SUBMIT2)])
    run = h.make(llm)
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    assert res.status == "DONE", g.run.status_reasons
    assert [(s.reason, s.end_reason) for s in sorted(g.sessions.values(), key=lambda s: s.n)] == \
        [("first", "stuck_handoff"), ("fresh", "submitted")]
    kinds = [(s.kind, s.action, s.sig) for s in g.stalls]
    assert ("requirement_misses", "hint", "req:R3") in kinds and ("requirement_misses", "handoff", "req:R3") in kinds
    notices = " ".join(json.dumps(r["messages"][-1]["content"]) for r in llm.requests)
    assert "R3 was judged not done on 2 submits in a row" in notices
    assert not re.search(r"\d+ min\b|minutes|time budget|[Tt]ime left", notices)
    i = summary_index(llm)
    assert "the same problem kept coming back" in json.dumps(llm.requests[i]["messages"][-1]["content"])
    opening = first_message(llm.requests[i + 1])
    assert opening.startswith("You are taking over from a previous session")
    assert "## Why a new session" in opening and "R3 was judged not done on 3 submits in a row" in opening
    assert "the reviewer says sub() is not defined" in opening
    ev = [e for e in h.verify_log(run) if e.type == "compacted"]
    assert ev and ev[-1].get("level") == 4 and ev[-1].source == "llm"


def test_improve_polish_continues_the_improvement_items_in_a_new_session(tmp_path):
    from tests.integration.test_belay_run import SUB_DOC

    def lead(opening, review_dir):
        v = oracle(opening, review_dir)
        v.update(score=1.0, score_note="python -c 'import pkg.mod'")
        mod = (review_dir / "pkg" / "mod.py").read_text()
        if "## Improvements" not in opening:
            return v                                          # POLISH 开始之前：复核者看不到改进项的说明
        if "Return a minus b" in mod:
            v.update(improvements=[{"id": "I1", "status": "done", "level": "E2", "runs": ["X1"],
                                    "evidence": ["sub has a docstring"]}],
                     no_more_improvements="sub and add are complete for what the task asks")
        elif "Improvement items so far: none" in opening:
            v["new_improvements"] = [{"title": "Document sub", "why": "callers should know the order of operands",
                                      "quote": "returns a minus b"}]
        return v
    h = Harness(tmp_path, cfg=BelayConfig(after_accept="polish", new_session_min_sec=60))
    llm = ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(ADD_SUB), call(SUBMIT),
                       say("Checked add and sub with pytest."), call(SUB_DOC), call(SUBMIT2)])
    run = h.make(llm, aux=FakeAux(lead, run_first="python -c 'import pkg.mod'"))
    res = asyncio.run(run.start(TASK))
    g = run.rt.graph
    assert res.status == "DONE", g.run.status_reasons
    assert g.run.polish_mode == "improve" and g.run.improve_closed.startswith("sub and add")
    assert [(s.reason, s.end_reason) for s in sorted(g.sessions.values(), key=lambda s: s.n)] == \
        [("first", "phase"), ("phase", "submitted")]
    assert g.improvements["I1"].status == "done"
    i = summary_index(llm)
    opening = first_message(llm.requests[i + 1])
    assert "I1 [open] Document sub" in opening and "continues to improve the delivered version" in opening
    first = [o for o in h.aux.openings if "## Improvements" not in o]
    assert first and h.aux.openings.index(first[-1]) < min(h.aux.openings.index(o) for o in h.aux.openings
                                                            if "## Improvements" in o)
    assert '"""Return a minus b."""' in (h.run_dir() / "deliverable.diff").read_text()
    h.verify_log(run)

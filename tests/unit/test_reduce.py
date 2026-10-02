"""事件格式、推导函数的纯度，以及状态机拒绝不合法的转换（reduce 是最后一道防线）。"""
from __future__ import annotations

import json

import pytest

from belay.core.events import Event, EventError, validate
from belay.core.model import Graph, graph_from_json, to_json
from belay.core.reduce import IllegalEvent, apply, replay
from tests.sim import Sim

BASE = {"tests/test_mod.py::test_add": "FAILED", "tests/test_mod.py::test_mul": "PASSED",
        "tests/test_other.py::test_z": "PASSED"}
TASK = "Fix the add function so that it returns the sum.\nAlso make mul handle negative numbers correctly."
PLAN = {"requirements": [{"id": "a", "quote": "Fix the add function so that it returns the sum.", "summary": "add",
                          "checks": ["tests/test_mod.py::test_add"]},
                         {"id": "b", "quote": "Also make mul handle negative numbers correctly.", "summary": "mul"}]}


def ready_sim(**kw) -> Sim:
    s = Sim(BASE, **kw)
    s.setup(TASK, PLAN)
    return s


def ev(g: Graph, type_: str, source: str = "rule", actor: str = "runtime", **payload) -> Event:
    return Event(g.seq + 1, 2000.0, type_, actor, source, payload)


def test_validate_format():
    with pytest.raises(EventError):
        validate(Event(1, 0, "no_such_event", "runtime", "rule", {}))
    with pytest.raises(EventError):                        # 复核者的结论不能伪装成规则的决定
        validate(Event(1, 0, "review_decided", "reviewer", "llm", {"review": "V1", "merge": True, "reasons": []}))
    with pytest.raises(EventError):                        # 需求判定不能直接来自 llm
        validate(Event(1, 0, "requirement_judged", "reviewer", "llm",
                       {"requirement": "R1", "status": "done", "by": "review"}))
    with pytest.raises(EventError):
        validate(Event(1, 0, "submit_requested", "worker:w1", "rule", {"submit": "U1"}))
    with pytest.raises(EventError):                        # 合并点只能来自观察
        validate(Event(1, 0, "merged", "runtime", "llm", {"checkpoint": 1, "commit": "c", "tree": "t"}))
    with pytest.raises(EventError):                        # 豁免只能来自规则（复核者的提议经过校验）
        validate(Event(1, 0, "waiver_granted", "reviewer", "llm", {"tests": [], "quote": "q", "reason": "r",
                                                                   "review": "V1"}))


def test_apply_is_pure_and_seq_must_follow():
    s = ready_sim()
    g = s.g
    before = json.dumps(to_json(g), sort_keys=True)
    g2 = apply(g, ev(g, "todos_updated", source="self_report", actor="worker:w1", worker="w1",
                     todos=[{"id": "P1", "n": 1, "title": "fix add for R1", "status": "in_progress",
                             "requirements": ["R1"]}]))
    assert json.dumps(to_json(g), sort_keys=True) == before        # 输入不变
    assert g2.todos["P1"].status == "in_progress" and not g.todos
    with pytest.raises(IllegalEvent):
        apply(g, Event(g.seq + 2, 0, "stall_detected", "runtime", "rule", {"kind": "no_progress", "action": "hint"}))


def test_snapshot_roundtrip():
    s = ready_sim()
    g = s.g
    assert graph_from_json(json.loads(json.dumps(to_json(g)))) == g
    s.world.define("t1", {"tests/test_mod.py::test_add": "PASSED"})
    s.submit("t1")
    g = s.g
    assert g.submits and g.reviews
    assert graph_from_json(json.loads(json.dumps(to_json(g)))) == g


@pytest.mark.parametrize("case", ["done_without_level", "e3_failing", "done_off_chain", "self_report_e2",
                                  "created_no_adv", "freeze_twice", "freeze_no_actionable", "advance_unapproved",
                                  "delivered_not_on_chain", "todo_new_completed", "todo_unknown_req", "job_key_dup",
                                  "todo_complete_unknown", "snapshot_gap", "submit_twice_open", "context_judged",
                                  "two_reviews", "decided_unrecorded", "waive_non_gate"])
def test_illegal_transitions(case):
    s = ready_sim()
    g = s.g
    if case == "done_without_level":
        e = ev(g, "requirement_judged", requirement="R2", status="done", by="review", checkpoint=0)
    elif case == "e3_failing":                                     # 基线上 test_add 是失败的
        e = ev(g, "requirement_judged", requirement="R1", status="done", level="E3", by="checks", checkpoint=0,
               tests=["tests/test_mod.py::test_add"])
    elif case == "done_off_chain":
        e = ev(g, "requirement_judged", requirement="R2", status="done", level="E1", by="review", checkpoint=7)
    elif case == "self_report_e2":                                 # 自述只能是 E0（规则层的来源纪律在 check_log）
        e = ev(g, "requirement_judged", source="rule", requirement="R2", status="done", level="E0", by="self_report",
               checkpoint=0)
    elif case == "created_no_adv":
        e = ev(g, "merged", source="observed", checkpoint=1, attempt="A9", commit="c", tree="t")
    elif case == "freeze_twice":
        e = ev(g, "requirement_frozen", requirements=[{"id": "R9", "quote": "q"}])
    elif case == "freeze_no_actionable":
        s2 = Sim(BASE)
        s2.do(__import__("belay.core.rules", fromlist=["start_run"]).start_run, "r", TASK, 100)
        g = s2.g
        e = ev(g, "requirement_frozen", requirements=[{"id": "R1", "quote": "q", "kind": "context"}])
    elif case == "advance_unapproved":                             # 复核者没批准的合并请求不能推进
        from tests.sim import MANUAL
        s4 = Sim(BASE, reviewer=MANUAL)
        s4.setup(TASK, PLAN)
        s4.world.define("t1", {})
        s4.snap("t1")
        a = next(iter(s4.g.attempts.values()))
        g = s4.g
        e = ev(g, "merge_advancing", attempt=a.id, parent_commit=g.head_cp.commit, date=1.0)
    elif case == "todo_new_completed":
        e = ev(g, "todos_updated", source="self_report", worker="w1",
               todos=[{"id": "P1", "n": 1, "title": "x", "status": "completed"}])
    elif case == "todo_unknown_req":
        e = ev(g, "todos_updated", source="self_report", worker="w1",
               todos=[{"id": "P1", "n": 1, "title": "x", "status": "pending", "requirements": ["R77"]}])
    elif case == "todo_complete_unknown":
        e = ev(g, "todo_completed", source="self_report", worker="w1", todo="P1", snapshot=0)
    elif case == "snapshot_gap":
        e = ev(g, "snapshot_taken", source="observed", snapshot=5, worker="w1", tree="t", raw_tree="t",
               reason="writes", testable=True)
    elif case == "delivered_not_on_chain":
        e = ev(g, "delivered", checkpoint=5, status="DONE")
    elif case == "job_key_dup":
        j = next(iter(g.jobs.values()))
        e = ev(g, "job_started", job="J99", key=j.key, tree=j.tree, selection=None, purpose="gate")
    elif case == "submit_twice_open":
        g = apply(g, ev(g, "submit_requested", actor="worker:w1", submit="U1", worker="w1", snapshot=0, checkpoint=0))
        e = ev(g, "submit_requested", actor="worker:w1", submit="U2", worker="w1", snapshot=0, checkpoint=0)
    elif case == "context_judged":
        s3 = Sim(BASE)
        plan = {"requirements": [{"id": "c", "kind": "context", "quote": "Fix the add function so that it returns "
                                  "the sum."}, {"id": "b", "quote": "Also make mul handle negative numbers correctly."}]}
        s3.setup(TASK, plan)
        g = s3.g
        e = ev(g, "requirement_judged", requirement="R1", status="done", level="E1", by="review", checkpoint=0)
    elif case == "two_reviews":
        g = apply(g, ev(g, "review_started", review="V1", trigger="judge", checkpoint=0, tree=g.head_cp.tree,
                        snapshot=0, focus=[]))
        e = ev(g, "review_started", review="V2", trigger="judge", checkpoint=0, tree=g.head_cp.tree, snapshot=0,
               focus=[])
    elif case == "decided_unrecorded":
        g = apply(g, ev(g, "review_started", review="V1", trigger="judge", checkpoint=0, tree=g.head_cp.tree,
                        snapshot=0, focus=[]))
        e = ev(g, "review_decided", review="V1", merge=None, reasons=[])
    elif case == "waive_non_gate":                                 # 原始代码上就失败的测试不在回归门里
        g = apply(g, ev(g, "review_started", review="V1", trigger="judge", checkpoint=0, tree=g.head_cp.tree,
                        snapshot=0, focus=[]))
        e = ev(g, "waiver_granted", tests=["tests/test_mod.py::test_add"], quote="q", reason="r", review="V1")
    with pytest.raises(IllegalEvent):
        apply(g, e)


def test_events_after_delivery_are_rejected():
    s = ready_sim()
    s.do(__import__("belay.core.rules", fromlist=["deliver"]).deliver, "test")
    with pytest.raises(IllegalEvent):
        apply(s.g, ev(s.g, "todos_updated", source="self_report", worker="w1", todos=[]))


def test_replay_equals_live_graph():
    s = ready_sim()
    assert replay(s.log) == s.g

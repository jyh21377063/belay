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
    with pytest.raises(EventError):                        # 验证通过不能来自自述
        validate(Event(1, 0, "requirement_verified", "worker:w1", "self_report",
                       {"requirement": "R1", "checkpoint": 1, "evidence": {}}))
    with pytest.raises(EventError):                        # 提交的需求是自述，不能伪装成规则
        validate(Event(1, 0, "requirement_submitted", "worker:w1", "rule",
                       {"requirement": "R1", "checkpoint": 1, "submit": "U1"}))
    with pytest.raises(EventError):
        validate(Event(1, 0, "submit_requested", "worker:w1", "rule", {"submit": "U1"}))
    with pytest.raises(EventError):                        # 存档只能来自观察
        validate(Event(1, 0, "checkpoint_created", "runtime", "llm", {"checkpoint": 1, "commit": "c", "tree": "t"}))


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


@pytest.mark.parametrize("case", ["verified_without_evidence", "verified_failing", "submitted_wrong_checkpoint",
                                  "reopen_open", "created_no_adv", "freeze_twice", "freeze_no_actionable",
                                  "confirm_confirmed", "delivered_not_on_chain", "todo_new_completed",
                                  "todo_unknown_req", "job_key_dup", "todo_complete_unknown", "snapshot_gap",
                                  "submit_twice_open", "context_submitted"])
def test_illegal_transitions(case):
    s = ready_sim()
    g = s.g
    if case == "verified_without_evidence":
        e = ev(g, "requirement_verified", requirement="R2", checkpoint=0, evidence={})
    elif case == "verified_failing":
        e = ev(g, "requirement_verified", requirement="R1", checkpoint=0, evidence={})    # 基线上 test_add 是失败的
    elif case == "submitted_wrong_checkpoint":
        e = ev(g, "requirement_submitted", source="self_report", actor="worker:w1", requirement="R2",
               checkpoint=0, submit="U9")
    elif case == "reopen_open":
        e = ev(g, "requirement_reopened", requirement="R2", reason="review_missing")
    elif case == "created_no_adv":
        e = ev(g, "checkpoint_created", source="observed", checkpoint=1, attempt="A9", commit="c", tree="t")
    elif case == "freeze_twice":
        e = ev(g, "requirement_frozen", requirements=[{"id": "R9", "quote": "q"}])
    elif case == "freeze_no_actionable":
        g = Graph()
        s2 = Sim(BASE)
        s2.do(__import__("belay.core.rules", fromlist=["start_run"]).start_run, "r", TASK, 100)
        g = s2.g
        e = ev(g, "requirement_frozen", requirements=[{"id": "R1", "quote": "q", "kind": "context"}])
    elif case == "confirm_confirmed":
        e = ev(g, "checkpoint_confirmed", source="observed", checkpoint=0)       # 0 号基线本来就是确认点
    elif case == "todo_new_completed":
        e = ev(g, "todos_updated", source="self_report", worker="w1",
               todos=[{"id": "P1", "n": 1, "title": "x", "status": "completed"}])  # 完成只能经 todo_completed
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
        e = ev(g, "job_started", job="J99", key=j.key, tree=j.tree, selection=None, purpose="dev")
    elif case == "submit_twice_open":
        g = apply(g, ev(g, "submit_requested", actor="worker:w1", submit="U1", worker="w1", snapshot=0, checkpoint=0))
        e = ev(g, "submit_requested", actor="worker:w1", submit="U2", worker="w1", snapshot=0, checkpoint=0)
    elif case == "context_submitted":
        s3 = Sim(BASE)
        plan = {"requirements": [{"id": "c", "kind": "context", "quote": "Fix the add function so that it returns "
                                  "the sum."}, {"id": "b", "quote": "Also make mul handle negative numbers correctly."}]}
        s3.setup(TASK, plan)
        g = s3.g
        g = apply(g, ev(g, "submit_requested", actor="worker:w1", submit="U1", worker="w1", snapshot=0, checkpoint=0))
        e = ev(g, "requirement_submitted", source="self_report", actor="worker:w1", requirement="R1", checkpoint=0,
               submit="U1")
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

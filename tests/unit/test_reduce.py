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
PLAN = {"requirements": [{"id": "a", "quote": "Fix the add function so that it returns the sum.", "summary": "add"},
                         {"id": "b", "quote": "Also make mul handle negative numbers correctly.", "summary": "mul"}],
        "tasks": [{"id": "x", "title": "fix add", "links": ["a"], "checks": ["tests/test_mod.py::test_add"]},
                  {"id": "y", "title": "fix mul", "links": ["b"], "blocked_by": ["x"]}]}


def ready_sim(**kw) -> Sim:
    s = Sim(BASE, **kw)
    s.setup(TASK, PLAN)
    return s


def ev(g: Graph, type_: str, source: str = "rule", actor: str = "runtime", **payload) -> Event:
    return Event(g.seq + 1, 2000.0, type_, actor, source, payload)


def test_validate_format():
    with pytest.raises(EventError):
        validate(Event(1, 0, "no_such_event", "runtime", "rule", {}))
    with pytest.raises(EventError):                        # 完成不能来自自述
        validate(Event(1, 0, "task_done", "worker:w1", "self_report", {"task": "T1", "checkpoint": 1, "verified": True}))
    with pytest.raises(EventError):
        validate(Event(1, 0, "task_claimed", "worker:w1", "rule", {"task": "T1"}))
    with pytest.raises(EventError):                        # 存档只能来自观察
        validate(Event(1, 0, "checkpoint_created", "runtime", "llm", {"checkpoint": 1, "commit": "c", "tree": "t"}))


def test_apply_is_pure_and_seq_must_follow():
    s = ready_sim()
    g = s.g
    before = json.dumps(to_json(g), sort_keys=True)
    g2 = apply(g, ev(g, "task_claimed", task="T1", worker="w1", head=0))
    assert json.dumps(to_json(g), sort_keys=True) == before        # 输入不变
    assert g2.tasks["T1"].status == "active" and g.tasks["T1"].status == "open"
    with pytest.raises(IllegalEvent):
        apply(g, Event(g.seq + 2, 0, "note", "worker:w1", "self_report", {"worker": "w1", "kind": "note", "text": "x"}))


def test_snapshot_roundtrip():
    s = ready_sim()
    g = s.g
    assert graph_from_json(json.loads(json.dumps(to_json(g)))) == g


@pytest.mark.parametrize("case", ["claim_active", "done_not_review", "done_wrong_verified", "created_no_adv",
                                  "freeze_twice", "confirm_confirmed", "delivered_not_on_chain",
                                  "split_drops_links", "add_unknown_req", "job_key_dup", "step_unknown",
                                  "snapshot_gap"])
def test_illegal_transitions(case):
    s = ready_sim()
    g = s.g
    if case == "claim_active":
        g = apply(g, ev(g, "task_claimed", task="T1", worker="w1", head=0))
        e = ev(g, "task_claimed", task="T1", worker="w1", head=0)
    elif case == "done_not_review":
        e = ev(g, "task_done", task="T1", checkpoint=0, verified=True)
    elif case == "done_wrong_verified":
        g = apply(g, ev(g, "task_claimed", task="T1", worker="w1", head=0))
        g = apply(g, ev(g, "review_requested", task="T1", worker="w1", checkpoint=0))
        e = ev(g, "task_done", task="T1", checkpoint=0, verified=False)       # T1 有检查，不能“未验证地完成”
    elif case == "created_no_adv":
        e = ev(g, "checkpoint_created", source="observed", checkpoint=1, attempt="A9", commit="c", tree="t")
    elif case == "freeze_twice":
        e = ev(g, "requirement_frozen", requirements=[{"id": "R9", "quote": "q"}])
    elif case == "confirm_confirmed":
        e = ev(g, "checkpoint_confirmed", source="observed", checkpoint=0)       # 0 号基线本来就是确认点
    elif case == "step_unknown":
        e = ev(g, "step_done", source="self_report", worker="w1", step="T1.1", snapshot=0)
    elif case == "snapshot_gap":
        e = ev(g, "snapshot_taken", source="observed", snapshot=5, worker="w1", tree="t", raw_tree="t",
               reason="writes", testable=True)
    elif case == "delivered_not_on_chain":
        e = ev(g, "delivered", checkpoint=5, status="DONE")
    elif case == "split_drops_links":
        e = ev(g, "task_split", source="llm", task="T1", children=[{"id": "T9", "title": "a", "links": []},
                                                                  {"id": "T10", "title": "b", "links": []}])
    elif case == "add_unknown_req":
        e = ev(g, "task_added", source="self_report", task="T9", title="t", links=["R77"])
    elif case == "job_key_dup":
        j = next(iter(g.jobs.values()))
        e = ev(g, "job_started", job="J99", key=j.key, tree=j.tree, selection=None, purpose="dev")
    with pytest.raises(IllegalEvent):
        apply(g, e)


def test_events_after_delivery_are_rejected():
    s = ready_sim()
    s.do(__import__("belay.core.rules", fromlist=["deliver"]).deliver, "test")
    with pytest.raises(IllegalEvent):
        apply(s.g, ev(s.g, "task_claimed", task="T1", worker="w1", head=0))


def test_replay_equals_live_graph():
    s = ready_sim()
    assert replay(s.log) == s.g

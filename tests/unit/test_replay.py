"""重放一致性：用随机的请求序列驱动规则（作业乱序完成、作业丢失、CAS 失败、回退、拆分、会话起止……），检查

  1. 每个事务之后不变量成立（模拟器里做）；日志满足来源纪律与序号连续；
  2. replay(日志) == 实时维护的图；
  3. 任意前缀：replay(前缀) == 那个时刻的图；
  4. 快照（JSON 往返）+ 重放尾部 == 重放全部；
  5. 事件经 JSON 往返（即经过事件存储）后重放，结果不变。
"""
from __future__ import annotations

import json
import random

import pytest

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.events import Event
from belay.core.invariants import check, check_log
from belay.core.model import ACTIVE, graph_from_json, to_json
from belay.core.queries import chain, held_tasks, open_attempt
from belay.core.reduce import replay
from belay.core.rules import Rejected
from tests.sim import Sim

ADD, MUL, Z, W = ("tests/test_mod.py::test_add", "tests/test_mod.py::test_mul", "tests/test_other.py::test_z",
                  "tests/test_other.py::test_w")
BASE = {ADD: "FAILED", MUL: "PASSED", Z: "PASSED", W: "PASSED"}
TASK = ("Fix the add function so that it returns the sum.\n"
        "Also make mul handle negative numbers correctly.\n"
        "Document the new behaviour in the module docstring please.\n"
        "Speed up the other module without changing its results.")
PLAN = {"requirements": [
    {"id": "a", "quote": "Fix the add function so that it returns the sum.", "summary": "add"},
    {"id": "b", "quote": "Also make mul handle negative numbers correctly.", "summary": "mul"},
    {"id": "c", "quote": "Document the new behaviour in the module docstring please.", "summary": "docs"},
    {"id": "d", "quote": "Speed up the other module without changing its results.", "summary": "perf"}],
    "tasks": [{"id": "x", "title": "fix add", "links": ["a"], "checks": [ADD]},
              {"id": "y", "title": "fix mul", "links": ["b"], "blocked_by": ["x"]},
              {"id": "z", "title": "docs", "links": ["c"], "blocked_by": ["y"]},
              {"id": "p", "title": "perf", "links": ["d"], "checks": [Z, W]}]}
FILES = [[("pkg/mod.py", 3, 1)], [("pkg/other.py", 2, 2)], [("setup.py", 1, 0)], [("docs/x.md", 1, 0)],
         [("pkg/mod.py", 1, 1), ("pkg/other.py", 1, 1)]]


def drive(seed: int, steps: int = 160):
    rnd = random.Random(seed)
    cfg = BelayConfig(lease_ttl_sec=300, stall_no_progress_sec=900, reserve_min_sec=60,
                      confirm_regressions=rnd.random() < 0.7)
    s = Sim(BASE, cfg=cfg, auto_jobs=False)
    s.setup(TASK, PLAN, budget=rnd.choice([2500, 20000]))
    s.do(R.start_session, "w1", "first", {})
    snapshots = [(s.g.seq, s.g)]
    n_tree = [0]

    def new_tree() -> str:
        n_tree[0] += 1
        tree = f"tree{seed}_{n_tree[0]}"
        over = {}
        r = rnd.random()
        if r < 0.5:
            over[ADD] = "PASSED"
        if rnd.random() < 0.25:
            over[rnd.choice([MUL, Z, W])] = rnd.choice(["FAILED", "ERROR", "SKIPPED"])
        if rnd.random() < 0.1:
            s.world.flaky_once.add((tree, rnd.choice([MUL, Z, W])))
        s.world.define(tree, over)
        return tree

    def some_task():
        return rnd.choice(sorted(s.g.tasks)) if s.g.tasks else "T1"

    ops = ["claim", "claim", "release", "review", "review", "checkpoint", "add_task", "note", "blocked", "job",
           "job", "job", "job", "tick", "heartbeat", "session", "rollback", "run_check", "split", "cas_fail",
           "verify_head"]
    for _ in range(steps):
        op = rnd.choice(ops)
        held = [t.id for t in held_tasks(s.g, "w1") if t.status == ACTIVE]
        try:
            if op == "claim":
                s.do(R.claim, "w1", some_task())
            elif op == "release" and held:
                s.do(R.release, "w1", rnd.choice(held), "later")
            elif op == "review" and held:
                s.do(R.request_review, "w1", rnd.choice(held), s.obs(new_tree(), files=rnd.choice(FILES)))
            elif op == "checkpoint":
                tree = new_tree() if rnd.random() < 0.8 else s.g.head_cp.tree
                s.do(R.request_checkpoint, "w1", s.obs(tree, files=rnd.choice(FILES)), "worker")
            elif op == "add_task":
                s.do(R.add_task, "w1", f"extra {rnd.randint(0, 99)}", [rnd.choice(sorted(s.g.requirements))],
                     blocked_by=[some_task()] if rnd.random() < 0.3 else [])
            elif op == "note":
                s.do(R.note, "w1", f"note {rnd.random():.3f}")
            elif op == "blocked":
                s.do(R.report_blocked, "w1", some_task(), rnd.choice(R.BLOCK_KINDS), "stuck",
                     quote="Also make mul handle negative numbers correctly.")
            elif op == "job" and s.pending_jobs:
                jid = rnd.choice(list(s.pending_jobs))
                r = rnd.random()
                state = "unknown" if r < 0.1 else "cancelled" if r < 0.14 else "finished"
                s.finish_job(jid, state=state)
            elif op == "tick":
                s.advance(rnd.uniform(0, 400))
                s.do(R.tick)
            elif op == "heartbeat":
                s.advance(rnd.uniform(0, 300))
                s.do(R.heartbeat, "w1")
            elif op == "session":
                if s.g.workers["w1"].session:
                    s.do(R.end_session, "w1", rnd.choice(["done", "handoff", "crash"]),
                         todos=[{"content": "x", "status": "pending"}])
                else:
                    s.do(R.start_session, "w1", R.session_reason(s.g, "w1"), {})
            elif op == "rollback" and open_attempt(s.g) is None:
                s.do(R.rollback, "w1", rnd.choice([c.id for c in chain(s.g)]))
            elif op == "run_check":
                s.do(R.run_check, "w1", new_tree(), tests=[MUL] if rnd.random() < 0.5 else [], full=rnd.random() < 0.2,
                     changed=["pkg/mod.py"])
            elif op == "split":
                t = s.g.tasks.get(some_task())
                if t is not None:
                    s.do(R.split_task, t.id, [{"title": "part 1", "links": list(t.links)},
                                              {"title": "part 2", "links": list(t.links)}])
            elif op == "cas_fail":
                s.ref_ok = False
            elif op == "verify_head":
                s.do(R.verify_head)
        except Rejected:
            pass
        s.ref_ok = True if rnd.random() < 0.7 else s.ref_ok
        snapshots.append((s.g.seq, s.g))
    # 收尾：跑完剩下的作业，然后交付
    while s.pending_jobs:
        s.finish_job(s.pending_jobs[0])
    if s.g.workers["w1"].session:
        s.do(R.end_session, "w1", "done")
    s.do(R.deliver, "test")
    snapshots.append((s.g.seq, s.g))
    return s, snapshots


@pytest.mark.parametrize("seed", range(40))
def test_replay_consistency(seed):
    s, snaps = drive(seed)
    log, g = s.log, s.g
    # 1
    assert not check(g) and not check_log(log)
    # 2
    assert replay(log) == g
    # 3
    rnd = random.Random(seed)
    for seq, gk in rnd.sample(snaps, min(12, len(snaps))):
        assert replay(log[:seq]) == gk
    # 4
    for k in rnd.sample(range(len(log) + 1), 6):
        snap = graph_from_json(json.loads(json.dumps(to_json(replay(log[:k])))))
        assert replay(log[k:], start=snap) == g
    # 5
    wire = [Event.from_dict(json.loads(json.dumps(e.to_dict()))) for e in log]
    assert replay(wire) == g


def test_fuzz_actually_exercises_the_rules():
    """确认随机驱动覆盖了关键路径（不然一致性测试没有意义）。"""
    types: set[str] = set()
    reasons: set[str] = set()
    for seed in range(40):
        s, _ = drive(seed)
        types |= {e.type for e in s.log}
        reasons |= {e.get("reason") for e in s.log if e.type in ("task_reopened", "checkpoint_rejected")}
    must = {"task_claimed", "task_released", "review_requested", "task_done", "task_blocked", "task_reopened",
            "task_split", "task_added", "checkpoint_attempted", "checkpoint_advancing", "checkpoint_created",
            "checkpoint_rejected", "rollback", "lease_renewed", "lease_expired", "stall_detected", "job_started",
            "job_finished", "session_started", "session_ended", "note", "wip_recorded", "deadline_reserve",
            "delivered"}
    assert must <= types, must - types
    assert {"checkpoint_rejected", "evidence_failed", "rolled_back", "regression", "cas_conflict"} <= reasons

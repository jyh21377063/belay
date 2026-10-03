"""重放一致性：用随机的请求序列驱动规则（作业乱序完成、作业丢失、CAS 失败、提交、复核结论（含失败、格式坏、
豁免、分数）、todo、回退、会话起止……），检查

  1. 每个事务之后不变量成立（模拟器里做）；日志满足来源纪律与序号连续；
  2. replay(日志) == 实时维护的图；
  3. 任意前缀：replay(前缀) == 那个时刻的图；
  4. 快照（JSON 往返）+ 重放尾部 == 重放全部；
  5. 事件经 JSON 往返（即经过事件存储）后重放，结果不变。
"""
from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.events import Event
from belay.core.invariants import check, check_log, llm_effects
from belay.core.model import TODO_COMPLETED, Graph, graph_from_json, to_json
from belay.core.queries import chain
from belay.core.reduce import apply, replay
from belay.core.rules import Rejected
from tests.sim import MANUAL, Sim

ADD, MUL, Z, W, V = ("tests/test_mod.py::test_add", "tests/test_mod.py::test_mul", "tests/test_other.py::test_z",
                     "tests/test_other.py::test_w", "tests/test_other.py::test_v")
BASE = {ADD: "FAILED", MUL: "PASSED", Z: "PASSED", W: "PASSED", V: "FAILED"}
TASK = ("# Release notes\n"
        "Fix the add function so that it returns the sum.\n"
        "Also make mul handle negative numbers correctly.\n"
        "Document the new behaviour in the module docstring please.\n"
        "Speed up the other module without changing its results.")
PLAN = {"requirements": [
    {"id": "a", "quote": "Fix the add function so that it returns the sum.", "summary": "add", "checks": [ADD]},
    {"id": "b", "quote": "Also make mul handle negative numbers correctly.", "summary": "mul", "checks": [MUL]},
    {"id": "c", "quote": "Document the new behaviour in the module docstring please.", "summary": "docs"},
    {"id": "d", "quote": "Speed up the other module without changing its results.", "summary": "perf",
     "checks": [Z, V]}]}
FILES = [[("pkg/mod.py", 3, 1)], [("pkg/other.py", 2, 2)], [("setup.py", 1, 0)], [("docs/x.md", 1, 0)],
         [("pkg/mod.py", 1, 1), ("pkg/other.py", 1, 1)]]
TODOS = ["read the code", "fix add (R1)", "mul negatives R2", "docstring", "speed up other (R4)"]


IMP_TITLES = ["cover mul edge cases", "doc examples", "faster other module", "more add tests", "x"]
IMP_QUOTES = ["make mul handle negative numbers correctly", "Speed up the other module", "not in the task at all", ""]


def improve_verdict(r: random.Random, g) -> dict:
    """随机的改进项结论：判定已有的（含格式坏的）、提新的（挂得上 / 挂不上原文、目标）、宣布没有值得做的了。"""
    out: dict = {}
    ids = list(g.improvements) + ["I9"]
    if r.random() < 0.8:
        statuses = ["done", "done", "partial", "not_done", "dropped", "maybe"]
        out["improvements"] = [{"id": i, "status": r.choice(statuses),
                                "level": r.choice(["E1", "E2", "E3", "E0", None]), "runs": r.choice([[], ["X1"]]),
                                "tests": r.choice([[], ["tests/test_mod.py::test_add"]]),
                                "reason": r.choice(["", "not worth it"])} for i in r.sample(ids, min(len(ids), 4))]
    if r.random() < 0.6:
        out["new_improvements"] = [{"title": r.choice(IMP_TITLES), "why": "w", "quote": r.choice(IMP_QUOTES),
                                    "objective": r.random() < 0.3} for _ in range(r.randint(1, 3))]
    if r.random() < 0.2:
        out["no_more_improvements"] = r.choice(["nothing left", "", True])
    return out


def drive(seed: int, steps: int = 200, boundaries: bool = False, improve: bool | None = None):
    """boundaries=True：多勾 todo、偶尔撤回（revert），间隔取边界快照的默认量级——用来检查后台怎么挑快照。"""
    rnd = random.Random(seed)
    cfg = BelayConfig(stall_no_progress_sec=900, reserve_min_sec=60, confirm_regressions=rnd.random() < 0.7,
                      locate=rnd.random() < 0.9, locate_max_steps=rnd.choice([3, 8]),
                      background=rnd.choice(["latest", "latest", "latest", "handoff", "off"]),
                      reviewer=rnd.random() < 0.85, review_retries=rnd.choice([0, 1]),
                      merge_min_interval_sec=rnd.choice([0, 0, 300, 600]), merge_todo_interval_sec=rnd.choice([0, 100]),
                      waive_max_tests=rnd.choice([1, 20]), stall_same_failure=rnd.choice([2, 3]))
    if boundaries:
        cfg = replace(cfg, merge_min_interval_sec=rnd.choice([0, 900]), merge_todo_interval_sec=rnd.choice([0, 60]))
    # 改进阶段（after_accept=improve）：用单独的随机数，不打乱上面的主序列
    rimp = random.Random(seed * 7919 + 13)
    if improve if improve is not None else rimp.random() < 0.4:
        cfg = replace(cfg, after_accept="improve", improve_max_open=rimp.choice([1, 3, 5]),
                      improve_idle_sessions=rimp.choice([1, 2]))
    s = Sim(BASE, cfg=cfg, auto_jobs=False, auto_located=rnd.random() < 0.8, reviewer=MANUAL)
    s.setup(TASK, PLAN, budget=rnd.choice([1500, 20000]))
    s.do(R.start_session, "w1", "first", {})
    snapshots = [(s.g.seq, s.g)]
    n_tree = [0]

    def new_tree() -> str:
        n_tree[0] += 1
        tree = f"tree{seed}_{n_tree[0]}"
        over = {}
        if rnd.random() < 0.5:
            over[ADD] = "PASSED"
        if rnd.random() < 0.4:
            over[V] = "PASSED"
        if rnd.random() < 0.25:
            over[rnd.choice([MUL, Z, W])] = rnd.choice(["FAILED", "ERROR", "SKIPPED"])
        if rnd.random() < 0.1:
            s.world.flaky_once.add((tree, rnd.choice([MUL, Z, W])))
        s.world.define(tree, over)
        return tree

    def todo_list() -> list[dict]:
        items = rnd.sample(TODOS, rnd.randint(1, len(TODOS)))
        return [{"content": c, "status": rnd.choice(["pending", "in_progress", "completed"])} for c in items]

    ops = ["submit", "submit", "submit_head", "job", "job", "job", "job", "job", "job", "tick", "session", "rollback",
           "cas_fail", "snap", "snap", "snap", "snap", "todos", "todos", "diagnosis", "review", "review", "review",
           "locate_diff"]
    reasons = ["writes", "writes", "model_test", "session_end", "handoff"]
    if boundaries:
        ops += ["todos", "todos", "todos", "tick", "review", "job", "job"]
        reasons += ["writes", "revert"]
    for _ in range(steps):
        op = rnd.choice(ops)
        try:
            if op == "submit":
                blocked, waivers = [], []
                if rnd.random() < 0.2:
                    blocked = [{"requirement": rnd.choice(["R2", "R3", "R4", "R1"]),
                                "kind": rnd.choice(R.BLOCK_KINDS), "reason": "stuck",
                                "quote": "Also make mul handle negative numbers correctly."}]
                if rnd.random() < 0.35:
                    waivers = [{"tests": [rnd.choice([MUL, Z, W])], "reason": "old behaviour",
                                "quote": "make mul handle negative numbers correctly"}]
                s.submit(new_tree() if rnd.random() < 0.85 else s.g.head_cp.tree, files=rnd.choice(FILES),
                         testable=rnd.random() < 0.95, blocked=blocked, waivers=waivers)
            elif op == "submit_head":
                n = s.snap(s.g.head_cp.tree, files=rnd.choice(FILES), reason="submit")
                s.do(R.request_submit, "w1", n, "on the head")
            elif op == "snap":
                tree = new_tree() if rnd.random() < 0.85 else s.g.head_cp.tree
                s.snap(tree, files=rnd.choice(FILES), testable=rnd.random() < 0.9, reason=rnd.choice(reasons))
            elif op == "todos":
                todos = todo_list()
                n = s.snap(new_tree(), reason="todo", testable=not boundaries or rnd.random() < 0.9) \
                    if R.newly_completed(s.g, todos) else None
                s.do(R.update_todos, "w1", todos, n)
            elif op == "job" and s.pending_jobs:
                jid = rnd.choice(list(s.pending_jobs))
                if rnd.random() < 0.1:
                    s.do(R.job_preempted, jid)
                r = rnd.random()
                state = "unknown" if r < 0.08 else "cancelled" if r < 0.12 else "finished"
                s.finish_job(jid, state=state)
            elif op == "tick":
                s.advance(rnd.uniform(0, 400))
                s.do(R.tick)
            elif op == "session":
                if s.g.workers["w1"].session:
                    s.do(R.end_session, "w1", rnd.choice(["done", "handoff", "crash", "submitted"]))
                else:
                    s.do(R.start_session, "w1", R.session_reason(s.g, "w1"), {})
            elif op == "rollback":
                s.do(R.rollback, "w1", rnd.choice([c.id for c in chain(s.g)]) if rnd.random() < 0.5 else None)
            elif op == "cas_fail":
                s.ref_ok = False
            elif op == "diagnosis":
                for d in [d for d in s.g.diagnoses.values() if d.status == "requested"][:1]:
                    s.do(R.record_diagnosis, d.id, {"suspects": [{"file": "pkg/mod.py"}],
                                                    "intentional": {"likely": rnd.random() < 0.5,
                                                                    "quote": rnd.choice(["nope", TASK[20:50]])}})
            elif op == "review":
                for v in [v for v in s.g.reviews.values() if v.status == "running"][:1]:
                    r = rnd.random()
                    if r < 0.12:
                        s.review(v.id, None, failed=True)
                    elif r < 0.18:
                        s.review(v.id, {"garbage": True})
                    else:
                        items = []
                        for rid in rnd.sample(["R1", "R2", "R3", "R4", "R9"], rnd.randint(0, 4)):
                            items.append({"id": rid, "status": rnd.choice(["done", "partial", "not_done", "blocked",
                                                                           "maybe"]),
                                          "level": rnd.choice(["E0", "E1", "E2", "E3", "E7"]),
                                          "tests": rnd.sample([ADD, MUL, Z, V], rnd.randint(0, 2)),
                                          "runs": rnd.choice([[], ["X1"], ["X5"]]), "missing": ["m"],
                                          "regressed": rnd.random() < 0.3, "reason": "r"})
                        waivers = []
                        gate = [x.rsplit(" (", 1)[0] for x in v.gate.get("regressions") or []]
                        if rnd.random() < 0.6:
                            waivers = [{"tests": gate if gate and rnd.random() < 0.7 else [rnd.choice([MUL, Z, W])],
                                        "reason": "contradicts",
                                        "quote": rnd.choice(["make mul handle negative numbers correctly", "nope"])}]
                        verdict = {"merge": rnd.random() < 0.8, "reason": "r", "summary": "s",
                                   "requirements": items, "waivers": waivers,
                                   "score": rnd.choice([None, None, rnd.random()]), "feedback": "f"}
                        if s.cfg.improve:
                            verdict.update(improve_verdict(rimp, s.g))
                        s.review(v.id, verdict, runs=rnd.choice([[], [{"id": "X1", "cmd": "c", "rc": 0}]]))
            elif op == "locate_diff":
                for loc in [l for l in s.g.locates.values() if l.status == "concluded"][:1]:
                    for i in range(len(loc.groups)):
                        s.do(R.record_located, loc.id, i, [("pkg/mod.py", 1, 1)], None)
        except Rejected:
            pass
        s.ref_ok = True if rnd.random() < 0.7 else s.ref_ok
        snapshots.append((s.g.seq, s.g))
    # 收尾：进入收尾，跑完剩下的作业，然后交付
    if s.g.workers["w1"].session:
        s.do(R.end_session, "w1", "done")
    s.do(R.begin_finalize, "test")
    while s.pending_jobs:
        s.finish_job(s.pending_jobs[0])
    s.do(R.deliver, "test")
    snapshots.append((s.g.seq, s.g))
    return s, snapshots


@pytest.mark.parametrize("seed", range(40))
def test_replay_consistency(seed):
    s, snaps = drive(seed)
    log, g = s.log, s.g
    # 1
    assert not check(g) and not check_log(log) and not llm_effects(log)
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
    statuses: set[str] = set()
    levels: set[str] = set()
    for seed in range(40):
        s, _ = drive(seed)
        types |= {e.type for e in s.log}
        reasons |= {e.get("reason") for e in s.log if e.type in ("requirement_judged", "merge_rejected")}
        statuses |= {e.get("status") for e in s.log if e.type == "submit_updated"}
        levels |= {e.get("level") for e in s.log if e.type == "requirement_judged" and e.get("status") == "done"}
    must = {"requirement_judged", "todos_updated", "todo_completed", "todo_anchored", "todo_invalidated",
            "submit_requested", "submit_updated", "merge_requested", "merge_advancing", "merged", "merge_rejected",
            "merge_superseded", "rollback", "stall_detected", "job_started", "job_finished", "session_started",
            "session_ended", "snapshot_taken", "deadline_reserve", "delivered", "locate_started", "locate_concluded",
            "regression_located", "diagnosis_requested", "diagnosis_recorded", "review_started", "merge_reviewed",
            "review_decided", "review_cancelled", "waiver_granted", "persistent_regression", "finalize_started",
            "job_preempted"}
    assert must <= types, must - types
    assert {"regression", "cas_conflict", "precheck", "review", "rolled_back", "reassessed", "self_report",
            "requirement_regression"} <= reasons, reasons
    assert {"accepted", "returned"} <= statuses, statuses
    assert {"E0", "E1", "E2", "E3"} <= levels, levels


# ======================================================================== 后台挑快照：边界快照优先（性质检查）

def _bg_choice_problems(log, cfg: BelayConfig) -> tuple[list[str], dict]:
    """逐个事件重放；每个后台合并请求发起前的那一刻检查它挑的快照：
      - 不回退：比这个 worker 同一段里之前所有请求的快照都新；
      - 只有一个：发起时这个 worker 没有别的进行中的请求（不抢占、不排队）；
      - 边界优先：auto 发起时，链头 / 上次请求之后、撤回（revert）之后没有可合并的边界快照（勾掉 todo 的锚点、交接）；
        todo / 交接发起时，它之后也没有更新的可合并边界；
      - auto 只取最新的可测快照；交接模式与降级时只有交接。"""
    problems, seen = [], {"auto": 0, "todo": 0, "handoff": 0, "coalesced": 0}
    g = Graph()
    for e in log:
        if e.type == "merge_requested" and e.get("lane") == "bg":
            w, n, trig = e.get("worker"), int(e.get("snapshot")), e.get("trigger")
            seen[trig if trig in seen else "handoff"] += 1
            mine = [a for a in g.attempts.values() if a.worker == w and a.epoch == g.epoch]
            floor = max((a.snapshot for a in mine), default=0)
            tried = {a.tree for a in g.attempts.values() if a.epoch == g.epoch}
            head = g.head_cp
            lo = max(floor, head.snapshot if head.epoch == g.epoch else 0)
            where = f"seq {e.seq} {trig}@{n}"
            if n <= floor:
                problems.append(f"{where}: went back to or before snapshot {floor}")
            if any(a.status in ("pending", "advancing") and a.lane == "bg" for a in mine):
                problems.append(f"{where}: another background request was still open")
            if (cfg.background == "handoff" or g.degraded) and trig != "handoff":
                problems.append(f"{where}: only handoffs should merge in this mode")

            def boundary(k: int) -> bool:
                s = g.snapshots[k]
                if not s.testable or s.tree == head.tree or s.tree in tried:
                    return False
                if cfg.background == "handoff" or g.degraded:
                    return s.reason in R.HANDOFF_REASONS
                return s.reason in R.HANDOFF_REASONS or any(
                    t.status == TODO_COMPLETED and t.anchor_snapshot == k and t.anchor_epoch == s.epoch
                    for t in g.todos.values())

            later = sorted((k for k, s in g.snapshots.items() if s.worker == w and s.epoch == g.epoch and not s.lost
                            and k > lo), reverse=True)
            for k in later:                                          # 从最新往回，直到撤回或前台快照
                s = g.snapshots[k]
                if s.reason in R.FOREGROUND_REASONS:
                    break
                if k == n:
                    if trig == "todo" and not boundary(k):
                        problems.append(f"{where}: not a todo anchor")
                    if trig == "todo" and len([t for t in g.todos.values() if t.status == TODO_COMPLETED
                                               and t.anchor_epoch == s.epoch and lo < (t.anchor_snapshot or 0) <= k]) > 1:
                        seen["coalesced"] += 1
                    if trig == "auto" and s.reason != "revert":
                        continue                                     # auto：更早的也不能是边界（撤回之前的除外）
                    break
                if boundary(k):
                    problems.append(f"{where}: skipped the newer boundary snapshot {k} ({s.reason})")
                    break
                if trig == "auto" and s.testable and k > n:
                    problems.append(f"{where}: snapshot {k} is newer and testable")
                    break
                if s.reason == "revert":
                    break
        g = apply(g, e)
    return problems, seen


@pytest.mark.parametrize("seed", range(60))
def test_background_picks_the_newest_boundary_under_random_interleavings(seed):
    s, _ = drive(1000 + seed, steps=250, boundaries=True)
    problems, _ = _bg_choice_problems(s.log, s.cfg)
    assert not problems, problems
    assert not check_log(s.log) and replay(s.log) == s.g


def test_boundary_fuzz_actually_coalesces_todos_and_falls_back_to_auto():
    total = {"auto": 0, "todo": 0, "handoff": 0, "coalesced": 0}
    for seed in range(60):
        s, _ = drive(1000 + seed, steps=250, boundaries=True)
        for k, v in _bg_choice_problems(s.log, s.cfg)[1].items():
            total[k] += v
    assert all(v >= 5 for v in total.values()), total


# ======================================================================== 改进阶段（after_accept=improve）

@pytest.mark.parametrize("seed", range(30))
def test_replay_consistency_in_improve_mode(seed):
    s, snaps = drive(5000 + seed, steps=250, improve=True)
    log, g = s.log, s.g
    assert not check(g) and not check_log(log) and not llm_effects(log)
    assert replay(log) == g
    for k in random.Random(seed).sample(range(len(log) + 1), 4):
        snap = graph_from_json(json.loads(json.dumps(to_json(replay(log[:k])))))
        assert replay(log[k:], start=snap) == g
    for i in g.improvements.values():                          # 改进项只由校验过的复核结论产生
        assert i.review in g.reviews and (i.quote or i.objective)


def test_fuzz_actually_exercises_the_improve_phase():
    types: set[str] = set()
    statuses: set[str] = set()
    triggers: set[str] = set()
    for seed in range(30):
        s, _ = drive(5000 + seed, steps=250, improve=True)
        types |= {e.type for e in s.log}
        statuses |= {e.get("status") for e in s.log if e.type == "improvement_judged"}
        triggers |= {e.get("trigger") for e in s.log if e.type == "review_started"}
    assert {"improve_started", "improvement_proposed", "improvement_judged", "improve_closed"} <= types, types
    assert {"done", "dropped", "open"} <= statuses, statuses
    assert "improve" in triggers

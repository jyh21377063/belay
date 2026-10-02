"""不变量：在事务边界上成立（一个请求产生的一批事件整体提交）；测试里每个事务之后都检查，运行时由 check_invariants 控制。

check(g) 检查视图内部的一致性；check_log(events) 检查只有看日志才能判断的性质（来源纪律、序号连续、交付后封口）。
"""
from __future__ import annotations

from typing import Iterable

from belay.core.events import LLM, SELF_REPORT, Event
from belay.core.model import (ACTIONABLE, ATT_ADVANCING, ATT_CREATED, ATT_PENDING, BY_SELF, E3, JOB_FINISHED,
                              JOB_RUNNING, LEVELS, REQ_BLOCKED, REQ_DONE, REQ_KINDS, REQ_OPEN, REQ_STATUSES,
                              REV_RUNNING, SUB_OPEN, TODO_ANCHORED, Graph)
from belay.core.queries import chain, is_ancestor
from belay.core.verify import PASSED, regression_ids, results_for_tree

AFTER_DELIVERY_OK = {"session_ended", "job_finished", "job_preempted", "runtime_recovered", "compacted"}
# 合并、判定、豁免、交付：只能来自规则或观察（需求的自述判定只能是 E0 或自述受阻）
EVIDENCE_EVENTS = ("merged", "merge_advancing", "todo_anchored", "delivered", "review_decided", "waiver_granted")


def check(g: Graph) -> list[str]:
    bad: list[str] = []
    # 需求
    if g.frozen and not any(r.kind == ACTIONABLE for r in g.requirements.values()):
        bad.append("no actionable requirement")
    for r in g.requirements.values():
        if r.kind not in REQ_KINDS:
            bad.append(f"{r.id} has a bad kind {r.kind}")
        if r.status not in REQ_STATUSES:
            bad.append(f"{r.id} has a bad status {r.status}")
        if r.kind != ACTIONABLE and r.status != REQ_OPEN:
            bad.append(f"context requirement {r.id} is {r.status}")
        if r.status in (REQ_DONE, REQ_BLOCKED):
            cp = g.checkpoints.get(r.checkpoint) if r.checkpoint is not None else None
            if cp is None or cp.abandoned or not is_ancestor(g, cp.id, g.head):
                bad.append(f"{r.id} is {r.status} on a merge point that is not on the chain ({r.checkpoint})")
                continue
            if r.status == REQ_DONE:
                if r.level not in LEVELS:
                    bad.append(f"{r.id} is done without an evidence level")
                if r.level == E3:
                    res = results_for_tree(g, cp.tree)
                    failing = [t for t in r.tests if res.get(t) != PASSED]
                    if not r.tests or failing:
                        bad.append(f"{r.id} is done (E3) but its tests {failing[:3]} do not pass on {cp.id}")
                if r.level == "E0" and r.by != BY_SELF:
                    bad.append(f"{r.id} is done with E0 by {r.by}: only a self-report is E0")
        elif r.level is not None:
            bad.append(f"{r.id} is {r.status} with an evidence level")
    # 提交：每个 worker 至多一个在判定中的提交
    for w in g.workers:
        n = [s.id for s in g.submits.values() if s.worker == w and s.status in SUB_OPEN]
        if len(n) > 1:
            bad.append(f"several open submits for {w}: {n}")
    for s in g.submits.values():
        if s.checkpoint is not None and s.checkpoint not in g.checkpoints:
            bad.append(f"submit {s.id} is on unknown merge point {s.checkpoint}")
    # 复核：同一时刻至多一个；正在复核的合并请求还在等结果
    running = [v for v in g.reviews.values() if v.status == REV_RUNNING]
    if len(running) > 1:
        bad.append(f"several reviews running: {[v.id for v in running]}")
    for v in running:
        if v.attempt is not None and g.attempts[v.attempt].status != ATT_PENDING:
            bad.append(f"review {v.id} is running for merge request {v.attempt} which is "
                       f"{g.attempts[v.attempt].status}")
    # 合并链
    if g.checkpoints:
        if g.head not in g.checkpoints:
            bad.append(f"head {g.head} is not a merge point")
        else:
            seen, cur = set(), g.head
            while cur is not None:
                if cur in seen:
                    bad.append("merge chain has a cycle")
                    break
                seen.add(cur)
                cp = g.checkpoints[cur]
                if cp.abandoned:
                    bad.append(f"abandoned merge point {cur} is on the chain")
                cur = cp.parent
            if 0 not in seen:
                bad.append("merge chain does not reach merge point 0")
            snaps = [c.snapshot for c in reversed(chain(g))]
            if any(b <= a for a, b in zip(snaps, snaps[1:])):
                bad.append(f"snapshot numbers along the chain are not increasing: {snaps}")
        for cp in g.checkpoints.values():
            if cp.id == 0:
                continue
            a = g.attempts.get(cp.attempt or "")
            if a is None or a.status != ATT_CREATED or a.checkpoint != cp.id:
                bad.append(f"merge point {cp.id} does not come from a created merge request")
            elif a.regressions and not set(regression_ids(a.regressions)) <= set(g.waived):
                bad.append(f"merge point {cp.id} was created with regressions")
            elif a.tree != cp.tree:
                bad.append(f"merge point {cp.id} tree differs from its reviewed tree")
            if cp.review is not None and cp.review not in g.reviews:
                bad.append(f"merge point {cp.id} refers to unknown review {cp.review}")
    # todo
    on_chain = chain(g)
    for t in g.todos.values():
        if t.status == TODO_ANCHORED:
            if not any(c.epoch == t.anchor_epoch and c.snapshot >= (t.anchor_snapshot or 0) for c in on_chain):
                bad.append(f"anchored todo {t.id} is not contained by any merge point on the chain")
    # 并发与去重
    adv = [a.id for a in g.attempts.values() if a.status == ATT_ADVANCING]
    if len(adv) > 1:
        bad.append(f"several merges advancing: {adv}")
    lanes: dict[tuple, list] = {}
    for a in g.attempts.values():
        if a.status in (ATT_PENDING, ATT_ADVANCING):
            lanes.setdefault((a.worker, a.lane), []).append(a.id)
    for k, v in lanes.items():
        if len(v) > 1:
            bad.append(f"several open {k[1]} merge requests for {k[0]}: {v}")
    live_keys: dict[str, str] = {}
    for j in g.jobs.values():
        if j.state in (JOB_RUNNING, JOB_FINISHED):
            if j.key in live_keys:
                bad.append(f"jobs {live_keys[j.key]} and {j.id} share key {j.key}")
            live_keys[j.key] = j.id
            if g.job_keys.get(j.key) != j.id:
                bad.append(f"job_keys does not point to {j.id}")
    # 会话
    for w in g.workers.values():
        if w.session is not None and (w.session not in g.sessions or g.sessions[w.session].ended_t is not None):
            bad.append(f"{w.id} points to a closed session {w.session}")
    # 快照
    ns = sorted(g.snapshots)
    if ns and ns != list(range(1, len(ns) + 1)):
        bad.append("snapshot numbers are not contiguous")
    return bad


def check_log(events: Iterable[Event]) -> list[str]:
    bad: list[str] = []
    prev = 0
    delivered = False
    for e in events:
        if e.seq != prev + 1:
            bad.append(f"seq gap: {prev} -> {e.seq}")
        prev = e.seq
        if e.type in EVIDENCE_EVENTS and e.source in (SELF_REPORT, LLM):
            bad.append(f"{e.type} at {e.seq} comes from {e.source}")
        if e.type == "requirement_judged" and e.source == SELF_REPORT and \
                not (e.get("status") == REQ_BLOCKED or (e.get("status") == REQ_DONE and e.get("level") == "E0")):
            bad.append(f"self-reported requirement_judged at {e.seq} claims more than E0")
        if e.type == "requirement_judged" and e.source == LLM:
            bad.append(f"requirement_judged at {e.seq} comes from the reviewer without validation")
        if delivered and e.type not in AFTER_DELIVERY_OK:
            bad.append(f"{e.type} at {e.seq} after delivery")
        if e.type == "delivered":
            delivered = True
    return bad


def llm_effects(events: list[Event]) -> list[str]:
    """复核者的结论（merge_reviewed，llm）不能跳过规则直接引起合并或判定：紧跟着它的必须是同一次复核的 review_decided
    （规则校验后的决定），豁免与需求判定都在决定之后。诊断只是建议，不引起任何状态变化。"""
    bad = []
    for i, e in enumerate(events):
        if e.type != "merge_reviewed" or e.get("failed"):        # 失败的复核没有内容可用
            continue
        nxt = events[i + 1] if i + 1 < len(events) else None
        if nxt is None or nxt.type != "review_decided" or nxt.get("review") != e.get("review"):
            bad.append(f"merge_reviewed at {e.seq} is not followed by its rule decision")
    for e in events:
        if e.source == LLM and e.type in ("requirement_judged", "merged", "merge_advancing", "waiver_granted",
                                          "todo_anchored", "review_decided"):
            bad.append(f"{e.type} at {e.seq} comes from the llm")
    return bad

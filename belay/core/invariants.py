"""不变量：在事务边界上成立（一个请求产生的一批事件整体提交）；测试里每个事务之后都检查，运行时由 check_invariants 控制。

check(g) 检查视图内部的一致性；check_log(events) 检查只有看日志才能判断的性质（来源纪律、序号连续、交付后封口）。
"""
from __future__ import annotations

from typing import Iterable

from belay.core.events import LLM, SELF_REPORT, Event
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_CREATED, DONE, DONE_UNVERIFIED, JOB_FINISHED, JOB_RUNNING,
                              REVIEW, SPLIT, Graph)
from belay.core.queries import has_cycle, linked_tasks
from belay.core.verify import PASSED, results_for_tree

AFTER_DELIVERY_OK = {"session_ended", "job_finished", "note", "runtime_recovered", "compacted"}


def check(g: Graph) -> list[str]:
    bad: list[str] = []
    # 2 租约
    for tid, lease in g.leases.items():
        if lease.task != tid:
            bad.append(f"lease key {tid} != {lease.task}")
        t = g.tasks.get(tid)
        if t is None or t.status not in (ACTIVE, REVIEW):
            bad.append(f"lease on {tid} whose status is {t.status if t else None}")
        if lease.worker not in g.workers:
            bad.append(f"lease on {tid} held by unknown worker {lease.worker}")
    for t in g.tasks.values():
        if t.status == ACTIVE and t.id not in g.leases:
            bad.append(f"active task {t.id} has no lease")
    # 3 需求与依赖
    if g.frozen:
        for rid in g.requirements:
            if g.tasks and not linked_tasks(g, rid):
                bad.append(f"requirement {rid} is not linked by any unsplit task")
    for t in g.tasks.values():
        for r in t.links:
            if r not in g.requirements:
                bad.append(f"{t.id} links unknown requirement {r}")
        for d in t.blocked_by:
            if d not in g.tasks:
                bad.append(f"{t.id} blocked_by unknown task {d}")
    cyc = has_cycle({t.id: t.blocked_by for t in g.tasks.values()})
    if cyc:
        bad.append(f"dependency cycle {cyc}")
    # 4 存档链
    if g.checkpoints:
        if g.head not in g.checkpoints:
            bad.append(f"head {g.head} is not a checkpoint")
        else:
            seen, cur = set(), g.head
            while cur is not None:
                if cur in seen:
                    bad.append("checkpoint chain has a cycle")
                    break
                seen.add(cur)
                cp = g.checkpoints[cur]
                if cp.abandoned:
                    bad.append(f"abandoned checkpoint {cur} is on the chain")
                cur = cp.parent
            if 0 not in seen:
                bad.append("checkpoint chain does not reach checkpoint 0")
        for cp in g.checkpoints.values():
            if cp.id == 0:
                continue
            a = g.attempts.get(cp.attempt or "")
            if a is None or a.status != ATT_CREATED or a.checkpoint != cp.id:
                bad.append(f"checkpoint {cp.id} does not come from a created attempt")
            elif a.regressions:
                bad.append(f"checkpoint {cp.id} was created with regressions")
            elif a.tree != cp.tree:
                bad.append(f"checkpoint {cp.id} tree differs from its verified tree")
    # 5 完成
    for t in g.tasks.values():
        if t.status in (DONE, DONE_UNVERIFIED):
            cp = g.checkpoints.get(t.done_checkpoint) if t.done_checkpoint is not None else None
            if cp is None or cp.abandoned:
                bad.append(f"{t.id} is {t.status} on a checkpoint that is not on the chain ({t.done_checkpoint})")
                continue
            if t.status == DONE:
                if not t.checks:
                    bad.append(f"{t.id} is done (verified) without checks")
                res = results_for_tree(g, cp.tree)
                failing = [c for c in t.checks if res.get(c) != PASSED]
                if failing:
                    bad.append(f"{t.id} is done but {failing[:3]} do not pass on checkpoint {cp.id}")
            elif t.checks:
                bad.append(f"{t.id} is done_unverified but has checks")
    # 6 并发与去重
    adv = [a.id for a in g.attempts.values() if a.status == ATT_ADVANCING]
    if len(adv) > 1:
        bad.append(f"several attempts advancing: {adv}")
    live_keys: dict[str, str] = {}
    for j in g.jobs.values():
        if j.state in (JOB_RUNNING, JOB_FINISHED):
            if j.key in live_keys:
                bad.append(f"jobs {live_keys[j.key]} and {j.id} share key {j.key}")
            live_keys[j.key] = j.id
            if g.job_keys.get(j.key) != j.id:
                bad.append(f"job_keys does not point to {j.id}")
    # 7 拆分
    for t in g.tasks.values():
        if t.status == SPLIT:
            covered = {r for c in t.children for r in g.tasks[c].links}
            if not set(t.links) <= covered:
                bad.append(f"split {t.id} lost links {sorted(set(t.links) - covered)}")
    # 会话
    for w in g.workers.values():
        if w.session is not None and (w.session not in g.sessions or g.sessions[w.session].ended_t is not None):
            bad.append(f"{w.id} points to a closed session {w.session}")
    return bad


def check_log(events: Iterable[Event]) -> list[str]:
    bad: list[str] = []
    prev = 0
    delivered = False
    for e in events:
        if e.seq != prev + 1:
            bad.append(f"seq gap: {prev} -> {e.seq}")
        prev = e.seq
        # 8 来源纪律：完成与存档不能来自自述或 LLM
        if e.type in ("task_done", "checkpoint_created", "checkpoint_advancing") and e.source in (SELF_REPORT, LLM):
            bad.append(f"{e.type} at {e.seq} comes from {e.source}")
        # 9 交付后封口
        if delivered and e.type not in AFTER_DELIVERY_OK:
            bad.append(f"{e.type} at {e.seq} after delivery")
        if e.type == "delivered":
            delivered = True
    return bad

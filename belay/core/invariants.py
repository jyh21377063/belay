"""不变量：在事务边界上成立（一个请求产生的一批事件整体提交）；测试里每个事务之后都检查，运行时由 check_invariants 控制。

check(g) 检查视图内部的一致性；check_log(events) 检查只有看日志才能判断的性质（来源纪律、序号连续、交付后封口）。
"""
from __future__ import annotations

from typing import Iterable

from belay.core.events import LLM, SELF_REPORT, Event
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_CREATED, ATT_PENDING, CONFIRMED, DONE, DONE_UNVERIFIED,
                              JOB_FINISHED, JOB_RUNNING, REVIEW, SPLIT, STEP_ANCHORED, Graph)
from belay.core.queries import chain, has_cycle, latest_confirmed_ancestor
from belay.core.verify import PASSED, results_for_tree

AFTER_DELIVERY_OK = {"session_ended", "job_finished", "job_preempted", "note", "runtime_recovered", "compacted"}
# 完成、存档、提升类事件：只能来自观察或规则
EVIDENCE_EVENTS = ("task_done", "checkpoint_created", "checkpoint_advancing", "checkpoint_confirmed",
                   "step_anchored", "delivered")


def check(g: Graph) -> list[str]:
    bad: list[str] = []
    # 持有者：active / review 的任务有且只有一个持有者
    for tid, lease in g.leases.items():
        if lease.task != tid:
            bad.append(f"lease key {tid} != {lease.task}")
        t = g.tasks.get(tid)
        if t is None or t.status not in (ACTIVE, REVIEW):
            bad.append(f"holder on {tid} whose status is {t.status if t else None}")
        if lease.worker not in g.workers:
            bad.append(f"{tid} held by unknown worker {lease.worker}")
    for t in g.tasks.values():
        if t.status == ACTIVE and t.id not in g.leases:
            bad.append(f"active task {t.id} has no holder")
    # 需求与依赖（依赖是排序提示，但仍然要无环）
    if g.frozen and g.tasks:
        linked = {r for t in g.tasks.values() if t.status != SPLIT for r in t.links}
        for rid in g.requirements:
            if rid not in linked:
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
    # 存档链
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
            ids = [c for c in chain(g)]
            snaps = [c.snapshot for c in reversed(ids)]
            if any(b <= a for a, b in zip(snaps, snaps[1:])):
                bad.append(f"snapshot numbers along the chain are not increasing: {snaps}")
            if g.confirmed != latest_confirmed_ancestor(g, g.head):
                bad.append(f"confirmed pointer {g.confirmed} is not the latest confirmed ancestor of the head")
            if g.confirmed is not None and g.confirmed not in seen:
                bad.append(f"confirmed checkpoint {g.confirmed} is not on the chain")
        for cp in g.checkpoints.values():
            if cp.level == CONFIRMED and cp.demoted:
                bad.append(f"checkpoint {cp.id} is both confirmed and demoted")
            if cp.id == 0:
                continue
            a = g.attempts.get(cp.attempt or "")
            if a is None or a.status != ATT_CREATED or a.checkpoint != cp.id:
                bad.append(f"checkpoint {cp.id} does not come from a created attempt")
            elif a.regressions:
                bad.append(f"checkpoint {cp.id} was created with regressions")
            elif a.tree != cp.tree:
                bad.append(f"checkpoint {cp.id} tree differs from its verified tree")
    # 完成
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
    # 步骤：anchored 的步骤，其锚点快照被链上某个同段存档包含
    on_chain = chain(g)
    for s in g.steps.values():
        if s.task not in g.tasks:
            bad.append(f"step {s.id} of unknown task {s.task}")
        if s.status == STEP_ANCHORED:
            if not any(c.epoch == s.anchor_epoch and c.snapshot >= (s.anchor_snapshot or 0) for c in on_chain):
                bad.append(f"anchored step {s.id} is not contained by any checkpoint on the chain")
    # 并发与去重
    adv = [a.id for a in g.attempts.values() if a.status == ATT_ADVANCING]
    if len(adv) > 1:
        bad.append(f"several attempts advancing: {adv}")
    lanes: dict[tuple, list] = {}
    for a in g.attempts.values():
        if a.status in (ATT_PENDING, ATT_ADVANCING):
            lanes.setdefault((a.worker, a.lane), []).append(a.id)
    for k, v in lanes.items():
        if len(v) > 1:
            bad.append(f"several open {k[1]} attempts for {k[0]}: {v}")
    live_keys: dict[str, str] = {}
    for j in g.jobs.values():
        if j.state in (JOB_RUNNING, JOB_FINISHED):
            if j.key in live_keys:
                bad.append(f"jobs {live_keys[j.key]} and {j.id} share key {j.key}")
            live_keys[j.key] = j.id
            if g.job_keys.get(j.key) != j.id:
                bad.append(f"job_keys does not point to {j.id}")
    # 拆分
    for t in g.tasks.values():
        if t.status == SPLIT:
            covered = {r for c in t.children for r in g.tasks[c].links}
            if not set(t.links) <= covered:
                bad.append(f"split {t.id} lost links {sorted(set(t.links) - covered)}")
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
        # 来源纪律：完成、存档、提升不能来自自述或 LLM
        if e.type in EVIDENCE_EVENTS and e.source in (SELF_REPORT, LLM):
            bad.append(f"{e.type} at {e.seq} comes from {e.source}")
        # 交付后封口
        if delivered and e.type not in AFTER_DELIVERY_OK:
            bad.append(f"{e.type} at {e.seq} after delivery")
        if e.type == "delivered":
            delivered = True
    return bad


def llm_effects(events: list[Event]) -> list[str]:
    """llm 来源的事件在同一批里不能紧跟着引起完成、存档或提升（它们只能引起重开与新增）。"""
    bad = []
    for i, e in enumerate(events):
        if e.source != LLM:
            continue
        for f in events[i + 1:i + 4]:
            if f.type in ("task_done", "checkpoint_created", "checkpoint_confirmed", "step_anchored"):
                bad.append(f"{f.type} at {f.seq} follows llm event {e.type} at {e.seq}")
            if f.source == LLM or f.type in ("job_finished", "snapshot_taken"):
                break
    return bad

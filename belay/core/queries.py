"""对图的只读查询：可做的任务、需求状态、存档链、进展……规则、调度建议、上下文构建、不变量共用。"""
from __future__ import annotations

from typing import Optional

from belay.core.config import BelayConfig
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_PENDING, BLOCKED, DONE, DONE_UNVERIFIED, FINISHED, OPEN,
                              RESOLVED, REVIEW, SPLIT, Checkpoint, Graph, Session, Task)


def num(ident: str) -> int:
    digits = "".join(ch for ch in ident if ch.isdigit())
    return int(digits) if digits else 0


def next_id(prefix: str, existing) -> str:
    return f"{prefix}{max([num(k) for k in existing] + [0]) + 1}"


# ---------------------------------------------------------------- 任务

def deps_done(g: Graph, t: Task) -> bool:
    return all(g.tasks[d].status in FINISHED for d in t.blocked_by if d in g.tasks)


def is_ready(g: Graph, t: Task) -> bool:
    return t.status == OPEN and deps_done(g, t) and t.id not in g.leases


def ready_tasks(g: Graph) -> list[Task]:
    return sorted((t for t in g.tasks.values() if is_ready(g, t)), key=lambda t: num(t.id))


def held_tasks(g: Graph, worker: str) -> list[Task]:
    return sorted((g.tasks[l.task] for l in g.leases.values() if l.worker == worker), key=lambda t: num(t.id))


def holder(g: Graph, task_id: str) -> Optional[str]:
    lease = g.leases.get(task_id)
    return lease.worker if lease else None


def workable(g: Graph) -> list[Task]:
    """还能推进的任务：可做的、正在做的、待验证的。"""
    return sorted((t for t in g.tasks.values() if is_ready(g, t) or t.status in (ACTIVE, REVIEW)),
                  key=lambda t: num(t.id))


def stranded(g: Graph) -> list[Task]:
    """open 但依赖永远不会完成（依赖受阻）的任务。"""
    out = []
    for t in g.tasks.values():
        if t.status == OPEN and not deps_done(g, t):
            if any(_dep_dead(g, d, set()) for d in t.blocked_by):
                out.append(t)
    return sorted(out, key=lambda t: num(t.id))


def _dep_dead(g: Graph, tid: str, seen: set) -> bool:
    if tid in seen or tid not in g.tasks:
        return False
    seen.add(tid)
    t = g.tasks[tid]
    if t.status in (BLOCKED, SPLIT):
        return True
    return t.status == OPEN and any(_dep_dead(g, d, seen) for d in t.blocked_by)


def downstream(g: Graph, task_id: str) -> set[str]:
    """所有（传递地）被这个任务阻塞、还没完成的任务。"""
    rev: dict[str, list[str]] = {}
    for t in g.tasks.values():
        for d in t.blocked_by:
            rev.setdefault(d, []).append(t.id)
    out: set[str] = set()
    stack = [task_id]
    while stack:
        for nxt in rev.get(stack.pop(), []):
            if nxt not in out and g.tasks[nxt].status not in RESOLVED + (SPLIT,):
                out.add(nxt)
                stack.append(nxt)
    return out


def linked_tasks(g: Graph, req_id: str) -> list[Task]:
    return sorted((t for t in g.tasks.values() if req_id in t.links and t.status != SPLIT), key=lambda t: num(t.id))


def has_cycle(edges: dict[str, tuple[str, ...]]) -> Optional[list[str]]:
    color: dict[str, int] = {}
    path: list[str] = []

    def visit(n: str) -> Optional[list[str]]:
        color[n] = 1
        path.append(n)
        for m in edges.get(n, ()):
            if color.get(m) == 1:
                return path[path.index(m):] + [m]
            if color.get(m) is None and m in edges:
                found = visit(m)
                if found:
                    return found
        color[n] = 2
        path.pop()
        return None

    for n in sorted(edges):
        if color.get(n) is None:
            found = visit(n)
            if found:
                return found
    return None


# ---------------------------------------------------------------- 需求

def requirement_status(g: Graph, req_id: str) -> str:
    ts = linked_tasks(g, req_id)
    if not ts:
        return "unlinked"
    sts = {t.status for t in ts}
    if sts <= {DONE}:
        return "done"
    if sts <= set(FINISHED):
        return "done_unverified"
    if sts <= set(RESOLVED):
        return "blocked"
    if sts & {ACTIVE, REVIEW, DONE, DONE_UNVERIFIED, BLOCKED}:
        return "in_progress"
    return "not_started"


def requirement_progressed(g: Graph, req_id: str) -> bool:
    return requirement_status(g, req_id) not in ("not_started", "unlinked")


def requirements_covered(g: Graph) -> bool:
    """每条需求都至少有一个已完成或受阻的任务。"""
    if not g.frozen or not g.requirements:
        return False
    return all(any(t.status in RESOLVED for t in linked_tasks(g, r)) for r in g.requirements)


# ---------------------------------------------------------------- 存档链

def chain(g: Graph) -> list[Checkpoint]:
    """从链头沿 parent 走回基线。"""
    out = []
    cur = g.head
    seen = set()
    while cur is not None and cur not in seen and cur in g.checkpoints:
        seen.add(cur)
        cp = g.checkpoints[cur]
        out.append(cp)
        cur = cp.parent
    return out


def open_attempt(g: Graph, worker: Optional[str] = None):
    for a in g.attempts.values():
        if a.status in (ATT_PENDING, ATT_ADVANCING) and (worker is None or a.worker == worker):
            return a
    return None


def task_files(g: Graph, task_id: str) -> list[tuple[str, int, int]]:
    """任务在存档里改了哪些文件（git 计算）：它被持有期间创建的所有存档的改动合并。"""
    agg: dict[str, list[int]] = {}
    for cp in g.checkpoints.values():
        if cp.abandoned or task_id not in cp.tasks:
            continue
        for path, add, dele in cp.files:
            a = agg.setdefault(path, [0, 0])
            a[0] += add
            a[1] += dele
    return sorted((p, a, d) for p, (a, d) in agg.items())


def last_checkpoint_t(g: Graph) -> float:
    cp = g.head_cp
    return cp.created_t if cp else 0.0


# ---------------------------------------------------------------- 会话与时间

def sessions_of(g: Graph, worker: str) -> list[Session]:
    return sorted((s for s in g.sessions.values() if s.worker == worker), key=lambda s: s.n)


def last_session(g: Graph, worker: str) -> Optional[Session]:
    ss = sessions_of(g, worker)
    return ss[-1] if ss else None


def sessions_without_progress(g: Graph, worker: str) -> int:
    n = 0
    for s in reversed(sessions_of(g, worker)):
        if s.ended_t is None:
            continue
        if s.progress:
            break
        n += 1
    return n


def consecutive_crashes(g: Graph, worker: str) -> int:
    n = 0
    for s in reversed(sessions_of(g, worker)):
        if s.end_reason != "crash":
            break
        n += 1
    return n


def remaining_sec(g: Graph, now: float) -> float:
    return (g.run.deadline_t - now) if g.run else 0.0


def reserve_sec(g: Graph, cfg: BelayConfig) -> float:
    """截止预留：全量验证的实测耗时 × 系数 + 余量，有下限，最多占预算的一定比例。"""
    if g.run is None:
        return 0.0
    reserve = max(cfg.reserve_min_sec, g.baseline_sec * cfg.reserve_factor + cfg.reserve_extra_sec)
    return min(reserve, g.run.budget_sec * cfg.reserve_max_frac)


def notes_of_session(g: Graph, session_id: Optional[str]) -> list:
    return [n for n in g.notes if n.session == session_id]


def compactions_of_session(g: Graph, session_id: Optional[str]) -> list:
    return [c for c in g.compactions if c.session == session_id]

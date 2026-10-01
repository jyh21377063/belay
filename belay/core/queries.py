"""对图的只读查询：可做的任务、需求状态、存档链、快照时间线、步骤、恢复点……
规则、调度建议、上下文构建、不变量共用。"""
from __future__ import annotations

from typing import Iterable, Optional

from belay.core.config import BelayConfig
from belay.core.verify import PT_PASS, jobs_by_tree, point_status
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_PENDING, BLOCKED, CONFIRMED, DONE, DONE_UNVERIFIED,
                              FINISHED, MILESTONE_KINDS, OPEN, RESOLVED, REVIEW, SPLIT, STEP_ACTIVE, STEP_ANCHORED,
                              STEP_DECLARED, STEP_PLANNED, Attempt, Checkpoint, Graph, Session, Snapshot, Step, Task)


def num(ident) -> int:
    digits = "".join(ch for ch in str(ident) if ch.isdigit())
    return int(digits) if digits else 0


def next_id(prefix: str, existing) -> str:
    return f"{prefix}{max([num(k) for k in existing] + [0]) + 1}"


def id_ranges(ids: Iterable[str]) -> str:
    """["T1","T2","T3","T7"] → "T1–T3, T7"（折叠长列表）。"""
    ids = sorted(ids, key=num)
    out, i = [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and num(ids[j + 1]) == num(ids[j]) + 1 and ids[j + 1][0] == ids[i][0]:
            j += 1
        out.append(ids[i] if i == j else f"{ids[i]}–{ids[j]}")
        i = j + 1
    return ", ".join(out)


# ---------------------------------------------------------------- 任务

def deps_done(g: Graph, t: Task) -> bool:
    return all(g.tasks[d].status in RESOLVED + (SPLIT,) for d in t.blocked_by if d in g.tasks)


def unfinished_deps(g: Graph, t: Task) -> list[str]:
    return [d for d in t.blocked_by if d in g.tasks and g.tasks[d].status not in RESOLVED + (SPLIT,)]


def is_ready(g: Graph, t: Task) -> bool:
    """单 worker 下依赖只是排序提示：任何 open 的任务都可以认领。"""
    return t.status == OPEN and t.id not in g.leases


def ready_tasks(g: Graph) -> list[Task]:
    return sorted((t for t in g.tasks.values() if is_ready(g, t)), key=lambda t: num(t.id))


def held_tasks(g: Graph, worker: str) -> list[Task]:
    return sorted((g.tasks[l.task] for l in g.leases.values() if l.worker == worker), key=lambda t: num(t.id))


def focus_task(g: Graph, worker: str) -> Optional[Task]:
    """当前焦点：持有的 active 任务中最近认领的那个。"""
    held = [(l.seq, g.tasks[l.task]) for l in g.leases.values() if l.worker == worker]
    active = [x for x in held if x[1].status == ACTIVE] or held
    return max(active, key=lambda x: (x[0], num(x[1].id)))[1] if active else None


def holder(g: Graph, task_id: str) -> Optional[str]:
    lease = g.leases.get(task_id)
    return lease.worker if lease else None


def workable(g: Graph) -> list[Task]:
    """还能推进的任务：可做的、正在做的、待验证的。"""
    return sorted((t for t in g.tasks.values() if t.status in (OPEN, ACTIVE, REVIEW)), key=lambda t: num(t.id))


def downstream(g: Graph, task_id: str) -> set[str]:
    """所有（传递地）排在这个任务之后、还没解决的任务。"""
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


def all_resolved(g: Graph) -> bool:
    """所有任务都已解决（完成或受阻）：没有 open / active / review 的任务。"""
    return bool(g.frozen and g.tasks) and not workable(g)


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


def chain_ids(g: Graph) -> list[int]:
    return [c.id for c in chain(g)]


def is_ancestor(g: Graph, a: Optional[int], b: Optional[int]) -> bool:
    """a 是 b 的祖先或就是 b。"""
    cur, seen = b, set()
    while cur is not None and cur not in seen and cur in g.checkpoints:
        if cur == a:
            return True
        seen.add(cur)
        cur = g.checkpoints[cur].parent
    return False


def latest_confirmed_ancestor(g: Graph, cid: Optional[int]) -> Optional[int]:
    cur, seen = cid, set()
    while cur is not None and cur not in seen and cur in g.checkpoints:
        seen.add(cur)
        cp = g.checkpoints[cur]
        if cp.level == CONFIRMED and not cp.demoted:
            return cur
        cur = cp.parent
    return None


def latest_milestone(g: Graph) -> int:
    """回退的默认目标：链上最近的里程碑（暂存或确认；0 号基线也是）。"""
    for cp in chain(g):
        if cp.kind in MILESTONE_KINDS:
            return cp.id
    return 0


def suspect(g: Graph, cid: int) -> bool:
    """链上某个被降级的存档之后、还没有自己跑完全量的暂存点。"""
    cp = g.checkpoints.get(cid)
    if cp is None or cp.level == CONFIRMED:
        return False
    cur = cp.parent
    while cur is not None:
        c = g.checkpoints[cur]
        if c.demoted:
            return True
        if c.level == CONFIRMED:
            return False
        cur = c.parent
    return False


def open_attempt(g: Graph, worker: Optional[str] = None, lane: Optional[str] = None) -> Optional[Attempt]:
    for a in sorted(g.attempts.values(), key=lambda a: a.created_seq):
        if a.status in (ATT_PENDING, ATT_ADVANCING) and (worker is None or a.worker == worker) and \
                (lane is None or a.lane == lane):
            return a
    return None


def open_attempts(g: Graph) -> list[Attempt]:
    return [a for a in sorted(g.attempts.values(), key=lambda a: a.created_seq)
            if a.status in (ATT_PENDING, ATT_ADVANCING)]


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


def delivery_checkpoint(g: Graph, cfg: BelayConfig) -> int:
    """交付点：最新的确认点；除基线外没有确认点时按 deliver_unconfirmed 决定。"""
    conf = latest_confirmed_ancestor(g, g.head)
    if conf not in (None, 0) or g.head in (None, 0):
        return conf if conf is not None else 0
    if cfg.deliver_unconfirmed:
        for cp in chain(g):
            if cp.id != 0 and not cp.demoted and not suspect(g, cp.id):
                return cp.id
    return 0


def done_not_delivered(g: Graph, delivered: Optional[int]) -> list[Task]:
    """已完成，但完成点不在交付点的祖先链上（含交付点本身）：改动不在交付物里。"""
    return sorted((t for t in g.tasks.values() if t.status in FINISHED and t.done_checkpoint is not None
                   and not is_ancestor(g, t.done_checkpoint, delivered)), key=lambda t: num(t.id))


# ---------------------------------------------------------------- 快照时间线

def snapshots_in_epoch(g: Graph, epoch: int) -> list[Snapshot]:
    return sorted((s for s in g.snapshots.values() if s.epoch == epoch), key=lambda s: s.n)


def latest_snapshot(g: Graph, worker: Optional[str] = None, testable: bool = False) -> Optional[Snapshot]:
    for n in sorted(g.snapshots, reverse=True):
        s = g.snapshots[n]
        if (worker is None or s.worker == worker) and (not testable or s.testable) and not s.lost:
            return s
    return None


def snapshot_contained(g: Graph, n: Optional[int], epoch: Optional[int]) -> Optional[int]:
    """包含这张快照的最早的链上存档：同一段内、存档的快照序号不小于它。"""
    if n is None:
        return None
    best = None
    for cp in chain(g):
        if cp.epoch == epoch and cp.snapshot >= n:
            if best is None or cp.snapshot < g.checkpoints[best].snapshot:
                best = cp.id
    return best


def checkpoint_snapshot_epoch(g: Graph, cid: int) -> tuple[int, int]:
    cp = g.checkpoints[cid]
    return cp.snapshot, cp.epoch


# ---------------------------------------------------------------- 步骤与恢复点

def steps_of(g: Graph, task_id: str) -> list[Step]:
    return sorted((s for s in g.steps.values() if s.task == task_id), key=lambda s: (s.order, s.n))


def current_step(g: Graph, task_id: Optional[str]) -> Optional[Step]:
    """当前步骤：第一个 active 的；没有就是第一个 planned 的。"""
    if task_id is None:
        return None
    ss = steps_of(g, task_id)
    for st in (STEP_ACTIVE, STEP_PLANNED):
        for s in ss:
            if s.status == st:
                return s
    return None


def active_step(g: Graph, worker: str) -> Optional[Step]:
    t = focus_task(g, worker)
    return current_step(g, t.id) if t is not None else None


def declared_steps(g: Graph, worker: Optional[str] = None) -> int:
    n = 0
    for s in g.steps.values():
        if s.status in (STEP_DECLARED, STEP_ANCHORED):
            if worker is None or holder(g, s.task) in (worker, None):
                n += 1
    return n


def resume_point(g: Graph, worker: str) -> dict:
    """恢复点 = (任务, 步骤, 基底存档, 部分快照)：对图的纯函数查询，不单独存储。"""
    t = focus_task(g, worker)
    snap = latest_snapshot(g, worker)
    out = {"task": t.id if t else None, "step": None, "base": g.head, "partial": snap.n if snap else None,
           "anchored": [], "base_reason": "head"}
    if t is None:
        return out
    cur = current_step(g, t.id)
    out["step"] = cur.id if cur else None
    anchored = [s for s in steps_of(g, t.id) if s.status == STEP_ANCHORED and s.checkpoint is not None]
    out["anchored"] = [s.id for s in anchored]
    if anchored:
        last = max(anchored, key=lambda s: (s.anchor_snapshot or 0))
        out["base"], out["base_reason"] = last.checkpoint, f"step {last.id}"
    elif t.claimed_head is not None and t.claimed_head in g.checkpoints and \
            not g.checkpoints[t.claimed_head].abandoned:
        out["base"], out["base_reason"] = t.claimed_head, "claim"
    return out


# ---------------------------------------------------------------- 会话与时间

def sessions_of(g: Graph, worker: str) -> list[Session]:
    return sorted((s for s in g.sessions.values() if s.worker == worker), key=lambda s: s.n)


def last_session(g: Graph, worker: str) -> Optional[Session]:
    ss = sessions_of(g, worker)
    return ss[-1] if ss else None


def last_ended_session(g: Graph, worker: str) -> Optional[Session]:
    ended = [s for s in sessions_of(g, worker) if s.ended_t is not None]
    return ended[-1] if ended else None


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


def notes_of_task(g: Graph, task_id: str, worker: Optional[str] = None) -> list:
    return [n for n in g.notes if n.task == task_id and (worker is None or n.worker == worker)]


def notes_of_session(g: Graph, session_id: Optional[str]) -> list:
    return [n for n in g.notes if n.session == session_id]


def compactions_of_session(g: Graph, session_id: Optional[str]) -> list:
    return [c for c in g.compactions if c.session == session_id]


def latest_handoff_summary(g: Graph, worker: str) -> Optional[str]:
    for c in reversed(g.compactions):
        if c.worker == worker and c.summary:
            return c.summary
    return None


def open_persistent(g: Graph, test: str) -> bool:
    """持续性回归还没解决：之后没有任何一张同段快照上它通过。"""
    rec = g.persistent.get(test)
    if rec is None or rec.epoch != g.epoch:
        return False
    index = jobs_by_tree(g)
    seen: set[str] = set()
    for n in sorted((n for n in g.snapshots if n > rec.since), reverse=True):
        s = g.snapshots[n]
        if s.epoch != rec.epoch or s.tree in seen:
            continue
        seen.add(s.tree)
        if point_status(g, s.tree, test, index) == PT_PASS:
            return False
    return True


def status_reasons(g: Graph, delivered: Optional[int]) -> list[str]:
    """运行为什么不是 DONE（空列表 = DONE）。DONE 要求：
      - 没有未解决的任务（open / active / review）；
      - 每条需求都被满足：链接它的任务全部完成（done 或 done_unverified），没有受阻的；
      - 复查者没有认定哪个 done_unverified 的任务没做完（截止收尾时复查结果只进账本，这里据此判定）；
      - 没有“完成但未交付”的任务；
      - 交付的是确认点（没有被降级）。
    全部任务受阻、或某条需求只完成了一部分，都如实记为 INCOMPLETE。"""
    out: list[str] = []
    open_ = [t.id for t in workable(g)]
    if open_:
        out.append(f"unfinished: {id_ranges(open_)}")
    for rid in sorted(g.requirements, key=num):
        ts = linked_tasks(g, rid)
        if not ts:
            out.append(f"{rid} has no task")
            continue
        blocked = [t for t in ts if t.status == BLOCKED]
        if blocked and not any(t.status in (OPEN, ACTIVE, REVIEW) for t in ts):
            out.append(f"{rid} is blocked (" + ", ".join(f"{t.id}: {t.blocked_kind}" for t in blocked) + ")")
    weak = [t.id for t in g.tasks.values() if t.status == DONE_UNVERIFIED and t.review in ("no", "partial")]
    if weak:
        out.append(f"the reviewer found {id_ranges(weak)} incomplete")
    nd = [t.id for t in done_not_delivered(g, delivered)]
    if nd:
        out.append(f"finished after the delivered checkpoint (not in the deliverable): {id_ranges(nd)}")
    cp = g.checkpoints.get(delivered) if delivered is not None else None
    if cp is None:
        out.append("nothing was delivered")
    elif cp.demoted or cp.level != CONFIRMED:
        out.append(f"the delivered checkpoint {cp.id} is not confirmed by the full test suite")
    elif cp.id == 0 and g.tasks and any(t.status in FINISHED for t in g.tasks.values()):
        out.append("only the original code is delivered")
    return out

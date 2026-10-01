"""对图的只读查询：需求状态、存档链、快照时间线、todo、恢复点……
规则、上下文构建、渲染、不变量共用。"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from belay.core.config import BelayConfig
from belay.core.verify import B_PASS, PT_PASS, jobs_by_tree, point_status
from belay.core.model import (ACTIONABLE, ATT_ADVANCING, ATT_PENDING, CONFIRMED, MILESTONE_KINDS, REQ_BLOCKED,
                              REQ_FINISHED, REQ_OPEN, REQ_SUBMITTED, REQ_VERIFIED, SUB_ACCEPTED, SUB_OPEN,
                              TODO_ACTIVE, TODO_ANCHORED, TODO_COMPLETED, TODO_PENDING, Attempt, Checkpoint, Graph,
                              Requirement, Session, Snapshot, Submit, Todo)

REQ_ID = re.compile(r"\bR(\d+)\b")


def num(ident) -> int:
    digits = "".join(ch for ch in str(ident) if ch.isdigit())
    return int(digits) if digits else 0


def next_id(prefix: str, existing) -> str:
    return f"{prefix}{max([num(k) for k in existing] + [0]) + 1}"


def id_ranges(ids: Iterable[str]) -> str:
    """["R1","R2","R3","R7"] → "R1–R3, R7"（折叠长列表）。"""
    ids = sorted(ids, key=num)
    out, i = [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and num(ids[j + 1]) == num(ids[j]) + 1 and ids[j + 1][0] == ids[i][0]:
            j += 1
        out.append(ids[i] if i == j else f"{ids[i]}–{ids[j]}")
        i = j + 1
    return ", ".join(out)


# ---------------------------------------------------------------- 需求

def actionable(g: Graph) -> list[Requirement]:
    """要改代码的需求（context 需求只为覆盖原文，不进清单）。"""
    return sorted((r for r in g.requirements.values() if r.kind == ACTIONABLE), key=lambda r: num(r.id))


def open_requirements(g: Graph) -> list[Requirement]:
    return [r for r in actionable(g) if r.status == REQ_OPEN]


def evidence_checks(g: Graph, r: Requirement) -> list[str]:
    """能证明需求做完的检查：在原始代码上不通过的那些。基线上本来就通过的检查已经在回归门里，证明不了任何事。"""
    return [c for c in r.checks if g.baseline.get(c) != B_PASS]


def mentioned_requirements(g: Graph, text: str) -> list[str]:
    """文字里提到的需求编号（只认存在的）。"""
    out = []
    for m in REQ_ID.finditer(text or ""):
        rid = f"R{m.group(1)}"
        if rid in g.requirements and rid not in out:
            out.append(rid)
    return out


def all_resolved(g: Graph) -> bool:
    """所有 actionable 需求都已解决（验证通过、已提交或受阻）。"""
    return bool(g.frozen and actionable(g)) and not open_requirements(g)


def latest_submit(g: Graph, worker: Optional[str] = None) -> Optional[Submit]:
    subs = [s for s in g.submits.values() if worker is None or s.worker == worker]
    return max(subs, key=lambda s: s.seq) if subs else None


def open_submit(g: Graph, worker: Optional[str] = None) -> Optional[Submit]:
    s = latest_submit(g, worker)
    return s if s is not None and s.status in SUB_OPEN else None


def submit_accepted(g: Graph, worker: Optional[str] = None) -> bool:
    s = latest_submit(g, worker)
    return s is not None and s.status == SUB_ACCEPTED


def reviews_running(g: Graph) -> list[str]:
    return sorted((v.id for v in g.reviews.values() if v.status == "running"), key=num)


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


def done_not_delivered(g: Graph, delivered: Optional[int]) -> list[Requirement]:
    """已完成（验证通过或已提交），但所在的存档不在交付点的祖先链上（含交付点本身）：改动不在交付物里。"""
    return [r for r in actionable(g) if r.status in REQ_FINISHED and r.checkpoint is not None
            and not is_ancestor(g, r.checkpoint, delivered)]


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


# ---------------------------------------------------------------- todo 与恢复点

def todos_in_order(g: Graph) -> list[Todo]:
    return sorted(g.todos.values(), key=lambda t: (t.order, t.n))


def current_todo(g: Graph) -> Optional[Todo]:
    """当前的 todo：第一个 in_progress 的；没有就是 None（模型没在做哪一条，或者根本没列）。"""
    for t in todos_in_order(g):
        if t.status == TODO_ACTIVE:
            return t
    return None


def completed_todos(g: Graph) -> int:
    return sum(1 for t in g.todos.values() if t.status in (TODO_COMPLETED, TODO_ANCHORED))


def resume_point(g: Graph, worker: str) -> dict:
    """恢复点 = (基底存档, 部分快照, 当前 todo)：基底就是链头（后台持续验证最新快照，链头紧跟工作区）。"""
    snap = latest_snapshot(g, worker)
    cur = current_todo(g)
    return {"base": g.head, "partial": snap.n if snap else None, "todo": cur.id if cur else None}


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


def compactions_of_session(g: Graph, session_id: Optional[str]) -> list:
    return [c for c in g.compactions if c.session == session_id]


def latest_handoff_summary(g: Graph, worker: str) -> Optional[str]:
    for c in reversed(g.compactions):
        if c.worker == worker and c.summary:
            return c.summary
    return None


def open_persistent(g: Graph, test: str) -> bool:
    """持续性回归还没解决：之后没有任何一张同段快照上它通过（被豁免的检查不再算）。"""
    rec = g.persistent.get(test)
    if rec is None or rec.epoch != g.epoch or test in g.waived:
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
      - 每条 actionable 需求都验证通过，或已提交且复查者没有认定没做完；没有未完成的、没有受阻的；
      - 每条需求所在的存档都在交付点的祖先链上；
      - 交付的是确认点（没有被降级）。
    受阻是诚实的结束方式，但如实记为 INCOMPLETE。"""
    out: list[str] = []
    open_ = [r.id for r in open_requirements(g)]
    if open_:
        out.append(f"unfinished: {id_ranges(open_)}")
    for r in actionable(g):
        if r.status == REQ_BLOCKED:
            out.append(f"{r.id} is blocked ({r.blocked_kind})")
    weak = [r.id for r in actionable(g) if r.status == REQ_SUBMITTED and r.review in ("no", "partial")]
    if weak:
        out.append(f"the reviewer found {id_ranges(weak)} incomplete")
    nd = [r.id for r in done_not_delivered(g, delivered)]
    if nd:
        out.append(f"finished after the delivered checkpoint (not in the deliverable): {id_ranges(nd)}")
    cp = g.checkpoints.get(delivered) if delivered is not None else None
    if cp is None:
        out.append("nothing was delivered")
    elif cp.demoted or cp.level != CONFIRMED:
        out.append(f"the delivered checkpoint {cp.id} is not confirmed by the full test suite")
    elif cp.id == 0 and any(r.status in REQ_FINISHED for r in actionable(g)):
        out.append("only the original code is delivered")
    if not actionable(g):
        out.append("no requirement was recorded")
    return out

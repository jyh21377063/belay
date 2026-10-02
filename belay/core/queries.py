"""对图的只读查询：需求状态、合并链、快照时间线、todo、恢复点……
规则、上下文构建、渲染、不变量共用。"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from belay.core.config import BelayConfig
from belay.core.verify import B_PASS, PASSED, PT_PASS, jobs_by_tree, point_status, results_for_tree
from belay.core.model import (ACTIONABLE, ATT_ADVANCING, ATT_PENDING, COUNTED_LEVELS, E0, REQ_BLOCKED, REQ_DONE,
                              REQ_OPEN, REQ_RESOLVED, REV_DECIDED, REV_RUNNING, SUB_ACCEPTED, SUB_OPEN, TODO_ACTIVE,
                              TODO_ANCHORED, TODO_COMPLETED, Attempt, BY_SELF, Checkpoint, Graph, Requirement, Review,
                              Session, Snapshot, Submit, Todo)

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
    return sorted((r for r in g.requirements.values() if r.kind == ACTIONABLE), key=lambda r: num(r.id))


def open_requirements(g: Graph) -> list[Requirement]:
    return [r for r in actionable(g) if r.status == REQ_OPEN]


def evidence_checks(g: Graph, r: Requirement) -> list[str]:
    """规划器关联的检查里能证明需求做完的：在原始代码上不通过的那些（基线上本来就通过的在回归门里，证明不了什么）。"""
    return [c for c in r.checks if g.baseline.get(c) != B_PASS]


def counted_done(r: Requirement) -> bool:
    """计为完成：done 且证据等级在 E1 及以上（E0 只是自述）。"""
    return r.status == REQ_DONE and r.level in COUNTED_LEVELS


def accepted_blocked(r: Requirement) -> bool:
    """受阻且复核者认可（复核者不可用时的自述受阻不算）。"""
    return r.status == REQ_BLOCKED and r.by != BY_SELF


def done_count(g: Graph) -> int:
    return sum(1 for r in actionable(g) if counted_done(r))


def mentioned_requirements(g: Graph, text: str) -> list[str]:
    out = []
    for m in REQ_ID.finditer(text or ""):
        rid = f"R{m.group(1)}"
        if rid in g.requirements and rid not in out:
            out.append(rid)
    return out


def all_resolved(g: Graph) -> bool:
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


# ---------------------------------------------------------------- 复核

def running_review(g: Graph) -> Optional[Review]:
    for v in sorted(g.reviews.values(), key=lambda v: v.seq):
        if v.status == REV_RUNNING:
            return v
    return None


def reviews_of_tree(g: Graph, tree: str) -> list[Review]:
    return sorted((v for v in g.reviews.values() if v.tree == tree and v.status == REV_DECIDED),
                  key=lambda v: v.seq)


def judged_on_tree(g: Graph, tree: str) -> set[str]:
    """在这棵树上已经被某次复核判定过的需求（复核者没有提到的不算）。"""
    out: set[str] = set()
    for v in reviews_of_tree(g, tree):
        out.update(str(j.get("requirement")) for j in v.decision.get("judgements") or [])
        out.update(str(x) for x in v.decision.get("mentioned") or [])
    return out


def last_bg_review_t(g: Graph) -> Optional[float]:
    """最近一次后台复核开始的时间（节流用；只按回归门被拒的请求没有复核，不计）。"""
    from belay.core.model import BG_TRIGGERS
    ts = [v.t for v in g.reviews.values() if v.trigger in BG_TRIGGERS]
    return max(ts) if ts else None


def last_score(g: Graph) -> tuple[Optional[float], str, Optional[int]]:
    """链上最近一次测到的分数（分数、怎么测的、所在合并点）：合并点没有测分数时不会把门槛清零。"""
    for cp in chain(g):
        if cp.score is not None:
            return cp.score, cp.score_note, cp.id
    return None, "", None


# ---------------------------------------------------------------- 合并链

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


def open_attempt(g: Graph, worker: Optional[str] = None, lane: Optional[str] = None) -> Optional[Attempt]:
    for a in sorted(g.attempts.values(), key=lambda a: a.created_seq):
        if a.status in (ATT_PENDING, ATT_ADVANCING) and (worker is None or a.worker == worker) and \
                (lane is None or a.lane == lane):
            return a
    return None


def open_attempts(g: Graph) -> list[Attempt]:
    return [a for a in sorted(g.attempts.values(), key=lambda a: a.created_seq)
            if a.status in (ATT_PENDING, ATT_ADVANCING)]


def delivery_checkpoint(g: Graph, cfg: Optional[BelayConfig] = None) -> int:
    """交付点 = 链头。合并链是单调的（已完成的需求不退回、分数不比链上最近一次测到的低超过容差、回归门不变差），
    所以链头就是最好的结果；不需要回头在链上挑。"""
    return g.head if g.head is not None else 0


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
    """包含这张快照的最早的链上合并点：同一段内、合并点的快照序号不小于它。"""
    if n is None:
        return None
    best = None
    for cp in chain(g):
        if cp.epoch == epoch and cp.snapshot >= n:
            if best is None or cp.snapshot < g.checkpoints[best].snapshot:
                best = cp.id
    return best


# ---------------------------------------------------------------- todo 与恢复点

def todos_in_order(g: Graph) -> list[Todo]:
    return sorted(g.todos.values(), key=lambda t: (t.order, t.n))


def current_todo(g: Graph) -> Optional[Todo]:
    for t in todos_in_order(g):
        if t.status == TODO_ACTIVE:
            return t
    return None


def completed_todos(g: Graph) -> int:
    return sum(1 for t in g.todos.values() if t.status in (TODO_COMPLETED, TODO_ANCHORED))


def resume_point(g: Graph, worker: str) -> dict:
    """恢复点 = (基底合并点, 部分快照, 当前 todo)：基底就是链头。"""
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
    """截止预留：全量回归门的实测耗时 × 系数 + 余量 + 一次复核，有下限，最多占预算的一定比例。"""
    if g.run is None:
        return 0.0
    review = cfg.reserve_review_sec if cfg.reviewer else 0.0
    reserve = max(cfg.reserve_min_sec, g.baseline_sec * cfg.reserve_factor + cfg.reserve_extra_sec + review)
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


def broken_requirements(g: Graph, tree: str) -> list[tuple[str, list[str]]]:
    """已完成（E3）的需求，它依据的测试在这棵树上不再通过：合并会让它退回，按单调规则不能合并。"""
    res = results_for_tree(g, tree)
    out = []
    for r in actionable(g):
        if r.status == REQ_DONE and r.tests:
            bad = [t for t in r.tests if t in res and res[t] != PASSED]
            if bad:
                out.append((r.id, bad))
    return out


def status_reasons(g: Graph, delivered: Optional[int]) -> list[str]:
    """运行为什么不是 DONE（空列表 = DONE）。DONE 要求交付的合并点上：每条 actionable 需求都完成（E1 及以上），
    或受阻且复核者认可。只有自述（E0）的完成、复核者没有认可的受阻都如实记为 INCOMPLETE。"""
    out: list[str] = []
    open_ = [r.id for r in open_requirements(g)]
    if open_:
        out.append(f"unfinished: {id_ranges(open_)}")
    e0 = [r.id for r in actionable(g) if r.status == REQ_DONE and r.level == E0]
    if e0:
        out.append(f"only self-reported (no evidence from the reviewer): {id_ranges(e0)}")
    for r in actionable(g):
        if r.status == REQ_BLOCKED and not accepted_blocked(r):
            out.append(f"{r.id} is blocked ({r.blocked_kind}, self-reported)")
    nd = [r.id for r in actionable(g) if r.status in REQ_RESOLVED and r.checkpoint is not None
          and delivered is not None and not is_ancestor(g, r.checkpoint, delivered)]
    if nd:
        out.append(f"judged on a merge point that is not delivered: {id_ranges(nd)}")
    cp = g.checkpoints.get(delivered) if delivered is not None else None
    if cp is None:
        out.append("nothing was delivered")
    elif cp.id == 0 and any(r.status == REQ_DONE for r in actionable(g)):
        out.append("only the original code is delivered")
    if not actionable(g):
        out.append("no requirement was recorded")
    return out

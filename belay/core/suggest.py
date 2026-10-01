"""调度建议 suggest(graph, worker, now)：只读图，只给建议，不指派。

候选：可做的任务 + 这个 worker 自己持有的任务。排序（任务无关的通用规则）：
  1. 存档被拒、证据失败、被复查者重开的任务排最前，先让状态回到可存档；
  2. 依赖（排序提示）还没完成的任务排后；解锁下游任务越多越靠前；
  3. 所链接的需求还没有任何进展的靠前，让覆盖面先铺开；
  4. 规划器的优先级提示只用来打破平局。
剩余时间低于截止预留时不给建议：此时 runtime 会停下 worker 并自行做全量存档，不需要模型配合，
也不把剩余时间写进给模型的文字。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from belay.core.config import BelayConfig
from belay.core.model import ACTIVE, REVIEW, Graph, Task
from belay.core.queries import held_tasks, num, ready_tasks, remaining_sec, reserve_sec, unfinished_deps

REOPEN_TEXT = {"checkpoint_rejected": "reopened: its checkpoint was rejected",
               "evidence_failed": "reopened: its checks failed on the checkpoint",
               "rolled_back": "reopened: its checkpoint was rolled back",
               "review_missing": "reopened: the reviewer found parts missing",
               "review_reading": "reopened: the reviewer found a reasonable reading",
               "reclaimed": "reopened"}
REOPEN_FIRST = ("checkpoint_rejected", "evidence_failed", "rolled_back", "review_missing", "review_reading")


@dataclass(frozen=True)
class Suggestion:
    task: Optional[str]
    rank: int
    reason: str


class _Index:
    """一次建议里要用的反向依赖与需求进展（避免对每个候选重新扫描整张图）。"""

    def __init__(self, g: Graph):
        self.g = g
        self.rev: dict[str, list[str]] = {}
        for t in g.tasks.values():
            for d in t.blocked_by:
                self.rev.setdefault(d, []).append(t.id)
        progressed: set[str] = set()
        for t in g.tasks.values():
            if t.status in ("active", "review", "done", "done_unverified", "blocked"):
                progressed.update(t.links)
        self.progressed = progressed
        self._down: dict[str, int] = {}

    def downstream(self, tid: str) -> int:
        if tid not in self._down:
            seen: set[str] = set()
            stack = [tid]
            while stack:
                for nxt in self.rev.get(stack.pop(), []):
                    if nxt not in seen and self.g.tasks[nxt].status in ("open", "active", "review"):
                        seen.add(nxt)
                        stack.append(nxt)
            self._down[tid] = len(seen)
        return self._down[tid]


def _key(ix: _Index, t: Task) -> tuple:
    g = ix.g
    reopened = t.reopen_count > 0 and t.reopen_reason in REOPEN_FIRST
    fresh = any(r not in ix.progressed for r in t.links)
    waiting = 1 if unfinished_deps(g, t) else 0
    return (0 if reopened else 1, waiting, -ix.downstream(t.id), 0 if fresh else 1, -t.priority, num(t.id))


def _reason(ix: _Index, t: Task, worker: str) -> str:
    g = ix.g
    parts = []
    if t.reopen_count and t.reopen_reason:
        parts.append(REOPEN_TEXT.get(t.reopen_reason, f"reopened ({t.reopen_reason})"))
    if t.id in g.leases and g.leases[t.id].worker == worker:
        parts.append("you hold it" + (" (under review)" if t.status == REVIEW else ""))
    deps = unfinished_deps(g, t)
    if deps:
        parts.append(f"after {', '.join(deps[:3])}")
    n = ix.downstream(t.id)
    if n:
        parts.append(f"unblocks {n} task(s)")
    fresh = [r for r in t.links if r not in ix.progressed]
    if fresh:
        parts.append(f"requirement {', '.join(fresh[:3])} has no progress yet")
    if t.priority:
        parts.append(f"planner priority {t.priority}")
    return "; ".join(parts) or "ready"


def suggest(g: Graph, worker: str, now: float, cfg: BelayConfig) -> list[Suggestion]:
    if not cfg.suggest or g.run is None:
        return []
    if g.run.reserve or remaining_sec(g, now) <= reserve_sec(g, cfg):
        return []
    ix = _Index(g)
    held = held_tasks(g, worker)
    cands = {t.id: t for t in ready_tasks(g)}
    cands.update({t.id: t for t in held if t.status == ACTIVE})
    ranked = sorted(cands.values(), key=lambda t: _key(ix, t))
    return [Suggestion(t.id, i + 1, _reason(ix, t, worker)) for i, t in enumerate(ranked[:cfg.suggest_top])]


def suggestion_rank(g: Graph, worker: str, task_id: str, now: float, cfg: BelayConfig) -> Optional[int]:
    """claim 时记录该任务在建议里的名次（不在前几名时为 None），用来评估建议有没有用。"""
    for s in suggest(g, worker, now, cfg):
        if s.task == task_id:
            return s.rank
    return None

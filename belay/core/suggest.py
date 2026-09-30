"""调度建议 suggest(graph, worker, now)：只读图，只给建议，不指派。

候选：可做的任务 + 这个 worker 自己持有的任务。排序（任务无关的通用规则）：
  1. 存档被拒或证据失败而重开的任务排最前，先让状态回到可存档；
  2. 解锁下游任务越多越靠前；
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
from belay.core.queries import (downstream, held_tasks, num, ready_tasks, remaining_sec, requirement_progressed,
                                reserve_sec)

REOPEN_TEXT = {"checkpoint_rejected": "reopened: its checkpoint was rejected",
               "evidence_failed": "reopened: its checks failed on the checkpoint",
               "rolled_back": "reopened: its checkpoint was rolled back",
               "reclaimed": "reopened"}


@dataclass(frozen=True)
class Suggestion:
    task: Optional[str]
    rank: int
    reason: str


def _key(g: Graph, t: Task) -> tuple:
    reopened = t.reopen_count > 0 and t.reopen_reason in ("checkpoint_rejected", "evidence_failed", "rolled_back")
    fresh = any(not requirement_progressed(g, r) for r in t.links)
    return (0 if reopened else 1, -len(downstream(g, t.id)), 0 if fresh else 1, -t.priority, num(t.id))


def _reason(g: Graph, t: Task, worker: str) -> str:
    parts = []
    if t.reopen_count and t.reopen_reason:
        parts.append(REOPEN_TEXT.get(t.reopen_reason, f"reopened ({t.reopen_reason})"))
    if t.id in g.leases and g.leases[t.id].worker == worker:
        parts.append("you hold it" + (" (under review)" if t.status == REVIEW else ""))
    n = len(downstream(g, t.id))
    if n:
        parts.append(f"unblocks {n} task(s)")
    fresh = [r for r in t.links if not requirement_progressed(g, r)]
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
    held = held_tasks(g, worker)
    cands = {t.id: t for t in ready_tasks(g)}
    cands.update({t.id: t for t in held if t.status == ACTIVE})
    ranked = sorted(cands.values(), key=lambda t: _key(g, t))
    return [Suggestion(t.id, i + 1, _reason(g, t, worker)) for i, t in enumerate(ranked[:cfg.suggest_top])]


def suggestion_rank(g: Graph, worker: str, task_id: str, now: float, cfg: BelayConfig) -> Optional[int]:
    """claim 时记录该任务在建议里的名次（不在前几名时为 None），用来评估建议有没有用。"""
    for s in suggest(g, worker, now, cfg):
        if s.task == task_id:
            return s.rank
    return None

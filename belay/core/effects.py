"""副作用的计划：effects_for(事件, 图) -> [Effect]，纯函数。

原则“先写事件，再做副作用”：副作用只从已经写入日志的事件推出，外壳按顺序执行；执行结果作为新的观察回到规则。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from belay.core.events import Event
from belay.core.model import Graph


@dataclass(frozen=True)
class Effect:
    kind: str                         # launch_job | advance_ref | mirror_checkpoint | restore_workspace | deliver |
    args: dict = field(default_factory=dict)   # replan | stop_workers | cancel_orphans | locate_diff | diagnose |
    #                                            review | label_checkpoint


def effects_for(events: Iterable[Event], g: Graph) -> list[Effect]:
    out: list[Effect] = []
    for e in events:
        if e.type == "job_started":
            out.append(Effect("launch_job", {"job": e.get("job")}))
        elif e.type == "checkpoint_advancing":
            out.append(Effect("advance_ref", {"attempt": e.get("attempt")}))
        elif e.type == "checkpoint_created" and int(e.get("checkpoint")) > 0:
            out.append(Effect("mirror_checkpoint", {"checkpoint": int(e.get("checkpoint"))}))
            out.append(Effect("label_checkpoint", {"checkpoint": int(e.get("checkpoint"))}))
        elif e.type == "checkpoint_marked":
            out.append(Effect("mirror_checkpoint", {"checkpoint": int(e.get("checkpoint"))}))
        elif e.type == "rollback":
            out.append(Effect("restore_workspace", {"worker": e.get("worker"), "checkpoint": int(e.get("to")),
                                                    "reset_ref": bool(e.get("abandoned"))}))
        elif e.type == "delivered":
            out.append(Effect("deliver", {"checkpoint": e.get("checkpoint"), "status": e.get("status")}))
        elif e.type == "stall_detected" and e.get("action") == "replan" and e.get("task"):
            out.append(Effect("replan", {"task": e.get("task"), "worker": e.get("worker")}))
        elif e.type == "deadline_reserve":
            out.append(Effect("stop_workers", {"reason": "deadline"}))
        elif e.type == "attempt_superseded":
            out.append(Effect("cancel_orphans", {"attempt": e.get("attempt")}))
        elif e.type == "locate_concluded":
            out.append(Effect("locate_diff", {"locate": e.get("locate"), "groups": len(e.get("groups") or ())}))
        elif e.type == "diagnosis_requested":
            out.append(Effect("diagnose", {"diagnosis": e.get("diagnosis")}))
        elif e.type == "review_started":
            out.append(Effect("review", {"task": e.get("task"), "phase": e.get("phase")}))
    return out

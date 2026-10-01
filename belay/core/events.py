"""事件：只追加的日志是唯一真相。

每条事件 = seq（连续递增，也是图的版本号）、t（墙钟秒）、type、actor、source、payload。
这里定义事件类型、每种事件必需的 payload 字段和允许的来源；reduce.py 负责把事件应用到视图上。

来源纪律：存档、完成、提升只能由 observed 与 rule 驱动；llm 的事件只能引起重开与新增
（诊断、复查、标签只记录，不改变任何完成或存档类状态）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 来源（决定一个字段能不能驱动状态转换）
OBSERVED = "observed"        # runtime / 验证器 / git 亲眼看到的
RULE = "rule"                # 从其他字段按确定规则算出（包括规则接受 worker 请求后做出的转换、由观察推断出的结论）
LLM = "llm"                  # 规划器、压缩器、诊断者、复查者等模型的产出，已经过规则校验
SELF_REPORT = "self_report"  # worker 通过工具报告的内容，只作参考
SOURCES = (OBSERVED, RULE, LLM, SELF_REPORT)

RUNTIME = "runtime"
VERIFIER = "verifier"
PLANNER = "planner"
COMPACTOR = "compactor"
DIAGNOSER = "diagnoser"
REVIEWER = "reviewer"


def worker_actor(worker: str) -> str:
    return f"worker:{worker}"


def actor_worker(actor: str) -> str | None:
    return actor.split(":", 1)[1] if actor.startswith("worker:") else None


@dataclass(frozen=True)
class Spec:
    required: tuple[str, ...]
    sources: tuple[str, ...]


EVENT_SPECS: dict[str, Spec] = {
    # ---- 运行
    "run_started": Spec(("run_id", "task", "budget_sec", "deadline_t", "workers"), (RULE,)),
    "runtime_recovered": Spec(("downtime_sec",), (OBSERVED,)),
    "clock_started": Spec(("deadline_t",), (RULE,)),
    "run_suspended": Spec(("reason",), (RULE,)),
    "deadline_reserve": Spec(("reserve_sec",), (RULE,)),
    "finalize_started": Spec(("reason",), (RULE,)),
    "delivered": Spec(("checkpoint", "status"), (RULE,)),
    # ---- 任务
    "plan_proposed": Spec(("round", "valid", "problems"), (LLM, RULE)),
    "requirement_frozen": Spec(("requirements",), (RULE,)),
    "task_added": Spec(("task", "title"), (LLM, SELF_REPORT, RULE)),
    "task_split": Spec(("task", "children"), (LLM, SELF_REPORT)),
    "task_claimed": Spec(("task", "worker", "head"), (RULE,)),
    "task_released": Spec(("task", "worker"), (RULE,)),
    "review_requested": Spec(("task", "worker"), (RULE,)),
    "task_done": Spec(("task", "checkpoint", "verified"), (RULE,)),
    "task_blocked": Spec(("task", "kind", "reason"), (SELF_REPORT,)),
    "task_reopened": Spec(("task", "reason"), (RULE,)),
    # ---- 步骤（模块 H）
    "steps_planned": Spec(("worker", "task", "steps"), (SELF_REPORT,)),
    "step_started": Spec(("worker", "step"), (SELF_REPORT,)),
    "step_done": Spec(("worker", "step", "snapshot"), (SELF_REPORT,)),
    "step_anchored": Spec(("step", "checkpoint"), (RULE,)),
    "step_invalidated": Spec(("step", "reason"), (RULE,)),
    # ---- 执行
    "session_started": Spec(("session", "worker", "reason", "opening"), (RULE,)),
    "session_resumed": Spec(("session", "mode"), (OBSERVED,)),
    "session_ended": Spec(("session", "worker", "reason"), (OBSERVED,)),
    "compacted": Spec(("session", "level", "before", "after"), (RULE, LLM)),
    "note": Spec(("worker", "kind", "text"), (SELF_REPORT,)),
    "snapshot_taken": Spec(("snapshot", "worker", "tree", "raw_tree", "reason", "testable"), (OBSERVED,)),
    "stall_detected": Spec(("kind", "action"), (RULE,)),
    # ---- 验证
    "job_started": Spec(("job", "key", "tree", "selection", "purpose"), (RULE,)),
    "job_preempted": Spec(("job",), (OBSERVED,)),
    "job_finished": Spec(("job", "state", "results"), (OBSERVED,)),
    "baseline_recorded": Spec(("classes", "available"), (OBSERVED,)),
    "checkpoint_attempted": Spec(("attempt", "worker", "trigger", "tree", "base", "tier", "selection", "tasks",
                                  "snapshot", "lane"), (RULE,)),
    "attempt_superseded": Spec(("attempt", "reason"), (RULE,)),
    "checkpoint_advancing": Spec(("attempt", "parent_commit", "date"), (RULE,)),
    "checkpoint_created": Spec(("checkpoint", "commit", "tree"), (OBSERVED,)),
    "checkpoint_rejected": Spec(("attempt", "regressions", "reason"), (RULE, OBSERVED)),
    "checkpoint_confirmed": Spec(("checkpoint",), (OBSERVED,)),
    "checkpoint_demoted": Spec(("checkpoint", "regressions"), (RULE,)),
    "checkpoint_marked": Spec(("checkpoint", "kind"), (RULE,)),
    "rollback": Spec(("worker", "to", "abandoned"), (RULE,)),
    # ---- 定位、诊断、复查（模块 C–F）
    "persistent_regression": Spec(("tests", "trigger"), (RULE,)),
    "locate_started": Spec(("locate", "tests", "bad", "epoch", "trigger"), (RULE,)),
    "locate_concluded": Spec(("locate", "groups"), (RULE,)),
    "regression_located": Spec(("locate", "tests", "good", "bad", "exact"), (OBSERVED,)),
    "relation_learned": Spec(("pairs",), (RULE,)),
    "diagnosis_requested": Spec(("diagnosis", "trigger", "tests"), (RULE,)),
    "diagnosis_recorded": Spec(("diagnosis",), (LLM,)),
    "review_started": Spec(("task", "phase"), (RULE,)),
    "review_recorded": Spec(("task", "phase", "implemented"), (LLM,)),
    "checkpoint_labeled": Spec(("checkpoint", "label"), (LLM,)),
    "check_waived": Spec(("task", "tests", "quote", "reason"), (RULE,)),
}
EVENT_TYPES = tuple(EVENT_SPECS)
# llm 来源的事件：只能记录、重开、新增，永远不能引起完成、存档、提升（不变量检查）
LLM_ONLY_RECORDS = ("diagnosis_recorded", "review_recorded", "checkpoint_labeled")


class EventError(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    seq: int
    t: float
    type: str
    actor: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "t": self.t, "type": self.type, "actor": self.actor, "source": self.source,
                "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(int(d["seq"]), float(d["t"]), d["type"], d["actor"], d["source"], dict(d.get("payload") or {}))


def validate(e: Event) -> None:
    """事件本身的格式检查（与图无关）；与图有关的合法性由 reduce 检查。"""
    spec = EVENT_SPECS.get(e.type)
    if spec is None:
        raise EventError(f"unknown event type {e.type!r}")
    if e.source not in SOURCES:
        raise EventError(f"{e.type}: unknown source {e.source!r}")
    if e.source not in spec.sources:
        raise EventError(f"{e.type} cannot come from source {e.source!r} (allowed: {spec.sources})")
    missing = [k for k in spec.required if k not in e.payload]
    if missing:
        raise EventError(f"{e.type}: payload is missing {missing}")
    if e.seq < 1:
        raise EventError("seq starts at 1")

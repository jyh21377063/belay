"""事件：只追加的日志是唯一真相。

每条事件 = seq（连续递增，也是图的版本号）、t（墙钟秒）、type、actor、source、payload。
这里定义事件类型、每种事件必需的 payload 字段和允许的来源；reduce.py 负责把事件应用到视图上。

来源纪律（v8）：合并、需求判定、豁免只能由规则或观察产生。复核者（llm）的结论先原样记为 merge_reviewed，
再由规则校验（证据等级、引文、回归门、单调性）后写 review_decided、waiver_granted、requirement_judged；
worker 的自述（todo、提交说明、受阻声明）只是线索，复核者不可用时才以 self_report 记为 E0 / 自述受阻。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 来源
OBSERVED = "observed"        # runtime / 验证器 / git 亲眼看到的
RULE = "rule"                # 从其他字段按确定规则算出（包括校验过的复核结论）
LLM = "llm"                  # 规划器、压缩器、诊断者、复核者等模型的产出
SELF_REPORT = "self_report"  # worker 通过工具报告的内容
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
    "run_started": Spec(("run_id", "task", "budget_sec", "deadline_t", "workers", "version"), (RULE,)),
    "runtime_recovered": Spec(("downtime_sec",), (OBSERVED,)),
    "clock_started": Spec(("deadline_t",), (RULE,)),
    "run_suspended": Spec(("reason",), (RULE,)),
    "deadline_reserve": Spec(("reserve_sec",), (RULE,)),
    "finalize_started": Spec(("reason",), (RULE,)),
    "delivered": Spec(("checkpoint", "status"), (RULE,)),
    # ---- 需求
    "plan_proposed": Spec(("round", "valid", "problems"), (LLM, RULE)),
    "requirement_frozen": Spec(("requirements",), (RULE,)),
    "requirement_judged": Spec(("requirement", "status", "by"), (RULE, SELF_REPORT)),
    # ---- todo
    "todos_updated": Spec(("worker", "todos"), (SELF_REPORT,)),
    "todo_completed": Spec(("worker", "todo", "snapshot"), (SELF_REPORT,)),
    "todo_anchored": Spec(("todo", "checkpoint"), (RULE,)),
    "todo_invalidated": Spec(("todo", "reason"), (RULE,)),
    # ---- 提交（请求立即复核）
    "submit_requested": Spec(("submit", "worker", "snapshot"), (RULE,)),
    "submit_updated": Spec(("submit", "status"), (RULE,)),
    # ---- 执行
    "session_started": Spec(("session", "worker", "reason", "opening"), (RULE,)),
    "session_resumed": Spec(("session", "mode"), (OBSERVED,)),
    "session_ended": Spec(("session", "worker", "reason"), (OBSERVED,)),
    "compacted": Spec(("session", "level", "before", "after"), (RULE, LLM)),
    "snapshot_taken": Spec(("snapshot", "worker", "tree", "raw_tree", "reason", "testable"), (OBSERVED,)),
    "stall_detected": Spec(("kind", "action"), (RULE,)),
    # ---- 验证
    "job_started": Spec(("job", "key", "tree", "selection", "purpose"), (RULE,)),
    "job_preempted": Spec(("job",), (OBSERVED,)),
    "job_finished": Spec(("job", "state", "results"), (OBSERVED,)),
    "baseline_recorded": Spec(("classes", "available"), (OBSERVED,)),
    # ---- 合并（模块 C）
    "merge_requested": Spec(("attempt", "worker", "trigger", "tree", "base", "selection", "snapshot", "lane"),
                            (RULE,)),
    "merge_superseded": Spec(("attempt", "reason"), (RULE,)),
    "merge_advancing": Spec(("attempt", "parent_commit", "date"), (RULE,)),
    "merged": Spec(("checkpoint", "commit", "tree"), (OBSERVED,)),
    "merge_rejected": Spec(("attempt", "regressions", "reason"), (RULE, OBSERVED)),
    "rollback": Spec(("worker", "to", "abandoned"), (RULE,)),
    # ---- 复核（模块 F）
    "review_started": Spec(("review", "trigger", "tree", "snapshot", "focus"), (RULE,)),
    "merge_reviewed": Spec(("review", "verdict", "runs", "failed"), (LLM,)),
    "review_decided": Spec(("review", "merge", "reasons"), (RULE,)),
    "review_cancelled": Spec(("review", "reason"), (RULE,)),
    "waiver_granted": Spec(("tests", "quote", "reason", "review"), (RULE,)),
    # ---- 定位、诊断（模块 D、E）
    "persistent_regression": Spec(("tests", "trigger"), (RULE,)),
    "locate_started": Spec(("locate", "tests", "bad", "epoch", "trigger"), (RULE,)),
    "locate_concluded": Spec(("locate", "groups"), (RULE,)),
    "regression_located": Spec(("locate", "tests", "good", "bad", "exact"), (OBSERVED,)),
    "diagnosis_requested": Spec(("diagnosis", "trigger", "tests"), (RULE,)),
    "diagnosis_recorded": Spec(("diagnosis",), (LLM,)),
}
EVENT_TYPES = tuple(EVENT_SPECS)
# llm 来源的事件：只记录，不直接改变需求、合并链或回归门（由规则校验后另写事件）
LLM_ONLY_RECORDS = ("diagnosis_recorded", "merge_reviewed")


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
        raise EventError(f"unknown event type {e.type!r} (logs written before Belay v8 cannot be replayed by this "
                         "version; analyse them with the v7 code or their ledger.json)")
    if e.source not in SOURCES:
        raise EventError(f"{e.type}: unknown source {e.source!r}")
    if e.source not in spec.sources:
        raise EventError(f"{e.type} cannot come from source {e.source!r} (allowed: {spec.sources})")
    missing = [k for k in spec.required if k not in e.payload]
    if missing:
        if e.type == "run_started" and missing == ["version"]:
            raise EventError("run_started has no version: this log was written before Belay v8 and cannot be "
                             "replayed by this version (use the v7 code or the run's ledger.json)")
        raise EventError(f"{e.type}: payload is missing {missing}")
    if e.seq < 1:
        raise EventError("seq starts at 1")

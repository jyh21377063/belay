"""三个视图的数据模型：任务图（A）、执行状态（B）、存档链（C）。

全部是不可变 dataclass：reduce 返回新图，旧图保持不变（结构共享，只复制被修改的那张表）。
字段后面的注释标明来源：obs（观察）/ rule（规则）/ llm（LLM 提议）/ self（自述）。
"""
from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Optional, Union

# ---- 任务状态
OPEN = "open"                        # 没有人持有
ACTIVE = "active"                    # 有租约，正在做
REVIEW = "review"                    # 已声明做完，等存档与证据
DONE = "done"                        # 关联检查在存档上全部通过
DONE_UNVERIFIED = "done_unverified"  # 没有检查；工作已进入存档
BLOCKED = "blocked"                  # 报告受阻
SPLIT = "split"                      # 已拆分为子任务
TASK_STATUSES = (OPEN, ACTIVE, REVIEW, DONE, DONE_UNVERIFIED, BLOCKED, SPLIT)
FINISHED = (DONE, DONE_UNVERIFIED)
RESOLVED = (DONE, DONE_UNVERIFIED, BLOCKED)

# ---- 作业
JOB_RUNNING, JOB_FINISHED, JOB_UNKNOWN, JOB_CANCELLED = "running", "finished", "unknown", "cancelled"
# ---- 存档尝试
ATT_PENDING, ATT_ADVANCING, ATT_CREATED, ATT_REJECTED = "pending", "advancing", "created", "rejected"
# ---- 运行
RUN_RUNNING, RUN_DONE, RUN_INCOMPLETE = "running", "done", "incomplete"


# ======================================================================== 视图 A：任务图

@dataclass(frozen=True)
class Requirement:
    id: str
    quote: str                       # llm，规则已校验逐字存在于任务原文
    summary: str = ""                # llm
    origin: str = "llm"


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    description: str = ""
    links: tuple[str, ...] = ()              # → Requirement（llm / self）
    blocked_by: tuple[str, ...] = ()         # → Task
    parent: Optional[str] = None             # 拆分自哪个任务
    discovered_from: Optional[str] = None    # 在做哪个任务时发现的
    priority: int = 0                        # llm 的优先级提示，只用来打破平局
    checks: tuple[str, ...] = ()             # verified_by：测试 node id 或 cmd:<name>
    origin: str = "llm"                      # llm | self_report | rule
    status: str = OPEN                       # rule
    created_seq: int = 0
    review_seq: Optional[int] = None
    review_attempt: Optional[str] = None
    review_checkpoint: Optional[int] = None  # 在哪个存档上判定证据
    done_checkpoint: Optional[int] = None
    verified: bool = False
    reopen_count: int = 0
    reopen_reason: Optional[str] = None
    last_failure: tuple[str, ...] = ()       # obs：最近一次被拒 / 证据失败的检查
    blocked_kind: Optional[str] = None       # self
    blocked_reason: Optional[str] = None     # self
    blocked_quote: Optional[str] = None      # self，规则校验逐字存在
    children: tuple[str, ...] = ()


# ======================================================================== 视图 B：执行状态

@dataclass(frozen=True)
class WorkerState:
    id: str
    status: str = "idle"                     # obs：running | idle
    session: Optional[str] = None
    last_heartbeat: float = 0.0


@dataclass(frozen=True)
class Session:
    id: str
    worker: str
    n: int                                   # 这个 worker 的第几个会话（从 1 开始）
    reason: str                              # 为什么开：first | handoff | restart | recover | crash
    started_seq: int
    started_t: float
    opening: dict = field(default_factory=dict)   # rule：开场上下文的摘要（各段 token、被裁的段）
    transcript: Optional[str] = None         # obs：完整对话记录（只用于审计，恢复时不重放）
    ended_t: Optional[float] = None
    end_reason: Optional[str] = None         # obs：done | handoff | crash | stuck | deadline | runtime_crash | max_turns
    peak_context: int = 0
    turns: int = 0
    compactions: tuple[tuple[int, int, int], ...] = ()   # (层级, 压缩前, 压缩后)
    progress: bool = False                   # rule：会话期间（含结束后替它做的存档）是否有新证据
    error: Optional[str] = None


@dataclass(frozen=True)
class Lease:
    task: str
    worker: str
    acquired_t: float
    expires_t: float


@dataclass(frozen=True)
class Wip:
    """未验证的进度（观察）。"""
    worker: str
    base: int                                # 基于哪个存档
    tree: str                                # 剔除测试改动后的候选树
    raw_tree: str = ""                       # 工作区原样的树
    files: tuple[tuple[str, int, int], ...] = ()   # 相对 base 的改动：(路径, 增, 删)
    dropped: tuple[str, ...] = ()            # 被剔除的测试路径改动
    diff: Optional[str] = None               # 完整 diff 附件
    seq: int = 0
    last_rejection: Optional[dict] = None    # 最近一次存档被拒的原因


@dataclass(frozen=True)
class Note:
    seq: int
    t: float
    worker: str
    session: Optional[str]
    kind: str                                # note | todos
    text: str


@dataclass(frozen=True)
class Job:
    id: str
    key: str                                 # (树, 检查集合, 标签) 的键；同键只跑一次
    tree: str
    selection: Optional[tuple[str, ...]]     # None = 全量；否则测试文件或 cmd:<name>
    purpose: str                             # baseline | verify | confirm | evidence | dev
    requested_by: str = "runtime"
    attempt: Optional[str] = None
    live: bool = False                       # 在活的工作区上跑（dev）：结果不作为存档证据
    tag: str = ""
    state: str = JOB_RUNNING
    results: dict = field(default_factory=dict)   # obs：检查 id → PASSED | FAILED | ERROR | SKIPPED | XFAIL
    error: str = ""
    sec: float = 0.0
    started_t: float = 0.0
    finished_t: Optional[float] = None
    replaces: Optional[str] = None


@dataclass(frozen=True)
class Stall:
    seq: int
    t: float
    kind: str                                # no_progress | repeated_failure | sessions_no_progress
    action: str                              # hint | replan | stop
    worker: Optional[str] = None
    task: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class Compaction:
    seq: int
    t: float
    session: str
    worker: str
    level: int
    before: int
    after: int
    summary: Optional[str] = None            # llm（L3）


# ======================================================================== 视图 C：存档链

@dataclass(frozen=True)
class Attempt:
    id: str
    worker: str
    trigger: str                             # worker | review | session_end | handoff | deadline | final
    tree: str
    base: int                                # 尝试时的链头
    tier: str                                # related | full
    selection: Optional[tuple[str, ...]]
    tasks: tuple[str, ...] = ()              # 随这次尝试判定的待验证任务
    jobs: tuple[str, ...] = ()
    status: str = ATT_PENDING
    regressions: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    reason: str = ""
    checkpoint: Optional[int] = None
    parent_commit: Optional[str] = None
    date: Optional[float] = None
    summary: str = ""                        # self
    raw_tree: str = ""
    created_seq: int = 0


@dataclass(frozen=True)
class Checkpoint:
    id: int
    commit: str
    tree: str
    parent: Optional[int]
    created_seq: int
    created_t: float
    attempt: Optional[str] = None
    tier: str = "baseline"
    trigger: str = "baseline"
    files: tuple[tuple[str, int, int], ...] = ()   # obs：相对上一个存档的改动
    tasks: tuple[str, ...] = ()              # rule：创建时 worker 持有 / 随尝试判定的任务
    abandoned: bool = False


# ======================================================================== 运行与整张图

@dataclass(frozen=True)
class Run:
    id: str
    task: str
    budget_sec: float
    started_t: float
    deadline_t: float
    workers: tuple[str, ...] = ("w1",)
    public_checks: tuple[str, ...] = ()
    verifier: bool = True
    status: str = RUN_RUNNING
    reserve: bool = False
    reserve_sec: float = 0.0
    delivered: Optional[int] = None
    recoveries: int = 0
    downtime_sec: float = 0.0


@dataclass(frozen=True)
class Graph:
    seq: int = 0
    run: Optional[Run] = None
    # A
    requirements: dict[str, Requirement] = field(default_factory=dict)
    frozen: bool = False
    tasks: dict[str, Task] = field(default_factory=dict)
    baseline: dict[str, str] = field(default_factory=dict)     # 检查 id → pass | fail | flaky | skip
    baseline_ready: bool = False
    baseline_sec: float = 0.0
    plans: tuple[dict, ...] = ()
    # B
    workers: dict[str, WorkerState] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    leases: dict[str, Lease] = field(default_factory=dict)      # 任务 id → 租约
    wips: dict[str, Wip] = field(default_factory=dict)          # worker → WIP
    notes: tuple[Note, ...] = ()
    jobs: dict[str, Job] = field(default_factory=dict)
    job_keys: dict[str, str] = field(default_factory=dict)      # 键 → 有效的作业（running / finished）
    compactions: tuple[Compaction, ...] = ()
    stalls: tuple[Stall, ...] = ()
    # C
    checkpoints: dict[int, Checkpoint] = field(default_factory=dict)
    attempts: dict[str, Attempt] = field(default_factory=dict)
    head: Optional[int] = None
    # 进展（rule）：最近一次新证据的时间与序号
    last_progress_t: float = 0.0
    last_progress_seq: int = 0

    @property
    def head_cp(self) -> Optional[Checkpoint]:
        return self.checkpoints.get(self.head) if self.head is not None else None


# ======================================================================== 序列化（视图快照）

def to_json(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: to_json(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (tuple, list)):
        return [to_json(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    return obj


_HINTS: dict[type, dict] = {}


def _hints(cls: type) -> dict:
    if cls not in _HINTS:
        _HINTS[cls] = typing.get_type_hints(cls)
    return _HINTS[cls]


def _decode(tp: Any, v: Any) -> Any:
    if v is None:
        return None
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is Union or origin is types.UnionType:
        inner = [a for a in args if a is not type(None)]
        return _decode(inner[0], v) if len(inner) == 1 else v
    if isinstance(tp, type) and is_dataclass(tp):
        h = _hints(tp)
        return tp(**{k: _decode(h[k], x) for k, x in v.items() if k in h})
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], x) for x in v)
        return tuple(_decode(a, x) for a, x in zip(args, v))
    if origin is dict:
        kt, vt = args
        return {_decode(kt, k): _decode(vt, x) for k, x in v.items()}
    if tp is int:
        return int(v)
    if tp is float:
        return float(v)
    return v


def graph_from_json(d: dict) -> Graph:
    return _decode(Graph, d)

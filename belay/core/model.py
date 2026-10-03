"""三个视图的数据模型：需求账本（A）、执行状态（B）、合并链（C）。

全部是不可变 dataclass：reduce 返回新图，旧图保持不变（结构共享，只复制被修改的那张表）。
字段后面的注释标明来源：obs（观察）/ rule（规则）/ llm（LLM 提议，已经过规则校验）/ self（自述）。

v8：worker 只管干活，runtime 只管存档，复核者是唯一的裁判，合并点是唯一的交付单位，账本是唯一的进度来源。

  快照       runtime 在工具边界自动拍：某一刻工作区的样子（恢复工作区、定位回归）
  合并请求   对一张快照发起（Attempt）：先过回归门（有测试时），再由复核者判定“不比上一个合并点差”，
             同一次复核里逐条判定需求
  合并点     通过的合并请求（Checkpoint）。合并点链是单调的：已完成的需求不会在后面的合并点上退回，
             分数不会下降，所以链头就是最好的结果，交付的永远是链头
"""
from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Optional, Union

VERSION = 8

# ---- 需求
ACTIONABLE, CONTEXT = "actionable", "context"     # 要改代码的 / 标题、套话、背景（只为覆盖原文）
REQ_KINDS = (ACTIONABLE, CONTEXT)
REQ_OPEN = "open"                                 # 还没做完（初始；复核者判为 partial / not_done；被回退）
REQ_DONE = "done"                                 # 在某个合并点上判定完成（带证据等级）
REQ_BLOCKED = "blocked"                           # 做不了，且复核者认可（复核者不可用时是自述）
REQ_STATUSES = (REQ_OPEN, REQ_DONE, REQ_BLOCKED)
REQ_RESOLVED = (REQ_DONE, REQ_BLOCKED)
# 证据等级：E3 测试 / E2 运行验证 / E1 代码审读 / E0 只有 worker 的自述（不计为完成）
E0, E1, E2, E3 = "E0", "E1", "E2", "E3"
LEVELS = (E0, E1, E2, E3)
LEVEL_RANK = {E0: 0, E1: 1, E2: 2, E3: 3}
COUNTED_LEVELS = (E1, E2, E3)
# 复核者的判定
J_DONE, J_PARTIAL, J_NOT_DONE, J_BLOCKED = "done", "partial", "not_done", "blocked"
JUDGEMENTS = (J_DONE, J_PARTIAL, J_NOT_DONE, J_BLOCKED)
# 判定的来源（requirement_judged.by）
BY_REVIEW, BY_CHECKS, BY_SELF, BY_ROLLBACK = "review", "checks", "self_report", "rollback"

# ---- 改进项（after_accept=improve：需求都做完之后，复核者提出、复核者判定）
IMP_OPEN, IMP_DONE, IMP_DROPPED = "open", "done", "dropped"
IMP_STATUSES = (IMP_OPEN, IMP_DONE, IMP_DROPPED)

# ---- todo（运行级，镜像 worker 的 todo_write）
TODO_PENDING, TODO_ACTIVE, TODO_COMPLETED, TODO_ANCHORED = "pending", "in_progress", "completed", "anchored"

# ---- 提交（worker 请求立即复核）
SUB_PENDING = "pending"                  # 等合并请求 / 复核的结果
SUB_ACCEPTED = "accepted"                # 没有未完成的 actionable 需求：运行可以收尾
SUB_RETURNED = "returned"                # 还有未完成的需求：清单交还 worker
SUB_REJECTED = "rejected"                # 合并被拒（回归、复核不通过、预检、取消）
SUB_OPEN = (SUB_PENDING,)
SUB_FINAL = (SUB_ACCEPTED, SUB_RETURNED, SUB_REJECTED)

# ---- 作业
JOB_RUNNING, JOB_FINISHED, JOB_UNKNOWN, JOB_CANCELLED = "running", "finished", "unknown", "cancelled"
# 作业在哪里运行：slot（验证目录）| workspace（切换工作区：降级模式与基线的工作区那一次）| live（活的工作区）
WHERE_SLOT, WHERE_WORKSPACE, WHERE_LIVE = "slot", "workspace", "live"
# ---- 合并请求
ATT_PENDING, ATT_ADVANCING, ATT_CREATED, ATT_REJECTED, ATT_SUPERSEDED = (
    "pending", "advancing", "created", "rejected", "superseded")
LANE_FG, LANE_BG = "fg", "bg"
# 触发：auto（后台验证最新快照）、todo（勾掉一条 todo）、handoff / session_end（交接）、submit、final / deadline（收尾）
BG_TRIGGERS = ("auto", "todo", "handoff", "session_end")
# ---- 复核
REV_RUNNING, REV_RECORDED, REV_DECIDED, REV_FAILED, REV_CANCELLED = (
    "running", "recorded", "decided", "failed", "cancelled")
# ---- 运行
RUN_RUNNING, RUN_DONE, RUN_INCOMPLETE = "running", "done", "incomplete"


# ======================================================================== 视图 A：需求账本

@dataclass(frozen=True)
class Requirement:
    id: str
    quote: str                               # llm，规则已校验逐字存在于任务原文
    summary: str = ""                        # llm
    origin: str = "llm"
    kind: str = ACTIONABLE                   # llm：actionable | context
    checks: tuple[str, ...] = ()             # llm（规则校验存在）：已有测试 node id 或 cmd:<name>
    acceptance: str = ""                     # llm：规划器写的验收方法（只是描述）
    status: str = REQ_OPEN                   # rule：open | done | blocked，只由合并时的判定改变
    level: Optional[str] = None              # rule：done 的证据等级 E0–E3
    judgement: Optional[str] = None          # rule：最近一次判定 done | partial | not_done | blocked
    by: Optional[str] = None                 # rule：最近一次判定的来源 review | checks | self_report | rollback
    evidence: tuple[str, ...] = ()           # llm（已校验）：证据摘要
    tests: tuple[str, ...] = ()              # rule：E3 依据的测试，在 checkpoint 那棵树上全部通过
    runs: tuple[str, ...] = ()               # rule：E2 依据的复核命令编号
    missing: tuple[str, ...] = ()            # llm：还缺什么（partial / not_done，或复核者不认可受阻时的读法）
    checkpoint: Optional[int] = None         # rule：最近一次判定所在的合并点
    review: Optional[str] = None             # rule：最近一次判定来自哪次复核
    judged_seq: Optional[int] = None
    blocked_kind: Optional[str] = None       # self / llm
    blocked_reason: Optional[str] = None
    blocked_quote: Optional[str] = None
    misses: int = 0                          # rule：连续被判为 partial / not_done 的次数（提醒 worker 用）
    reason: Optional[str] = None             # rule：最近一次状态变化的原因（reassessed / rolled_back / ...）
    passed_checks: tuple[str, ...] = ()      # obs：在某个合并点上第一次通过过的证据检查（进展）
    history: tuple[tuple[int, str, str], ...] = ()   # rule：(序号, 状态, 原因)，最多保留最近 30 条


@dataclass(frozen=True)
class Improvement:
    """改进项（after_accept=improve）：需求清单都判完成之后，复核者提出的“还能怎样加强已交付的版本”。
    每条挂到任务原文的引文（规则逐字校验）或可测的目标（链上测过分数）上。不影响 DONE 的判定；做完它（或分数提高）
    算进展。只由复核者判定，规则校验证据等级。"""
    id: str                                  # I1
    n: int
    title: str                               # llm（已校验）：要做什么
    why: str = ""                            # llm：为什么值得做
    quote: str = ""                          # llm（规则校验逐字存在于任务原文）：它服务的那段原文
    objective: bool = False                  # llm（规则校验链上测过分数）：它是为了提高测得的分数
    review: str = ""                         # 提出它的复核
    seq: int = 0
    proposed_checkpoint: Optional[int] = None
    status: str = IMP_OPEN                   # rule：open | done | dropped
    judgement: Optional[str] = None          # rule：最近一次判定 done | partial | not_done | dropped
    level: Optional[str] = None              # rule：done 的证据等级
    evidence: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    runs: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    checkpoint: Optional[int] = None         # rule：最近一次判定所在的合并点
    judged_review: Optional[str] = None
    reason: str = ""                         # rule：放弃的原因 / 重新打开的原因


@dataclass(frozen=True)
class Todo:
    """worker 的 todo 条目（自述）。勾掉时拍锚点快照；锚点被链上合并点包含即 anchored。"""
    id: str                                  # P3
    n: int
    title: str
    status: str = TODO_PENDING               # self / rule：pending → in_progress → completed → anchored
    order: int = 0
    requirements: tuple[str, ...] = ()       # 条目文字里提到的需求编号（R12），不写也没关系
    anchor_snapshot: Optional[int] = None
    anchor_epoch: Optional[int] = None
    checkpoint: Optional[int] = None         # rule：包含锚点的合并点
    completed_seq: Optional[int] = None


@dataclass(frozen=True)
class Submit:
    """worker 请求立即复核（不改变任何需求状态，只是“请现在看一眼”）。结论一定回给 worker。"""
    id: str                                  # U1
    worker: str
    seq: int
    t: float
    summary: str = ""                        # self
    blocked: tuple[dict, ...] = ()           # self：[{requirement, kind, reason, quote}]，交给复核者裁决
    waivers: tuple[dict, ...] = ()           # self：[{tests, quote, reason, requirement}]，交给复核者裁决
    implicit: bool = False                   # rule：worker 停下不调用工具，被当作提交
    snapshot: int = 0
    attempt: Optional[str] = None            # 合并请求（快照与链头不同时）
    review: Optional[str] = None             # 只判定、不合并的复核（快照就是链头时）
    checkpoint: Optional[int] = None         # 结论所在的合并点
    status: str = SUB_PENDING
    reason: str = ""                         # 被拒的原因
    open: tuple[str, ...] = ()               # rule：结论时仍未完成的 actionable 需求
    accepted_seq: Optional[int] = None


@dataclass(frozen=True)
class Review:
    """一次复核：一个带工具的复核者会话。有 attempt 时决定是否合并；没有时只判定需求（快照就是链头）。"""
    id: str                                  # V1
    trigger: str                             # 合并请求的触发，或 judge（提交时快照就是链头）
    tree: str                                # 被复核的候选树
    snapshot: int
    base: Optional[int]                      # 复核开始时的链头（diff 的基准）
    seq: int
    t: float
    attempt: Optional[str] = None
    checkpoint: Optional[int] = None         # 只判定时：被判定的合并点
    submit: Optional[str] = None
    focus: tuple[str, ...] = ()              # 要求判定的需求
    gate: dict = field(default_factory=dict)  # rule：给复核者看的回归门结果 {regressions, flaky}
    retry_of: Optional[str] = None
    status: str = REV_RUNNING
    verdict: dict = field(default_factory=dict)   # llm：复核者的结论（规整过格式）
    runs: tuple[dict, ...] = ()              # obs：复核者执行过的命令 {id, cmd, rc}
    error: str = ""
    transcript: Optional[str] = None
    decision: dict = field(default_factory=dict)  # rule：校验后的结论 {merge, reasons, notes, judgements, ...}


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
    n: int
    reason: str                              # first | handoff | restart | recover | crash | rebuild | resume
    started_seq: int
    started_t: float
    opening: dict = field(default_factory=dict)
    transcript: Optional[str] = None
    ended_t: Optional[float] = None
    ended_seq: Optional[int] = None
    end_reason: Optional[str] = None
    peak_context: int = 0
    turns: int = 0
    compactions: tuple[tuple[int, int, int], ...] = ()
    progress: bool = False                   # rule：会话期间（含结束后替它做的合并）是否有进展
    resumes: tuple[str, ...] = ()
    error: Optional[str] = None


@dataclass(frozen=True)
class Wip:
    """未合并的进度（观察）：最近一张快照相对链头的样子。"""
    worker: str
    base: int
    tree: str
    raw_tree: str = ""
    files: tuple[tuple[str, int, int], ...] = ()
    dropped: tuple[str, ...] = ()
    snapshot: int = 0
    seq: int = 0
    last_rejection: Optional[dict] = None    # 最近一次值得告诉 worker 的合并被拒（前台的，或复核不通过的）


@dataclass(frozen=True)
class Snapshot:
    n: int
    seq: int
    t: float
    worker: str
    tree: str                                # 候选树（剔除测试改动）
    raw_tree: str                            # 工作区原样
    epoch: int
    reason: str                              # writes | model_test | todo | submit | session_end | handoff | recover |
    #                                          deadline | final | suspend | revert | rollback
    testable: bool
    commit: str = ""
    base: int = 0
    files: tuple[tuple[str, int, int], ...] = ()
    dropped: tuple[str, ...] = ()
    todo: Optional[str] = None               # 拍快照时第一项进行中的 todo
    todos: tuple[str, ...] = ()              # 拍快照时全部进行中的 todo（worker 可以同时进行几项）
    session: Optional[str] = None
    tool_seq: int = 0
    precheck: str = ""
    lost: bool = False


@dataclass(frozen=True)
class Job:
    id: str
    key: str
    tree: str
    selection: Optional[tuple[str, ...]]     # None = 全量；否则测试文件或 cmd:<name>
    purpose: str                             # baseline | gate | confirm | review | locate
    requested_by: str = "runtime"
    attempt: Optional[str] = None
    live: bool = False
    where: str = WHERE_SLOT
    tag: str = ""
    locate: Optional[str] = None
    checkpoint: Optional[int] = None
    state: str = JOB_RUNNING
    results: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)
    error: str = ""
    sec: float = 0.0
    started_t: float = 0.0
    started_seq: int = 0
    finished_t: Optional[float] = None
    replaces: Optional[str] = None
    preemptions: int = 0


@dataclass(frozen=True)
class Stall:
    seq: int
    t: float
    kind: str                                # no_progress | repeated_failure | review_rejections | sessions_no_progress
    action: str                              # hint | stop
    worker: Optional[str] = None
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
    summary: Optional[str] = None


@dataclass(frozen=True)
class Waiver:
    """从回归门里豁免的检查（rule：复核者认定这个现有测试与任务原文要求的行为冲突，规则校验引文后接受）。"""
    test: str
    seq: int
    t: float
    requirement: Optional[str]
    review: str
    quote: str
    reason: str


@dataclass(frozen=True)
class Persistent:
    """持续性回归（rule）：同一回归在连续两个被拒的后台合并请求上都出现。"""
    test: str
    seq: int
    t: float
    since: int
    epoch: int
    trigger: str                             # background
    checkpoint: Optional[int] = None


@dataclass(frozen=True)
class Locate:
    """快照二分定位（rule）。"""
    id: str
    tests: tuple[str, ...]
    bad_tree: str
    bad_snapshot: Optional[int]
    bad_checkpoint: Optional[int]
    epoch: int
    lower: int
    trigger: str                             # rejected | background | review
    started_seq: int
    started_t: float
    ref: Optional[str] = None
    status: str = "running"
    groups: tuple[dict, ...] = ()
    results: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Diagnosis:
    id: str
    trigger: str                             # rejected | background | repeated
    tests: tuple[str, ...]
    key: str
    seq: int
    locate: Optional[str] = None
    previous: Optional[str] = None
    status: str = "requested"
    result: dict = field(default_factory=dict)


# ======================================================================== 视图 C：合并链

@dataclass(frozen=True)
class Attempt:
    """合并请求：回归门 → 复核 → 合并（CAS）。"""
    id: str
    worker: str
    trigger: str                             # auto | todo | handoff | session_end | submit | final | deadline
    tree: str
    base: int                                # 请求时的链头（只作记录；父节点在推进时才确定）
    selection: Optional[tuple[str, ...]]     # None = 全量回归门；() = 没有测试可跑
    submit: Optional[str] = None
    jobs: tuple[str, ...] = ()
    status: str = ATT_PENDING
    regressions: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    reason: str = ""                         # 被拒的原因：regression | review | precheck | cancelled | ...
    detail: str = ""
    checkpoint: Optional[int] = None
    parent_commit: Optional[str] = None
    date: Optional[float] = None
    summary: str = ""                        # self：todo 条目、提交摘要（复核者没有给出标签时用）
    raw_tree: str = ""
    created_seq: int = 0
    created_t: float = 0.0
    snapshot: int = 0
    epoch: int = 0
    lane: str = LANE_FG
    review: Optional[str] = None             # 最近一次复核
    reviews: tuple[str, ...] = ()            # 全部复核（失败后会重试）


@dataclass(frozen=True)
class Checkpoint:
    """合并点。"""
    id: int
    commit: str
    tree: str
    parent: Optional[int]
    created_seq: int
    created_t: float
    attempt: Optional[str] = None
    trigger: str = "baseline"
    files: tuple[tuple[str, int, int], ...] = ()
    abandoned: bool = False
    snapshot: int = 0
    epoch: int = 0
    review: Optional[str] = None             # 判定它的复核（None：基线，或复核者不可用、只按回归门合并）
    score: Optional[float] = None            # llm（已校验）：复核者按任务自测的指标，越大越好
    score_note: str = ""                     # 怎么测的
    label: str = ""                          # 复核者写的一行说明（也是合并提交的说明）


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
    verifier: bool = True                    # 有没有测试配置（回归门是否可用）
    version: int = VERSION
    status: str = RUN_RUNNING
    reserve: bool = False
    reserve_sec: float = 0.0
    finalizing: bool = False
    finalize_reason: str = ""
    suspended: int = 0
    delivered: Optional[int] = None
    status_reasons: tuple[str, ...] = ()
    # 改进阶段（after_accept=improve）：需求都做完、submit 被接受之后开始；复核者认为没有值得做的改进了就结束
    improving: bool = False
    improve_seq: Optional[int] = None
    improve_closed: str = ""                 # 结束的原因（空 = 没结束）
    recoveries: int = 0
    rebuilds: int = 0
    downtime_sec: float = 0.0


@dataclass(frozen=True)
class Graph:
    seq: int = 0
    run: Optional[Run] = None
    # A
    requirements: dict[str, Requirement] = field(default_factory=dict)
    frozen: bool = False
    todos: dict[str, Todo] = field(default_factory=dict)
    improvements: dict[str, Improvement] = field(default_factory=dict)
    submits: dict[str, Submit] = field(default_factory=dict)
    reviews: dict[str, Review] = field(default_factory=dict)
    baseline: dict[str, str] = field(default_factory=dict)
    baseline_ready: bool = False
    baseline_sec: float = 0.0
    isolation: dict = field(default_factory=dict)
    plans: tuple[dict, ...] = ()
    # B
    workers: dict[str, WorkerState] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    wips: dict[str, Wip] = field(default_factory=dict)
    snapshots: dict[int, Snapshot] = field(default_factory=dict)
    epoch: int = 0
    epoch_base: dict[int, int] = field(default_factory=lambda: {0: 0})
    jobs: dict[str, Job] = field(default_factory=dict)
    job_keys: dict[str, str] = field(default_factory=dict)
    compactions: tuple[Compaction, ...] = ()
    stalls: tuple[Stall, ...] = ()
    persistent: dict[str, Persistent] = field(default_factory=dict)
    locates: dict[str, Locate] = field(default_factory=dict)
    diagnoses: dict[str, Diagnosis] = field(default_factory=dict)
    waived: dict[str, Waiver] = field(default_factory=dict)
    # C
    checkpoints: dict[int, Checkpoint] = field(default_factory=dict)
    attempts: dict[str, Attempt] = field(default_factory=dict)
    head: Optional[int] = None
    # 进展（rule）
    last_progress_t: float = 0.0
    last_progress_seq: int = 0

    @property
    def head_cp(self) -> Optional[Checkpoint]:
        return self.checkpoints.get(self.head) if self.head is not None else None

    @property
    def degraded(self) -> bool:
        """导入隔离无效：验证回到切换工作区的方式，并且只在交接时做后台验证。"""
        return self.isolation.get("valid") is False

    @property
    def last_snapshot(self) -> int:
        return max(self.snapshots) if self.snapshots else 0


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

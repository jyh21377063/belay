"""三个视图的数据模型：需求账本（A）、执行状态（B）、存档链（C）。

全部是不可变 dataclass：reduce 返回新图，旧图保持不变（结构共享，只复制被修改的那张表）。
字段后面的注释标明来源：obs（观察）/ rule（规则）/ llm（LLM 提议）/ self（自述）。

v7：工作单位是需求（没有任务层、没有认领与当前焦点）。worker 只做自然的事：读、改、跑测试、（可选的）todo、
结束时 submit；需求的状态由图从观察推出（检查项在存档上通过）或在 submit 时由自述记下、由复查者收紧。

三层状态（模块 B、C）：
  快照       runtime 在工具边界自动拍：某一刻工作区的样子（恢复工作区、定位回归）
  暂存存档   related 档位验证通过：相关测试上没有回归（后台空闲时持续验证最新的快照）
  确认存档   全量验证通过：全部守护测试上没有回归（交付）
"""
from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Optional, Union

# ---- 需求
ACTIONABLE, CONTEXT = "actionable", "context"     # 要改代码的 / 标题、套话、背景（只为覆盖原文）
REQ_KINDS = (ACTIONABLE, CONTEXT)
REQ_OPEN = "open"                                 # 还没做完（初始；被重开也回到这里）
REQ_VERIFIED = "verified"                         # 证据检查项在链上的存档里全部通过（rule）
REQ_SUBMITTED = "submitted"                       # worker 提交、提交的存档通过回归门（self）；复查者可以重开
REQ_BLOCKED = "blocked"                           # worker 提交时声明做不了（self）；复查者可以给出读法重开
REQ_STATUSES = (REQ_OPEN, REQ_VERIFIED, REQ_SUBMITTED, REQ_BLOCKED)
REQ_FINISHED = (REQ_VERIFIED, REQ_SUBMITTED)
REQ_RESOLVED = (REQ_VERIFIED, REQ_SUBMITTED, REQ_BLOCKED)

# ---- todo（运行级，镜像 worker 的 todo_write）
TODO_PENDING, TODO_ACTIVE, TODO_COMPLETED, TODO_ANCHORED = "pending", "in_progress", "completed", "anchored"

# ---- 提交
SUB_PENDING = "pending"                  # 等存档尝试的结果
SUB_CHECKPOINTED = "checkpointed"        # 存档已有（或已被包含）：待判定需求
SUB_REVIEWING = "reviewing"              # 需求已判定，复查进行中
SUB_ACCEPTED = "accepted"                # 没有未完成的 actionable 需求：运行可以收尾
SUB_RETURNED = "returned"                # 还有未完成的需求：清单交还 worker
SUB_REJECTED = "rejected"                # 存档被拒（回归、预检、取消）
SUB_OPEN = (SUB_PENDING, SUB_CHECKPOINTED, SUB_REVIEWING)
SUB_FINAL = (SUB_ACCEPTED, SUB_RETURNED, SUB_REJECTED)

# ---- 作业
JOB_RUNNING, JOB_FINISHED, JOB_UNKNOWN, JOB_CANCELLED = "running", "finished", "unknown", "cancelled"
# 作业在哪里运行：slot（验证目录）| workspace（切换工作区：降级模式与基线的工作区那一次）| live（活的工作区，开发检查）
WHERE_SLOT, WHERE_WORKSPACE, WHERE_LIVE = "slot", "workspace", "live"
# ---- 存档尝试
ATT_PENDING, ATT_ADVANCING, ATT_CREATED, ATT_REJECTED, ATT_SUPERSEDED = (
    "pending", "advancing", "created", "rejected", "superseded")
LANE_FG, LANE_BG = "fg", "bg"
# ---- 存档级别
PROVISIONAL, CONFIRMED = "provisional", "confirmed"
# 存档类别：auto（后台验证最新快照）、todo（勾掉一条 todo）、submit、handoff（交接 / 会话结束）、final（收尾）
KIND_AUTO, KIND_TODO, KIND_SUBMIT, KIND_HANDOFF, KIND_FINAL, KIND_BASE = (
    "auto", "todo", "submit", "handoff", "final", "baseline")
MILESTONE_KINDS = (KIND_TODO, KIND_SUBMIT, KIND_HANDOFF, KIND_FINAL, KIND_BASE)
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
    status: str = REQ_OPEN                   # rule / self
    checkpoint: Optional[int] = None         # verified / submitted 所在的存档
    status_seq: Optional[int] = None
    submit: Optional[str] = None             # 由哪次提交记下（submitted / blocked）
    blocked_kind: Optional[str] = None       # self
    blocked_reason: Optional[str] = None     # self
    blocked_quote: Optional[str] = None      # self，规则校验逐字存在
    reopen_count: int = 0
    reopen_reason: Optional[str] = None
    last_failure: tuple[str, ...] = ()       # obs / llm：最近一次没过的原因（检查项失败、复查者说缺什么）
    passed_checks: tuple[str, ...] = ()      # obs：在某个存档上第一次通过过的证据检查（进展）
    review: Optional[str] = None             # llm：复查结论 running | yes | partial | no | reading | none | failed
    review_missing: tuple[str, ...] = ()     # llm
    review_reopens: int = 0                  # rule：被复查者重开的次数
    history: tuple[tuple[int, str, str], ...] = ()   # rule：(序号, 状态, 原因)，最多保留最近 30 条


@dataclass(frozen=True)
class Todo:
    """worker 的 todo 条目（自述），镜像为运行级的步骤。勾掉时拍锚点快照；锚点被链上存档包含即 anchored。"""
    id: str                                  # P3
    n: int
    title: str
    status: str = TODO_PENDING               # self / rule：pending → in_progress → completed → anchored
    order: int = 0                           # 在最新列表里的位置
    requirements: tuple[str, ...] = ()       # 条目文字里提到的需求编号（R12），不写也没关系
    anchor_snapshot: Optional[int] = None    # obs：勾掉时强制拍下的快照
    anchor_epoch: Optional[int] = None
    checkpoint: Optional[int] = None         # rule：包含锚点的存档
    completed_seq: Optional[int] = None


@dataclass(frozen=True)
class Submit:
    """worker 的完成声明（唯一要求模型做的声明）。存档 → 判定需求 → 复查 → 接受或交还清单。"""
    id: str                                  # U1
    worker: str
    seq: int
    t: float
    summary: str = ""                        # self
    blocked: tuple[dict, ...] = ()           # self：[{requirement, kind, reason, quote}]
    implicit: bool = False                   # rule：worker 停下不调用工具，被当作提交
    snapshot: int = 0
    attempt: Optional[str] = None
    checkpoint: Optional[int] = None
    status: str = SUB_PENDING
    reason: str = ""                         # 被拒的原因
    failing: dict = field(default_factory=dict)   # 需求 → 没过的检查（证据失败）
    open: tuple[str, ...] = ()               # rule：判定结束时仍未完成的 actionable 需求
    accepted_seq: Optional[int] = None


@dataclass(frozen=True)
class Review:
    """一批需求的复查（llm 只能收紧）。phase：done（提交的需求是否真的做了）| blocked（受阻有没有合理读法）。"""
    id: str                                  # V1
    phase: str
    requirements: tuple[str, ...]
    checkpoint: Optional[int]
    submit: Optional[str]
    seq: int
    status: str = "running"                  # running | recorded
    results: dict = field(default_factory=dict)   # llm：需求 → {implemented, missing, evidence, reading}
    retry_of: Optional[str] = None           # rule：失败条目单条重试时，原来那批复查的 id


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
    reason: str                              # first | handoff | restart | recover | crash | rebuild | resume
    started_seq: int
    started_t: float
    opening: dict = field(default_factory=dict)   # rule：开场上下文的摘要（各段 token、被裁的段）
    transcript: Optional[str] = None         # obs：完整对话记录（读盘重放用）
    ended_t: Optional[float] = None
    ended_seq: Optional[int] = None
    end_reason: Optional[str] = None         # obs：done | handoff | crash | stuck | deadline | runtime_crash | ...
    peak_context: int = 0
    turns: int = 0
    compactions: tuple[tuple[int, int, int], ...] = ()   # (层级, 压缩前, 压缩后)
    progress: bool = False                   # rule：会话期间（含结束后替它做的存档）是否有进展
    resumes: tuple[str, ...] = ()            # obs：原样接上对话的方式 memory | replay
    error: Optional[str] = None


@dataclass(frozen=True)
class Wip:
    """未验证的进度（观察）：最近一张快照相对链头的样子。"""
    worker: str
    base: int                                # 基于哪个存档
    tree: str                                # 剔除测试改动后的候选树
    raw_tree: str = ""                       # 工作区原样的树
    files: tuple[tuple[str, int, int], ...] = ()   # 相对 base 的改动：(路径, 增, 删)
    dropped: tuple[str, ...] = ()            # 被剔除的测试路径改动
    snapshot: int = 0
    seq: int = 0
    last_rejection: Optional[dict] = None    # 最近一次 worker 声明的存档被拒（submit、收尾）


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
    commit: str = ""                         # 影子仓库里包住这张快照的提交（refs/belay/snap/<n>）
    base: int = 0                            # 拍下时的链头
    files: tuple[tuple[str, int, int], ...] = ()
    dropped: tuple[str, ...] = ()
    todo: Optional[str] = None               # 拍下时进行中的 todo
    session: Optional[str] = None
    tool_seq: int = 0
    precheck: str = ""                       # 预检失败的原因
    lost: bool = False                       # 容器重建时没能恢复（最后一次导出之后）


@dataclass(frozen=True)
class Job:
    id: str
    key: str                                 # (树, 检查集合, 标签) 的键；同键只跑一次
    tree: str
    selection: Optional[tuple[str, ...]]     # None = 全量；否则测试文件或 cmd:<name>
    purpose: str                             # baseline | verify | confirm | evidence | dev | promote | locate |
    #                                          recheck | gate
    requested_by: str = "runtime"
    attempt: Optional[str] = None
    live: bool = False                       # 在活的工作区上跑（dev）：结果不作为存档证据
    where: str = WHERE_SLOT
    tag: str = ""
    locate: Optional[str] = None
    checkpoint: Optional[int] = None         # promote / recheck 针对的存档
    state: str = JOB_RUNNING
    results: dict = field(default_factory=dict)   # obs：检查 id → PASSED | FAILED | ERROR | SKIPPED | XFAIL
    reasons: dict = field(default_factory=dict)   # obs：失败原因（每条截断 400 字符）
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
    kind: str                                # no_progress | repeated_failure | sessions_no_progress
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
    summary: Optional[str] = None            # llm（L3 / L4）


@dataclass(frozen=True)
class Waiver:
    """从回归门里豁免的检查（rule：worker 声明“这个现有测试与任务原文要求的行为冲突”，规则校验后接受）。
    证据：任务原文的逐字引文 + 这个测试确实在 worker 的候选树上失败过。账本里逐条列出。"""
    test: str
    seq: int
    t: float
    requirement: Optional[str]
    worker: str
    quote: str
    reason: str


@dataclass(frozen=True)
class Persistent:
    """持续性回归（rule）：worker 声明的存档被降级后，失败的测试在最新快照上仍然失败。"""
    test: str
    seq: int
    t: float
    since: int                               # 从哪张快照起失败
    epoch: int
    trigger: str                             # demoted（旧日志里还可能有 background | dev_check）
    checkpoint: Optional[int] = None


@dataclass(frozen=True)
class Locate:
    """快照二分定位（rule）。区间内各点的结果全部来自作业，所以状态由图推出，不单独记录中间步骤。"""
    id: str
    tests: tuple[str, ...]
    bad_tree: str
    bad_snapshot: Optional[int]              # 坏端的快照（None 表示坏端是某个存档本身）
    bad_checkpoint: Optional[int]
    epoch: int
    lower: int                               # 该段起点存档（回退目标或 0 号）
    trigger: str                             # rejected | demoted | background（旧日志：persistent | step）
    started_seq: int
    started_t: float
    ref: Optional[str] = None                # 尝试 id 或 "cp:<k>"
    status: str = "running"                  # running | concluded
    groups: tuple[dict, ...] = ()            # 规则给出的分组结果
    results: tuple[dict, ...] = ()           # regression_located（观察，含 diff）


@dataclass(frozen=True)
class Diagnosis:
    id: str
    trigger: str                             # rejected | demoted | background | repeated（旧日志：persistent）
    tests: tuple[str, ...]
    key: str                                 # (回归签名, 定位区间)：同一个键只诊断一次（repeated 除外）
    seq: int
    locate: Optional[str] = None
    previous: Optional[str] = None
    status: str = "requested"                # requested | recorded | failed
    result: dict = field(default_factory=dict)   # llm


# ======================================================================== 视图 C：存档链

@dataclass(frozen=True)
class Attempt:
    id: str
    worker: str
    trigger: str                             # auto | todo | submit | handoff | session_end | deadline | final
    tree: str
    base: int                                # 尝试时的链头（只作记录；父节点在推进时才确定）
    tier: str                                # related | full
    selection: Optional[tuple[str, ...]]
    submit: Optional[str] = None             # 随这次尝试判定的提交
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
    snapshot: int = 0
    epoch: int = 0
    lane: str = LANE_FG
    kind: str = KIND_AUTO


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
    files: tuple[tuple[str, int, int], ...] = ()   # obs：相对父存档的改动
    abandoned: bool = False
    snapshot: int = 0
    epoch: int = 0
    kind: str = KIND_BASE
    level: str = CONFIRMED
    confirmed_seq: Optional[int] = None
    demoted: bool = False
    demote_regressions: tuple[str, ...] = ()
    label: str = ""                          # self / llm：todo 条目、提交摘要，或没有它们时模型写的一行说明


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
    finalizing: bool = False
    finalize_reason: str = ""
    suspended: int = 0
    delivered: Optional[int] = None
    delivered_level: Optional[str] = None
    deliver_unconfirmed: Optional[bool] = None   # 交付时用的 deliver_unconfirmed 取值（账本写明）
    status_reasons: tuple[str, ...] = ()         # rule：为什么不是 DONE
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
    submits: dict[str, Submit] = field(default_factory=dict)
    reviews: dict[str, Review] = field(default_factory=dict)
    baseline: dict[str, str] = field(default_factory=dict)     # 检查 id → pass | fail | flaky | skip
    baseline_ready: bool = False
    baseline_sec: float = 0.0
    isolation: dict = field(default_factory=dict)              # obs：{valid, reason, diff, probe}
    plans: tuple[dict, ...] = ()
    # B
    workers: dict[str, WorkerState] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    wips: dict[str, Wip] = field(default_factory=dict)          # worker → WIP
    snapshots: dict[int, Snapshot] = field(default_factory=dict)
    epoch: int = 0
    epoch_base: dict[int, int] = field(default_factory=lambda: {0: 0})   # 段号 → 段起点存档
    jobs: dict[str, Job] = field(default_factory=dict)
    job_keys: dict[str, str] = field(default_factory=dict)      # 键 → 有效的作业（running / finished）
    compactions: tuple[Compaction, ...] = ()
    stalls: tuple[Stall, ...] = ()
    persistent: dict[str, Persistent] = field(default_factory=dict)   # 测试 id → 最近一次持续性回归记录
    locates: dict[str, Locate] = field(default_factory=dict)
    diagnoses: dict[str, Diagnosis] = field(default_factory=dict)
    relations: tuple[tuple[str, str], ...] = ()                 # rule：(源文件, 测试文件)
    waived: dict[str, Waiver] = field(default_factory=dict)      # rule：从回归门里豁免的检查
    # C
    checkpoints: dict[int, Checkpoint] = field(default_factory=dict)
    attempts: dict[str, Attempt] = field(default_factory=dict)
    head: Optional[int] = None
    confirmed: Optional[int] = None
    # 进展（rule）：最近一次进展的时间与序号
    last_progress_t: float = 0.0
    last_progress_seq: int = 0

    @property
    def head_cp(self) -> Optional[Checkpoint]:
        return self.checkpoints.get(self.head) if self.head is not None else None

    @property
    def degraded(self) -> bool:
        """导入隔离无效：验证回到切换工作区的方式，并且不做任何后台验证。"""
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

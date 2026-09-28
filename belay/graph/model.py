"""证据图的数据模型：节点、状态常量、内存中的图状态与变更。

只依赖标准库。所有实体都是可 JSON 序列化的 dataclass；decide() 不直接修改状态，
而是产出 Put / Delete / Event 变更，由 Orchestrator 统一 apply 并落库。

节点（v4）：
  Requirement      任务原文中一条可单独核对的承诺；没有"完成"状态，状态由证据计算（ledger.py）
  Check            可执行、结果确定的判定：已有测试（基线，合并门用）或验收测试（Test Author 写，完成门用）
  Work             一个 worker 负责的一段工作
  Candidate        一次 submit 产生的候选
  Job              一次耗时的外部操作（开发检查、门禁、测试验证）
  FailureSignature 归一化的失败，与基线比较后归类
  Report           上报的冲突 / 信息不足 / 环境问题，附 reviewer 结论
  Integration      集成链上的一个提交
  Run              运行本身：预算、阶段、集成分支 HEAD
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

# ---- 常量 -------------------------------------------------------------------------

REQ_KINDS = ("change", "new", "keep", "docs", "maintenance")

# 需求的证据结论（ledger.requirement_status 计算，不存储）
OPEN, SUPPORTED, FAILED, UNKNOWN, WAIVED = "OPEN", "SUPPORTED", "FAILED", "UNKNOWN", "WAIVED"

# 检查在某个树上的结果；基线结果另有 FLAKY / NONE
PASS, FAIL, NOT_RUN, FLAKY, NONE = "PASS", "FAIL", "NOT_RUN", "FLAKY", "NONE"

# Work 状态（v4：5 个状态，1 条退回：VERIFYING → RUNNING）
READY, RUNNING, VERIFYING, MERGED, ABANDONED = "READY", "RUNNING", "VERIFYING", "MERGED", "ABANDONED"

# Job 状态
QUEUED, JOB_RUNNING, DONE, TIMEOUT, ERROR, CANCELLED, JOB_UNKNOWN = (
    "QUEUED", "RUNNING", "DONE", "TIMEOUT", "ERROR", "CANCELLED", "UNKNOWN")
JOB_FINAL = (DONE, TIMEOUT, ERROR, CANCELLED)

# Run 终态
RUN_RUNNING, RUN_DONE, RUN_INCOMPLETE = "RUNNING", "DONE", "INCOMPLETE"


# ---- 实体 -------------------------------------------------------------------------

@dataclass
class Requirement:
    id: str
    text: str                       # 需求表述：规则切分时是原文；LLM 拆解时是归一化后的表述
    section: str = ""
    kind: str = "change"
    order: int = 0
    quotes: list[str] = field(default_factory=list)   # 逐字的原文片段（LLM 拆解时由 runtime 校验过）

    def original(self) -> str:
        """这条需求在任务原文里的文字：reviewer 的引文按它校验。"""
        return "\n".join(self.quotes) if self.quotes else self.text


@dataclass
class Check:
    id: str
    source: str                     # existing_test | authored
    selector: str                   # 已有测试：pytest node id；authored：测试文件在工作区中的相对路径
    baseline: str = NONE            # PASS | FAIL | FLAKY | NONE
    req_id: str | None = None       # authored：验证哪条需求
    nodes: list[str] = field(default_factory=list)      # authored：在原始代码上以断言失败的测试
    status: str = "active"          # 验收测试：queued（排队）| pending（在写、在验证）| active | rejected
                                    #           （写不出有效测试）| withdrawn（申诉获批后作废）
    stored_at: str = ""             # authored：runtime 保存测试文件的位置（worker 不可见）
    results: dict[str, str] = field(default_factory=dict)   # authored：树哈希 → PASS / FAIL
    last_failure: str = ""          # authored：最近一次在门禁里失败的原因（ledger 显示给 worker）
    note: str = ""
    digest: str = ""                # authored：冻结时的定义摘要（不变量：检查定义不可改）
    attempts: int = 0
    requested_by: str = ""          # authored：接收通知的工作节点
    interface: str = ""             # authored：（保留）声明的公开接口
    content: str = ""               # authored：测试文件内容（收录后 worker 可以查看）


@dataclass
class Job:
    id: str
    key: str
    purpose: str                    # dev | gate | validate
    workspace: str
    tree: str
    selection: list[str] = field(default_factory=list)  # 测试文件或 node id；空 = 全量
    command: str | None = None      # 任意命令作业（没有测试配置的题、长时间构建）
    level: str = ""                 # 门禁：related | full | none
    state: str = QUEUED
    work_id: str | None = None
    candidate_id: str | None = None
    check_id: str | None = None     # validate：被验证的 authored 检查
    overlay: dict[str, str] = field(default_factory=dict)   # 工作区相对路径 → 存放位置
    created_t: float = 0.0
    finished_t: float | None = None
    result: dict[str, Any] = field(default_factory=dict)
    log: str = ""
    sec: float = 0.0


@dataclass
class Work:
    id: str
    covers: list[str]
    state: str = READY
    workspace: str = ""
    base_commit: str = ""
    rejections: int = 0
    final_bounces: int = 0          # 最终提交因验收测试失败被退回的次数（只记录，不设上限）
    last_rejection: str = ""        # 上次被拒的差分证据（重建上下文时带上）
    summary: str = ""


@dataclass
class Candidate:
    id: str
    work_id: str
    final: bool
    commit: str
    tree: str
    base_commit: str
    changed: list[str] = field(default_factory=list)            # 相对原始代码
    changed_since_head: list[str] = field(default_factory=list)  # 相对提交时的集成分支（选相关测试用）
    dropped_tests: list[str] = field(default_factory=list)
    summary: str = ""
    by_runtime: str = ""            # 空 = worker 提交；否则为 runtime 代为提交的原因（deadline / worker_exit）
    rid: str | None = None          # 等待裁决的工具请求
    job_id: str | None = None
    level: str = ""
    verdict: str = "pending"        # pending | merged | rejected | error | unchanged
    waiting_tests: bool = False     # 最终提交：等验收测试写完再跑门禁
    regressions: list[str] = field(default_factory=list)
    gate_note: str = ""             # 门禁结果的说明（合并后回复给 worker）
    created_t: float = 0.0


@dataclass
class FailureSignature:
    id: str
    test: str
    error: str
    klass: str                      # baseline | environment | new
    first_job: str
    count: int = 1


@dataclass
class Report:
    id: str
    kind: str                       # test_conflict | wrong_test | insufficient_info | environment
    work_id: str
    req_id: str | None
    check_ids: list[str]
    reason: str
    rid: str | None = None
    verdict: str = "pending"        # pending | approved | rejected
    quote: str = ""
    review: str = ""
    created_t: float = 0.0


@dataclass
class Integration:
    id: str
    seq: int
    commit: str
    tree: str
    candidate_id: str
    level: str
    t: float = 0.0


@dataclass
class Run:
    id: str = "run"
    status: str = RUN_RUNNING
    phase: str = "working"          # working | stopping | finalizing | finished
    started_t: float = 0.0
    deadline_t: float = 0.0
    reserve_sec: float = 0.0
    full_gate_sec: float = 0.0
    base_commit: str = ""
    base_tree: str = ""
    head_commit: str = ""
    head_tree: str = ""
    workspace: str = ""
    gate_available: bool = False    # 有可用的测试配置与基线（门禁是否拦截另由 RuntimeConfig.gate 决定）
    test_author_available: bool = False
    protect_tests: bool = False
    test_files: list[str] = field(default_factory=list)   # 测试配置中的测试文件（相关子集从这里选）
    req_ids: list[str] = field(default_factory=list)       # 冻结时的需求集合（不变量）
    counters: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Waiter:
    """wait 工具的挂起请求。只在内存中，不落库（崩溃后 worker 也不在了）。"""
    id: str                         # 即请求 id
    work_id: str
    job_ids: list[str]
    until_t: float


def check_digest(c: Check) -> str:
    """独立检查的定义摘要：收录时写入，之后不可改（不变量 1）。"""
    return hashlib.sha1(json.dumps([c.selector, c.req_id, c.nodes, c.stored_at]).encode()).hexdigest()[:12]


KINDS: dict[str, type] = {
    "requirement": Requirement, "check": Check, "job": Job, "work": Work, "candidate": Candidate,
    "signature": FailureSignature, "report": Report, "integration": Integration, "run": Run, "waiter": Waiter,
}
TRANSIENT_KINDS = {"waiter"}
KIND_OF = {cls: name for name, cls in KINDS.items()}


def kind_of(obj: Any) -> str:
    return KIND_OF[type(obj)]


def to_dict(obj: Any) -> dict:
    return asdict(obj)


def from_dict(kind: str, d: dict) -> Any:
    cls = KINDS[kind]
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


# ---- 变更 -------------------------------------------------------------------------

@dataclass
class Put:
    obj: Any


@dataclass
class Delete:
    kind: str
    id: str


@dataclass
class Event:
    type: str
    t: float
    data: dict = field(default_factory=dict)


# ---- 图状态 -----------------------------------------------------------------------

@dataclass
class GraphState:
    run: Run
    requirement: dict[str, Requirement] = field(default_factory=dict)
    check: dict[str, Check] = field(default_factory=dict)
    job: dict[str, Job] = field(default_factory=dict)
    work: dict[str, Work] = field(default_factory=dict)
    candidate: dict[str, Candidate] = field(default_factory=dict)
    signature: dict[str, FailureSignature] = field(default_factory=dict)
    report: dict[str, Report] = field(default_factory=dict)
    integration: dict[str, Integration] = field(default_factory=dict)
    waiter: dict[str, Waiter] = field(default_factory=dict)

    def table(self, kind: str) -> dict:
        if kind == "run":
            return {"run": self.run}
        return getattr(self, kind)

    def get(self, kind: str, id_: str) -> Any:
        return self.table(kind).get(id_)

    def apply(self, changes: list) -> None:
        for c in changes:
            if isinstance(c, Put):
                k = kind_of(c.obj)
                if k == "run":
                    self.run = c.obj
                else:
                    self.table(k)[c.obj.id] = c.obj
            elif isinstance(c, Delete):
                self.table(c.kind).pop(c.id, None)

    # ---- 常用查询（纯）
    def integration_chain(self) -> list[Integration]:
        return sorted(self.integration.values(), key=lambda x: x.seq)

    def baseline(self) -> dict[str, str]:
        """已有测试 → 基线结果。"""
        return {c.selector: c.baseline for c in self.check.values() if c.source == "existing_test"}

    def authored(self, req_id: str | None = None, active_only: bool = True) -> list[Check]:
        out = [c for c in self.check.values() if c.source == "authored"
               and (not active_only or c.status == "active") and (req_id is None or c.req_id == req_id)]
        return sorted(out, key=lambda c: _num(c.id))

    def waived_tests(self) -> set[str]:
        """获批的测试冲突上报所涉及的已有测试：门禁不再把它们计为回归。"""
        out: set[str] = set()
        for r in self.report.values():
            if r.verdict == "approved" and r.kind in ("test_conflict", "environment"):
                out.update(r.check_ids)
        return out


def _num(id_: str) -> int:
    digits = "".join(ch for ch in id_ if ch.isdigit())
    return int(digits) if digits else 0


# ---- 事务：decide() 用它读写，不改动原状态 -------------------------------------------

class Tx:
    """decide 的工作区：读穿透到原状态，写入只记录为变更。"""

    def __init__(self, state: GraphState, now: float):
        self.state = state
        self.now = now
        self.changes: list = []
        self.actions: list = []
        self._pending: dict[tuple[str, str], Any] = {}
        self._deleted: set[tuple[str, str]] = set()

    def get(self, kind: str, id_: str) -> Any:
        """返回的对象不能原地修改：一律用 update() / put() 写入新对象（decide 的约定）。"""
        key = (kind, id_)
        if key in self._deleted:
            return None
        if key in self._pending:
            return self._pending[key]
        return self.state.get(kind, id_)

    @property
    def run(self) -> Run:
        return self.get("run", "run")

    def all(self, kind: str) -> list:
        ids = set(self.state.table(kind)) | {i for (k, i) in self._pending if k == kind}
        out = [self.get(kind, i) for i in ids]
        return [o for o in out if o is not None]

    def put(self, obj: Any) -> Any:
        key = (kind_of(obj), obj.id)
        self._deleted.discard(key)
        self._pending[key] = obj
        self.changes = [c for c in self.changes if not (isinstance(c, Put) and (kind_of(c.obj), c.obj.id) == key)]
        self.changes.append(Put(obj))
        return obj

    def update(self, obj: Any, **kw) -> Any:
        return self.put(replace(obj, **kw))

    def delete(self, kind: str, id_: str) -> None:
        self._pending.pop((kind, id_), None)
        self._deleted.add((kind, id_))
        self.changes.append(Delete(kind, id_))

    def view(self) -> GraphState:
        """应用了本事务变更之后的图（表是浅拷贝，对象共享；只读）。"""
        v = GraphState(run=self.state.run, **{k: dict(self.state.table(k)) for k in KINDS if k != "run"})
        v.apply(self.changes)
        return v

    def event(self, type_: str, **data) -> None:
        self.changes.append(Event(type_, self.now, data))

    def act(self, action: Any) -> None:
        self.actions.append(action)

    def next_id(self, prefix: str) -> str:
        run = self.run
        n = run.counters.get(prefix, 0) + 1
        self.put(replace(run, counters={**run.counters, prefix: n}))
        return f"{prefix}{n}"

"""收件箱里的消息与 decide() 产出的动作。

原则：消息只带事实，决定只在 decide() 里做。需要 IO 才能得到的事实（树哈希、候选提交、改动文件）
由发送方（WorkerRuntime / Effects）先取好再放进消息，decide() 因此不需要 IO。
每条消息都带 now（墙钟时间），decide() 用它计算预算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    now: float


# ---- 运行控制 ---------------------------------------------------------------------

@dataclass
class Start(Message):
    pass


@dataclass
class Tick(Message):
    pass


@dataclass
class Stop(Message):
    reason: str = "cancelled"       # 外部取消（例如 Pier 超时）


# ---- 来自工具（worker 的请求；rid 用于回复） ----------------------------------------

@dataclass
class RunCheck(Message):
    rid: str
    work_id: str
    tree: str
    changed: list[str] = field(default_factory=list)   # 相对原始代码的改动文件（选相关测试用）
    tests: list[str] = field(default_factory=list)
    full: bool = False
    command: str | None = None


@dataclass
class Wait(Message):
    rid: str
    work_id: str
    job_ids: list[str]
    timeout: float = 600


@dataclass
class LedgerQuery(Message):
    rid: str
    work_id: str
    req_id: str = ""                # 非空：只看这条需求（含验收测试的内容）


@dataclass
class Submit(Message):
    rid: str | None
    work_id: str
    summary: str
    final: bool
    commit: str = ""
    tree: str = ""
    changed: list[str] = field(default_factory=list)          # 相对当前 HEAD 的改动（测试路径已剔除）
    changed_since_base: list[str] = field(default_factory=list)
    dropped_tests: list[str] = field(default_factory=list)
    by_runtime: str = ""            # deadline | worker_exit；空 = worker 自己提交
    error: str = ""                 # 快照失败


@dataclass
class ReportConflict(Message):
    rid: str
    work_id: str
    kind: str
    req_id: str | None
    check_ids: list[str]
    reason: str


# ---- 来自副作用 -------------------------------------------------------------------

@dataclass
class JobFinished(Message):
    job_id: str
    state: str                      # DONE | TIMEOUT | ERROR | CANCELLED
    result: dict[str, Any] = field(default_factory=dict)
    sec: float = 0.0
    log: str = ""


@dataclass
class Merged(Message):
    candidate_id: str
    ok: bool
    error: str = ""


@dataclass
class AuthoredDraft(Message):
    check_id: str
    ok: bool
    stored_at: str = ""
    content: str = ""
    error: str = ""


@dataclass
class ReviewDone(Message):
    report_id: str
    approved: bool
    quote: str = ""
    reason: str = ""
    failure_text: str = ""          # 环境问题的引文按失败输出校验
    error: str = ""


@dataclass
class WorkerExited(Message):
    work_id: str
    status: str
    summary: str = ""


# ---- 动作 -------------------------------------------------------------------------

@dataclass
class StartJob:
    job_id: str


@dataclass
class CancelJob:
    job_id: str


@dataclass
class Reply:
    rid: str
    text: str
    finished: bool = False
    error: bool = False
    data: dict = field(default_factory=dict)


@dataclass
class Notify:
    work_id: str
    text: str


@dataclass
class Advance:
    candidate_id: str
    expected: str
    new: str


@dataclass
class CallTestAuthor:
    check_id: str
    req_id: str
    interface: str = ""
    feedback: str = ""


@dataclass
class CallReviewer:
    report_id: str


@dataclass
class StartWorker:
    work_id: str


@dataclass
class StopWorker:
    work_id: str
    reason: str


@dataclass
class FinalizeWorkspace:
    """把 worker 当前的工作区作为最终候选（runtime 代为提交）。"""
    work_id: str
    reason: str


@dataclass
class Finish:
    status: str
    reason: str

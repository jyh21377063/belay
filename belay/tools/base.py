"""工具的公共部分：Tool 定义、运行时上下文、行动边界策略。"""
from __future__ import annotations

import posixpath
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from belay.env import Env


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, "ToolContext"], Awaitable[str]]
    read_only: bool = False          # 只读工具在同一轮里可以并行执行

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolError(Exception):
    """工具的可预期错误：错误信息直接返回给模型。"""


# ---- 行动边界策略 ---------------------------------------------------------------
# 每类规则三种模式：off 不检查；audit 放行但记为越界事件；deny 拒绝并记为越界事件。
# B 组（FlatAgent）默认 audit，行为与原版 Claude Code 可比；Belay 默认 deny。

@dataclass
class Policy:
    git_write: str = "audit"         # git 提交、切换分支、reset 等写操作
    network: str = "audit"           # curl / wget / pip download / git clone 等
    disk_search: str = "audit"       # 在工作区外全盘搜索
    harness_paths: str = "deny"      # 访问 harness 的状态目录与评分目录
    protected_prefixes: tuple[str, ...] = ("/opt/belay", "/logs")

    @classmethod
    def strict(cls) -> "Policy":
        return cls(git_write="deny", network="deny", disk_search="deny", harness_paths="deny")

    @classmethod
    def from_dict(cls, d: dict | None) -> "Policy":
        return cls(**{k: (tuple(v) if k == "protected_prefixes" else v) for k, v in (d or {}).items()})


_GIT_WRITE = re.compile(
    r"\bgit\s+(?:-[cC]\s+\S+\s+)*(commit|push|reset|checkout|switch|stash|rebase|merge|cherry-pick|revert|"
    r"am|apply|clean|restore|tag|worktree|update-ref|filter-branch|branch\s+-[dDmMf])\b")
_NETWORK = re.compile(r"\b(curl|wget)\b|\bpip3?\s+download\b|\bgit\s+(clone|fetch|pull)\b|\bnpm\s+(view|pack)\b")
_WRITE = re.compile(
    r"(?<![<>&0-9])>>?(?!&)\s*(?!/dev/null)[\w./~$-]|\b(rm|mv|cp|touch|mkdir|rmdir|chmod|chown|ln|tee|truncate|dd|patch)\b|"
    r"\bsed\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*i|\bpip3?\s+(install|uninstall)\b|\bnpm\s+(install|ci|uninstall)\b|"
    r"\bgit\s+(add|commit|checkout|switch|reset|stash|rebase|merge|cherry-pick|revert|apply|am|clean|restore|mv|rm)\b")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_DISK = re.compile(r"\b(find|locate|rg|grep\s+-[a-zA-Z]*r[a-zA-Z]*)\s+(?:[^|;&]*\s)?"
                   r"/(?:\s|$|usr\b|opt\b|root\b|home\b|var\b|srv\b|etc\b|tmp\b|logs\b)")


class RuntimeClient(Protocol):
    """工具与 Orchestrator 之间唯一的接口（实现见 belay/runtime/orchestrator.py 的 WorkerRuntime）。

    工具把请求投进 Orchestrator 的收件箱并等待回复；工具不认识 Orchestrator 的内部实现。
    回复是 {"text": 给模型看的文字, "finished": 运行是否已结束, "error": 是否是错误}。
    B 组（FlatAgent）没有 runtime，ToolContext.runtime 为 None，runtime 工具不会注册。
    """

    async def request(self, kind: str, **payload: Any) -> dict: ...

    def drain_notices(self) -> list[str]:
        """取走 runtime 发给本 worker 的通知（独立测试收录 / 被拒等），由 worker 在下一轮注入。"""
        ...


@dataclass
class ToolContext:
    env: Env
    workdir: str
    policy: Policy = field(default_factory=Policy)
    runtime: RuntimeClient | None = None                  # M2 起由 Belay 传入
    work_id: str | None = None                            # M5 起：本 worker 负责的工作节点
    # 读后被改检测：路径 → 最近一次读取或写入时的 sha256。编辑前文件必须在这里，且内容未变
    file_digests: dict[str, str] = field(default_factory=dict)
    # 只读探索子 agent 的入口，由 worker 注入（tools 不依赖 worker）；参数为任务描述，返回报告
    subagent: Callable[[str, str], Awaitable[str]] | None = None
    read_only: bool = False                               # 探索子 agent：拒绝明显会写文件的命令
    todos: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)      # 越界等事件，由 worker 写入轨迹
    submitted: bool = False
    summary: str = ""

    def event(self, kind: str, **data) -> None:
        self.events.append({"t": time.time(), "kind": kind, **data})

    def resolve(self, path: str) -> str:
        """把模型给出的路径规范化为绝对路径，并检查 harness 目录。"""
        if not path:
            raise ToolError("Empty path")
        full = posixpath.normpath(path if path.startswith("/") else posixpath.join(self.workdir, path))
        mode = self.policy.harness_paths
        if mode != "off" and any(full == p or full.startswith(p.rstrip("/") + "/") for p in self.policy.protected_prefixes):
            self.event("violation", category="harness_paths", path=full, action=mode)
            if mode == "deny":
                raise ToolError(f"Access to {full} is not allowed: it belongs to the evaluation harness, not to the task.")
        return full

    def check_read_only(self, command: str) -> None:
        """探索子 agent 的 bash 只允许读。正则只能挡住明显的写法，事后还有工作区变更检查兜底。"""
        m = _WRITE.search(_QUOTED.sub("''", command))       # 引号里的 > 不是重定向
        if m:
            self.event("violation", category="read_only", command=command[:500], match=m.group(0), action="deny")
            raise ToolError("Command rejected: this is a read-only exploration agent and the command may modify "
                            f"files ({m.group(0).strip()!r}). Use read-only commands only.")

    def check_command(self, command: str) -> None:
        checks = [("git_write", _GIT_WRITE, "Git write operations are handled by the harness. Only modify files in the working tree."),
                  ("network", _NETWORK, "The environment is offline and downloading external content is not allowed."),
                  ("disk_search", _DISK, "Search only within the task repository.")]
        for category, pattern, reason in checks:
            mode = getattr(self.policy, category)
            if mode == "off":
                continue
            m = pattern.search(command)
            if m:
                self.event("violation", category=category, command=command[:500], match=m.group(0), action=mode)
                if mode == "deny":
                    raise ToolError(f"Command rejected: {reason}")
        mode = self.policy.harness_paths
        if mode != "off":
            for p in self.policy.protected_prefixes:
                if re.search(rf"(^|[\s'\"=:]){re.escape(p.rstrip('/'))}(/|\b)", command):
                    self.event("violation", category="harness_paths", command=command[:500], action=mode)
                    if mode == "deny":
                        raise ToolError(f"Command rejected: {p} belongs to the evaluation harness and is not accessible.")

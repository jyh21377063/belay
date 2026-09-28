"""工具注册表。

DEFAULT_TOOLS 是 B 组（FlatAgent）的工具集；BELAY_TOOLS 用 runtime 版的 submit 替换 B 组的 submit，
并加入 run_check / wait / ledger / request_test / report_conflict（见 belay/tools/runtime.py）。
"""
from __future__ import annotations

from belay.tools import agents, files, runtime, shell
from belay.tools.base import Policy, RuntimeClient, Tool, ToolContext, ToolError

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in files.TOOLS + shell.TOOLS + agents.TOOLS}
RUNTIME_TOOLS: dict[str, Tool] = {t.name: t for t in runtime.TOOLS}
DEFAULT_TOOLS = ["read_file", "edit_file", "write_file", "list_files", "grep_search", "bash", "todo_write",
                 "explore", "submit"]
BELAY_TOOLS = [n for n in DEFAULT_TOOLS if n != "submit"] + ["run_check", "wait", "ledger", "submit",
                                                             "request_test", "report_conflict"]
EXPLORE_TOOLS = ["read_file", "list_files", "grep_search", "bash"]        # 探索子 agent：只读，不能再开子 agent


def get_tools(names: list[str] | None = None) -> list[Tool]:
    return [ALL_TOOLS[n] for n in (DEFAULT_TOOLS if names is None else names)]


def get_belay_tools(names: list[str] | None = None) -> list[Tool]:
    """runtime 工具优先（submit 用 runtime 版）。"""
    registry = {**ALL_TOOLS, **RUNTIME_TOOLS}
    return [registry[n] for n in (BELAY_TOOLS if names is None else names)]


__all__ = ["ALL_TOOLS", "BELAY_TOOLS", "DEFAULT_TOOLS", "EXPLORE_TOOLS", "RUNTIME_TOOLS", "Policy", "RuntimeClient",
           "Tool", "ToolContext", "ToolError", "get_belay_tools", "get_tools"]

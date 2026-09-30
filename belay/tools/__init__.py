"""工具注册表。

DEFAULT_TOOLS 是 B 组（FlatAgent）的工具集；BELAY_TOOLS = B 组的工具（去掉 submit：运行何时结束由 runtime 的图决定）
+ Belay 工具（见 belay/tools/belay.py）。
"""
from __future__ import annotations

from belay.tools import agents, belay, files, shell
from belay.tools.base import Policy, RuntimeClient, Tool, ToolContext, ToolError

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in files.TOOLS + shell.TOOLS + agents.TOOLS}
RUNTIME_TOOLS: dict[str, Tool] = {t.name: t for t in belay.TOOLS}
DEFAULT_TOOLS = ["read_file", "edit_file", "write_file", "list_files", "grep_search", "bash", "todo_write",
                 "explore", "submit"]
BELAY_TOOLS = [n for n in DEFAULT_TOOLS if n != "submit"] + [t.name for t in belay.TOOLS]
EXPLORE_TOOLS = ["read_file", "list_files", "grep_search", "bash"]        # 探索子 agent：只读，不能再开子 agent


def get_tools(names: list[str] | None = None) -> list[Tool]:
    return [ALL_TOOLS[n] for n in (DEFAULT_TOOLS if names is None else names)]


def get_belay_tools(names: list[str] | None = None) -> list[Tool]:
    registry = {**ALL_TOOLS, **RUNTIME_TOOLS}
    return [registry[n] for n in (BELAY_TOOLS if names is None else names)]


__all__ = ["ALL_TOOLS", "BELAY_TOOLS", "DEFAULT_TOOLS", "EXPLORE_TOOLS", "RUNTIME_TOOLS", "Policy", "RuntimeClient",
           "Tool", "ToolContext", "ToolError", "get_belay_tools", "get_tools"]

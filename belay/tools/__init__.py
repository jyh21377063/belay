"""工具注册表。M2 起在这里加入 run_check / wait / ledger 等 runtime 工具。"""
from __future__ import annotations

from belay.tools import agents, files, shell
from belay.tools.base import Policy, RuntimeClient, Tool, ToolContext, ToolError

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in files.TOOLS + shell.TOOLS + agents.TOOLS}
DEFAULT_TOOLS = ["read_file", "edit_file", "write_file", "list_files", "grep_search", "bash", "todo_write",
                 "explore", "submit"]
EXPLORE_TOOLS = ["read_file", "list_files", "grep_search", "bash"]        # 探索子 agent：只读，不能再开子 agent


def get_tools(names: list[str] | None = None) -> list[Tool]:
    return [ALL_TOOLS[n] for n in (DEFAULT_TOOLS if names is None else names)]


__all__ = ["ALL_TOOLS", "DEFAULT_TOOLS", "EXPLORE_TOOLS", "Policy", "RuntimeClient", "Tool", "ToolContext",
           "ToolError", "get_tools"]

"""工具注册表。

DEFAULT_TOOLS 是 B 组（FlatAgent）的工具集；BELAY_TOOLS = B 组的工具，其中 submit 换成 Belay 的版本（提交后由
runtime 发起合并请求、由复核者判定需求，必要时把还没做完的清单交还 worker），再加 board 与几个反应式工具（见 belay/tools/belay.py）。
"""
from __future__ import annotations

from dataclasses import replace

from belay.tools import agents, belay, files, shell
from belay.tools.base import Policy, RuntimeClient, Tool, ToolContext, ToolError

ALL_TOOLS: dict[str, Tool] = {t.name: t for t in files.TOOLS + shell.TOOLS + agents.TOOLS}
RUNTIME_TOOLS: dict[str, Tool] = {t.name: t for t in belay.TOOLS}
DEFAULT_TOOLS = ["read_file", "edit_file", "write_file", "list_files", "grep_search", "bash", "todo_write",
                 "explore", "submit"]
BELAY_TOOLS = [n for n in DEFAULT_TOOLS if n != "submit"] + [t.name for t in belay.TOOLS]   # submit 由 RUNTIME_TOOLS 提供
EXPLORE_TOOLS = ["read_file", "list_files", "grep_search", "bash"]        # 探索子 agent：只读，不能再开子 agent


def get_tools(names: list[str] | None = None) -> list[Tool]:
    return [ALL_TOOLS[n] for n in (DEFAULT_TOOLS if names is None else names)]


# Belay 的 todo_write：同一实现，描述不同。勾掉 todo 是后台合并的时机，worker 常常几项一起推进、一起做完，所以不要求
# “同一时间只保留一项 in_progress”（B 组的共用定义保持原样，对比基线不变）
BELAY_TODO_DESCRIPTION = (
    "Create and maintain your task list (each call replaces the whole list).\n"
    "Use it for any task with three or more steps, and whenever the task statement lists several requirements: "
    "capture every requirement as an item before you start, so none is forgotten.\n"
    "- Mark an item in_progress when you start on it. Usually that is one item at a time; several items can be "
    "in_progress together when you are doing them as one change.\n"
    "- Mark each item completed as soon as it is fully done (items finished together can be ticked together), and "
    "only then. Keep it in_progress if its tests fail, the implementation is partial, or errors are unresolved; add "
    "a new item for whatever blocks it.\n"
    "- Add follow-up items you discover along the way; remove items that no longer apply.\n"
    "Skip it for a single, simple change.")
BELAY_OVERRIDES: dict[str, Tool] = {"todo_write": replace(ALL_TOOLS["todo_write"], description=BELAY_TODO_DESCRIPTION)}


def get_belay_tools(names: list[str] | None = None) -> list[Tool]:
    registry = {**ALL_TOOLS, **RUNTIME_TOOLS, **BELAY_OVERRIDES}
    return [registry[n] for n in (BELAY_TOOLS if names is None else names)]


__all__ = ["ALL_TOOLS", "BELAY_TOOLS", "DEFAULT_TOOLS", "EXPLORE_TOOLS", "RUNTIME_TOOLS", "Policy", "RuntimeClient",
           "Tool", "ToolContext", "ToolError", "get_belay_tools", "get_tools"]

"""explore：把一个调查问题交给只读的探索子 agent，只拿回一份报告。

子 agent 有独立的上下文，只有 read_file / list_files / grep_search / bash（只读），不能再开子 agent。
它的用途是并行探索、保护主上下文：大范围搜索的中间结果留在子 agent 里，主 worker 只看到结论。
模型可以不用它；同一轮里的多个 explore 会并行执行（它被标记为只读工具）。

子 agent 由 worker 创建并通过 ToolContext.subagent 注入（依赖规则：tools 不依赖 worker）。
"""
from __future__ import annotations

from belay.tools.base import Tool, ToolContext, ToolError


async def explore(inp: dict, ctx: ToolContext) -> str:
    prompt = (inp.get("prompt") or "").strip()
    if not prompt:
        raise ToolError("Missing prompt")
    if ctx.subagent is None:
        raise ToolError("Exploration subagents are not available here")
    return await ctx.subagent(str(inp.get("description") or "explore"), prompt)


TOOLS = [
    Tool("explore",
         "Delegate a codebase investigation to a read-only subagent with its own context, and get back a concise "
         "report. Use it for broad questions whose search results would otherwise fill your context: where "
         "something is implemented, how a value flows through the code, which tests cover a behaviour, how a "
         "subsystem is organised. It cannot modify files. Independent questions can be explored in parallel by "
         "calling this tool several times in one response. For a specific known file or symbol, use read_file or "
         "grep_search directly instead. The subagent sees only your prompt, so make it self-contained.",
         {"type": "object", "properties": {
             "description": {"type": "string", "description": "A short (3-5 word) label for the exploration"},
             "prompt": {"type": "string", "description": "The question to investigate, with any context the "
                                                         "subagent needs and what the report should contain"}},
          "required": ["description", "prompt"]},
         explore, read_only=True),
]

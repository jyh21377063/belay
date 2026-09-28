"""bash、todo_write、submit。"""
from __future__ import annotations

import shlex
import uuid

from belay.tools.output import truncate_output
from belay.tools.base import Tool, ToolContext, ToolError

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800
BG_DIR = "/tmp/belay-bg"


async def bash(inp: dict, ctx: ToolContext) -> str:
    command = (inp.get("command") or "").strip()
    if not command:
        raise ToolError("Missing command")
    ctx.check_command(command)
    if ctx.read_only:
        if inp.get("run_in_background"):
            raise ToolError("Background commands are not available to a read-only exploration agent")
        ctx.check_read_only(command)

    if inp.get("run_in_background"):
        # M1 的简单版本：后台启动，输出写到日志文件。M2 起改由 run_check / wait 负责长时间操作。
        job = uuid.uuid4().hex[:8]
        log = f"{BG_DIR}/{job}.log"
        res = await ctx.env.run(f"mkdir -p {BG_DIR} && (setsid nohup bash -c {shlex.quote(command)} "
                                f"> {log} 2>&1 & echo $! > {BG_DIR}/{job}.pid)", timeout=30)
        if res.return_code != 0:
            raise ToolError(f"Failed to start in background: {res.output[-1000:]}")
        return f"Started in background (id={job}); output goes to {log}. Use read_file or `tail` to check it."

    timeout = min(max(1, int(inp.get("timeout") or DEFAULT_TIMEOUT)), MAX_TIMEOUT)
    res = await ctx.env.run(command, timeout=timeout)
    out = truncate_output(res.output.rstrip("\n"))
    if res.timed_out:
        return f"{out}\n\n[Command timed out after {timeout}s and was killed. Increase timeout or run long commands in the background]"
    if res.return_code != 0:
        return f"{out}\n\n[exit code {res.return_code}]" if out else f"[exit code {res.return_code}, no output]"
    return out or "(no output)"


async def todo_write(inp: dict, ctx: ToolContext) -> str:
    todos = inp.get("todos")
    if not isinstance(todos, list):
        raise ToolError("todos must be a list")
    clean = []
    for t in todos:
        status = t.get("status", "pending")
        if status not in ("pending", "in_progress", "completed"):
            raise ToolError(f"Unknown status: {status}")
        clean.append({"content": str(t.get("content", "")), "status": status})
    ctx.todos = clean
    mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    return "Todo list updated:\n" + "\n".join(f"{mark[t['status']]} {t['content']}" for t in clean)


async def submit(inp: dict, ctx: ToolContext) -> str:
    ctx.submitted = True
    ctx.summary = str(inp.get("summary") or "")
    return "Submitted."


TOOLS = [
    Tool("bash",
         "Run a bash command in the repository root and return combined stdout/stderr. Default timeout 120s, max 1800s. "
         "Prefer the dedicated tools for reading, editing, finding files and searching content. "
         "Long-running commands can use run_in_background.",
         {"type": "object", "properties": {
             "command": {"type": "string"},
             "timeout": {"type": "integer", "description": "Timeout in seconds"},
             "run_in_background": {"type": "boolean", "description": "Run in the background, writing output to a log file"}},
          "required": ["command"]},
         bash),
    Tool("todo_write",
         "Maintain your own todo list (replaces the whole list). Useful for tracking progress on multi-step tasks.",
         {"type": "object", "properties": {"todos": {"type": "array", "items": {
             "type": "object", "properties": {
                 "content": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
             "required": ["content", "status"]}}},
          "required": ["todos"]},
         todo_write, read_only=True),
    Tool("submit",
         "Call this when the task is complete to submit your work and finish. In summary, state what you changed, "
         "how you verified it, and anything left unfinished.",
         {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
         submit),
]

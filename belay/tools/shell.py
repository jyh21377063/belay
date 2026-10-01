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
        # 简单版本：后台启动，输出写到日志文件。
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
         "Run a bash command and return its combined stdout and stderr.\n"
         "- Every call starts in a fresh shell at the repository root: `cd` and exported variables do not persist "
         "between calls. Chain dependent commands with `&&` in a single call.\n"
         "- Default timeout is 120 seconds, maximum 1800. Give long test runs and builds a timeout that covers them; "
         "the call returns as soon as the command finishes.\n"
         "- run_in_background is for servers or commands longer than 1800 seconds: output goes to a log file you can "
         "read later. Do not poll it with long fixed sleeps.\n"
         "- Use non-interactive flags and never open an editor or a pager (use `git --no-pager`, `PAGER=cat`). "
         "Quote paths that contain spaces.\n"
         "- Prefer the dedicated tools for reading, editing, finding and searching files.",
         {"type": "object", "properties": {
             "command": {"type": "string"},
             "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 1800)"},
             "run_in_background": {"type": "boolean", "description": "Run in the background, writing output to a log file"}},
          "required": ["command"]},
         bash),
    Tool("todo_write",
         "Create and maintain your task list (each call replaces the whole list).\n"
         "Use it for any task with three or more steps, and whenever the task statement lists several requirements: "
         "capture every requirement as an item before you start, so none is forgotten.\n"
         "- Keep exactly one item in_progress; mark it in_progress before you start on it.\n"
         "- Mark an item completed immediately after finishing it, and only when it is fully done. Keep it "
         "in_progress if its tests fail, the implementation is partial, or errors are unresolved; add a new item for "
         "whatever blocks it.\n"
         "- Add follow-up items you discover along the way; remove items that no longer apply.\n"
         "Skip it for a single, simple change.",
         {"type": "object", "properties": {"todos": {"type": "array", "items": {
             "type": "object", "properties": {
                 "content": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
             "required": ["content", "status"]}}},
          "required": ["todos"]},
         todo_write, read_only=True),
    Tool("submit",
         "Finish the task. Call it once, after you have checked every requirement, run the relevant tests and "
         "reviewed your diff. The summary must report what actually happened: say first which requirements are not "
         "done, which tests fail and which checks you skipped, then what you changed and how you verified it.",
         {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
         submit),
]

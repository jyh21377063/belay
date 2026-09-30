"""Belay 工具：只读图和向图报告，不替换任何已有工具。

工具只负责把请求交给 ctx.runtime（runtime/port.py 的 WorkerPort），不认识 runtime 的内部实现。
回复是 {"text": 给模型看的文字, "error": 是否被拒绝}。
"""
from __future__ import annotations

from belay.tools.base import Tool, ToolContext, ToolError


def _rt(ctx: ToolContext):
    if ctx.runtime is None:
        raise ToolError("This tool is not available in this run.")
    return ctx.runtime


def _out(reply: dict) -> str:
    text = str(reply.get("text") or "")
    if reply.get("error"):
        raise ToolError(text or "request rejected")
    return text


def _list(v, name: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, (str, int)) for x in v):
        raise ToolError(f"{name} must be a list of strings")
    return [str(x) for x in v]


def _task(inp: dict) -> str:
    t = str(inp.get("task") or "").strip()
    if not t:
        raise ToolError("task is required (e.g. \"T3\")")
    return t


async def board(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("board"))


async def claim(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("claim", task=_task(inp)))


async def release(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("release", task=_task(inp), note=str(inp.get("note") or "")))


async def add_task(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("add_task", title=str(inp.get("title") or ""),
                                       description=str(inp.get("description") or ""),
                                       links=_list(inp.get("links"), "links"),
                                       blocked_by=_list(inp.get("blocked_by"), "blocked_by"),
                                       checks=_list(inp.get("checks"), "checks")))


async def note(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("note", text=str(inp.get("text") or "")))


async def checkpoint(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("checkpoint", summary=str(inp.get("summary") or "")))


async def ready_for_review(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("ready_for_review", task=_task(inp), summary=str(inp.get("summary") or "")))


async def report_blocked(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("report_blocked", task=_task(inp), kind=str(inp.get("kind") or ""),
                                       reason=str(inp.get("reason") or ""), quote=inp.get("quote")))


async def run_check(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("run_check", tests=_list(inp.get("tests"), "tests"), full=bool(inp.get("full"))))


async def wait(inp: dict, ctx: ToolContext) -> str:
    ids = _list(inp.get("jobs"), "jobs")
    if not ids:
        raise ToolError("jobs is required")
    return _out(await _rt(ctx).request("wait", jobs=ids, timeout=float(inp.get("timeout") or 600)))


async def rollback(inp: dict, ctx: ToolContext) -> str:
    to = inp.get("checkpoint")
    return _out(await _rt(ctx).request("rollback", checkpoint=None if to in (None, "") else int(to)))


_TASK = {"type": "string", "description": "Task id, e.g. T3"}

TOOLS = [
    Tool("board",
         "Show the task graph the harness keeps for this run: requirements and their status, every task (who holds "
         "it, what blocks it), a suggested order, the latest verified checkpoint and your unverified changes.",
         {"type": "object", "properties": {}}, board, read_only=True),
    Tool("claim",
         "Claim a task before working on it. Claims are leases kept alive by your activity. You may claim any ready "
         "task; the suggested order is only advice.",
         {"type": "object", "properties": {"task": _TASK}, "required": ["task"]}, claim),
    Tool("release",
         "Give a task back without finishing it (it becomes available again). Leave a note for whoever picks it up.",
         {"type": "object", "properties": {"task": _TASK, "note": {"type": "string"}}, "required": ["task"]}, release),
    Tool("add_task",
         "Add a task you discovered while working (for example a prerequisite refactor or a bug the task needs "
         "fixed). It must link to at least one requirement. checks may only name tests that exist on the original "
         "code (see board); tests you write yourself are development signals, not verification.",
         {"type": "object", "properties": {
             "title": {"type": "string"}, "description": {"type": "string"},
             "links": {"type": "array", "items": {"type": "string"}, "description": "Requirement ids, e.g. [\"R2\"]"},
             "blocked_by": {"type": "array", "items": {"type": "string"}, "description": "Task ids"},
             "checks": {"type": "array", "items": {"type": "string"}, "description": "Existing test node ids"}},
          "required": ["title", "links"]}, add_task),
    Tool("note",
         "Leave a note for yourself or whoever continues this work in a later session: decisions and why, what you "
         "tried that did not work, what to do next. Notes are kept across sessions and shown as your own "
         "(unverified) notes; facts such as files changed and test results are tracked by the harness anyway.",
         {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, note),
    Tool("checkpoint",
         "Ask the harness to save your current working tree as a verified checkpoint. Changes under test paths are "
         "restored to the original first (tests are the acceptance baseline). The tests that passed twice on the "
         "original code and relate to your changes are run; if none of them fails, errors, is skipped or goes "
         "missing, the checkpoint chain advances and this state becomes the deliverable. Otherwise nothing changes "
         "and you get the failing checks back. Only checkpointed work is delivered.",
         {"type": "object", "properties": {"summary": {"type": "string", "description": "What this state contains"}}},
         checkpoint),
    Tool("ready_for_review",
         "Declare a task you hold finished. The harness checkpoints your working tree and then looks at the task's "
         "checks on that checkpoint: if they all pass the task is done; a task without checks becomes "
         "done_unverified once its work is in a checkpoint. If the checkpoint is rejected or a check fails, the task "
         "is reopened and stays yours.",
         {"type": "object", "properties": {"task": _TASK, "summary": {"type": "string"}}, "required": ["task"]},
         ready_for_review),
    Tool("report_blocked",
         "Report that a task cannot be finished, instead of working around it.\n"
         "- insufficient_info: the task text does not give enough information.\n"
         "- environment: the environment prevents it (missing service, permissions, network).\n"
         "- check_conflict: an existing test that passed on the original code contradicts what the task text "
         "explicitly asks for. Quote the task text verbatim in quote. The conflict is recorded and reported; the "
         "regression gate does not change, so leave that behaviour intact in your checkpoints.\n"
         "The task is set aside and listed honestly in the final report; you can work on other tasks.",
         {"type": "object", "properties": {
             "task": _TASK, "kind": {"type": "string", "enum": ["insufficient_info", "environment", "check_conflict"]},
             "reason": {"type": "string"}, "quote": {"type": "string", "description": "Verbatim task text"}},
          "required": ["task", "kind", "reason"]}, report_blocked),
    Tool("run_check",
         "Start a check as a background job run by the harness and return at once with a job id (or the cached "
         "result if the same working tree was already checked). With no arguments it runs the tests related to your "
         "changes; tests=[...] runs specific test files or node ids; full=true runs everything. Results are compared "
         "with the original code: regressions, pre-existing failures and new tests. Keep working and call wait when "
         "you need the result, instead of sleeping.",
         {"type": "object", "properties": {
             "tests": {"type": "array", "items": {"type": "string"}}, "full": {"type": "boolean"}}}, run_check),
    Tool("wait",
         "Wait until the given jobs finish and return their results. Returns early with the current status after "
         "the timeout (seconds, default 600).",
         {"type": "object", "properties": {
             "jobs": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "integer"}},
          "required": ["jobs"]}, wait),
    Tool("rollback",
         "Discard your unverified changes and restore the working tree to a checkpoint (default: the latest one). "
         "Rolling back to an earlier checkpoint also abandons the checkpoints after it, and tasks finished on them "
         "are reopened. Use it when your working tree is broken beyond repair.",
         {"type": "object", "properties": {"checkpoint": {"type": "integer"}}}, rollback),
]

"""Belay 工具：只读图和向图报告，不替换任何已有工具。

v8 只留 worker 需要的四个：submit（请求立即复核）、board（只读）、以及被拒、被定位时消息里会点名的
反应式工具 failure_log、revert_change。回归门的豁免不再由 worker 决定：它在 submit 里提议，由复核者裁决。

工具只负责把请求交给 ctx.runtime（runtime/port.py 的 WorkerPort），不认识 runtime 的内部实现。
回复是 {"text": 给模型看的文字, "error": 是否被拒绝}；submit 被接受时另带 {"accepted": True}，会话随之结束。
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


async def submit(inp: dict, ctx: ToolContext) -> str:
    blocked = inp.get("blocked") or []
    if not isinstance(blocked, list) or not all(isinstance(b, dict) for b in blocked):
        raise ToolError("blocked must be a list of objects {requirement, kind, reason, quote}")
    waivers = inp.get("waivers") or []
    if not isinstance(waivers, list) or not all(isinstance(w, dict) for w in waivers):
        raise ToolError("waivers must be a list of objects {tests, quote, reason}")
    reply = await _rt(ctx).request("submit", summary=str(inp.get("summary") or ""), blocked=blocked,
                                   waivers=waivers)
    text = _out(reply)
    if reply.get("accepted"):
        ctx.submitted = True
        ctx.summary = str(inp.get("summary") or "")
    return text


async def board(inp: dict, ctx: ToolContext) -> str:
    return _out(await _rt(ctx).request("board", status=inp.get("status"), requirement=inp.get("requirement"),
                                       view=inp.get("view"), page=int(inp.get("page") or 1)))


async def failure_log(inp: dict, ctx: ToolContext) -> str:
    test = str(inp.get("test") or "").strip()
    if not test:
        raise ToolError("test is required (a test node id)")
    return _out(await _rt(ctx).request("failure_log", test=test))


async def revert_change(inp: dict, ctx: ToolContext) -> str:
    loc = str(inp.get("located") or "").strip()
    if not loc:
        raise ToolError("located is required (e.g. \"L1#0\", from a located regression)")
    return _out(await _rt(ctx).request("revert_change", located=loc))


TOOLS = [
    Tool("submit",
         "Ask for your work to be reviewed now; call it when you believe the requirements are done. The harness "
         "snapshots your working tree (changes to test files are left out), runs the regression gate, and a reviewer "
         "checks every requirement, running tests and your code where it can. If it is approved it becomes the merge "
         "point that will be delivered. You get the reviewer's verdict back: what is still missing, or why it was not "
         "merged; keep working and call submit again. In the summary, report what actually happened (it is treated "
         "as a claim, not as evidence). If a requirement cannot be done here, list it in blocked with a kind: "
         "insufficient_info (the task text does not say enough), environment (the environment prevents it), "
         "check_conflict (the task text explicitly asks for something existing tests contradict; quote the task text "
         "verbatim). If specific gate tests fail only because the task text explicitly asks for the behaviour they "
         "contradict, propose waivers: the reviewer decides.",
         {"type": "object", "properties": {
             "summary": {"type": "string"},
             "blocked": {"type": "array", "items": {"type": "object", "properties": {
                 "requirement": {"type": "string", "description": "Requirement id, e.g. R3"},
                 "kind": {"type": "string", "enum": ["insufficient_info", "environment", "check_conflict"]},
                 "reason": {"type": "string"},
                 "quote": {"type": "string", "description": "Verbatim task text (check_conflict)"}},
                 "required": ["requirement", "kind", "reason"]}},
             "waivers": {"type": "array", "items": {"type": "object", "properties": {
                 "tests": {"type": "array", "items": {"type": "string"},
                           "description": "Full test ids as the gate reports them"},
                 "quote": {"type": "string", "description": "Verbatim task text that asks for the new behaviour"},
                 "reason": {"type": "string", "description": "How each test contradicts it"},
                 "requirement": {"type": "string"}},
                 "required": ["tests", "quote", "reason"]}}},
          "required": ["summary"]},
         submit),
    Tool("board",
         "Show the requirements checklist the harness keeps for this run, with each requirement's status as the "
         "reviewer judged it (open, done with its evidence level, blocked) and what is still missing; the latest merge "
         "points. Filters: requirement (e.g. \"R3\") for its task text, how it will be checked, evidence and history; "
         "status (open | done | blocked | unfinished); view (\"requirements\" | \"merges\" | \"failures\" for the "
         "tests that already fail on the original code); page.",
         {"type": "object", "properties": {
             "requirement": {"type": "string"}, "status": {"type": "string"},
             "view": {"type": "string", "enum": ["requirements", "merges", "failures"]},
             "page": {"type": "integer"}}},
         board, read_only=True),
    Tool("failure_log",
         "Show the traceback of a failing test from the most recent harness run that included it (the harness's "
         "own logs are otherwise not accessible).",
         {"type": "object", "properties": {"test": {"type": "string", "description": "Test node id"}},
          "required": ["test"]}, failure_log, read_only=True),
    Tool("revert_change",
         "Undo only the change that the harness located as the start of a regression (for example \"L1#0\" from a "
         "located regression message): the difference between the last good and the first bad snapshot, limited to "
         "the files it names, is reversed in your working tree. Later work is kept. If it conflicts with later "
         "changes nothing is modified and you are told why.",
         {"type": "object", "properties": {"located": {"type": "string"}}, "required": ["located"]}, revert_change),
]

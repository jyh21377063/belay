"""Belay 工具：只读图和向图报告，不替换任何已有工具。

v7 只留 worker 需要的五个：submit（唯一的完成声明）、board（只读）、以及被拒、被定位时消息里会点名的
反应式工具 failure_log、revert_change、waive_check。认领、步骤、手动存档、回退这些需要模型主动想起来的流程工具都删掉了。

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


def _list(v, name: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, (str, int)) for x in v):
        raise ToolError(f"{name} must be a list of strings")
    return [str(x) for x in v]


async def submit(inp: dict, ctx: ToolContext) -> str:
    blocked = inp.get("blocked") or []
    if not isinstance(blocked, list) or not all(isinstance(b, dict) for b in blocked):
        raise ToolError("blocked must be a list of objects {requirement, kind, reason, quote}")
    reply = await _rt(ctx).request("submit", summary=str(inp.get("summary") or ""), blocked=blocked)
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


async def waive_check(inp: dict, ctx: ToolContext) -> str:
    tests = _list(inp.get("tests"), "tests")
    if not tests:
        raise ToolError("tests is required")
    req = str(inp.get("requirement") or "").strip() or None
    return _out(await _rt(ctx).request("waive_check", tests=tests, quote=str(inp.get("quote") or ""),
                                       reason=str(inp.get("reason") or ""), requirement=req))


TOOLS = [
    Tool("submit",
         "Declare that you are done. The harness checkpoints your working tree (changes to test files are left out), "
         "runs the regression gate, checks every requirement on the checklist, and has a reviewer look at the ones "
         "without tests. If anything is missing you get the list back and keep working; call submit again when it "
         "is done. In the summary, report what actually happened. If a requirement cannot be done here, list it in "
         "blocked with a kind: insufficient_info (the task text does not say enough), environment (the environment "
         "prevents it), check_conflict (the task text explicitly asks for something existing tests contradict; "
         "quote the task text verbatim).",
         {"type": "object", "properties": {
             "summary": {"type": "string"},
             "blocked": {"type": "array", "items": {"type": "object", "properties": {
                 "requirement": {"type": "string", "description": "Requirement id, e.g. R3"},
                 "kind": {"type": "string", "enum": ["insufficient_info", "environment", "check_conflict"]},
                 "reason": {"type": "string"},
                 "quote": {"type": "string", "description": "Verbatim task text (check_conflict)"}},
                 "required": ["requirement", "kind", "reason"]}}},
          "required": ["summary"]},
         submit),
    Tool("board",
         "Show the requirements checklist the harness keeps for this run, with each requirement's status (open, "
         "verified by its checks, submitted, blocked) and why a reopened one is not done; the latest checkpoints. "
         "Filters: requirement (e.g. \"R3\") for its task text, checks and history; status (open | verified | "
         "submitted | blocked | unfinished | done); view (\"requirements\" | \"checkpoints\" | \"failures\" for the "
         "tests that already fail on the original code); page.",
         {"type": "object", "properties": {
             "requirement": {"type": "string"}, "status": {"type": "string"},
             "view": {"type": "string", "enum": ["requirements", "checkpoints", "failures"]},
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
    Tool("waive_check",
         "Take existing tests out of the regression gate because the task text explicitly asks for behaviour they "
         "contradict (for example the task changes a default value or an error message that an old test asserts). "
         "Use it only after the harness has seen these tests fail on your changes (a rejected submit), and only for "
         "that reason: never to get past a failure you caused by mistake. quote must be copied verbatim from the "
         "task text; reason says how each test contradicts it. Every waiver is listed in the final report. Then "
         "submit again.",
         {"type": "object", "properties": {
             "tests": {"type": "array", "items": {"type": "string"},
                       "description": "Full test ids as the gate reports them"},
             "quote": {"type": "string", "description": "Verbatim task text that asks for the new behaviour"},
             "reason": {"type": "string"},
             "requirement": {"type": "string", "description": "Requirement id, if one applies"}},
          "required": ["tests", "quote", "reason"]}, waive_check),
]

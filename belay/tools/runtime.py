"""Belay 的 runtime 工具：只负责把请求交给 ctx.runtime（Orchestrator 的收件箱），不认识它的内部实现。

  run_check        后台跑检查（测试或命令），立即返回作业 id；命中缓存时直接返回结果
  wait             挂起到作业完成，返回按基线归类的结果
  ledger           需求账本、剩余预算、已知失败
  submit           提交候选：检查点（final=false）或最终提交（final=true）
  request_test     请独立的 Test Author 为一条需求写测试
  report_conflict  上报测试与需求冲突、信息不足、环境问题

B 组（FlatAgent）没有 runtime，这些工具不注册。submit 与 shell.py 里 B 组的 submit 同名，二者只注册其一。
"""
from __future__ import annotations

from belay.tools.base import Tool, ToolContext, ToolError


def _runtime(ctx: ToolContext):
    if ctx.runtime is None:
        raise ToolError("This tool is not available in this run.")
    return ctx.runtime


def _out(reply: dict) -> str:
    text = str(reply.get("text") or "")
    if reply.get("error"):
        raise ToolError(text or "request rejected")
    return text


def _str_list(v, name: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list) or not all(isinstance(x, (str, int)) for x in v):
        raise ToolError(f"{name} must be a list of strings")
    return [str(x) for x in v]


async def run_check(inp: dict, ctx: ToolContext) -> str:
    command = (inp.get("command") or "").strip() or None
    if command:
        ctx.check_command(command)
    return _out(await _runtime(ctx).request("run_check", tests=_str_list(inp.get("tests"), "tests"),
                                            full=bool(inp.get("full")), command=command))


async def wait(inp: dict, ctx: ToolContext) -> str:
    ids = _str_list(inp.get("job_ids"), "job_ids")
    if not ids:
        raise ToolError("job_ids is required")
    return _out(await _runtime(ctx).request("wait", job_ids=ids, timeout=float(inp.get("timeout") or 600)))


async def ledger(inp: dict, ctx: ToolContext) -> str:
    return _out(await _runtime(ctx).request("ledger"))


async def submit(inp: dict, ctx: ToolContext) -> str:
    summary = str(inp.get("summary") or "")
    reply = await _runtime(ctx).request("submit", summary=summary, final=bool(inp.get("final")))
    if reply.get("finished"):
        ctx.submitted = True
        ctx.summary = summary
    return _out(reply)


async def request_test(inp: dict, ctx: ToolContext) -> str:
    req = str(inp.get("requirement") or "").strip()
    if not req:
        raise ToolError("requirement is required (an id such as R3)")
    return _out(await _runtime(ctx).request("request_test", req_id=req,
                                            interface=str(inp.get("interface") or "")))


async def report_conflict(inp: dict, ctx: ToolContext) -> str:
    reason = str(inp.get("reason") or "").strip()
    if not reason:
        raise ToolError("reason is required")
    return _out(await _runtime(ctx).request("report_conflict", report_kind=str(inp.get("kind") or ""),
                                            req_id=(str(inp.get("requirement")).strip() or None)
                                            if inp.get("requirement") else None,
                                            check_ids=_str_list(inp.get("checks"), "checks"), reason=reason))


TOOLS = [
    Tool("run_check",
         "Start a check as a background job managed by the harness and return at once with a job id (or with the "
         "cached result if this exact working tree was already checked).\n"
         "- With no arguments it runs the test files related to the files you changed; tests=[...] runs specific "
         "test files or node ids; full=true runs the task's whole test suite.\n"
         "- Test results are compared with the baseline recorded on the original code: regressions (passed before "
         "your changes, fail now), pre-existing failures (also fail on the original code, not your problem) and new "
         "tests.\n"
         "- command=\"...\" runs any shell command as a job instead (for example a long build); you get the exit code "
         "and the end of the output.\n"
         "Keep working while it runs and call wait when you need the result. Do not use sleep to wait for results.",
         {"type": "object", "properties": {
             "tests": {"type": "array", "items": {"type": "string"},
                       "description": "Test files or pytest node ids"},
             "full": {"type": "boolean", "description": "Run the full test suite"},
             "command": {"type": "string", "description": "A shell command to run as a job instead of tests"}}},
         run_check),
    Tool("wait",
         "Wait until the given jobs finish and return their results (classified against the original code). "
         "Returns early with the current status after the timeout.",
         {"type": "object", "properties": {
             "job_ids": {"type": "array", "items": {"type": "string"}},
             "timeout": {"type": "integer", "description": "Seconds to wait at most (default 600)"}},
          "required": ["job_ids"]},
         wait),
    Tool("ledger",
         "Show the requirement ledger: each requirement's status and evidence, the time left, what is on the "
         "integration branch, pending reports and independent tests, and the tests that already fail on the "
         "original code.",
         {"type": "object", "properties": {}},
         ledger, read_only=True),
    Tool("submit",
         "Hand your current working tree to the harness as a candidate. The harness checks it against the tests "
         "that passed on the original code and merges it into the deliverable only if none of them fail now; "
         "otherwise you get the failures back and continue.\n"
         "- final=false: a checkpoint. Tests related to your changes are checked; on success you keep working.\n"
         "- final=true: you have finished the whole task. The full suite is checked; the harness then decides from "
         "the ledger whether the run ends.\n"
         "The summary must report what actually happened: first which requirements are not done and which checks "
         "fail or were skipped, then what you changed and how you verified it.",
         {"type": "object", "properties": {
             "summary": {"type": "string"},
             "final": {"type": "boolean", "description": "true when the whole task is finished"}},
          "required": ["summary", "final"]},
         submit),
    Tool("request_test",
         "Ask an independent Test Author to write a test for one requirement. It sees the requirement text, the "
         "original code and the interface you declare, never your implementation. The test is accepted only if it "
         "fails on the original code at the assertion level; then it runs in every gate, and the requirement counts "
         "as SUPPORTED once it passes on the integration branch. The call returns at once; you are notified of the "
         "outcome. Declare the public names and signatures you are adding or changing for this requirement, so the "
         "test uses them.",
         {"type": "object", "properties": {
             "requirement": {"type": "string", "description": "Requirement id, e.g. R3"},
             "interface": {"type": "string",
                           "description": "Public functions, classes, options or CLI flags (with signatures) that "
                                          "the requirement adds or changes"}},
          "required": ["requirement"]},
         request_test),
    Tool("report_conflict",
         "Report a problem instead of working around it. A reviewer who sees only the requirement text, your diff "
         "and the failing checks decides; the decision is recorded in the ledger.\n"
         "- test_conflict: a test that passed on the original code now fails because the requirement explicitly "
         "asks for that behaviour change. If approved, the gate stops counting that test as a regression.\n"
         "- insufficient_info: the requirement cannot be implemented from the information available.\n"
         "- environment: a failure is caused by the environment (network, permissions, services), not the code.\n"
         "Name the tests exactly as run_check or the gate reported them.",
         {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["test_conflict", "insufficient_info", "environment"]},
             "requirement": {"type": "string", "description": "Requirement id, e.g. R3"},
             "checks": {"type": "array", "items": {"type": "string"}, "description": "Test ids"},
             "reason": {"type": "string", "description": "Why; cite the requirement text"}},
          "required": ["kind", "reason"]},
         report_conflict),
]

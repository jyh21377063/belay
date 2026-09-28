"""需求账本：按证据计算需求状态，并把账本、作业结果格式化为给模型看的文字（纯函数）。

需求状态（v4）：
  SUPPORTED  至少一项独立检查在集成分支 HEAD 上通过（独立检查在收录时已经证明在原始代码上以断言失败）
  FAILED     独立检查在 HEAD 上失败
  WAIVED     测试冲突的上报获批：reviewer 引用了需求原文中明确要求的行为变化
  UNKNOWN    worker 上报信息不足且获批；或运行结束时仍没有任何独立证据
  OPEN       运行中、还没有独立证据
"""
from __future__ import annotations

from dataclasses import dataclass, field

from belay.graph.evidence import Classified, test_file
from belay.graph.model import (FAIL, FAILED, FLAKY, OPEN, PASS, SUPPORTED, UNKNOWN, WAIVED, GraphState, Job,
                               Requirement)


@dataclass
class ReqStatus:
    id: str
    status: str
    why: str
    evidence: list[str] = field(default_factory=list)


def requirement_status(state: GraphState, req: Requirement, finished: bool | None = None) -> ReqStatus:
    finished = state.run.phase == "finished" if finished is None else finished
    head = state.run.head_tree
    checks = state.authored(req.id)
    results = {c.id: c.results.get(head) for c in checks}
    passed = [cid for cid, r in results.items() if r == PASS]
    failed = [cid for cid, r in results.items() if r == FAIL]
    if passed:
        return ReqStatus(req.id, SUPPORTED, f"independent test {', '.join(passed)} passes on the integration branch",
                         passed)
    if failed:
        return ReqStatus(req.id, FAILED, f"independent test {', '.join(failed)} fails on the integration branch",
                         failed)
    approved = [r for r in state.report.values() if r.req_id == req.id and r.verdict == "approved"]
    for r in approved:
        if r.kind == "test_conflict":
            return ReqStatus(req.id, WAIVED, f"report {r.id} approved: {', '.join(r.check_ids)} may change", [r.id])
    for r in approved:
        if r.kind == "insufficient_info":
            return ReqStatus(req.id, UNKNOWN, f"report {r.id} approved: not enough information", [r.id])
    if checks:
        return ReqStatus(req.id, UNKNOWN if finished else OPEN,
                         f"independent test {', '.join(c.id for c in checks)} has not run on the integration branch")
    pending = [c.id for c in state.authored(req.id, active_only=False) if c.status == "pending"]
    if pending:
        return ReqStatus(req.id, UNKNOWN if finished else OPEN, f"independent test {pending[0]} is being written")
    return ReqStatus(req.id, UNKNOWN if finished else OPEN, "no independent evidence")


def statuses(state: GraphState, finished: bool | None = None) -> list[ReqStatus]:
    reqs = sorted(state.requirement.values(), key=lambda r: r.order)
    return [requirement_status(state, r, finished) for r in reqs]


def counts(sts: list[ReqStatus]) -> dict[str, int]:
    out = {k: 0 for k in (SUPPORTED, FAILED, WAIVED, UNKNOWN, OPEN)}
    for s in sts:
        out[s.status] += 1
    return out


def _short(text: str, n: int = 90) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[:n - 3] + "..."


def _minutes(sec: float) -> str:
    return f"{max(0.0, sec) / 60:.0f} min"


def ledger_text(state: GraphState, now: float, max_reqs: int = 80) -> str:
    run = state.run
    sts = statuses(state)
    cnt = counts(sts)
    chain = state.integration_chain()
    left = run.deadline_t - now
    lines = [f"Time: {_minutes(left)} left in the budget; the harness stops accepting work about "
             f"{_minutes(run.reserve_sec)} before the end to run the final gate."]
    if chain:
        last = chain[-1]
        lines.append(f"Integration branch: {len(chain)} merged commit(s); last #{last.seq} (gate: {last.level}).")
    else:
        lines.append("Integration branch: nothing merged yet (it still equals the original code).")
    lines.append(f"Requirements ({len(sts)}): " + ", ".join(f"{k} {v}" for k, v in cnt.items() if v))
    reqs = {r.id: r for r in state.requirement.values()}
    for s in sts[:max_reqs]:
        r = reqs[s.id]
        lines.append(f"  {s.id} [{r.kind}] {s.status}: {s.why} | {_short(r.text)}")
    if len(sts) > max_reqs:
        lines.append(f"  ... {len(sts) - max_reqs} more")
    authored = state.authored(active_only=False)
    if run.test_author_available:
        acc = sum(c.status == "active" for c in authored)
        pend = sum(c.status == "pending" for c in authored)
        rej = sum(c.status == "rejected" for c in authored)
        lines.append(f"Independent tests: {acc} accepted, {pend} being written, {rej} rejected.")
    base = state.baseline()
    if base:
        n_pass = sum(v == PASS for v in base.values())
        pre = sorted(t for t, v in base.items() if v == FAIL)
        fl = sum(v == FLAKY for v in base.values())
        lines.append(f"Baseline (original code): {n_pass} tests pass, {len(pre)} fail already (pre-existing, not "
                     f"your problem), {fl} flaky.")
        if pre:
            lines.append("  Pre-existing failures: " + ", ".join(pre[:15]) + (" ..." if len(pre) > 15 else ""))
    reports = sorted(state.report.values(), key=lambda r: r.created_t)
    for r in reports[-10:]:
        lines.append(f"Report {r.id} ({r.kind}, {r.req_id or '-'}): {r.verdict}" +
                     (f" — {_short(r.review, 160)}" if r.review else ""))
    running = [j for j in state.job.values() if j.state in ("QUEUED", "RUNNING")]
    for j in running:
        lines.append(f"Job {j.id} ({j.purpose}) is {j.state.lower()}.")
    return "\n".join(lines)


# ---- 作业结果的文字 ---------------------------------------------------------------

def _reason(reasons: dict[str, str], t: str) -> str:
    r = reasons.get(t, "")
    return f": {_short(r, 200)}" if r else ""


def _listing(title: str, tests: list[str], reasons: dict[str, str], limit: int) -> list[str]:
    if not tests:
        return []
    out = [f"{title} ({len(tests)}):"]
    out += [f"  - {t}{_reason(reasons, t)}" for t in tests[:limit]]
    if len(tests) > limit:
        out.append(f"  ... and {len(tests) - limit} more")
    return out


def job_text(job: Job, cl: Classified | None, authored: dict[str, str] | None = None) -> str:
    """一次开发检查的结果（给 wait / run_check 看）。"""
    res = job.result or {}
    head = f"Job {job.id} {job.state.lower()} after {job.sec:.0f}s"
    if job.command:
        tail = res.get("tail", "")
        return f"{head}, exit code {res.get('rc')}.\nOutput (tail):\n{tail}" + (
            f"\nFull log: {job.log}" if job.log else "")
    if res.get("status") not in (None, "ok"):
        head += f" ({res.get('status')}: {_short(res.get('error', ''), 300)})"
    if cl is None:
        return head + "."
    reasons = res.get("reasons", {})
    lines = [f"{head}: {cl.total} tests ran, {cl.passed} passed."]
    lines += _listing("REGRESSIONS — passed on the original code, fail now", cl.regressions, reasons, 30)
    lines += _listing("Failing but approved by a report", cl.waived, reasons, 10)
    lines += _listing("New or changed tests failing", cl.new_failed, reasons, 20)
    if cl.fixed:
        lines.append(f"Fixed ({len(cl.fixed)}): tests that failed on the original code now pass.")
    if cl.known:
        lines.append(f"Pre-existing failures ({len(cl.known)}): these also fail on the original code; ignore them.")
    if cl.flaky:
        lines.append(f"Flaky on the original code ({len(cl.flaky)}), failing now: " + ", ".join(cl.flaky[:5]))
    for cid, st in (authored or {}).items():
        lines.append(f"Independent test {cid}: {st}")
    if job.log:
        lines.append(f"Full log: {job.log}")
    return "\n".join(lines)


def rejection_text(cl: Classified, reasons: dict[str, str], level: str) -> str:
    lines = [f"Candidate rejected by the gate ({level} tests). These tests passed on the original code and fail "
             "on your candidate:"]
    lines += [f"  - {t}{_reason(reasons, t)}" for t in cl.regressions[:40]]
    if len(cl.regressions) > 40:
        lines.append(f"  ... and {len(cl.regressions) - 40} more")
    files = sorted({test_file(t) for t in cl.regressions})
    lines.append("The gate always runs the original test files, so fix the code, not the tests. If a failing test "
                 "encodes behaviour that the task explicitly asks to change, call report_conflict for it. "
                 f"Re-run with run_check(tests=[...]) on: {', '.join(files[:10])}")
    return "\n".join(lines)

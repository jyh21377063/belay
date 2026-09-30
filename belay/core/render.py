"""给 worker 和人看的文字（纯函数）：board、存档结果、作业结果、需求账本。"""
from __future__ import annotations

from typing import Optional

from belay.core.config import BelayConfig
from belay.core.model import (ACTIVE, ATT_CREATED, ATT_REJECTED, BLOCKED, DONE, DONE_UNVERIFIED, JOB_FINISHED,
                              OPEN, SPLIT, Graph)
from belay.core.queries import chain, deps_done, holder, num, requirement_status, stranded, workable
from belay.core.suggest import suggest
from belay.core.verify import (B_FAIL, B_FLAKY, B_PASS, PASSED, full_verified, guard_set, head_full_ok,
                               tree_regressions)


def task_line(g: Graph, tid: str) -> str:
    t = g.tasks[tid]
    extra = []
    h = holder(g, tid)
    if h:
        extra.append(f"held by {h}")
    if t.status == OPEN and not deps_done(g, t):
        extra.append("waiting for " + ", ".join(d for d in t.blocked_by if g.tasks[d].status not in
                                                (DONE, DONE_UNVERIFIED)))
    if t.checks:
        extra.append(f"{len(t.checks)} check(s)")
    if t.status in (DONE, DONE_UNVERIFIED):
        extra.append(f"checkpoint {t.done_checkpoint}")
    if t.status == BLOCKED:
        extra.append(f"{t.blocked_kind}: {(t.blocked_reason or '')[:80]}")
    if t.status == SPLIT:
        extra.append("split into " + ", ".join(t.children))
    return f"{t.id} [{t.status}] {t.title} -> {', '.join(t.links)}" + (f" ({'; '.join(extra)})" if extra else "")


def render_board(g: Graph, worker: str, now: float, cfg: BelayConfig) -> str:
    out = []
    cp = g.head_cp
    if cp is not None:
        out.append(f"Latest checkpoint: {cp.id}" + ("" if cp.id == 0 else f" ({cp.trigger}, tier {cp.tier})")
                   + f"; chain: {' <- '.join(str(c.id) for c in chain(g))}")
    w = g.wips.get(worker)
    if w and w.base == g.head:
        out.append(f"Your unverified changes: {len(w.files)} file(s)" +
                   (f"; last rejection: {w.last_rejection['reason']} "
                    f"({w.last_rejection.get('n_regressions', 0)} regression(s))" if w.last_rejection else ""))
    ss = suggest(g, worker, now, cfg)
    if ss:
        out.append("\nSuggested next:")
        for s in ss:
            out.append(f"  {s.rank}. " + (f"{s.task}: {s.reason}" if s.task else s.reason))
    out.append("\nRequirements:")
    for rid in sorted(g.requirements, key=num):
        r = g.requirements[rid]
        out.append(f"  {rid} [{requirement_status(g, rid)}] {r.summary or r.quote[:120]}")
    out.append("\nTasks:")
    for tid in sorted(g.tasks, key=num):
        if g.tasks[tid].status != SPLIT:
            out.append("  " + task_line(g, tid))
    return "\n".join(out)


def render_attempt(g: Graph, aid: Optional[str], task_id: Optional[str] = None) -> str:
    """checkpoint / ready_for_review 的回复。"""
    out = []
    if aid is None:
        cp = g.head_cp
        out.append(f"Nothing new to checkpoint: the working tree (without test-path changes) equals checkpoint {cp.id}.")
    else:
        a = g.attempts[aid]
        sel = "full suite" if a.selection is None else f"{len(a.selection)} related unit(s)"
        if a.status == ATT_CREATED:
            out.append(f"Checkpoint {a.checkpoint} created (attempt {aid}, tier {a.tier}, {sel}). It is now the "
                       "deliverable.")
            if a.flaky:
                out.append(f"Flaky (failed once, passed on rerun; not counted): {', '.join(a.flaky[:10])}")
        elif a.status == ATT_REJECTED:
            out.append(f"Checkpoint rejected (attempt {aid}, {a.reason}). The checkpoint chain did not move; your "
                       "working tree is unchanged.")
            if a.regressions:
                out.append(f"{len(a.regressions)} check(s) that passed on the original code do not pass now:")
                out.extend(f"  - {r}" for r in a.regressions[:30])
                if len(a.regressions) > 30:
                    out.append(f"  ... and {len(a.regressions) - 30} more")
            if a.flaky:
                out.append(f"Flaky (not counted): {', '.join(a.flaky[:10])}")
            w = g.wips.get(a.worker)
            detail = (w.last_rejection or {}).get("detail") if w else ""
            if detail:
                out.append(f"Runner notes: {detail[:600]}")
        else:
            out.append(f"Attempt {aid} is {a.status}.")
        wip = g.wips.get(a.worker)
        if wip and wip.dropped:
            out.append("Not included (test paths are restored to the original): " + ", ".join(wip.dropped[:10]))
    if task_id:
        t = g.tasks[task_id]
        if t.status == DONE:
            out.append(f"{task_id} is DONE: all its checks passed on checkpoint {t.done_checkpoint}.")
        elif t.status == DONE_UNVERIFIED:
            out.append(f"{task_id} is done_unverified: it has no checks; its work is in checkpoint "
                       f"{t.done_checkpoint}.")
        elif t.status == ACTIVE and t.reopen_reason:
            out.append(f"{task_id} was reopened ({t.reopen_reason}); you still hold it."
                       + (f" Failing: {'; '.join(t.last_failure[:10])}" if t.last_failure else ""))
        else:
            out.append(f"{task_id} is {t.status}.")
    return "\n".join(out)


def render_job(g: Graph, jid: str, max_lines: int = 40) -> str:
    j = g.jobs[jid]
    if j.state != JOB_FINISHED:
        return f"Job {jid}: {j.state}" + (f" ({j.error[:300]})" if j.error else "")
    res = j.results
    base = g.baseline
    regressed = sorted(t for t, s in res.items() if base.get(t) == B_PASS and s != PASSED)
    pre = sorted(t for t, s in res.items() if base.get(t) in (B_FAIL, B_FLAKY) and s != PASSED)
    fixed = sorted(t for t, s in res.items() if base.get(t) in (B_FAIL, B_FLAKY) and s == PASSED)
    new = sorted(t for t in res if t not in base)
    new_fail = [t for t in new if res[t] != PASSED]
    n_pass = sum(1 for s in res.values() if s == PASSED)
    out = [f"Job {jid} finished in {j.sec:.0f}s: {len(res)} results, {n_pass} passed."]
    if j.error:
        out.append(f"Runner: {j.error[:800]}")
    if regressed:
        out.append(f"REGRESSIONS ({len(regressed)}; passed on the original code):")
        out.extend(f"  - {t} ({res[t]})" for t in regressed[:max_lines])
    if fixed:
        out.append(f"Now passing (failed on the original code): {len(fixed)}: " + ", ".join(fixed[:20]))
    if pre:
        out.append(f"Pre-existing failures (also fail on the original code, not your problem): {len(pre)}")
    if new:
        out.append(f"Tests not in the baseline: {len(new)} ({len(new_fail)} not passing"
                   + (": " + ", ".join(f"{t} ({res[t]})" for t in new_fail[:15]) if new_fail else "") + ")")
    if not res and not j.error:
        out.append("(no test results; the command's exit code decides)")
    return "\n".join(out)


def ledger(g: Graph) -> dict:
    """结构化账本：运行结束时写入报告，也用于实验指标。"""
    reqs = []
    for rid in sorted(g.requirements, key=num):
        r = g.requirements[rid]
        reqs.append({"id": rid, "status": requirement_status(g, rid), "summary": r.summary, "quote": r.quote,
                     "tasks": [t.id for t in g.tasks.values() if rid in t.links]})
    cp = g.head_cp
    return {
        "status": g.run.status if g.run else None,
        "delivered_checkpoint": g.run.delivered if g.run else None,
        "head": g.head,
        "head_full_verified": bool(cp and full_verified(g, cp.tree)),
        "head_full_ok": head_full_ok(g),
        "head_regressions": list(tree_regressions(g, cp.tree)) if cp and full_verified(g, cp.tree) else [],
        "guard_checks": len(guard_set(g.baseline)),
        "requirements": reqs,
        "tasks": [{"id": t.id, "title": t.title, "status": t.status, "links": list(t.links), "checks": len(t.checks),
                   "origin": t.origin, "done_checkpoint": t.done_checkpoint, "reopened": t.reopen_count,
                   "blocked": {"kind": t.blocked_kind, "reason": t.blocked_reason, "quote": t.blocked_quote}
                   if t.status == BLOCKED else None} for t in sorted(g.tasks.values(), key=lambda t: num(t.id))],
        "unfinished": [t.id for t in workable(g)],
        "stranded": [t.id for t in stranded(g)],
        "checkpoints": [{"id": c.id, "parent": c.parent, "trigger": c.trigger, "tier": c.tier, "files": len(c.files),
                         "abandoned": c.abandoned} for c in sorted(g.checkpoints.values(), key=lambda c: c.id)],
        "attempts": {"total": len(g.attempts), "rejected": sum(a.status == ATT_REJECTED for a in g.attempts.values())},
        "sessions": [{"id": s.id, "reason": s.reason, "end": s.end_reason, "turns": s.turns, "progress": s.progress,
                      "compactions": len(s.compactions)} for s in sorted(g.sessions.values(), key=lambda s: num(s.id))],
        "stalls": [{"kind": s.kind, "action": s.action, "task": s.task} for s in g.stalls],
    }


def ledger_markdown(g: Graph) -> str:
    L = ledger(g)
    out = ["# Belay ledger", "", f"- status: **{L['status']}**, delivered checkpoint {L['delivered_checkpoint']}",
           f"- head full verification: {'ok' if L['head_full_ok'] else 'not ok / not run'}"
           + (f" ({len(L['head_regressions'])} regressions)" if L["head_regressions"] else ""),
           f"- regression gate: {L['guard_checks']} checks", "", "## Requirements", ""]
    for r in L["requirements"]:
        out.append(f"- {r['id']} [{r['status']}] {r['summary'] or r['quote'][:120]} (tasks: {', '.join(r['tasks'])})")
    out += ["", "## Tasks", ""]
    for t in L["tasks"]:
        line = f"- {t['id']} [{t['status']}] {t['title']}"
        if t["blocked"]:
            line += f" — blocked ({t['blocked']['kind']}): {t['blocked']['reason']}"
        out.append(line)
    if L["unfinished"]:
        out += ["", f"Unfinished: {', '.join(L['unfinished'])}"]
    if L["stranded"]:
        out += [f"Stranded behind blocked tasks: {', '.join(L['stranded'])}"]
    return "\n".join(out) + "\n"

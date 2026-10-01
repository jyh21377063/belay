"""给 worker 和人看的文字（纯函数）：board、需求详情、提交结果、存档结果、定位与诊断、需求账本。"""
from __future__ import annotations

from typing import Optional

from belay.core.config import BelayConfig
from belay.core.model import (ACTIONABLE, ATT_CREATED, ATT_REJECTED, ATT_SUPERSEDED, CONFIRMED, JOB_FINISHED,
                              REQ_BLOCKED, REQ_FINISHED, REQ_OPEN, REQ_SUBMITTED, REQ_VERIFIED, SUB_ACCEPTED,
                              SUB_REJECTED, SUB_RETURNED, TODO_ANCHORED, TODO_COMPLETED, Graph, Requirement)
from belay.core.queries import (actionable, chain, delivery_checkpoint, done_not_delivered, evidence_checks, id_ranges, is_ancestor,
                                latest_submit, num, open_requirements, status_reasons, suspect, todos_in_order)
from belay.core.verify import (B_FAIL, B_FLAKY, B_PASS, PASSED, active_guard, checkpoint_full_ok, full_verified,
                               reasons_for_tree, regression_ids, results_for_tree, tree_regressions)

PAGE = 50
STATUS_FILTERS = ("open", "verified", "submitted", "blocked", "unfinished", "done")
REOPEN_TEXT = {"review_missing": "the reviewer found parts missing", "review_reading": "the reviewer found a "
               "reasonable reading", "review_workaround": "the reviewer found a way to do it in this repository",
               "rolled_back": "its checkpoint was rolled back"}


def requirement_state(r: Requirement) -> str:
    """一个词的状态（给 worker 看）：verified / submitted（reviewed）/ blocked / open（reopened）。"""
    if r.status == REQ_SUBMITTED:
        return {"yes": "submitted, reviewed", "running": "submitted, under review"}.get(r.review or "",
                                                                                       "submitted")
    if r.status == REQ_OPEN and r.reopen_count:
        return "open, reopened"
    return r.status


def requirement_line(g: Graph, rid: str, width: int = 120) -> str:
    r = g.requirements[rid]
    line = f"{r.id} [{requirement_state(r)}] {(r.summary or r.quote)[:width]}"
    extra = []
    ev = evidence_checks(g, r)
    if ev:
        extra.append(f"{len(ev)} check(s)")
    if r.status in REQ_FINISHED and r.checkpoint is not None:
        extra.append(f"checkpoint {r.checkpoint}")
    if r.status == REQ_BLOCKED:
        extra.append(f"{r.blocked_kind}: {(r.blocked_reason or '')[:80]}")
    if r.status == REQ_OPEN and r.last_failure:
        why = REOPEN_TEXT.get(r.reopen_reason or "", "")
        extra.append((why + ": " if why else "") + "; ".join(r.last_failure[:3])[:300])
    return line + (f" ({'; '.join(extra)})" if extra else "")


def checkpoint_line(g: Graph, cid: int) -> str:
    cp = g.checkpoints[cid]
    if cid == 0:
        return "0 original code (confirmed)"
    tags = [cp.kind, cp.level]
    if cp.demoted:
        tags.append("demoted: " + "; ".join(cp.demote_regressions[:3]))
    elif suspect(g, cid):
        tags.append("suspect")
    if cp.abandoned:
        tags.append("abandoned")
    label = f" — {cp.label}" if cp.label else ""
    return f"{cid} ({', '.join(tags)}; {len(cp.files)} file(s)){label}"


def _paginate(lines: list[str], page: int, hint: str) -> list[str]:
    page = max(1, int(page or 1))
    start = (page - 1) * PAGE
    out = lines[start:start + PAGE]
    if len(lines) > start + PAGE:
        out.append(f"  ... {len(lines) - start - PAGE} more: {hint.format(page=page + 1)}")
    return out


def render_board(g: Graph, worker: str, now: float, cfg: BelayConfig, status: Optional[str] = None,
                 requirement: Optional[str] = None, view: Optional[str] = None, page: int = 1) -> str:
    """默认只给摘要与清单；requirement / status / view 过滤，page 分页。"""
    if requirement:
        return render_requirement(g, requirement)
    if view == "failures":
        return _render_failures(g, page)
    if view == "checkpoints":
        return render_history(g)
    if status or view == "requirements":
        return _render_requirements(g, status, page)
    out = []
    cp = g.head_cp
    if cp is not None:
        conf = g.confirmed
        out.append(f"Latest checkpoint: {checkpoint_line(g, cp.id)}")
        if conf is not None and conf != cp.id:
            out.append(f"Latest confirmed checkpoint (the deliverable): {conf}")
    w = g.wips.get(worker)
    if w and w.base == g.head and (w.files or w.dropped):
        out.append(f"Changes not in a checkpoint yet: {len(w.files)} file(s) (the harness verifies them in the "
                   "background)")
    sub = latest_submit(g, worker)
    if sub is not None:
        out.append(f"Last submit {sub.id}: {sub.status}" + (f" ({sub.reason})" if sub.reason else "")
                   + (f"; still open: {id_ranges(sub.open)}" if sub.open else ""))
    reqs = actionable(g)
    counts: dict[str, int] = {}
    for r in reqs:
        counts[r.status] = counts.get(r.status, 0) + 1
    out.append("\nRequirements: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    unfinished = [r for r in reqs if r.status in (REQ_OPEN, REQ_BLOCKED)]
    if unfinished:
        out.append("Open and blocked:")
        out.extend(_paginate(["  " + requirement_line(g, r.id) for r in unfinished], 1,
                             'board(status="unfinished", page={page})'))
    done = [r.id for r in reqs if r.status in REQ_FINISHED]
    if done:
        out.append(f"Finished: {id_ranges(done)} (board(status=\"done\") for details)")
    out.append("\nMore: board(requirement=\"R3\"), board(status=...), board(view=\"checkpoints\"), "
               "board(view=\"failures\").")
    return "\n".join(out)


def _render_requirements(g: Graph, status: Optional[str], page: int) -> str:
    rs = actionable(g)
    if status == "unfinished":
        rs = [r for r in rs if r.status in (REQ_OPEN, REQ_BLOCKED)]
    elif status == "done":
        rs = [r for r in rs if r.status in REQ_FINISHED]
    elif status:
        rs = [r for r in rs if r.status == status]
    if not rs:
        return f"No requirements with status {status}."
    hint = f'board(status="{status}", page={{page}})' if status else 'board(view="requirements", page={page})'
    return "\n".join([f"Requirements ({status or 'all'}, {len(rs)}):"] +
                     _paginate(["  " + requirement_line(g, r.id) for r in rs], page, hint))


def render_requirement(g: Graph, rid: str) -> str:
    """一条需求的全部信息：原文、检查项与它们在链头上的结果、状态历史、被重开的原因、复查结论、关联的 todo。"""
    r = g.requirements.get(rid)
    if r is None:
        return f"Unknown requirement {rid}."
    if r.kind != ACTIONABLE:
        return f"{rid} (context, not on the checklist): \"{r.quote}\""
    out = [requirement_line(g, rid), f"Task text: \"{r.quote}\""]
    ev = evidence_checks(g, r)
    if ev:
        res = results_for_tree(g, g.head_cp.tree) if g.head_cp is not None else {}
        out.append("Checks (fail on the original code; they decide when it is verified): "
                   + ", ".join(f"{c} ({res.get(c, 'not run on the latest checkpoint')})" for c in ev[:20]))
    if r.history:
        out.append("History: " + "; ".join(f"#{seq} {st} ({why})" for seq, st, why in r.history[-12:]))
    if r.last_failure:
        out.append("Last failure: " + "; ".join(r.last_failure[:10]))
    if r.review_missing:
        out.append("Reviewer found missing: " + "; ".join(r.review_missing[:10]))
    todos = [t for t in todos_in_order(g) if rid in t.requirements]
    if todos:
        out.append("Your todo items for it: " + "; ".join(f"[{t.status}] {t.title}" for t in todos[:10]))
    return "\n".join(out)


def _render_failures(g: Graph, page: int) -> str:
    fails = sorted(t for t, c in g.baseline.items() if c == B_FAIL)
    flaky = sorted(t for t, c in g.baseline.items() if c == B_FLAKY)
    out = [f"Checks that already fail on the original code ({len(fails)}):"]
    out += _paginate([f"  {t}" for t in fails], page, 'board(view="failures", page={page})')
    if flaky:
        out.append(f"Flaky on the original code ({len(flaky)}): " + ", ".join(flaky[:50]))
    return "\n".join(out)


def render_history(g: Graph) -> str:
    out = ["Checkpoint chain (newest first):"]
    for cp in chain(g):
        out.append("  " + checkpoint_line(g, cp.id))
    gone = [c.id for c in g.checkpoints.values() if c.abandoned]
    if gone:
        out.append(f"Abandoned by rollbacks: {', '.join(str(x) for x in sorted(gone))}")
    return "\n".join(out)


def _reason_lines(g: Graph, tree: str, regs, limit: int = 30) -> list[str]:
    reasons = reasons_for_tree(g, tree)
    out = []
    for r in list(regs)[:limit]:
        rid = regression_ids([r])[0]
        why = reasons.get(rid)
        out.append(f"  - {r}" + (f"\n      {why[:300]}" if why else ""))
    return out


def _rejection_lines(g: Graph, aid: str) -> list[str]:
    a = g.attempts[aid]
    out = []
    if a.regressions:
        out.append(f"{len(a.regressions)} check(s) that passed on the original code do not pass now "
                   "(failure reason from the test output under each):")
        out.extend(_reason_lines(g, a.tree, a.regressions))
        if len(a.regressions) > 30:
            out.append(f"  ... and {len(a.regressions) - 30} more")
        out.append("failure_log(test=...) shows the full traceback.")
    if a.flaky:
        out.append(f"Flaky (not counted): {', '.join(a.flaky[:10])}")
    w = g.wips.get(a.worker)
    detail = (w.last_rejection or {}).get("detail") if w else ""
    if a.reason == "precheck":
        snap = g.snapshots.get(a.snapshot)
        out.append("The changed files do not compile: " + ((snap.precheck if snap else "") or detail or "")[:600])
    elif detail:
        out.append(f"Runner notes: {detail[:600]}")
    snap = g.snapshots.get(a.snapshot)
    if snap is not None and a.regressions:
        reg_files = {regression_ids([r])[0].split("::")[0] for r in a.regressions}
        overlap = sorted(reg_files & set(snap.dropped))
        if overlap:
            out.append("These tests ran in their original version: your changes to "
                       + ", ".join(overlap[:10]) + " were not used.")
    return out


def render_submit(g: Graph, sid: str) -> str:
    """submit 的回复：被拒（回归）/ 交还清单（还有没完成的需求）/ 接受。"""
    s = g.submits[sid]
    out = []
    if s.status == SUB_REJECTED:
        a = g.attempts.get(s.attempt) if s.attempt else None
        out.append(f"Submit {sid} was not accepted: your working tree did not pass the regression gate "
                   f"({s.reason or (a.reason if a else 'rejected')}). Nothing was recorded; your working tree is "
                   "unchanged.")
        if a is not None:
            out.extend(_rejection_lines(g, a.id))
        out.append("Fix this and call submit again.")
        return "\n".join(out)
    cp = g.checkpoints.get(s.checkpoint) if s.checkpoint is not None else None
    if cp is not None:
        level = ("confirmed by the full suite" if cp.level == CONFIRMED else
                 "related tests pass; the full suite runs in the background")
        out.append(f"Your work is in checkpoint {cp.id} ({level}).")
        snap = g.snapshots.get(s.snapshot)
        if snap is not None and snap.dropped:
            out.append("Not included (test paths are restored to the original): " + ", ".join(snap.dropped[:10]))
    if s.status == SUB_ACCEPTED:
        reqs = actionable(g)
        ver = [r.id for r in reqs if r.status == REQ_VERIFIED]
        sub = [r.id for r in reqs if r.status == REQ_SUBMITTED]
        blk = [r.id for r in reqs if r.status == REQ_BLOCKED]
        out.append(f"Submit {sid} accepted: no requirement on the checklist is left open.")
        if ver:
            out.append(f"Verified by their checks: {id_ranges(ver)}")
        unrev = [rid for rid in sub if g.requirements[rid].review == "failed"]
        sub = [rid for rid in sub if rid not in unrev]
        if sub:
            out.append(f"Submitted (self-reported; the reviewer did not find anything missing): {id_ranges(sub)}")
        if unrev:
            out.append(f"Submitted (self-reported; not reviewed, the reviewer gave no answer): {id_ranges(unrev)}")
        if blk:
            out.append(f"Reported blocked: {id_ranges(blk)}")
        out.append("The harness now finalizes the run; you can stop.")
        return "\n".join(out)
    if s.status == SUB_RETURNED:
        out.append(f"Submit {sid} is not accepted yet: {len(s.open)} requirement(s) are still open. Keep working on "
                   "them and call submit again:")
        for rid in s.open[:40]:
            out.append("  - " + requirement_line(g, rid, 100))
        if len(s.open) > 40:
            out.append(f"  ... {len(s.open) - 40} more: board(status=\"open\")")
        out.append("If one of them cannot be done here, say so in submit(blocked=[{requirement, kind, reason}]).")
        return "\n".join(out)
    out.append(f"Submit {sid} is still being checked ({s.status}); keep working, the result will be reported.")
    return "\n".join(out)


def render_located(g: Graph, lid: str, group: Optional[int] = None) -> str:
    """定位结果：“X 在快照 s17（会话 S3，任务 T5 第 2 步）第一次失败；s16 → s17 改了 …，diff 见附件。”"""
    loc = g.locates.get(lid)
    if loc is None:
        return ""
    out = []
    for rec in loc.results:
        if group is not None and rec.get("group") != group:
            continue
        good, bad = rec.get("good") or {}, rec.get("bad") or {}
        att = rec.get("attribution") or {}
        where = []
        if att.get("session"):
            where.append(f"session {att['session']}")
        if att.get("todo") and att["todo"] in g.todos:
            where.append(f"while working on \"{g.todos[att['todo']].title[:80]}\"")
        tests = ", ".join(rec.get("tests", [])[:5])
        gl = f"s{good.get('id')}" if good.get("kind") == "snapshot" else f"checkpoint {good.get('id')}"
        bl = f"s{bad.get('id')}" if bad.get("kind") == "snapshot" else f"checkpoint {bad.get('id')}"
        head = (f"{tests} first failed at {bl}" + (f" ({'; '.join(where)})" if where else "")
                if rec.get("exact") else f"{tests} started failing somewhere after {gl}, at or before {bl}"
                + (f" ({'; '.join(where)})" if where else ""))
        files = rec.get("files") or []
        chg = ", ".join(f"{p} (+{a} -{d})" for p, a, d in files[:8]) + (" ..." if len(files) > 8 else "")
        out.append(f"{head}. {gl} -> {bl} changed {chg or 'nothing outside test paths'}"
                   + (f"; diff: {rec['diff']}" if rec.get("diff") else "") + ".")
        out.append(f"revert_change(located=\"{lid}#{rec.get('group', 0)}\") undoes only that change in your working "
                   "tree (nothing is changed if it conflicts).")
    return "\n".join(out)


def render_diagnosis(g: Graph, did: str) -> str:
    d = g.diagnoses.get(did)
    if d is None or d.status != "recorded":
        return ""
    r = d.result
    out = [f"Diagnosis of {', '.join(d.tests[:4])} (model-written advice; the regression gate is unchanged):"]
    for s in (r.get("suspects") or [])[:4]:
        out.append(f"- suspect {s.get('file')} {s.get('hunk', '')} (confidence {s.get('confidence')}): "
                   f"{s.get('reason', '')}")
    inten = r.get("intentional") or {}
    if inten.get("likely"):
        out.append(f"- the change looks intentional for the task text \"{inten.get('quote')}\": if the task really "
                   "asks for it, waive_check(tests=[...], quote=...) takes these tests out of the gate (listed in the "
                   "final report); otherwise undo the change.")
    if r.get("suggestion"):
        out.append(f"- suggestion: {r['suggestion']}")
    if r.get("flaky_suspect"):
        out.append("- the diagnoser suspects the test may be flaky (the confirmation rerun decides).")
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
    how = " (run as the gate runs it: original test files, verification directory)" if j.purpose == "gate" else ""
    out = [f"Job {jid} finished in {j.sec:.0f}s: {len(res)} results, {n_pass} passed{how}."]
    if j.error:
        out.append(f"Runner: {j.error[:800]}")
    if regressed:
        out.append(f"REGRESSIONS ({len(regressed)}; passed on the original code):")
        for t in regressed[:max_lines]:
            why = j.reasons.get(t)
            out.append(f"  - {t} ({res[t]})" + (f"\n      {why[:300]}" if why else ""))
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


# ======================================================================== 账本

def requirement_category(g: Graph, r: Requirement, delivered: Optional[int]) -> str:
    """verified / reviewed / self-reported / done-not-delivered / blocked / open。"""
    if r.status in REQ_FINISHED and delivered is not None and r.checkpoint is not None and \
            not is_ancestor(g, r.checkpoint, delivered):
        return "done-not-delivered"
    if r.status == REQ_VERIFIED:
        return "verified"
    if r.status == REQ_SUBMITTED:
        return "reviewed" if r.review == "yes" else "self-reported"
    if r.status == REQ_BLOCKED:
        return "blocked"
    return "open"


CATEGORIES = ("verified", "reviewed", "self-reported", "done-not-delivered", "blocked", "open")


def ledger(g: Graph) -> dict:
    """结构化账本：运行结束时写入报告，也用于实验指标。"""
    if g.run and g.run.delivered is not None:
        delivered = g.run.delivered
    elif g.run and g.head is not None:                 # 运行中：现在交付的话会交付哪个存档
        delivered = delivery_checkpoint(g, BelayConfig(deliver_unconfirmed=bool(g.run.deliver_unconfirmed)))
    else:
        delivered = g.confirmed
    reqs = actionable(g)
    cats = {c: [] for c in CATEGORIES}
    for r in reqs:
        cats[requirement_category(g, r, delivered)].append(r.id)
    cp = g.head_cp
    dcp = g.checkpoints.get(delivered) if delivered is not None else None
    return {
        "status": g.run.status if g.run else None,
        "status_reasons": list(g.run.status_reasons) if g.run and g.run.delivered is not None
        else status_reasons(g, delivered),
        "delivered_checkpoint": g.run.delivered if g.run else None,
        "would_deliver": delivered if not (g.run and g.run.delivered is not None) else None,
        "delivered_level": dcp.level if dcp else None,
        "delivered_full_ok": checkpoint_full_ok(g, delivered),
        "deliver_unconfirmed": g.run.deliver_unconfirmed if g.run else None,
        "head": g.head,
        "confirmed": g.confirmed,
        "degraded": g.degraded,
        "isolation": dict(g.isolation),
        "head_full_verified": bool(cp and full_verified(g, cp.tree)),
        "head_regressions": list(tree_regressions(g, cp.tree)) if cp and full_verified(g, cp.tree) else [],
        "guard_checks": len(active_guard(g)),
        "waived": [{"test": w.test, "requirement": w.requirement, "quote": w.quote, "reason": w.reason}
                   for w in sorted(g.waived.values(), key=lambda w: (w.seq, w.test))],
        "categories": {c: len(v) for c, v in cats.items()},
        "category_requirements": cats,
        "requirements": [{"id": r.id, "kind": r.kind, "status": r.status,
                          "category": requirement_category(g, r, delivered) if r.kind == ACTIONABLE else "context",
                          "summary": r.summary, "quote": r.quote, "checks": len(r.checks),
                          "evidence_checks": len(evidence_checks(g, r)), "checkpoint": r.checkpoint,
                          "reopened": r.reopen_count, "review": r.review,
                          "blocked": {"kind": r.blocked_kind, "reason": r.blocked_reason, "quote": r.blocked_quote}
                          if r.status == REQ_BLOCKED else None}
                         for r in sorted(g.requirements.values(), key=lambda r: num(r.id))],
        "not_delivered": [r.id for r in done_not_delivered(g, delivered)],
        "unfinished": [r.id for r in open_requirements(g)],
        "submits": [{"id": s.id, "status": s.status, "implicit": s.implicit, "checkpoint": s.checkpoint,
                     "open": list(s.open), "reason": s.reason} for s in sorted(g.submits.values(), key=lambda s: s.seq)],
        "reviews": [{"id": v.id, "phase": v.phase, "requirements": list(v.requirements), "status": v.status,
                     "retry_of": v.retry_of,
                     "results": {k: x.get("implemented") for k, x in v.results.items()}}
                    for v in sorted(g.reviews.values(), key=lambda v: v.seq)],
        "unreviewed": [r.id for r in actionable(g) if r.review == "failed"],
        "checkpoints": [{"id": c.id, "parent": c.parent, "trigger": c.trigger, "kind": c.kind, "level": c.level,
                         "tier": c.tier, "files": len(c.files), "demoted": c.demoted, "abandoned": c.abandoned,
                         "snapshot": c.snapshot, "label": c.label}
                        for c in sorted(g.checkpoints.values(), key=lambda c: c.id)],
        "attempts": {"total": len(g.attempts), "rejected": sum(a.status == ATT_REJECTED for a in g.attempts.values()),
                     "superseded": sum(a.status == ATT_SUPERSEDED for a in g.attempts.values()),
                     "background": sum(a.lane == "bg" for a in g.attempts.values())},
        "snapshots": len(g.snapshots),
        "todos": {"total": len(g.todos), "anchored": sum(t.status == TODO_ANCHORED for t in g.todos.values()),
                  "completed": sum(t.status == TODO_COMPLETED for t in g.todos.values())},
        "locates": [{"id": l.id, "trigger": l.trigger, "tests": list(l.tests)[:10], "status": l.status,
                     "results": [{"good": r.get("good", {}).get("id"), "bad": r.get("bad", {}).get("id"),
                                  "exact": r.get("exact")} for r in l.results]} for l in g.locates.values()],
        "persistent": sorted(g.persistent),
        "diagnoses": [{"id": d.id, "trigger": d.trigger, "status": d.status} for d in g.diagnoses.values()],
        "relations": [list(r) for r in g.relations],
        "sessions": [{"id": s.id, "reason": s.reason, "end": s.end_reason, "turns": s.turns, "progress": s.progress,
                      "compactions": len(s.compactions), "resumes": list(s.resumes)}
                     for s in sorted(g.sessions.values(), key=lambda s: num(s.id))],
        "stalls": [{"kind": s.kind, "action": s.action} for s in g.stalls],
        "recoveries": g.run.recoveries if g.run else 0,
        "rebuilds": g.run.rebuilds if g.run else 0,
    }


def ledger_markdown(g: Graph) -> str:
    L = ledger(g)
    c = L["categories"]
    if L["delivered_checkpoint"] is not None:
        dl = f"delivered checkpoint {L['delivered_checkpoint']} ({L['delivered_level']})"
    elif L["would_deliver"] is not None:
        dl = f"not delivered yet; delivering now would deliver checkpoint {L['would_deliver']} ({L['delivered_level']})"
    else:
        dl = "not delivered yet"
    out = ["# Belay ledger", "",
           f"- status: **{L['status']}**, {dl}",
           *[f"  - not DONE because {r}" for r in L["status_reasons"]],
           f"- deliver_unconfirmed={L['deliver_unconfirmed']}; chain head {L['head']}, latest confirmed "
           f"{L['confirmed']}"
           + (" — delivery falls behind the head" if L["delivered_checkpoint"] not in (None, L["head"]) else ""),
           f"- regression gate: {L['guard_checks']} checks"
           + (f", {len(L['waived'])} waived (see below)" if L["waived"] else "")
           + (" (degraded mode: verification switched the working tree; background verification only at handoffs)"
              if L["degraded"] else ""),
           "- requirements: " + ", ".join(f"{k} {v}" for k, v in c.items()),
           f"- submits: {len(L['submits'])}"
           + (f" (last: {L['submits'][-1]['status']})" if L["submits"] else ""),
           *([f"- not reviewed (the reviewer gave no answer, even when retried alone): {id_ranges(L['unreviewed'])}"]
             if L["unreviewed"] else []),
           "", "## Requirements", ""]
    for r in L["requirements"]:
        if r["kind"] != "actionable":
            continue
        line = f"- {r['id']} [{r['category']}] {r['summary'] or r['quote'][:120]}"
        if r["blocked"]:
            line += f" — blocked ({r['blocked']['kind']}): {r['blocked']['reason']}"
        out.append(line)
    ctx = [r["id"] for r in L["requirements"] if r["kind"] != "actionable"]
    if ctx:
        out += ["", f"Context (not on the checklist): {id_ranges(ctx)}"]
    if L["not_delivered"]:
        out += ["", f"Finished but not in the deliverable (finished after the delivered checkpoint): "
                    f"{', '.join(L['not_delivered'])}"]
    if L["waived"]:
        out += ["", "## Waived regression checks", "",
                "Existing tests taken out of the regression gate because the worker quoted task text asking for "
                "behaviour they contradict (the tests had failed on its changes):", ""]
        for w in L["waived"]:
            out.append(f"- {w['test']} ({w['requirement'] or '-'}): \"{w['quote'][:200]}\" — {w['reason'][:300]}")
    return "\n".join(out) + "\n"

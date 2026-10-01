"""给 worker 和人看的文字（纯函数）：board、任务详情、存档结果、作业结果、定位与诊断、需求账本。"""
from __future__ import annotations

from typing import Optional

from belay.core.config import BelayConfig
from belay.core.model import (ACTIVE, ATT_CREATED, ATT_REJECTED, ATT_SUPERSEDED, BLOCKED, CONFIRMED, DONE,
                              DONE_UNVERIFIED, JOB_FINISHED, OPEN, REVIEW, SPLIT, STEP_ANCHORED, STEP_DECLARED, Graph)
from belay.core.queries import (chain, done_not_delivered, holder, id_ranges, is_ancestor, notes_of_task, num,
                                requirement_status, status_reasons, steps_of, suspect, task_files, unfinished_deps,
                                workable)
from belay.core.suggest import suggest
from belay.core.verify import (B_FAIL, B_FLAKY, B_PASS, PASSED, active_guard, checkpoint_full_ok, full_verified,
                               reasons_for_tree, regression_ids, tree_regressions)

PAGE = 50
STATUS_FILTERS = ("open", "active", "review", "done", "done_unverified", "blocked", "split", "unfinished")


def task_line(g: Graph, tid: str) -> str:
    t = g.tasks[tid]
    extra = []
    h = holder(g, tid)
    if h:
        extra.append(f"held by {h}")
    deps = unfinished_deps(g, t)
    if t.status == OPEN and deps:
        extra.append("after " + ", ".join(deps))
    if t.checks:
        extra.append(f"{len(t.checks)} check(s)")
    if t.status in (DONE, DONE_UNVERIFIED):
        extra.append(f"checkpoint {t.done_checkpoint}")
        if t.review in ("yes", "partial", "no"):
            extra.append(f"review: {t.review}")
    if t.status == BLOCKED:
        extra.append(f"{t.blocked_kind}: {(t.blocked_reason or '')[:80]}")
    if t.status == SPLIT:
        extra.append("split into " + ", ".join(t.children))
    return f"{t.id} [{t.status}] {t.title} -> {', '.join(t.links)}" + (f" ({'; '.join(extra)})" if extra else "")


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
                 requirement: Optional[str] = None, task: Optional[str] = None, view: Optional[str] = None,
                 page: int = 1) -> str:
    """默认只给摘要与计数；status / requirement / task / view 过滤，page 分页。"""
    if task:
        return render_task(g, task)
    if requirement:
        return _render_requirement(g, requirement)
    if view == "failures":
        return _render_failures(g, page)
    if view == "checkpoints":
        return render_history(g)
    if status or view == "tasks":
        return _render_tasks(g, status, page)
    out = []
    cp = g.head_cp
    if cp is not None:
        conf = g.confirmed
        out.append(f"Latest checkpoint: {checkpoint_line(g, cp.id)}")
        if conf is not None and conf != cp.id:
            out.append(f"Latest confirmed checkpoint (the deliverable): {conf}")
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
    counts: dict[str, int] = {}
    for rid in g.requirements:
        st = requirement_status(g, rid)
        counts[st] = counts.get(st, 0) + 1
    out.append("\nRequirements: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    tcounts: dict[str, int] = {}
    for t in g.tasks.values():
        tcounts[t.status] = tcounts.get(t.status, 0) + 1
    out.append("Tasks: " + ", ".join(f"{k} {v}" for k, v in sorted(tcounts.items())))
    unfinished = [t for t in sorted(g.tasks.values(), key=lambda t: num(t.id)) if t.status in (OPEN, ACTIVE, REVIEW,
                                                                                              BLOCKED)]
    if unfinished:
        out.append("\nUnfinished and blocked tasks:")
        out.extend(_paginate(["  " + task_line(g, t.id) for t in unfinished], 1, 'board(status="unfinished", '
                                                                              'page={page})'))
    done = [t.id for t in g.tasks.values() if t.status in (DONE, DONE_UNVERIFIED)]
    if done:
        out.append(f"\nFinished: {id_ranges(done)} (board(status=\"done\") for details)")
    out.append("\nMore: board(requirement=\"R3\"), board(task=\"T7\") or task(id=\"T7\"), board(status=...), "
               "board(view=\"checkpoints\"), board(view=\"failures\").")
    return "\n".join(out)


def _render_tasks(g: Graph, status: Optional[str], page: int) -> str:
    ts = sorted(g.tasks.values(), key=lambda t: num(t.id))
    if status == "unfinished":
        ts = [t for t in ts if t.status in (OPEN, ACTIVE, REVIEW)]
    elif status == "done":
        ts = [t for t in ts if t.status in (DONE, DONE_UNVERIFIED)]
    elif status:
        ts = [t for t in ts if t.status == status]
    else:
        ts = [t for t in ts if t.status != SPLIT]
    if not ts:
        return f"No tasks with status {status}."
    hint = f'board(status="{status}", page={{page}})' if status else 'board(view="tasks", page={page})'
    return "\n".join([f"Tasks ({status or 'all'}, {len(ts)}):"] + _paginate(["  " + task_line(g, t.id) for t in ts],
                                                                           page, hint))


def _render_requirement(g: Graph, rid: str) -> str:
    r = g.requirements.get(rid)
    if r is None:
        return f"Unknown requirement {rid}."
    out = [f"{rid} [{requirement_status(g, rid)}] {r.summary}", f"Task text: \"{r.quote}\"", "Tasks:"]
    out += ["  " + task_line(g, t.id) for t in sorted(g.tasks.values(), key=lambda t: num(t.id)) if rid in t.links]
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
        out.append(f"Abandoned by rollbacks: {id_ranges([str(x) for x in gone])}")
    out.append("history(a=..., b=...) shows the diff between two checkpoints.")
    return "\n".join(out)


def render_task(g: Graph, tid: str) -> str:
    """任务的完整信息：状态变化历史、被拒与重开的原因、全部笔记与步骤、改过的文件。"""
    t = g.tasks.get(tid)
    if t is None:
        return f"Unknown task {tid}."
    out = [task_line(g, tid)]
    if t.description:
        out.append(t.description.strip())
    for rid in t.links:
        r = g.requirements.get(rid)
        if r:
            out.append(f"- {rid} (task text): \"{r.quote}\"")
    if t.checks:
        out.append("- checks: " + ", ".join(t.checks[:30]))
    if t.blocked_by:
        out.append("- ordering hint: after " + ", ".join(t.blocked_by))
    if t.history:
        out.append("- history: " + "; ".join(f"#{seq} {st} ({why})" for seq, st, why in t.history[-15:]))
    if t.last_failure:
        out.append("- last failure: " + "; ".join(t.last_failure[:10]))
    if t.review_missing:
        out.append("- reviewer found missing: " + "; ".join(t.review_missing[:10]))
    steps = steps_of(g, tid)
    if steps:
        out.append("- steps:")
        for s in steps:
            extra = f" — {s.summary}" if s.summary else ""
            anchor = f" (checkpoint {s.checkpoint})" if s.checkpoint is not None else ""
            out.append(f"  {s.id} [{s.status}] {s.title}{anchor}{extra}")
    files = task_files(g, tid)
    if files:
        out.append("- files changed in checkpoints: " + ", ".join(f"{p} (+{a} -{d})" for p, a, d in files[:40]))
    notes = notes_of_task(g, tid)
    if notes:
        out.append("- notes (self-reported):")
        out.extend(f"  [{n.session or '-'}] {n.text}" for n in notes[-30:])
    return "\n".join(out)


def _reason_lines(g: Graph, tree: str, regs, limit: int = 30) -> list[str]:
    reasons = reasons_for_tree(g, tree)
    out = []
    for r in list(regs)[:limit]:
        rid = regression_ids([r])[0]
        why = reasons.get(rid)
        out.append(f"  - {r}" + (f"\n      {why[:300]}" if why else ""))
    return out


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
            cp = g.checkpoints[a.checkpoint]
            level = ("confirmed by the full suite" if cp.level == CONFIRMED else
                     "provisional: related tests pass; the full suite runs in the background")
            out.append(f"Checkpoint {a.checkpoint} created (attempt {aid}, tier {a.tier}, {sel}; {level}).")
            if a.flaky:
                out.append(f"Flaky (failed once, passed on rerun; not counted): {', '.join(a.flaky[:10])}")
        elif a.status == ATT_SUPERSEDED:
            out.append(f"Your working tree is already covered by checkpoint {g.head} (attempt {aid} was superseded "
                       "by a newer checkpoint of the same work).")
        elif a.status == ATT_REJECTED:
            out.append(f"Checkpoint rejected (attempt {aid}, {a.reason}). The checkpoint chain did not move; your "
                       "working tree is unchanged.")
            if a.regressions:
                out.append(f"{len(a.regressions)} check(s) that passed on the original code do not pass now "
                           "(failure reason from the test output under each):")
                out.extend(_reason_lines(g, a.tree, a.regressions))
                if len(a.regressions) > 30:
                    out.append(f"  ... and {len(a.regressions) - 30} more")
                out.append("Use failure_log(test=...) for the full traceback, run_check(tests=[...], as_gate=true) "
                           "to reproduce exactly as the gate runs it.")
            if a.flaky:
                out.append(f"Flaky (not counted): {', '.join(a.flaky[:10])}")
            w = g.wips.get(a.worker)
            detail = (w.last_rejection or {}).get("detail") if w else ""
            if detail:
                out.append(f"Runner notes: {detail[:600]}")
            snap = g.snapshots.get(a.snapshot)
            if snap is not None and a.regressions:
                reg_files = {regression_ids([r])[0].split("::")[0] for r in a.regressions}
                overlap = sorted(reg_files & set(snap.dropped))
                if overlap:
                    out.append("These tests ran in their original version: your changes to "
                               + ", ".join(overlap[:10]) + " were not used.")
        else:
            out.append(f"Attempt {aid} is {a.status}.")
        snap = g.snapshots.get(g.attempts[aid].snapshot)
        if snap is not None and snap.dropped and g.attempts[aid].status != ATT_REJECTED:
            out.append("Not included (test paths are restored to the original): " + ", ".join(snap.dropped[:10]))
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
        if att.get("held"):
            where.append("task " + ", ".join(att["held"][:3]))
        if att.get("step"):
            where.append(f"step {att['step']}")
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
        out.append(f"First choice: revert_change(located=\"{lid}#{rec.get('group', 0)}\") undoes only that change in "
                   "your working tree (nothing is changed if it conflicts). rollback is the alternative.")
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

def task_category(g: Graph, t, delivered: Optional[int]) -> str:
    """verified / reviewed / self-reported / done-not-delivered / blocked / open。"""
    if t.status in (DONE, DONE_UNVERIFIED) and delivered is not None and \
            not is_ancestor(g, t.done_checkpoint, delivered):
        return "done-not-delivered"
    if t.status == DONE:
        return "verified"
    if t.status == DONE_UNVERIFIED:
        return "reviewed" if t.review == "yes" else "self-reported"
    if t.status == BLOCKED:
        return "blocked"
    return "open"


CATEGORIES = ("verified", "reviewed", "self-reported", "done-not-delivered", "blocked", "open")


def ledger(g: Graph) -> dict:
    """结构化账本：运行结束时写入报告，也用于实验指标。"""
    delivered = g.run.delivered if g.run and g.run.delivered is not None else g.confirmed
    reqs = []
    for rid in sorted(g.requirements, key=num):
        r = g.requirements[rid]
        reqs.append({"id": rid, "status": requirement_status(g, rid), "summary": r.summary, "quote": r.quote,
                     "tasks": [t.id for t in g.tasks.values() if rid in t.links]})
    tasks = [t for t in sorted(g.tasks.values(), key=lambda t: num(t.id)) if t.status != SPLIT]
    cats = {c: [] for c in CATEGORIES}
    for t in tasks:
        cats[task_category(g, t, delivered)].append(t.id)
    cp = g.head_cp
    dcp = g.checkpoints.get(delivered) if delivered is not None else None
    return {
        "status": g.run.status if g.run else None,
        "status_reasons": list(g.run.status_reasons) if g.run and g.run.delivered is not None
        else status_reasons(g, delivered),
        "delivered_checkpoint": g.run.delivered if g.run else None,
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
        "waived": [{"test": w.test, "task": w.task, "quote": w.quote, "reason": w.reason}
                   for w in sorted(g.waived.values(), key=lambda w: (w.seq, w.test))],
        "categories": {c: len(v) for c, v in cats.items()},
        "category_tasks": cats,
        "requirements": reqs,
        "tasks": [{"id": t.id, "title": t.title, "status": t.status, "category": task_category(g, t, delivered),
                   "links": list(t.links), "checks": len(t.checks), "origin": t.origin,
                   "done_checkpoint": t.done_checkpoint, "reopened": t.reopen_count, "review": t.review,
                   "steps": len(steps_of(g, t.id)),
                   "blocked": {"kind": t.blocked_kind, "reason": t.blocked_reason, "quote": t.blocked_quote}
                   if t.status == BLOCKED else None} for t in tasks],
        "not_delivered": [t.id for t in done_not_delivered(g, delivered)],
        "unfinished": [t.id for t in workable(g)],
        "checkpoints": [{"id": c.id, "parent": c.parent, "trigger": c.trigger, "kind": c.kind, "level": c.level,
                         "tier": c.tier, "files": len(c.files), "demoted": c.demoted, "abandoned": c.abandoned,
                         "snapshot": c.snapshot, "label": c.label}
                        for c in sorted(g.checkpoints.values(), key=lambda c: c.id)],
        "attempts": {"total": len(g.attempts), "rejected": sum(a.status == ATT_REJECTED for a in g.attempts.values()),
                     "superseded": sum(a.status == ATT_SUPERSEDED for a in g.attempts.values()),
                     "background": sum(a.lane == "bg" for a in g.attempts.values())},
        "snapshots": len(g.snapshots),
        "steps": {"total": len(g.steps), "anchored": sum(s.status == STEP_ANCHORED for s in g.steps.values()),
                  "declared": sum(s.status == STEP_DECLARED for s in g.steps.values())},
        "locates": [{"id": l.id, "trigger": l.trigger, "tests": list(l.tests)[:10], "status": l.status,
                     "results": [{"good": r.get("good", {}).get("id"), "bad": r.get("bad", {}).get("id"),
                                  "exact": r.get("exact")} for r in l.results]} for l in g.locates.values()],
        "persistent": sorted(g.persistent),
        "diagnoses": [{"id": d.id, "trigger": d.trigger, "status": d.status} for d in g.diagnoses.values()],
        "relations": [list(r) for r in g.relations],
        "sessions": [{"id": s.id, "reason": s.reason, "end": s.end_reason, "turns": s.turns, "progress": s.progress,
                      "compactions": len(s.compactions), "resumes": list(s.resumes)}
                     for s in sorted(g.sessions.values(), key=lambda s: num(s.id))],
        "stalls": [{"kind": s.kind, "action": s.action, "task": s.task} for s in g.stalls],
        "recoveries": g.run.recoveries if g.run else 0,
        "rebuilds": g.run.rebuilds if g.run else 0,
    }


def ledger_markdown(g: Graph) -> str:
    L = ledger(g)
    c = L["categories"]
    out = ["# Belay ledger", "",
           f"- status: **{L['status']}**, delivered checkpoint {L['delivered_checkpoint']} ({L['delivered_level']})",
           *[f"  - not DONE because {r}" for r in L["status_reasons"]],
           f"- deliver_unconfirmed={L['deliver_unconfirmed']}; chain head {L['head']}, latest confirmed "
           f"{L['confirmed']}"
           + (" — delivery falls behind the head" if L["delivered_checkpoint"] not in (None, L["head"]) else ""),
           f"- regression gate: {L['guard_checks']} checks"
           + (f", {len(L['waived'])} waived (see below)" if L["waived"] else "")
           + (" (degraded mode: verification switched the working tree; no background verification)"
              if L["degraded"] else ""),
           "- tasks: " + ", ".join(f"{k} {v}" for k, v in c.items()), "", "## Requirements", ""]
    for r in L["requirements"]:
        out.append(f"- {r['id']} [{r['status']}] {r['summary'] or r['quote'][:120]} (tasks: {', '.join(r['tasks'])})")
    out += ["", "## Tasks", ""]
    for t in L["tasks"]:
        line = f"- {t['id']} [{t['category']}] {t['title']}"
        if t["blocked"]:
            line += f" — blocked ({t['blocked']['kind']}): {t['blocked']['reason']}"
        out.append(line)
    if L["not_delivered"]:
        out += ["", f"Finished but not in the deliverable (finished after the delivered checkpoint): "
                    f"{', '.join(L['not_delivered'])}"]
    if L["unfinished"]:
        out += ["", f"Unfinished: {', '.join(L['unfinished'])}"]
    if L["waived"]:
        out += ["", "## Waived regression checks", "",
                "Existing tests taken out of the regression gate because the worker quoted task text asking for "
                "behaviour they contradict (the tests had failed on its changes):", ""]
        for w in L["waived"]:
            out.append(f"- {w['test']} ({w['task']}): \"{w['quote'][:200]}\" — {w['reason'][:300]}")
    return "\n".join(out) + "\n"

"""给 worker 和人看的文字（纯函数）：board、需求详情、提交结果、复核结论、定位与诊断、需求账本。"""
from __future__ import annotations

from typing import Optional

from belay.core.config import BelayConfig
from belay.core.model import (ACTIONABLE, ATT_REJECTED, ATT_SUPERSEDED, BY_CHECKS, BY_SELF, E0, IMP_DONE, IMP_DROPPED,
                              IMP_OPEN, JOB_FINISHED,
                              REQ_BLOCKED, REQ_DONE, REQ_OPEN, REV_DECIDED, REV_FAILED, SUB_ACCEPTED, SUB_REJECTED,
                              SUB_RETURNED, TODO_ANCHORED, TODO_COMPLETED, Graph, Requirement)
from belay.core.queries import (accepted_blocked, actionable, chain, counted_done, delivery_checkpoint,
                                evidence_checks, id_ranges, improvements_in_order, improving, latest_submit, num,
                                open_requirements, status_reasons, todos_in_order)
from belay.core.verify import (B_FAIL, B_FLAKY, B_PASS, PASSED, active_guard, checkpoint_full_ok, full_verified,
                               reasons_for_tree, regression_ids, results_for_tree, tree_regressions)

PAGE = 50
STATUS_FILTERS = ("open", "done", "blocked", "unfinished")
REASON_TEXT = {"reassessed": "the reviewer found it no longer works", "rolled_back": "its merge point was rolled back",
               "blocked_not_accepted": "the reviewer did not accept that it is blocked",
               "checks fail": "its checks fail"}


def requirement_state(r: Requirement) -> str:
    """一个词的状态（给 worker 看）：done (E2) / blocked / open（partial）。"""
    if r.status == REQ_DONE:
        return f"done {r.level}" + (", self-reported" if r.level == E0 else "")
    if r.status == REQ_BLOCKED:
        return "blocked" + ("" if accepted_blocked(r) else ", self-reported")
    if r.judgement in ("partial", "not_done"):
        return f"open, judged {r.judgement.replace('_', ' ')}"
    return "open"


def requirement_line(g: Graph, rid: str, width: int = 120) -> str:
    r = g.requirements[rid]
    line = f"{r.id} [{requirement_state(r)}] {(r.summary or r.quote)[:width]}"
    extra = []
    if r.status in (REQ_DONE, REQ_BLOCKED) and r.checkpoint is not None:
        extra.append(f"merge point {r.checkpoint}")
    if r.status == REQ_BLOCKED:
        extra.append(f"{r.blocked_kind}: {(r.blocked_reason or '')[:80]}")
    if r.status == REQ_OPEN and r.missing:
        why = REASON_TEXT.get(r.reason or "", "")
        extra.append((why + "; " if why else "") + "missing: " + "; ".join(r.missing[:3])[:300])
    elif r.status == REQ_OPEN and g.head_cp is not None:                 # 规划器关联的检查在最新合并点上没过
        ev = evidence_checks(g, r)
        res = results_for_tree(g, g.head_cp.tree) if ev else {}
        failing = [f"{c} ({res[c]})" for c in ev if c in res and res[c] != PASSED]
        if failing:
            extra.append("its checks fail: " + "; ".join(failing[:3])[:300])
    return line + (f" ({'; '.join(extra)})" if extra else "")


def improvement_line(g: Graph, iid: str, width: int = 160) -> str:
    """I2 [open, judged partial] 标题 — 为什么（task: "引文" / objective: the measured score）。"""
    i = g.improvements[iid]
    if i.status == IMP_DONE:
        state = f"done {i.level}"
    elif i.status == IMP_DROPPED:
        state = "dropped"
    else:
        state = "open" + (f", judged {i.judgement.replace('_', ' ')}" if i.judgement in ("partial", "not_done") else "")
    line = f"{i.id} [{state}] {i.title[:width]}"
    tie = f"task: \"{i.quote[:120]}\"" if i.quote else "objective: the measured score" if i.objective else ""
    extra = [x for x in (i.why[:200] if i.why else "", tie) if x]
    if i.status == IMP_OPEN and i.missing:
        extra.append("missing: " + "; ".join(i.missing[:3])[:300])
    if i.status == IMP_DROPPED and i.reason:
        extra.append(f"dropped: {i.reason[:200]}")
    if i.status == IMP_DONE and i.checkpoint is not None:
        extra.append(f"merge point {i.checkpoint}")
    return line + (f" — {' | '.join(extra)}" if extra else "")


def improvement_lines(g: Graph, include_closed: bool = True, limit: int = 30) -> list[str]:
    items = [i for i in improvements_in_order(g) if include_closed or i.status == IMP_OPEN]
    items = sorted(items, key=lambda i: (i.status != IMP_OPEN, i.n))[:limit]
    return ["  - " + improvement_line(g, i.id) for i in items]


def checkpoint_line(g: Graph, cid: int) -> str:
    cp = g.checkpoints[cid]
    if cid == 0:
        return "0 original code"
    tags = [cp.trigger, "reviewed" if cp.review else "gate only"]
    if cp.score is not None:
        tags.append(f"score {cp.score:g}")
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
    if requirement:
        return render_requirement(g, requirement)
    if view == "failures":
        return _render_failures(g, page)
    if view in ("checkpoints", "merges"):
        return render_history(g)
    if status or view == "requirements":
        return _render_requirements(g, status, page)
    out = []
    cp = g.head_cp
    if cp is not None:
        out.append(f"Latest merge point (what would be delivered now): {checkpoint_line(g, cp.id)}")
    w = g.wips.get(worker)
    if w and w.base == g.head and (w.files or w.dropped):
        out.append(f"Changes not merged yet: {len(w.files)} file(s) (the harness reviews them in the background)")
    rej = w.last_rejection if w else None
    if rej and rej.get("reason") == "review":
        out.append(f"Last merge request {rej.get('attempt')} was not approved: {str(rej.get('detail'))[:300]}")
    sub = latest_submit(g, worker)
    if sub is not None:
        out.append(f"Last submit {sub.id}: {sub.status}" + (f" ({sub.reason})" if sub.reason else "")
                   + (f"; still open: {id_ranges(sub.open)}" if sub.open else ""))
    reqs = actionable(g)
    counts: dict[str, int] = {}
    for r in reqs:
        counts[r.status] = counts.get(r.status, 0) + 1
    out.append("\nRequirements: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    unfinished = [r for r in reqs if r.status != REQ_DONE]
    if unfinished:
        out.append("Open and blocked:")
        out.extend(_paginate(["  " + requirement_line(g, r.id) for r in unfinished], 1,
                             'board(status="unfinished", page={page})'))
    done = [r.id for r in reqs if r.status == REQ_DONE]
    if done:
        out.append(f"Done: {id_ranges(done)} (board(status=\"done\") for evidence)")
    if g.improvements or (g.run is not None and g.run.improving):
        state = "in progress" if improving(g) else (f"over ({g.run.improve_closed[:200]})" if g.run.improve_closed
                                                    else "not started")
        out.append(f"\nImprovement phase: {state}")
        out.extend(improvement_lines(g))
    out.append("\nMore: board(requirement=\"R3\"), board(status=...), board(view=\"merges\"), "
               "board(view=\"failures\").")
    return "\n".join(out)


def _render_requirements(g: Graph, status: Optional[str], page: int) -> str:
    rs = actionable(g)
    if status == "unfinished":
        rs = [r for r in rs if r.status != REQ_DONE]
    elif status:
        rs = [r for r in rs if r.status == status]
    if not rs:
        return f"No requirements with status {status}."
    hint = f'board(status="{status}", page={{page}})' if status else 'board(view="requirements", page={page})'
    return "\n".join([f"Requirements ({status or 'all'}, {len(rs)}):"] +
                     _paginate(["  " + requirement_line(g, r.id) for r in rs], page, hint))


def render_requirement(g: Graph, rid: str) -> str:
    r = g.requirements.get(rid)
    if r is None:
        return f"Unknown requirement {rid}."
    if r.kind != ACTIONABLE:
        return f"{rid} (context, not on the checklist): \"{r.quote}\""
    out = [requirement_line(g, rid), f"Task text: \"{r.quote}\""]
    if r.acceptance:
        out.append(f"How it will be checked: {r.acceptance}")
    ev = evidence_checks(g, r)
    if ev:
        res = results_for_tree(g, g.head_cp.tree) if g.head_cp is not None else {}
        out.append("Linked checks (fail on the original code): "
                   + ", ".join(f"{c} ({res.get(c, 'not run on the latest merge point')})" for c in ev[:20]))
    if r.evidence:
        out.append("Evidence: " + "; ".join(r.evidence[:8]))
    if r.missing:
        out.append("Missing: " + "; ".join(r.missing[:10]))
    if r.history:
        out.append("History: " + "; ".join(f"#{seq} {st} ({why})" for seq, st, why in r.history[-12:]))
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
    out = ["Merge chain (newest first):"]
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
    if a.reason == "review":
        v = g.reviews.get(a.review) if a.review else None
        d = v.decision if v is not None else {}
        out.append("The reviewer did not merge it:")
        out.extend(f"  - {x}" for x in (d.get("reasons") or [a.detail])[:10])
        if d.get("feedback"):
            out.append(f"Reviewer's feedback: {d['feedback'][:2500]}")
    if a.regressions:
        out.append(f"{len(a.regressions)} check(s) that passed on the original code (or that showed a requirement "
                   "done) do not pass now (failure reason under each):")
        out.extend(_reason_lines(g, a.tree, a.regressions))
        if len(a.regressions) > 30:
            out.append(f"  ... and {len(a.regressions) - 30} more")
        out.append("failure_log(test=...) shows the full traceback.")
    if a.flaky:
        out.append(f"Flaky (not counted): {', '.join(a.flaky[:10])}")
    snap = g.snapshots.get(a.snapshot)
    if a.reason == "precheck":
        out.append("The changed files do not compile: " + ((snap.precheck if snap else "") or a.detail or "")[:600])
    elif a.reason not in ("review",) and a.detail:
        out.append(f"Runner notes: {a.detail[:600]}")
    if snap is not None and a.regressions:
        reg_files = {regression_ids([r])[0].split("::")[0] for r in a.regressions}
        overlap = sorted(reg_files & set(snap.dropped))
        if overlap:
            out.append("These tests ran in their original version: your changes to "
                       + ", ".join(overlap[:10]) + " were not used.")
    return out


def _review_lines(g: Graph, vid: Optional[str], focus_only: bool = False) -> list[str]:
    """一次复核的结论：每条需求的判定与证据等级、被忽略的判断、反馈。"""
    v = g.reviews.get(vid) if vid else None
    if v is None:
        return []
    if v.status == REV_FAILED:
        return ["The reviewer gave no verdict" + (f" ({v.error[:200]})" if v.error else "") + "."]
    if v.status != REV_DECIDED:
        return []
    d = v.decision
    out = []
    for j in d.get("judgements") or []:
        rid = j["requirement"]
        if j["status"] == REQ_DONE:
            out.append(f"  - {rid}: done ({j.get('level')})" + (f" — {'; '.join(j.get('evidence')[:2])[:200]}"
                                                                  if j.get("evidence") else ""))
        elif j["status"] == REQ_BLOCKED:
            out.append(f"  - {rid}: blocked, accepted by the reviewer")
        else:
            why = REASON_TEXT.get(j.get("reason") or "", "")
            out.append(f"  - {rid}: {(j.get('judgement') or 'open').replace('_', ' ')}"
                       + (f" ({why})" if why else "")
                       + (f" — missing: {'; '.join(j.get('missing')[:4])[:400]}" if j.get("missing") else ""))
    for j in (d.get("improvements") or {}).get("judged") or []:
        iid = j["improvement"]
        if j["status"] == IMP_DONE:
            out.append(f"  - {iid}: done ({j.get('level')})" + (f" — {'; '.join(j.get('evidence')[:2])[:200]}"
                                                                  if j.get("evidence") else ""))
        elif j["status"] == IMP_DROPPED:
            out.append(f"  - {iid}: dropped — {str(j.get('reason') or '')[:300]}")
        else:
            out.append(f"  - {iid}: {(j.get('judgement') or 'open').replace('_', ' ')}"
                       + (f" — missing: {'; '.join(j.get('missing')[:4])[:400]}" if j.get("missing") else ""))
    if out:
        out.insert(0, "Reviewer's judgements:")
    for n in (d.get("notes") or [])[:5]:
        out.append(f"Note: {n[:300]}")
    if d.get("feedback"):
        out.append(f"Reviewer's feedback: {d['feedback'][:2500]}")
    return out


IMPROVE_PHASE_TEXT = (
    "The run does not stop here: the remaining time goes to making the delivered version better. Work on the open "
    "improvement items below (keeping a todo item per improvement helps); ticking a todo item or calling submit gets "
    "your work reviewed, and only merged work is delivered, so nothing that already works may break. The reviewer "
    "judges each item and may add new ones; if you believe an item is not worth doing or cannot be done here, say why "
    "in your submit summary and the reviewer decides.")


def render_submit(g: Graph, sid: str) -> str:
    """submit 的回复：合并被拒 / 交还清单（还有没完成的需求）/ 接受。"""
    s = g.submits[sid]
    out = []
    if s.status == SUB_REJECTED:
        a = g.attempts.get(s.attempt) if s.attempt else None
        why = {"regression": "your working tree did not pass the regression gate",
               "requirement_regression": "your change breaks a requirement that was already done",
               "review": "the reviewer did not approve the merge", "precheck": "the changed files do not compile"
               }.get(s.reason, s.reason or "rejected")
        out.append(f"Submit {sid} was not merged: {why}. Nothing was recorded; your working tree is unchanged.")
        if a is not None:
            out.extend(_rejection_lines(g, a.id))
        out.append("Fix this and call submit again.")
        return "\n".join(out)
    cp = g.checkpoints.get(s.checkpoint) if s.checkpoint is not None else None
    a = g.attempts.get(s.attempt) if s.attempt else None
    if a is not None and a.checkpoint is not None and cp is not None and cp.id == a.checkpoint:
        out.append(f"Your work was merged as merge point {cp.id}" + (" (reviewed)." if cp.review else
                                                                    " (regression gate only; no reviewer)."))
        snap = g.snapshots.get(s.snapshot)
        if snap is not None and snap.dropped:
            out.append("Not included (test paths are restored to the original): " + ", ".join(snap.dropped[:10]))
        out.extend(_review_lines(g, cp.review))
        if s.review is not None and s.review != cp.review:          # 合并之后请复核者提改进方向的那次复核
            out.extend(_review_lines(g, s.review))
    elif cp is not None:
        out.append(f"No new changes since merge point {cp.id}.")
        out.extend(_review_lines(g, s.review))
    if s.status == SUB_ACCEPTED:
        reqs = actionable(g)
        by_level: dict[str, list[str]] = {}
        for r in reqs:
            if r.status == REQ_DONE:
                by_level.setdefault(r.level or "?", []).append(r.id)
        blk = [r.id for r in reqs if r.status == REQ_BLOCKED]
        out.append(f"Submit {sid} accepted: no requirement on the checklist is left open.")
        for lv in sorted(by_level, reverse=True):
            label = {"E3": "tests", "E2": "the reviewer ran it", "E1": "the reviewer read the code",
                     "E0": "self-reported, not verified"}.get(lv, lv)
            out.append(f"Done ({lv}, {label}): {id_ranges(by_level[lv])}")
        if blk:
            out.append(f"Blocked: {id_ranges(blk)}")
        if improving(g):
            out.append(IMPROVE_PHASE_TEXT)
            lines = improvement_lines(g)
            if any(i.status == IMP_OPEN for i in g.improvements.values()):
                out.append("The reviewer's improvement items:")
                out.extend(lines)
            else:
                out.append("No improvement item is open right now; the reviewer will propose more when it reviews "
                           "your next change." + ("\nEarlier items:\n" + "\n".join(lines) if lines else ""))
            return "\n".join(out)
        if g.run is not None and g.run.improving and g.run.improve_closed:
            out.append(f"The improvement phase is over: {g.run.improve_closed[:400]}")
        out.append("The harness now finalizes the run; you can stop.")
        return "\n".join(out)
    if s.status == SUB_RETURNED:
        out.append(f"Submit {sid}: {len(s.open)} requirement(s) are still open. Keep working on them and call "
                   "submit again:")
        for rid in s.open[:40]:
            out.append("  - " + requirement_line(g, rid, 100))
        if len(s.open) > 40:
            out.append(f"  ... {len(s.open) - 40} more: board(status=\"open\")")
        out.append("If one of them cannot be done here, say so in submit(blocked=[{requirement, kind, reason}]).")
        return "\n".join(out)
    out.append(f"Submit {sid} is still being reviewed; keep working, the result will be reported.")
    return "\n".join(out)


def render_review_notice(g: Graph, vid: str) -> str:
    """后台合并请求没被复核者批准时给 worker 的提醒。"""
    v = g.reviews.get(vid)
    if v is None or v.status != REV_DECIDED or v.decision.get("merge") is not False:
        return ""
    d = v.decision
    lines = [f"The reviewer did not merge your snapshot s{v.snapshot} (background check; you were not interrupted). "
             "Your working tree is unchanged and the last merge point stays what would be delivered:"]
    lines += [f"- {x[:400]}" for x in (d.get("reasons") or [])[:6]]
    if d.get("feedback"):
        lines.append(f"Reviewer's feedback: {d['feedback'][:2000]}")
    return "\n".join(lines)


def render_located(g: Graph, lid: str, group: Optional[int] = None) -> str:
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
        gl = f"s{good.get('id')}" if good.get("kind") == "snapshot" else f"merge point {good.get('id')}"
        bl = f"s{bad.get('id')}" if bad.get("kind") == "snapshot" else f"merge point {bad.get('id')}"
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
                   "asks for it, submit again with waivers=[{tests, quote, reason}] and the reviewer decides whether "
                   "these tests leave the gate (listed in the final report); otherwise undo the change.")
    if r.get("suggestion"):
        out.append(f"- suggestion: {r['suggestion']}")
    if r.get("flaky_suspect"):
        out.append("- the diagnoser suspects the test may be flaky (the confirmation rerun decides).")
    return "\n".join(out)


def render_job(g: Graph, jid: str, max_lines: int = 40, only: Optional[list[str]] = None) -> str:
    j = g.jobs[jid]
    if j.state != JOB_FINISHED:
        return f"Job {jid}: {j.state}" + (f" ({j.error[:300]})" if j.error else "")
    res = j.results if only is None else {t: s for t, s in j.results.items() if t in only or
                                          any(t.startswith(o.rstrip(":") + "::") for o in only)}
    base = g.baseline
    regressed = sorted(t for t, s in res.items() if base.get(t) == B_PASS and s != PASSED)
    pre = sorted(t for t, s in res.items() if base.get(t) in (B_FAIL, B_FLAKY) and s != PASSED)
    fixed = sorted(t for t, s in res.items() if base.get(t) in (B_FAIL, B_FLAKY) and s == PASSED)
    new = sorted(t for t in res if t not in base)
    new_fail = [t for t in new if res[t] != PASSED]
    n_pass = sum(1 for s in res.values() if s == PASSED)
    out = [f"Job {jid} finished in {j.sec:.0f}s: {len(res)} results, {n_pass} passed (original test files, "
           "verification directory)."]
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
        out.append(f"Still failing (also fail on the original code): {len(pre)}: " + ", ".join(pre[:20]))
    if new:
        out.append(f"Tests not in the baseline: {len(new)} ({len(new_fail)} not passing"
                   + (": " + ", ".join(f"{t} ({res[t]})" for t in new_fail[:15]) if new_fail else "") + ")")
    other = sorted(t for t, s in res.items() if t not in regressed and t not in pre and t not in fixed
                   and t not in new and s == PASSED)
    if only is not None and other:
        out.append(f"Passing (also passed on the original code): {len(other)}: " + ", ".join(other[:20]))
    if not res and not j.error:
        out.append("(no test results)")
    return "\n".join(out)


# ======================================================================== 账本

def requirement_category(g: Graph, r: Requirement, delivered: Optional[int] = None) -> str:
    """done-E3 / done-E2 / done-E1 / self-reported / blocked / blocked-self-reported / open。"""
    if r.status == REQ_DONE:
        return "self-reported" if r.level == E0 else f"done-{r.level}"
    if r.status == REQ_BLOCKED:
        return "blocked" if accepted_blocked(r) else "blocked-self-reported"
    return "open"


CATEGORIES = ("done-E3", "done-E2", "done-E1", "self-reported", "blocked", "blocked-self-reported", "open")


def ledger(g: Graph) -> dict:
    """结构化账本：运行结束时写入报告，也用于实验指标。"""
    delivered = g.run.delivered if g.run and g.run.delivered is not None else delivery_checkpoint(g)
    reqs = actionable(g)
    cats = {c: [] for c in CATEGORIES}
    for r in reqs:
        cats[requirement_category(g, r)].append(r.id)
    cp = g.head_cp
    dcp = g.checkpoints.get(delivered) if delivered is not None else None
    reviews = sorted(g.reviews.values(), key=lambda v: v.seq)
    return {
        "version": g.run.version if g.run else None,
        "status": g.run.status if g.run else None,
        "status_reasons": list(g.run.status_reasons) if g.run and g.run.delivered is not None
        else status_reasons(g, delivered),
        "delivered_checkpoint": g.run.delivered if g.run else None,
        "would_deliver": delivered if not (g.run and g.run.delivered is not None) else None,
        "delivered_score": dcp.score if dcp else None,
        "delivered_reviewed": bool(dcp and dcp.review) if dcp and dcp.id else None,
        "delivered_full_ok": checkpoint_full_ok(g, delivered),
        "head": g.head,
        "degraded": g.degraded,
        "isolation": dict(g.isolation),
        "gate_available": bool(g.baseline),
        "head_full_verified": bool(cp and full_verified(g, cp.tree)),
        "head_regressions": list(tree_regressions(g, cp.tree)) if cp and full_verified(g, cp.tree) else [],
        "guard_checks": len(active_guard(g)),
        "waived": [{"test": w.test, "requirement": w.requirement, "quote": w.quote, "reason": w.reason,
                    "review": w.review} for w in sorted(g.waived.values(), key=lambda w: (w.seq, w.test))],
        "categories": {c: len(v) for c, v in cats.items()},
        "category_requirements": cats,
        "done_counted": sum(1 for r in reqs if counted_done(r)),
        "requirements": [{"id": r.id, "kind": r.kind, "status": r.status, "level": r.level,
                          "category": requirement_category(g, r) if r.kind == ACTIONABLE else "context",
                          "summary": r.summary, "quote": r.quote, "acceptance": r.acceptance,
                          "checks": len(r.checks), "evidence_checks": len(evidence_checks(g, r)),
                          "checkpoint": r.checkpoint, "by": r.by, "review": r.review, "judgement": r.judgement,
                          "evidence": list(r.evidence), "tests": list(r.tests), "missing": list(r.missing),
                          "blocked": {"kind": r.blocked_kind, "reason": r.blocked_reason, "quote": r.blocked_quote}
                          if r.status == REQ_BLOCKED else None}
                         for r in sorted(g.requirements.values(), key=lambda r: num(r.id))],
        "unfinished": [r.id for r in open_requirements(g)],
        "submits": [{"id": s.id, "status": s.status, "implicit": s.implicit, "checkpoint": s.checkpoint,
                     "open": list(s.open), "reason": s.reason} for s in sorted(g.submits.values(), key=lambda s: s.seq)],
        "reviews": [{"id": v.id, "trigger": v.trigger, "attempt": v.attempt, "checkpoint": v.checkpoint,
                     "status": v.status, "merge": v.decision.get("merge"), "reasons": v.decision.get("reasons"),
                     "notes": v.decision.get("notes"), "score": v.decision.get("score"), "runs": len(v.runs),
                     "retry_of": v.retry_of, "error": v.error or None,
                     "judgements": {j["requirement"]: j["status"] + (f" {j.get('level')}" if j["status"] == REQ_DONE
                                                                      else "") for j in
                                    v.decision.get("judgements") or []}}
                    for v in reviews],
        "merges": [{"id": c.id, "parent": c.parent, "trigger": c.trigger, "review": c.review, "score": c.score,
                    "score_note": c.score_note, "files": len(c.files), "abandoned": c.abandoned,
                    "snapshot": c.snapshot, "label": c.label}
                   for c in sorted(g.checkpoints.values(), key=lambda c: c.id)],
        "merge_requests": {"total": len(g.attempts),
                           "rejected": sum(a.status == ATT_REJECTED for a in g.attempts.values()),
                           "rejected_by_review": sum(a.status == ATT_REJECTED and a.reason == "review"
                                                     for a in g.attempts.values()),
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
        "sessions": [{"id": s.id, "reason": s.reason, "end": s.end_reason, "turns": s.turns, "progress": s.progress,
                      "compactions": len(s.compactions), "resumes": list(s.resumes)}
                     for s in sorted(g.sessions.values(), key=lambda s: num(s.id))],
        "stalls": [{"kind": s.kind, "action": s.action} for s in g.stalls],
        "improve": {"started": bool(g.run and g.run.improving), "closed": g.run.improve_closed if g.run else "",
                    "items": [{"id": i.id, "title": i.title, "status": i.status, "level": i.level,
                               "quote": i.quote, "objective": i.objective, "review": i.review,
                               "judged_review": i.judged_review, "checkpoint": i.checkpoint, "reason": i.reason}
                              for i in improvements_in_order(g)],
                    "merges_after_start": sum(1 for c in g.checkpoints.values() if g.run and g.run.improve_seq
                                              and c.created_seq > g.run.improve_seq and not c.abandoned)},
        "recoveries": g.run.recoveries if g.run else 0,
        "rebuilds": g.run.rebuilds if g.run else 0,
    }


def ledger_markdown(g: Graph) -> str:
    L = ledger(g)
    c = L["categories"]
    if L["delivered_checkpoint"] is not None:
        dl = f"delivered merge point {L['delivered_checkpoint']}"
    else:
        dl = f"not delivered yet; delivering now would deliver merge point {L['would_deliver']}"
    if L["delivered_score"] is not None:
        dl += f" (score {L['delivered_score']:g})"
    out = ["# Belay ledger (v8)", "",
           f"- status: **{L['status']}**, {dl}",
           *[f"  - not DONE because {r}" for r in L["status_reasons"]],
           f"- merge chain: head {L['head']}, {len(L['merges']) - 1} merge point(s); "
           f"{L['merge_requests']['total']} merge request(s), {L['merge_requests']['rejected_by_review']} not approved "
           "by the reviewer",
           f"- regression gate: {L['guard_checks']} checks" if L["gate_available"] else
           "- regression gate: not available (no tests); the reviewer verified each merge by reading and running "
           "the code",
           *([f"  - {len(L['waived'])} waived by the reviewer (see below)"] if L["waived"] else []),
           *(["  - degraded mode: verification switched the working tree; background merges only at handoffs"]
             if L["degraded"] else []),
           "- requirements: " + ", ".join(f"{k} {v}" for k, v in c.items() if v)
           + f" (counted as done: {L['done_counted']})",
           f"- reviews: {len(L['reviews'])}"
           + (f", {sum(1 for v in L['reviews'] if v['status'] == 'failed')} without a verdict"
              if any(v['status'] == 'failed' for v in L['reviews']) else ""),
           "", "## Requirements", ""]
    for r in L["requirements"]:
        if r["kind"] != "actionable":
            continue
        line = f"- {r['id']} [{r['category']}] {r['summary'] or r['quote'][:120]}"
        if r["checkpoint"] is not None and r["status"] != "open":
            line += f" (merge point {r['checkpoint']})"
        if r["evidence"] and r["status"] == "done":
            line += f" — {'; '.join(r['evidence'][:2])[:200]}"
        if r["blocked"]:
            line += f" — blocked ({r['blocked']['kind']}): {r['blocked']['reason']}"
        if r["status"] == "open" and r["missing"]:
            line += f" — missing: {'; '.join(r['missing'][:3])[:200]}"
        out.append(line)
    ctx = [r["id"] for r in L["requirements"] if r["kind"] != "actionable"]
    if ctx:
        out += ["", f"Context (not on the checklist): {id_ranges(ctx)}"]
    out += ["", "## Merge chain", ""]
    for m in reversed(L["merges"]):
        if m["abandoned"]:
            continue
        tag = "original code" if m["id"] == 0 else (f"{m['trigger']}, " + ("reviewed" if m["review"] else
                                                                          "gate only"))
        out.append(f"- {m['id']} ({tag}" + (f", score {m['score']:g}" if m["score"] is not None else "") + ")"
                   + (f" — {m['label']}" if m["label"] else ""))
    imp = L["improve"]
    if imp["started"] or imp["items"]:
        out += ["", "## Improvement phase", "",
                f"- {'started' if imp['started'] else 'not started'}"
                + (f"; closed: {imp['closed']}" if imp["closed"] else "")
                + f"; {imp['merges_after_start']} merge point(s) after it started"]
        for i in imp["items"]:
            out.append(f"- {i['id']} [{i['status']}{' ' + i['level'] if i['level'] else ''}] {i['title'][:160]}"
                       + (f" (merge point {i['checkpoint']})" if i["status"] == "done" else "")
                       + (f" — dropped: {i['reason'][:200]}" if i["status"] == "dropped" else ""))
    if L["waived"]:
        out += ["", "## Waived regression checks", "",
                "Existing tests taken out of the regression gate because the reviewer found task text asking for "
                "behaviour they contradict:", ""]
        for w in L["waived"]:
            out.append(f"- {w['test']} ({w['requirement'] or '-'}, {w['review']}): \"{w['quote'][:200]}\" — "
                       f"{w['reason'][:300]}")
    return "\n".join(out) + "\n"

"""视图的推导函数：apply(graph, event) -> graph'，replay(events) -> graph。

纯函数：不修改输入的图（只复制被修改的表），不读时钟，不做 IO。
reduce 是机械的，但它同时是状态机的最后一道防线：不合法的转换（例如在不在链上的合并点上判定完成、父节点不对的
合并点）会抛 IllegalEvent。规则只产生合法的事件；一条被接受过的日志重放时永远不会抛出。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, Optional

from belay.core.events import Event, validate
from belay.core.model import (ACTIONABLE, ATT_ADVANCING, ATT_CREATED, ATT_PENDING, ATT_REJECTED, ATT_SUPERSEDED,
                              BY_CHECKS, BY_ROLLBACK, BY_SELF, J_NOT_DONE, J_PARTIAL, JOB_FINISHED, JUDGEMENTS,
                              LANE_BG, LANE_FG, LEVELS, REQ_BLOCKED, REQ_DONE, REQ_KINDS, REQ_OPEN, REQ_STATUSES,
                              REV_CANCELLED, REV_DECIDED, REV_FAILED, REV_RECORDED, REV_RUNNING, RUN_DONE,
                              RUN_INCOMPLETE, RUN_RUNNING, SUB_ACCEPTED, SUB_PENDING, SUB_REJECTED, SUB_RETURNED,
                              TODO_ACTIVE, TODO_ANCHORED, TODO_COMPLETED, TODO_PENDING, VERSION, WHERE_LIVE,
                              WHERE_SLOT, Attempt, Checkpoint, Compaction, Diagnosis, Graph, Job, Locate, Persistent,
                              Requirement, Review, Run, Session, Snapshot, Stall, Submit, Todo, Waiver, Wip,
                              WorkerState)
from belay.core.queries import evidence_checks, is_ancestor, last_session
from belay.core.verify import PASSED, results_for_tree


class IllegalEvent(ValueError):
    pass


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise IllegalEvent(msg)


def _put(d: dict, k, v) -> dict:
    out = dict(d)
    out[k] = v
    return out


def _drop(d: dict, k) -> dict:
    out = dict(d)
    out.pop(k, None)
    return out


def _req(g: Graph, rid: str) -> Requirement:
    _need(rid in g.requirements, f"unknown requirement {rid}")
    return g.requirements[rid]


def _set_req(g: Graph, r: Requirement, e: Optional[Event] = None, reason: str = "") -> Graph:
    old = g.requirements.get(r.id)
    if e is not None and (old is None or old.status != r.status or old.level != r.level):
        tag = r.status + (f" {r.level}" if r.status == REQ_DONE and r.level else "")
        r = replace(r, history=(r.history + ((e.seq, tag, reason or e.type),))[-30:])
    return replace(g, requirements=_put(g.requirements, r.id, r))


def _set_sub(g: Graph, s: Submit) -> Graph:
    return replace(g, submits=_put(g.submits, s.id, s))


def _set_review(g: Graph, v: Review) -> Graph:
    return replace(g, reviews=_put(g.reviews, v.id, v))


def _on_chain(g: Graph, cid) -> bool:
    return cid is not None and cid in g.checkpoints and not g.checkpoints[cid].abandoned and \
        is_ancestor(g, cid, g.head)


def _progress(g: Graph, e: Event, worker: Optional[str]) -> Graph:
    """进展：记到这个 worker 最近的会话上（包括刚结束的：会话结束后 runtime 替它做的合并也算）。"""
    g = replace(g, last_progress_t=e.t, last_progress_seq=e.seq)
    if worker is None and len(g.workers) == 1:
        worker = next(iter(g.workers))
    if worker:
        s = last_session(g, worker)
        if s is not None and not s.progress:
            g = replace(g, sessions=_put(g.sessions, s.id, replace(s, progress=True)))
    return g


def _check_passes(g: Graph, e: Event, tree: str) -> Graph:
    """某条需求的证据检查第一次在合并点上通过：算进展。"""
    if not any(cp.tree == tree and not cp.abandoned for cp in g.checkpoints.values()):
        return g
    res = results_for_tree(g, tree)
    progressed = False
    for r in list(g.requirements.values()):
        ev = evidence_checks(g, r)
        if not ev:
            continue
        new = [c for c in ev if res.get(c) == PASSED and c not in r.passed_checks]
        if new:
            g = _set_req(g, replace(r, passed_checks=r.passed_checks + tuple(new)))
            progressed = True
    return _progress(g, e, None) if progressed else g


def _running(g: Graph) -> None:
    _need(g.run is not None, "run has not started")
    _need(g.run.status == RUN_RUNNING, "run already delivered")


# ======================================================================== 运行

def _run_started(g: Graph, e: Event) -> Graph:
    _need(g.run is None, "run_started twice")
    _need(int(e.get("version")) == VERSION, f"this log was written by Belay v{e.get('version')}; this code replays "
                                            f"v{VERSION} logs only")
    workers = tuple(e.get("workers"))
    run = Run(id=e.get("run_id"), task=e.get("task"), budget_sec=float(e.get("budget_sec")), started_t=e.t,
              deadline_t=float(e.get("deadline_t")), workers=workers,
              public_checks=tuple(e.get("public_checks") or ()), verifier=bool(e.get("verifier", True)),
              version=int(e.get("version")))
    return replace(g, run=run, workers={w: WorkerState(w) for w in workers}, last_progress_t=e.t)


def _runtime_recovered(g: Graph, e: Event) -> Graph:
    _need(g.run is not None, "recovered before start")
    rebuilt = bool(e.get("rebuilt"))
    run = replace(g.run, recoveries=g.run.recoveries + 1, rebuilds=g.run.rebuilds + (1 if rebuilt else 0),
                  downtime_sec=g.run.downtime_sec + float(e.get("downtime_sec")))
    g = replace(g, run=run)
    lost = [int(n) for n in (e.get("lost_snapshots") or ())]
    if lost:
        snaps = dict(g.snapshots)
        for n in lost:
            if n in snaps:
                snaps[n] = replace(snaps[n], lost=True)
        g = replace(g, snapshots=snaps)
    iso = e.get("isolation")
    if iso:
        g = replace(g, isolation={**g.isolation, **dict(iso)})
    return g


def _clock_started(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(not g.sessions, "the clock starts before the first session")
    return replace(g, run=replace(g.run, deadline_t=float(e.get("deadline_t"))), last_progress_t=e.t)


def _run_suspended(g: Graph, e: Event) -> Graph:
    _running(g)
    return replace(g, run=replace(g.run, suspended=g.run.suspended + 1))


def _deadline_reserve(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(not g.run.reserve, "deadline_reserve twice")
    return replace(g, run=replace(g.run, reserve=True, reserve_sec=float(e.get("reserve_sec"))))


def _finalize_started(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(not g.run.finalizing, "finalize_started twice")
    return replace(g, run=replace(g.run, finalizing=True, finalize_reason=e.get("reason")))


def _delivered(g: Graph, e: Event) -> Graph:
    _running(g)
    cid = e.get("checkpoint")
    _need(cid in g.checkpoints and is_ancestor(g, cid, g.head), f"delivered merge point {cid} is not on the chain")
    status = e.get("status")
    _need(status in ("DONE", "INCOMPLETE"), f"bad delivered status {status}")
    run = replace(g.run, status=RUN_DONE if status == "DONE" else RUN_INCOMPLETE, delivered=cid,
                  status_reasons=tuple(e.get("status_reasons") or ()))
    return replace(g, run=run)


# ======================================================================== 需求

def _plan_proposed(g: Graph, e: Event) -> Graph:
    summary = {"seq": e.seq, "round": e.get("round"), "valid": bool(e.get("valid")),
               "problems": list(e.get("problems") or [])[:20], "purpose": e.get("purpose", "initial")}
    return replace(g, plans=g.plans + (summary,))


def _requirement_frozen(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(not g.frozen, "requirements are frozen once")
    reqs = {}
    for r in e.get("requirements"):
        _need(r["id"] not in reqs, f"duplicate requirement {r['id']}")
        _need(bool(r.get("quote")), f"requirement {r['id']} has no quote")
        kind = r.get("kind") or ACTIONABLE
        _need(kind in REQ_KINDS, f"requirement {r['id']} has a bad kind {kind}")
        reqs[r["id"]] = Requirement(r["id"], r["quote"], r.get("summary", ""), r.get("origin", "llm"), kind,
                                    tuple(r.get("checks") or ()), str(r.get("acceptance") or "")[:500],
                                    history=((e.seq, REQ_OPEN, "frozen"),))
    _need(bool(reqs), "no requirements")
    _need(any(r.kind == ACTIONABLE for r in reqs.values()), "no actionable requirement")
    return replace(g, requirements=reqs, frozen=True)


def _requirement_judged(g: Graph, e: Event) -> Graph:
    """一条需求在某个合并点上的判定（规则校验过的复核结论、测试、自述，或回退）。"""
    _running(g)
    r = _req(g, e.get("requirement"))
    _need(r.kind == ACTIONABLE, f"{r.id} is not actionable")
    status = e.get("status")
    _need(status in REQ_STATUSES, f"bad requirement status {status}")
    by = e.get("by")
    judgement = e.get("judgement")
    _need(judgement is None or judgement in JUDGEMENTS, f"bad judgement {judgement}")
    level = e.get("level")
    cp = e.get("checkpoint")
    if status in (REQ_DONE, REQ_BLOCKED):
        _need(_on_chain(g, cp), f"{r.id}: merge point {cp} is not on the chain")
    if status == REQ_DONE:
        _need(level in LEVELS, f"{r.id}: done needs an evidence level (got {level})")
        tests = tuple(e.get("tests") or ())
        if level == "E3":
            _need(bool(tests), f"{r.id}: E3 needs tests")
            res = results_for_tree(g, g.checkpoints[cp].tree)
            _need(all(res.get(t) == PASSED for t in tests), f"{r.id}: E3 tests do not pass on merge point {cp}")
    else:
        level = None
    if by == BY_SELF:
        _need(e.source == "self_report", "self-reported judgements come from the worker")
    misses = r.misses + 1 if judgement in (J_PARTIAL, J_NOT_DONE) else 0
    blocked = status == REQ_BLOCKED
    r2 = replace(r, status=status, level=level, judgement=judgement, by=by,
                 evidence=tuple(str(x) for x in (e.get("evidence") or ()))[:20],
                 tests=tuple(e.get("tests") or ()) if status == REQ_DONE else (),
                 runs=tuple(e.get("runs") or ()) if status == REQ_DONE else (),
                 missing=tuple(str(x) for x in (e.get("missing") or ()))[:20],
                 checkpoint=cp, review=e.get("review"), judged_seq=e.seq, misses=misses,
                 blocked_kind=e.get("blocked_kind") if blocked else None,
                 blocked_reason=e.get("blocked_reason") if blocked else None,
                 blocked_quote=e.get("blocked_quote") if blocked else None,
                 reason=e.get("reason") or r.reason)
    g = _set_req(g, r2, e, e.get("reason") or by)
    if r.status == REQ_OPEN and status in (REQ_DONE, REQ_BLOCKED):
        g = _progress(g, e, None)
    return g


# ======================================================================== todo

def _todos_updated(g: Graph, e: Event) -> Graph:
    _running(g)
    keep = {t.id for t in g.todos.values() if t.status in (TODO_COMPLETED, TODO_ANCHORED)}
    listed = set()
    todos = dict(g.todos)
    for spec in e.get("todos"):
        tid = spec["id"]
        listed.add(tid)
        cur = todos.get(tid)
        status = spec.get("status", TODO_PENDING)
        reqs = tuple(spec.get("requirements") or ())
        for rid in reqs:
            _need(rid in g.requirements, f"todo {tid} mentions unknown requirement {rid}")
        if cur is None:
            _need(status in (TODO_PENDING, TODO_ACTIVE), f"new todo {tid} must be pending or in progress")
            todos[tid] = Todo(tid, int(spec["n"]), spec["title"], status, int(spec.get("order", 0)), reqs)
        else:
            if cur.status in (TODO_COMPLETED, TODO_ANCHORED):
                status = cur.status
            else:
                _need(status in (TODO_PENDING, TODO_ACTIVE), f"todo {tid} is completed only by todo_completed")
            todos[tid] = replace(cur, title=spec["title"], status=status, order=int(spec.get("order", 0)),
                                 requirements=reqs)
    for tid in list(todos):
        if tid not in listed and tid not in keep:
            del todos[tid]
    return replace(g, todos=todos)


def _todo(g: Graph, tid: str) -> Todo:
    _need(tid in g.todos, f"unknown todo {tid}")
    return g.todos[tid]


def _todo_completed(g: Graph, e: Event) -> Graph:
    t = _todo(g, e.get("todo"))
    _need(t.status in (TODO_PENDING, TODO_ACTIVE), f"cannot complete todo {t.id} in status {t.status}")
    n = int(e.get("snapshot"))
    _need(n == 0 or n in g.snapshots, f"unknown anchor snapshot {n}")
    epoch = g.snapshots[n].epoch if n in g.snapshots else 0
    if e.get("anchor_epoch") is not None:
        epoch = int(e.get("anchor_epoch"))
    t2 = replace(t, status=TODO_COMPLETED, anchor_snapshot=n, anchor_epoch=epoch, completed_seq=e.seq)
    return replace(g, todos=_put(g.todos, t.id, t2))


def _todo_anchored(g: Graph, e: Event) -> Graph:
    t = _todo(g, e.get("todo"))
    _need(t.status == TODO_COMPLETED, f"todo {t.id} is {t.status}, not completed")
    cid = int(e.get("checkpoint"))
    cp = g.checkpoints.get(cid)
    _need(cp is not None and not cp.abandoned, f"anchor merge point {cid} is not on the chain")
    _need(cp.epoch == t.anchor_epoch and cp.snapshot >= (t.anchor_snapshot or 0),
          f"merge point {cid} does not contain the anchor of {t.id}")
    return replace(g, todos=_put(g.todos, t.id, replace(t, status=TODO_ANCHORED, checkpoint=cid)))


def _todo_invalidated(g: Graph, e: Event) -> Graph:
    t = _todo(g, e.get("todo"))
    _need(t.status in (TODO_COMPLETED, TODO_ANCHORED), f"todo {t.id} is {t.status}")
    t2 = replace(t, status=TODO_ACTIVE, anchor_snapshot=None, anchor_epoch=None, checkpoint=None)
    return replace(g, todos=_put(g.todos, t.id, t2))


# ======================================================================== 提交

def _submit_requested(g: Graph, e: Event) -> Graph:
    _running(g)
    sid = e.get("submit")
    _need(sid not in g.submits, f"submit {sid} exists")
    w = e.get("worker")
    _need(w in g.workers, f"unknown worker {w}")
    _need(not any(x.worker == w and x.status == SUB_PENDING for x in g.submits.values()),
          f"{w} already has a submit in progress")
    cp, aid = e.get("checkpoint"), e.get("attempt")
    _need((cp is None) != (aid is None), "a submit has either a merge request or a merge point")
    if cp is not None:
        _need(cp == g.head, "a submit without a merge request is on the head")
    for b in e.get("blocked") or ():
        _need(b.get("requirement") in g.requirements, f"submit blocks unknown requirement {b.get('requirement')}")
    s = Submit(id=sid, worker=w, seq=e.seq, t=e.t, summary=str(e.get("summary") or ""),
               blocked=tuple(dict(b) for b in (e.get("blocked") or ())),
               waivers=tuple(dict(x) for x in (e.get("waivers") or ())), implicit=bool(e.get("implicit")),
               snapshot=int(e.get("snapshot") or 0), attempt=aid, checkpoint=cp)
    if aid is not None:
        _need(aid in g.attempts and g.attempts[aid].status == ATT_PENDING and g.attempts[aid].submit is None,
              f"merge request {aid} is not a pending request")
        g = replace(g, attempts=_put(g.attempts, aid, replace(g.attempts[aid], submit=sid)))
    return _set_sub(g, s)


def _submit_updated(g: Graph, e: Event) -> Graph:
    sid = e.get("submit")
    _need(sid in g.submits, f"unknown submit {sid}")
    s = g.submits[sid]
    st = e.get("status")
    _need(s.status == SUB_PENDING and st in (SUB_ACCEPTED, SUB_RETURNED, SUB_REJECTED),
          f"submit {sid}: {s.status} -> {st} is not allowed")
    cp = e.get("checkpoint", s.checkpoint)
    s2 = replace(s, status=st, reason=str(e.get("reason") or s.reason), open=tuple(e.get("open") or ()),
                 checkpoint=cp, accepted_seq=e.seq if st == SUB_ACCEPTED else s.accepted_seq)
    return _set_sub(g, s2)


# ======================================================================== 执行状态

def _session_started(g: Graph, e: Event) -> Graph:
    _running(g)
    w = e.get("worker")
    _need(w in g.workers, f"unknown worker {w}")
    ws = g.workers[w]
    _need(ws.session is None, f"{w} already has an open session {ws.session}")
    sid = e.get("session")
    _need(sid not in g.sessions, f"session {sid} exists")
    n = sum(1 for s in g.sessions.values() if s.worker == w) + 1
    s = Session(id=sid, worker=w, n=n, reason=e.get("reason"), started_seq=e.seq, started_t=e.t,
                opening=dict(e.get("opening") or {}), transcript=e.get("transcript"))
    g = replace(g, sessions=_put(g.sessions, sid, s))
    return replace(g, workers=_put(g.workers, w, replace(ws, status="running", session=sid, last_heartbeat=e.t)))


def _session_resumed(g: Graph, e: Event) -> Graph:
    sid = e.get("session")
    _need(sid in g.sessions and g.sessions[sid].ended_t is None, f"cannot resume session {sid}")
    s = g.sessions[sid]
    return replace(g, sessions=_put(g.sessions, sid, replace(s, resumes=s.resumes + (e.get("mode"),))))


def _session_ended(g: Graph, e: Event) -> Graph:
    sid = e.get("session")
    _need(sid in g.sessions, f"unknown session {sid}")
    s = g.sessions[sid]
    _need(s.ended_t is None, f"session {sid} already ended")
    s2 = replace(s, ended_t=e.t, ended_seq=e.seq, end_reason=e.get("reason"),
                 peak_context=int(e.get("peak_context") or 0), turns=int(e.get("turns") or 0), error=e.get("error"))
    g = replace(g, sessions=_put(g.sessions, sid, s2))
    ws = g.workers[s.worker]
    if ws.session == sid:
        g = replace(g, workers=_put(g.workers, s.worker, replace(ws, status="idle", session=None)))
    return g


def _compacted(g: Graph, e: Event) -> Graph:
    sid = e.get("session")
    _need(sid in g.sessions and g.sessions[sid].ended_t is None, f"compaction outside an open session {sid}")
    s = g.sessions[sid]
    lvl, before, after = int(e.get("level")), int(e.get("before")), int(e.get("after"))
    _need(0 <= lvl <= 4, f"bad compaction level {lvl}")
    g = replace(g, sessions=_put(g.sessions, sid, replace(s, compactions=s.compactions + ((lvl, before, after),))))
    c = Compaction(e.seq, e.t, sid, s.worker, lvl, before, after, e.get("summary"))
    return replace(g, compactions=g.compactions + (c,))


def _snapshot_taken(g: Graph, e: Event) -> Graph:
    _running(g)
    n = int(e.get("snapshot"))
    _need(n == g.last_snapshot + 1, f"snapshot numbers are sequential (got {n})")
    w = e.get("worker")
    base = int(e.get("base", g.head if g.head is not None else 0))
    snap = Snapshot(n=n, seq=e.seq, t=e.t, worker=w, tree=e.get("tree"), raw_tree=e.get("raw_tree"), epoch=g.epoch,
                    reason=e.get("reason"), testable=bool(e.get("testable")), commit=e.get("commit", ""), base=base,
                    files=tuple(tuple(f) for f in (e.get("files") or ())), dropped=tuple(e.get("dropped") or ()),
                    todo=e.get("todo"), session=e.get("session"),
                    tool_seq=int(e.get("tool_seq") or 0), precheck=e.get("precheck", ""))
    prev = g.wips.get(w)
    wip = Wip(worker=w, base=base, tree=snap.tree, raw_tree=snap.raw_tree, files=snap.files, dropped=snap.dropped,
              snapshot=n, seq=e.seq, last_rejection=prev.last_rejection if prev else None)
    return replace(g, snapshots=_put(g.snapshots, n, snap), wips=_put(g.wips, w, wip))


def _stall_detected(g: Graph, e: Event) -> Graph:
    s = Stall(e.seq, e.t, e.get("kind"), e.get("action"), e.get("worker"), e.get("detail", ""))
    return replace(g, stalls=g.stalls + (s,))


# ======================================================================== 验证与合并链

def _job_started(g: Graph, e: Event) -> Graph:
    jid, key = e.get("job"), e.get("key")
    _need(jid not in g.jobs, f"job {jid} exists")
    _need(key not in g.job_keys, f"job key {key} already has job {g.job_keys.get(key)}")
    sel = e.get("selection")
    live = bool(e.get("live"))
    job = Job(id=jid, key=key, tree=e.get("tree"), selection=None if sel is None else tuple(sel),
              purpose=e.get("purpose"), requested_by=e.actor, attempt=e.get("attempt"), live=live,
              where=e.get("where") or (WHERE_LIVE if live else WHERE_SLOT), tag=e.get("tag", ""),
              locate=e.get("locate"), checkpoint=e.get("checkpoint"), started_t=e.t, started_seq=e.seq,
              replaces=e.get("replaces"))
    g = replace(g, jobs=_put(g.jobs, jid, job), job_keys=_put(g.job_keys, key, jid))
    aid = e.get("attempt")
    if aid is not None:
        _need(aid in g.attempts, f"job for unknown merge request {aid}")
        a = g.attempts[aid]
        _need(a.status == ATT_PENDING, f"job for merge request {aid} in status {a.status}")
        jobs = tuple(j for j in a.jobs if j != e.get("replaces")) + (jid,)
        g = replace(g, attempts=_put(g.attempts, aid, replace(a, jobs=jobs)))
    return g


def _job_preempted(g: Graph, e: Event) -> Graph:
    jid = e.get("job")
    _need(jid in g.jobs and g.jobs[jid].state == "running", f"job {jid} is not running")
    j = g.jobs[jid]
    return replace(g, jobs=_put(g.jobs, jid, replace(j, preemptions=j.preemptions + 1)))


def _job_finished(g: Graph, e: Event) -> Graph:
    jid = e.get("job")
    _need(jid in g.jobs, f"unknown job {jid}")
    job = g.jobs[jid]
    _need(job.state == "running", f"job {jid} already {job.state}")
    state = e.get("state")
    _need(state in ("finished", "unknown", "cancelled"), f"bad job state {state}")
    job2 = replace(job, state=state, results=dict(e.get("results") or {}), reasons=dict(e.get("reasons") or {}),
                   error=e.get("error", ""), sec=float(e.get("sec") or 0.0), finished_t=e.t)
    keys = g.job_keys if state == JOB_FINISHED else _drop(g.job_keys, job.key)
    g = replace(g, jobs=_put(g.jobs, jid, job2), job_keys=keys)
    if state == JOB_FINISHED and not job.live and g.run is not None and g.run.status == RUN_RUNNING:
        g = _check_passes(g, e, job.tree)
    return g


def _baseline_recorded(g: Graph, e: Event) -> Graph:
    _need(not g.baseline_ready, "baseline recorded twice")
    return replace(g, baseline=dict(e.get("classes")), baseline_ready=True,
                   baseline_sec=float(e.get("full_sec") or 0.0), isolation=dict(e.get("isolation") or {}))


def _merge_requested(g: Graph, e: Event) -> Graph:
    _running(g)
    aid = e.get("attempt")
    _need(aid not in g.attempts, f"merge request {aid} exists")
    _need(g.baseline_ready, "merge requested before the baseline")
    _need(e.get("base") == g.head, "a merge request's base is the chain head when it is made")
    _need(e.get("tree") != g.head_cp.tree, "merge request on the head tree (nothing to merge)")
    lane = e.get("lane")
    _need(lane in (LANE_FG, LANE_BG), f"bad lane {lane}")
    for a in g.attempts.values():
        _need(not (a.worker == e.get("worker") and a.lane == lane and a.status in (ATT_PENDING, ATT_ADVANCING)),
              f"{e.get('worker')} already has a {lane} merge request {a.id} in progress")
    n = int(e.get("snapshot"))
    _need(n in g.snapshots, f"merge request on unknown snapshot {n}")
    snap = g.snapshots[n]
    _need(snap.tree == e.get("tree"), "merge request tree differs from its snapshot")
    sel = e.get("selection")
    a = Attempt(id=aid, worker=e.get("worker"), trigger=e.get("trigger"), tree=e.get("tree"), base=e.get("base"),
                selection=None if sel is None else tuple(sel), summary=e.get("summary", ""),
                raw_tree=e.get("raw_tree", ""), created_seq=e.seq, created_t=e.t, snapshot=n, epoch=snap.epoch,
                lane=lane)
    return replace(g, attempts=_put(g.attempts, aid, a))


def _merge_superseded(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown merge request {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_PENDING, f"merge request {aid} is {a.status}, only pending requests can be superseded")
    g = replace(g, attempts=_put(g.attempts, aid, replace(a, status=ATT_SUPERSEDED, reason=e.get("reason"))))
    if a.submit is not None and g.submits[a.submit].status == SUB_PENDING:
        s = g.submits[a.submit]
        cp = e.get("submit_checkpoint")
        if cp is not None:                          # 链头已经包含提交的快照：提交转到链头上判定
            _need(cp == g.head, f"superseded submit request {aid} must hand {s.id} to the head")
            g = _set_sub(g, replace(s, checkpoint=cp))
        else:
            g = _set_sub(g, replace(s, status=SUB_REJECTED, reason=f"cancelled ({e.get('reason')})"))
    return g


def _merge_advancing(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown merge request {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_PENDING, f"merge request {aid} is {a.status}")
    _need(e.get("parent_commit") == g.head_cp.commit, "parent commit must be the head commit")
    _need(not any(x.status == ATT_ADVANCING for x in g.attempts.values()), "another merge is advancing")
    hs = g.head_cp
    _need(not (hs.epoch == a.epoch and hs.snapshot >= a.snapshot and hs.id != 0),
          f"merge request {aid} is older than the head (it should be superseded)")
    if a.review is not None:
        v = g.reviews.get(a.review)
        _need(v is not None and (v.status == REV_FAILED or (v.status == REV_DECIDED and v.decision.get("merge"))),
              f"merge request {aid}: its review {a.review} did not approve the merge")
    a2 = replace(a, status=ATT_ADVANCING, parent_commit=e.get("parent_commit"), date=float(e.get("date")),
                 flaky=tuple(e.get("flaky") or ()))
    return replace(g, attempts=_put(g.attempts, aid, a2))


def _merged(g: Graph, e: Event) -> Graph:
    cid = int(e.get("checkpoint"))
    _need(cid not in g.checkpoints, f"merge point {cid} exists")
    files = tuple(tuple(f) for f in (e.get("files") or ()))
    if cid == 0:
        _need(not g.checkpoints, "merge point 0 is the first one")
        cp = Checkpoint(0, e.get("commit"), e.get("tree"), None, e.seq, e.t)
        return replace(g, checkpoints={0: cp}, head=0)
    _running(g)
    aid = e.get("attempt")
    _need(aid in g.attempts, f"merge point {cid} from unknown merge request {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_ADVANCING, f"merge request {aid} is {a.status}, not advancing")
    _need(a.parent_commit == g.head_cp.commit, "CAS: the parent commit is no longer the head")
    _need(e.get("tree") == a.tree, "merge point tree differs from the reviewed tree")
    _need(cid == max(g.checkpoints) + 1, f"merge point ids are sequential (got {cid})")
    v = g.reviews.get(a.review) if a.review else None
    d = v.decision if v is not None and v.status == REV_DECIDED else {}
    label = str(d.get("label") or "").strip() or (a.summary or "").strip()
    cp = Checkpoint(cid, e.get("commit"), a.tree, g.head, e.seq, e.t, attempt=aid, trigger=a.trigger, files=files,
                    snapshot=a.snapshot, epoch=a.epoch, review=v.id if d else None,
                    score=d.get("score") if d else None, score_note=str(d.get("score_note") or "") if d else "",
                    label=label.split("\n")[0][:300])
    g = replace(g, checkpoints=_put(g.checkpoints, cid, cp), head=cid,
                attempts=_put(g.attempts, aid, replace(a, status=ATT_CREATED, checkpoint=cid)))
    if a.submit is not None and g.submits[a.submit].status == SUB_PENDING:
        g = _set_sub(g, replace(g.submits[a.submit], checkpoint=cid))
    wip = g.wips.get(a.worker)
    if wip is not None and wip.tree == a.tree:
        g = replace(g, wips=_put(g.wips, a.worker, replace(wip, base=cid, files=(), last_rejection=None)))
    # 合并本身不算进展：进展只来自需求完成、证据检查第一次通过、todo 被锚定（见 _requirement_judged）
    return _check_passes(g, e, a.tree)


def _merge_rejected(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown merge request {aid}")
    a = g.attempts[aid]
    _need(a.status in (ATT_PENDING, ATT_ADVANCING), f"merge request {aid} is {a.status}")
    regs = tuple(e.get("regressions") or ())
    a2 = replace(a, status=ATT_REJECTED, regressions=regs, flaky=tuple(e.get("flaky") or ()), reason=e.get("reason"),
                 detail=str(e.get("detail") or "")[:2000])
    g = replace(g, attempts=_put(g.attempts, aid, a2))
    if a.submit is not None and g.submits[a.submit].status == SUB_PENDING:
        g = _set_sub(g, replace(g.submits[a.submit], status=SUB_REJECTED, reason=str(e.get("reason") or "")))
    wip = g.wips.get(a.worker)
    # 告诉 worker 的：它在等的（提交、收尾），以及复核者不认可的；后台回归门上的中间态测不过是常态
    if wip is not None and (a.lane == LANE_FG or e.get("reason") == "review"):
        rej = {"attempt": aid, "seq": e.seq, "reason": e.get("reason"), "regressions": list(regs[:50]),
               "n_regressions": len(regs), "flaky": list(a2.flaky[:20]), "detail": e.get("detail", ""),
               "snapshot": a.snapshot, "trigger": a.trigger, "review": a.review}
        g = replace(g, wips=_put(g.wips, a.worker, replace(wip, last_rejection=rej)))
    return g


def _rollback(g: Graph, e: Event) -> Graph:
    _running(g)
    to = int(e.get("to"))
    _need(to in g.checkpoints and not g.checkpoints[to].abandoned, f"cannot roll back to {to}")
    _need(not any(a.status in (ATT_PENDING, ATT_ADVANCING) for a in g.attempts.values()),
          "rollback while a merge request is in progress")
    chain_ids = []
    cur = g.head
    while cur is not None and cur != to:
        chain_ids.append(cur)
        cur = g.checkpoints[cur].parent
    _need(cur == to, f"merge point {to} is not on the chain")
    _need(sorted(chain_ids) == sorted(int(x) for x in e.get("abandoned")), "abandoned list does not match the chain")
    cps = dict(g.checkpoints)
    for cid in chain_ids:
        cps[cid] = replace(cps[cid], abandoned=True)
    for r in g.requirements.values():
        _need(not (r.status in (REQ_DONE, REQ_BLOCKED) and r.checkpoint in chain_ids),
              f"requirement {r.id} is {r.status} on abandoned merge point {r.checkpoint}; reopen it first")
    for t in g.todos.values():
        _need(not (t.status == TODO_ANCHORED and t.checkpoint in chain_ids),
              f"todo {t.id} is anchored on abandoned merge point {t.checkpoint}; invalidate it first")
    _need(not any(x.status == SUB_PENDING for x in g.submits.values()), "rollback while a submit is in progress")
    epoch = g.epoch + 1
    return replace(g, checkpoints=cps, head=to, epoch=epoch, epoch_base=_put(g.epoch_base, epoch, to))


# ======================================================================== 复核

def _review_started(g: Graph, e: Event) -> Graph:
    _running(g)
    vid = e.get("review")
    _need(vid not in g.reviews, f"review {vid} exists")
    _need(not any(v.status == REV_RUNNING for v in g.reviews.values()), "another review is running")
    aid, cp = e.get("attempt"), e.get("checkpoint")
    _need((aid is None) != (cp is None), "a review is either for a merge request or for a merge point")
    if aid is not None:
        _need(aid in g.attempts and g.attempts[aid].status == ATT_PENDING, f"review of a non-pending request {aid}")
        _need(g.attempts[aid].tree == e.get("tree"), "review tree differs from the merge request")
    else:
        _need(cp == g.head, "a review without a merge request judges the head")
    for rid in e.get("focus") or ():
        _need(rid in g.requirements, f"review focus has unknown requirement {rid}")
    sub = e.get("submit")
    _need(sub is None or sub in g.submits, f"review for unknown submit {sub}")
    v = Review(id=vid, trigger=e.get("trigger"), tree=e.get("tree"), snapshot=int(e.get("snapshot") or 0),
               base=e.get("base"), seq=e.seq, t=e.t, attempt=aid, checkpoint=cp, submit=sub,
               focus=tuple(e.get("focus") or ()), gate=dict(e.get("gate") or {}), retry_of=e.get("retry_of"),
               transcript=e.get("transcript"))
    g = _set_review(g, v)
    if aid is not None:
        a = g.attempts[aid]
        g = replace(g, attempts=_put(g.attempts, aid, replace(a, review=vid, reviews=a.reviews + (vid,))))
    if sub is not None and aid is None:
        g = _set_sub(g, replace(g.submits[sub], review=vid))
    return g


def _merge_reviewed(g: Graph, e: Event) -> Graph:
    vid = e.get("review")
    _need(vid in g.reviews and g.reviews[vid].status == REV_RUNNING, f"review {vid} is not running")
    v = g.reviews[vid]
    failed = bool(e.get("failed"))
    v2 = replace(v, status=REV_FAILED if failed else REV_RECORDED, verdict=dict(e.get("verdict") or {}),
                 runs=tuple(dict(x) for x in (e.get("runs") or ())), error=str(e.get("error") or "")[:2000],
                 transcript=e.get("transcript") or v.transcript)
    return _set_review(g, v2)


def _review_decided(g: Graph, e: Event) -> Graph:
    vid = e.get("review")
    _need(vid in g.reviews and g.reviews[vid].status == REV_RECORDED, f"review {vid} is not recorded")
    v = g.reviews[vid]
    merge = e.get("merge")
    _need((merge is None) == (v.attempt is None), "only a review of a merge request decides a merge")
    d = {k: e.get(k) for k in ("merge", "reasons", "notes", "judgements", "mentioned", "score", "score_note", "label",
                                "feedback")}
    return _set_review(g, replace(v, status=REV_DECIDED, decision=d))


def _review_cancelled(g: Graph, e: Event) -> Graph:
    vid = e.get("review")
    _need(vid in g.reviews and g.reviews[vid].status in (REV_RUNNING, REV_RECORDED), f"review {vid} is not running")
    return _set_review(g, replace(g.reviews[vid], status=REV_CANCELLED, error=str(e.get("reason") or "")))


def _waiver_granted(g: Graph, e: Event) -> Graph:
    _running(g)
    vid = e.get("review")
    _need(vid in g.reviews, f"waiver from unknown review {vid}")
    out = dict(g.waived)
    rid = e.get("requirement")
    _need(rid is None or rid in g.requirements, f"unknown requirement {rid}")
    for test in e.get("tests"):
        _need(g.baseline.get(test) == "pass", f"{test} is not in the regression gate")
        _need(test not in out, f"{test} is already waived")
        out[test] = Waiver(test, e.seq, e.t, rid, vid, str(e.get("quote")), str(e.get("reason")))
    return replace(g, waived=out)


# ======================================================================== 定位、诊断

def _persistent_regression(g: Graph, e: Event) -> Graph:
    _running(g)
    out = dict(g.persistent)
    for test in e.get("tests"):
        out[test] = Persistent(test, e.seq, e.t, int(e.get("since") or 0), int(e.get("epoch", g.epoch)),
                               e.get("trigger"), e.get("checkpoint"))
    return replace(g, persistent=out)


def _locate_started(g: Graph, e: Event) -> Graph:
    _running(g)
    lid = e.get("locate")
    _need(lid not in g.locates, f"locate {lid} exists")
    bad = e.get("bad")
    loc = Locate(id=lid, tests=tuple(e.get("tests")), bad_tree=bad["tree"], bad_snapshot=bad.get("snapshot"),
                 bad_checkpoint=bad.get("checkpoint"), epoch=int(e.get("epoch")), lower=int(e.get("lower", 0)),
                 trigger=e.get("trigger"), started_seq=e.seq, started_t=e.t, ref=e.get("ref"))
    return replace(g, locates=_put(g.locates, lid, loc))


def _locate_concluded(g: Graph, e: Event) -> Graph:
    lid = e.get("locate")
    _need(lid in g.locates and g.locates[lid].status == "running", f"locate {lid} is not running")
    loc = g.locates[lid]
    return replace(g, locates=_put(g.locates, lid, replace(loc, status="concluded",
                                                         groups=tuple(dict(x) for x in e.get("groups")))))


def _regression_located(g: Graph, e: Event) -> Graph:
    lid = e.get("locate")
    _need(lid in g.locates and g.locates[lid].status == "concluded", f"locate {lid} is not concluded")
    loc = g.locates[lid]
    rec = {k: e.get(k) for k in ("tests", "good", "bad", "exact", "files", "diff", "attribution", "group")}
    rec["seq"] = e.seq
    return replace(g, locates=_put(g.locates, lid, replace(loc, results=loc.results + (rec,))))


def _diagnosis_requested(g: Graph, e: Event) -> Graph:
    did = e.get("diagnosis")
    _need(did not in g.diagnoses, f"diagnosis {did} exists")
    d = Diagnosis(id=did, trigger=e.get("trigger"), tests=tuple(e.get("tests")), key=e.get("key", ""), seq=e.seq,
                  locate=e.get("locate"), previous=e.get("previous"))
    return replace(g, diagnoses=_put(g.diagnoses, did, d))


def _diagnosis_recorded(g: Graph, e: Event) -> Graph:
    did = e.get("diagnosis")
    _need(did in g.diagnoses and g.diagnoses[did].status == "requested", f"diagnosis {did} is not requested")
    d = g.diagnoses[did]
    status = "failed" if e.get("failed") else "recorded"
    return replace(g, diagnoses=_put(g.diagnoses, did, replace(d, status=status,
                                                            result=dict(e.get("result") or {}))))


HANDLERS: dict[str, Callable[[Graph, Event], Graph]] = {
    "run_started": _run_started, "runtime_recovered": _runtime_recovered, "clock_started": _clock_started,
    "run_suspended": _run_suspended, "deadline_reserve": _deadline_reserve, "finalize_started": _finalize_started,
    "delivered": _delivered,
    "plan_proposed": _plan_proposed, "requirement_frozen": _requirement_frozen,
    "requirement_judged": _requirement_judged,
    "todos_updated": _todos_updated, "todo_completed": _todo_completed, "todo_anchored": _todo_anchored,
    "todo_invalidated": _todo_invalidated,
    "submit_requested": _submit_requested, "submit_updated": _submit_updated,
    "session_started": _session_started, "session_resumed": _session_resumed, "session_ended": _session_ended,
    "compacted": _compacted, "snapshot_taken": _snapshot_taken, "stall_detected": _stall_detected,
    "job_started": _job_started, "job_preempted": _job_preempted, "job_finished": _job_finished,
    "baseline_recorded": _baseline_recorded,
    "merge_requested": _merge_requested, "merge_superseded": _merge_superseded, "merge_advancing": _merge_advancing,
    "merged": _merged, "merge_rejected": _merge_rejected, "rollback": _rollback,
    "review_started": _review_started, "merge_reviewed": _merge_reviewed, "review_decided": _review_decided,
    "review_cancelled": _review_cancelled, "waiver_granted": _waiver_granted,
    "persistent_regression": _persistent_regression, "locate_started": _locate_started,
    "locate_concluded": _locate_concluded, "regression_located": _regression_located,
    "diagnosis_requested": _diagnosis_requested, "diagnosis_recorded": _diagnosis_recorded,
}


def apply(g: Graph, e: Event) -> Graph:
    validate(e)
    _need(e.seq == g.seq + 1, f"event seq {e.seq} does not follow graph version {g.seq}")
    return replace(HANDLERS[e.type](g, e), seq=e.seq)


def replay(events: Iterable[Event], start: Graph | None = None) -> Graph:
    g = start or Graph()
    for e in events:
        g = apply(g, e)
    return g

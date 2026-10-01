"""视图的推导函数：apply(graph, event) -> graph'，replay(events) -> graph。

纯函数：不修改输入的图（只复制被修改的表），不读时钟，不做 IO。
reduce 是机械的，但它同时是状态机的最后一道防线：不合法的转换（例如 open → done、给冻结的需求加字段、
父节点不对的存档）会抛 IllegalEvent。规则只产生合法的事件；一条被接受过的日志重放时永远不会抛出。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, Optional

from belay.core.events import Event, actor_worker, validate
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_CREATED, ATT_PENDING, ATT_REJECTED, ATT_SUPERSEDED, BLOCKED,
                              CONFIRMED, DONE, DONE_UNVERIFIED, FINISHED, JOB_FINISHED, JOB_RUNNING, KIND_FINAL,
                              KIND_MILESTONE, KIND_REVIEW, KIND_STEP, LANE_BG, LANE_FG, OPEN, PROVISIONAL, REVIEW,
                              RUN_DONE, RUN_INCOMPLETE, RUN_RUNNING, SPLIT, STEP_ACTIVE, STEP_ANCHORED, STEP_DECLARED,
                              STEP_PLANNED, WHERE_LIVE, WHERE_SLOT, Attempt, Checkpoint, Compaction, Diagnosis, Graph,
                              Job, Lease, Locate, Note, Persistent, Requirement, Run, Session, Snapshot, Stall, Step,
                              Task, Wip, WorkerState)
from belay.core.queries import has_cycle, is_ancestor, last_session, latest_confirmed_ancestor
from belay.core.verify import PASSED, results_for_tree

PROGRESS_KINDS = (KIND_MILESTONE, KIND_STEP, KIND_REVIEW, KIND_FINAL)   # 交接存档（及旧日志里的自动存档）不算进展


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


def _task(g: Graph, tid: str) -> Task:
    _need(tid in g.tasks, f"unknown task {tid}")
    return g.tasks[tid]


def _set_task(g: Graph, t: Task, e: Optional[Event] = None, reason: str = "") -> Graph:
    old = g.tasks.get(t.id)
    if e is not None and (old is None or old.status != t.status):
        t = replace(t, history=(t.history + ((e.seq, t.status, reason or e.type),))[-30:])
    return replace(g, tasks=_put(g.tasks, t.id, t))


def _set_cp(g: Graph, cp: Checkpoint) -> Graph:
    return replace(g, checkpoints=_put(g.checkpoints, cp.id, cp))


def _progress(g: Graph, e: Event, worker: Optional[str]) -> Graph:
    """进展：记到这个 worker 最近的会话上（包括刚结束的：会话结束后 runtime 替它做的存档也算）。"""
    g = replace(g, last_progress_t=e.t, last_progress_seq=e.seq)
    if worker is None and len(g.workers) == 1:
        worker = next(iter(g.workers))
    if worker:
        s = last_session(g, worker)
        if s is not None and not s.progress:
            g = replace(g, sessions=_put(g.sessions, s.id, replace(s, progress=True)))
    return g


def _check_passes(g: Graph, e: Event, tree: str) -> Graph:
    """某个任务的检查项第一次在存档上通过：算进展。"""
    if not any(cp.tree == tree and not cp.abandoned for cp in g.checkpoints.values()):
        return g
    res = results_for_tree(g, tree)
    progressed = False
    for t in list(g.tasks.values()):
        if not t.checks or t.status == SPLIT:
            continue
        new = [c for c in t.checks if res.get(c) == PASSED and c not in t.passed_checks]
        if new:
            g = _set_task(g, replace(t, passed_checks=t.passed_checks + tuple(new)))
            progressed = True
    return _progress(g, e, None) if progressed else g


def _running(g: Graph) -> None:
    _need(g.run is not None, "run has not started")
    _need(g.run.status == RUN_RUNNING, "run already delivered")


# ======================================================================== 运行

def _run_started(g: Graph, e: Event) -> Graph:
    _need(g.run is None, "run_started twice")
    workers = tuple(e.get("workers"))
    run = Run(id=e.get("run_id"), task=e.get("task"), budget_sec=float(e.get("budget_sec")), started_t=e.t,
              deadline_t=float(e.get("deadline_t")), workers=workers,
              public_checks=tuple(e.get("public_checks") or ()), verifier=bool(e.get("verifier", True)))
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
    """准备阶段（基线、规划）在预算之外做完后，预算从这里开始计时（评测框架的 setup 阶段不计入 agent 预算）。"""
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
    _need(cid in g.checkpoints and is_ancestor(g, cid, g.head), f"delivered checkpoint {cid} is not on the chain")
    status = e.get("status")
    _need(status in ("DONE", "INCOMPLETE"), f"bad delivered status {status}")
    cp = g.checkpoints[cid]
    run = replace(g.run, status=RUN_DONE if status == "DONE" else RUN_INCOMPLETE, delivered=cid,
                  delivered_level=cp.level, deliver_unconfirmed=e.get("unconfirmed_policy"),
                  status_reasons=tuple(e.get("status_reasons") or ()))
    return replace(g, run=run)


# ======================================================================== 任务图

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
        reqs[r["id"]] = Requirement(r["id"], r["quote"], r.get("summary", ""), r.get("origin", "llm"))
    _need(bool(reqs), "no requirements")
    return replace(g, requirements=reqs, frozen=True)


def _new_task(g: Graph, spec: dict, seq: int, origin: str, parent: str | None = None) -> Task:
    tid = spec["id"] if "id" in spec else spec["task"]
    _need(tid not in g.tasks, f"task {tid} exists")
    links = tuple(spec.get("links") or ())
    blocked_by = tuple(spec.get("blocked_by") or ())
    for r in links:
        _need(r in g.requirements, f"task {tid} links unknown requirement {r}")
    for d in blocked_by:
        _need(d in g.tasks, f"task {tid} blocked_by unknown task {d}")
    return Task(id=tid, title=spec.get("title", ""), description=spec.get("description", ""), links=links,
                blocked_by=blocked_by, parent=parent, discovered_from=spec.get("discovered_from"),
                priority=int(spec.get("priority") or 0), checks=tuple(spec.get("checks") or ()), origin=origin,
                created_seq=seq, history=((seq, OPEN, "created"),))


def _task_added(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(g.frozen, "tasks are added after requirements are frozen")
    t = _new_task(g, e.payload, e.seq, e.source)
    if t.discovered_from is not None:
        _need(t.discovered_from in g.tasks, f"discovered_from unknown task {t.discovered_from}")
    g = _set_task(g, t)
    _need(has_cycle({x.id: x.blocked_by for x in g.tasks.values()}) is None, "task_added creates a cycle")
    return g


def _task_split(g: Graph, e: Event) -> Graph:
    _running(g)
    parent = _task(g, e.get("task"))
    _need(parent.status in (OPEN, ACTIVE, BLOCKED), f"cannot split {parent.id} in status {parent.status}")
    children = []
    covered: set[str] = set()
    for spec in e.get("children"):
        c = _new_task(g, spec, e.seq, e.source, parent=parent.id)
        g = _set_task(g, c)
        children.append(c.id)
        covered.update(c.links)
    _need(bool(children), "split without children")
    _need(set(parent.links) <= covered, f"split of {parent.id} drops requirement links "
                                        f"{sorted(set(parent.links) - covered)}")
    edges = {t.id: t.blocked_by for t in g.tasks.values()}
    _need(has_cycle(edges) is None, "split creates a dependency cycle")
    g = replace(g, leases=_drop(g.leases, parent.id))
    return _set_task(g, replace(parent, status=SPLIT, children=tuple(children)), e)


def _task_claimed(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    w = e.get("worker")
    _need(w in g.workers, f"unknown worker {w}")
    _need(t.status == OPEN, f"cannot claim {t.id} in status {t.status}")
    _need(t.id not in g.leases, f"{t.id} is already held")
    head = e.get("head")
    g = replace(g, leases=_put(g.leases, t.id, Lease(t.id, w, e.t, head, e.seq)))
    return _set_task(g, replace(t, status=ACTIVE, claimed_head=head), e)


def _task_released(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    lease = g.leases.get(t.id)
    _need(t.status == ACTIVE and lease is not None and lease.worker == e.get("worker"),
          f"{e.get('worker')} does not hold active task {t.id}")
    g = replace(g, leases=_drop(g.leases, t.id))
    return _set_task(g, replace(t, status=OPEN), e)


def _review_requested(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    lease = g.leases.get(t.id)
    _need(t.status == ACTIVE and lease is not None and lease.worker == e.get("worker"),
          f"{e.get('worker')} does not hold active task {t.id}")
    cp = e.get("checkpoint")
    if cp is not None:
        _need(cp == g.head, "review on a checkpoint that is not the head")
    return _set_task(g, replace(t, status=REVIEW, review_seq=e.seq, review_attempt=None, review_checkpoint=cp), e)


def _task_done(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    _need(t.status == REVIEW, f"{t.id} must be in review to be done (is {t.status})")
    cp = e.get("checkpoint")
    _need(cp is not None and cp == t.review_checkpoint, f"{t.id} done on checkpoint {cp}, reviewed on "
                                                        f"{t.review_checkpoint}")
    _need(cp in g.checkpoints and not g.checkpoints[cp].abandoned, f"checkpoint {cp} is not on the chain")
    verified = bool(e.get("verified"))
    _need(verified == bool(t.checks), f"{t.id}: verified must be {bool(t.checks)}")
    worker = g.leases[t.id].worker if t.id in g.leases else None
    g = replace(g, leases=_drop(g.leases, t.id))
    g = _set_task(g, replace(t, status=DONE if verified else DONE_UNVERIFIED, done_checkpoint=cp, done_seq=e.seq,
                             verified=verified, last_failure=(), review=None, review_missing=()), e)
    return _progress(g, e, worker)


def _task_blocked(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    _need(t.status in (OPEN, ACTIVE, REVIEW), f"cannot block {t.id} in status {t.status}")
    worker = actor_worker(e.actor)
    g = replace(g, leases=_drop(g.leases, t.id))
    g = _set_task(g, replace(t, status=BLOCKED, blocked_kind=e.get("kind"), blocked_reason=e.get("reason"),
                             blocked_quote=e.get("quote"), review_attempt=None, review_checkpoint=None,
                             review=None), e, e.get("kind"))
    return _progress(g, e, worker)


def _task_reopened(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    _need(t.status in (REVIEW, DONE, DONE_UNVERIFIED, BLOCKED), f"cannot reopen {t.id} in status {t.status}")
    status = ACTIVE if (t.status == REVIEW and t.id in g.leases) else OPEN
    if status == OPEN:
        g = replace(g, leases=_drop(g.leases, t.id))
    by_review = e.get("reason") in ("review_missing", "review_reading")
    t2 = replace(t, status=status, reopen_count=t.reopen_count + 1, reopen_reason=e.get("reason"),
                 last_failure=tuple(e.get("failures") or ()), review_attempt=None, review_checkpoint=None,
                 review_seq=None, done_checkpoint=None, done_seq=None, verified=False, blocked_kind=None,
                 blocked_reason=None, blocked_quote=None,
                 review_reopens=t.review_reopens + (1 if by_review else 0))
    return _set_task(g, t2, e, e.get("reason"))


# ======================================================================== 步骤

def _steps_planned(g: Graph, e: Event) -> Graph:
    _running(g)
    tid = e.get("task")
    _task(g, tid)
    keep = {s.id for s in g.steps.values() if s.task == tid and s.status in (STEP_DECLARED, STEP_ANCHORED)}
    listed = set()
    steps = dict(g.steps)
    for spec in e.get("steps"):
        sid = spec["id"]
        listed.add(sid)
        cur = steps.get(sid)
        status = spec.get("status", STEP_PLANNED)
        if cur is None:
            _need(status in (STEP_PLANNED, STEP_ACTIVE), f"new step {sid} must be planned or active")
            steps[sid] = Step(sid, tid, int(spec["n"]), spec["title"], status, order=int(spec.get("order", 0)))
        else:
            _need(cur.task == tid, f"step {sid} belongs to {cur.task}")
            if cur.status in (STEP_DECLARED, STEP_ANCHORED):
                status = cur.status
            steps[sid] = replace(cur, title=spec["title"], status=status, order=int(spec.get("order", 0)))
    for sid in [s.id for s in steps.values() if s.task == tid]:
        if sid not in listed and sid not in keep:
            del steps[sid]
    return replace(g, steps=steps)


def _step(g: Graph, sid: str) -> Step:
    _need(sid in g.steps, f"unknown step {sid}")
    return g.steps[sid]


def _step_started(g: Graph, e: Event) -> Graph:
    s = _step(g, e.get("step"))
    _need(s.status in (STEP_PLANNED, STEP_ACTIVE), f"cannot start step {s.id} in status {s.status}")
    return replace(g, steps=_put(g.steps, s.id, replace(s, status=STEP_ACTIVE)))


def _step_done(g: Graph, e: Event) -> Graph:
    s = _step(g, e.get("step"))
    _need(s.status in (STEP_PLANNED, STEP_ACTIVE), f"cannot finish step {s.id} in status {s.status}")
    n = int(e.get("snapshot"))
    _need(n == 0 or n in g.snapshots, f"unknown anchor snapshot {n}")
    epoch = g.snapshots[n].epoch if n in g.snapshots else 0
    if e.get("anchor_epoch") is not None:
        epoch = int(e.get("anchor_epoch"))
    s2 = replace(s, status=STEP_DECLARED, anchor_snapshot=n, anchor_epoch=epoch, summary=e.get("summary", ""),
                 files=tuple(tuple(f) for f in (e.get("files") or ())), declared_seq=e.seq)
    return replace(g, steps=_put(g.steps, s.id, s2))


def _step_anchored(g: Graph, e: Event) -> Graph:
    s = _step(g, e.get("step"))
    _need(s.status == STEP_DECLARED, f"step {s.id} is {s.status}, not declared")
    cid = int(e.get("checkpoint"))
    cp = g.checkpoints.get(cid)
    _need(cp is not None and not cp.abandoned, f"anchor checkpoint {cid} is not on the chain")
    _need(cp.epoch == s.anchor_epoch and cp.snapshot >= (s.anchor_snapshot or 0),
          f"checkpoint {cid} does not contain the anchor of {s.id}")
    g = replace(g, steps=_put(g.steps, s.id, replace(s, status=STEP_ANCHORED, checkpoint=cid)))
    h = g.leases.get(s.task)
    return _progress(g, e, h.worker if h else None)


def _step_invalidated(g: Graph, e: Event) -> Graph:
    s = _step(g, e.get("step"))
    _need(s.status in (STEP_DECLARED, STEP_ANCHORED), f"step {s.id} is {s.status}")
    s2 = replace(s, status=STEP_ACTIVE, anchor_snapshot=None, anchor_epoch=None, checkpoint=None)
    return replace(g, steps=_put(g.steps, s.id, s2))


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


def _note(g: Graph, e: Event) -> Graph:
    w = e.get("worker")
    session = g.workers[w].session if w in g.workers else None
    session = e.get("session", session)
    note = Note(e.seq, e.t, w, session, e.get("kind"), e.get("text"), e.get("task"))
    return replace(g, notes=g.notes + (note,))


def _snapshot_taken(g: Graph, e: Event) -> Graph:
    _running(g)
    n = int(e.get("snapshot"))
    _need(n == g.last_snapshot + 1, f"snapshot numbers are sequential (got {n})")
    w = e.get("worker")
    base = int(e.get("base", g.head if g.head is not None else 0))
    snap = Snapshot(n=n, seq=e.seq, t=e.t, worker=w, tree=e.get("tree"), raw_tree=e.get("raw_tree"), epoch=g.epoch,
                    reason=e.get("reason"), testable=bool(e.get("testable")), commit=e.get("commit", ""), base=base,
                    files=tuple(tuple(f) for f in (e.get("files") or ())), dropped=tuple(e.get("dropped") or ()),
                    held=tuple(e.get("held") or ()), step=e.get("step"), session=e.get("session"),
                    tool_seq=int(e.get("tool_seq") or 0), precheck=e.get("precheck", ""))
    prev = g.wips.get(w)
    wip = Wip(worker=w, base=base, tree=snap.tree, raw_tree=snap.raw_tree, files=snap.files, dropped=snap.dropped,
              snapshot=n, seq=e.seq, last_rejection=prev.last_rejection if prev else None)
    return replace(g, snapshots=_put(g.snapshots, n, snap), wips=_put(g.wips, w, wip))


def _stall_detected(g: Graph, e: Event) -> Graph:
    s = Stall(e.seq, e.t, e.get("kind"), e.get("action"), e.get("worker"), e.get("task"), e.get("detail", ""))
    return replace(g, stalls=g.stalls + (s,))


# ======================================================================== 验证与存档链

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
        _need(aid in g.attempts, f"job for unknown attempt {aid}")
        a = g.attempts[aid]
        _need(a.status == ATT_PENDING, f"job for attempt {aid} in status {a.status}")
        jobs = tuple(j for j in a.jobs if j != e.get("replaces")) + (jid,)
        g = replace(g, attempts=_put(g.attempts, aid, replace(a, jobs=jobs)))
    return g


def _job_preempted(g: Graph, e: Event) -> Graph:
    jid = e.get("job")
    _need(jid in g.jobs and g.jobs[jid].state == JOB_RUNNING, f"job {jid} is not running")
    j = g.jobs[jid]
    return replace(g, jobs=_put(g.jobs, jid, replace(j, preemptions=j.preemptions + 1)))


def _job_finished(g: Graph, e: Event) -> Graph:
    jid = e.get("job")
    _need(jid in g.jobs, f"unknown job {jid}")
    job = g.jobs[jid]
    _need(job.state == JOB_RUNNING, f"job {jid} already {job.state}")
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


def _checkpoint_attempted(g: Graph, e: Event) -> Graph:
    _running(g)
    aid = e.get("attempt")
    _need(aid not in g.attempts, f"attempt {aid} exists")
    _need(g.baseline_ready, "checkpoint attempted before the baseline")
    _need(e.get("base") == g.head, "attempt base must be the chain head when it is made")
    _need(e.get("tree") != g.head_cp.tree, "attempt on the head tree (nothing to checkpoint)")
    lane = e.get("lane")
    _need(lane in (LANE_FG, LANE_BG), f"bad lane {lane}")
    for a in g.attempts.values():
        _need(not (a.worker == e.get("worker") and a.lane == lane and a.status in (ATT_PENDING, ATT_ADVANCING)),
              f"{e.get('worker')} already has a {lane} attempt {a.id} in progress")
    n = int(e.get("snapshot"))
    _need(n in g.snapshots, f"attempt on unknown snapshot {n}")
    snap = g.snapshots[n]
    _need(snap.tree == e.get("tree"), "attempt tree differs from its snapshot")
    tasks = tuple(e.get("tasks") or ())
    for tid in tasks:
        t = _task(g, tid)
        _need(t.status == REVIEW and t.review_attempt is None and t.review_checkpoint is None,
              f"attempt task {tid} is not awaiting review")
        g = _set_task(g, replace(t, review_attempt=aid))
    sel = e.get("selection")
    a = Attempt(id=aid, worker=e.get("worker"), trigger=e.get("trigger"), tree=e.get("tree"), base=e.get("base"),
                tier=e.get("tier"), selection=None if sel is None else tuple(sel), tasks=tasks,
                summary=e.get("summary", ""), raw_tree=e.get("raw_tree", ""), created_seq=e.seq, snapshot=n,
                epoch=snap.epoch, lane=lane, kind=e.get("kind") or KIND_MILESTONE)
    return replace(g, attempts=_put(g.attempts, aid, a))


def _attempt_superseded(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown attempt {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_PENDING, f"attempt {aid} is {a.status}, only pending attempts can be superseded")
    g = replace(g, attempts=_put(g.attempts, aid, replace(a, status=ATT_SUPERSEDED, reason=e.get("reason"))))
    cp = e.get("review_checkpoint")
    for tid in a.tasks:
        t = g.tasks[tid]
        if t.status == REVIEW and t.review_attempt == aid:
            _need(cp is not None and cp == g.head, f"superseded review attempt {aid} must hand {tid} to the head")
            g = _set_task(g, replace(t, review_checkpoint=cp))
    return g


def _checkpoint_advancing(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown attempt {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_PENDING, f"attempt {aid} is {a.status}")
    _need(e.get("parent_commit") == g.head_cp.commit, "parent commit must be the head commit")
    _need(not any(x.status == ATT_ADVANCING for x in g.attempts.values()), "another attempt is advancing")
    hs = g.head_cp
    _need(not (hs.epoch == a.epoch and hs.snapshot >= a.snapshot and hs.id != 0),
          f"attempt {aid} is older than the head (it should be superseded)")
    a2 = replace(a, status=ATT_ADVANCING, parent_commit=e.get("parent_commit"), date=float(e.get("date")),
                 flaky=tuple(e.get("flaky") or ()))
    return replace(g, attempts=_put(g.attempts, aid, a2))


def _checkpoint_created(g: Graph, e: Event) -> Graph:
    cid = int(e.get("checkpoint"))
    _need(cid not in g.checkpoints, f"checkpoint {cid} exists")
    files = tuple(tuple(f) for f in (e.get("files") or ()))
    if cid == 0:
        _need(not g.checkpoints, "checkpoint 0 is the first checkpoint")
        cp = Checkpoint(0, e.get("commit"), e.get("tree"), None, e.seq, e.t, confirmed_seq=e.seq)
        return replace(g, checkpoints={0: cp}, head=0, confirmed=0)
    _running(g)
    aid = e.get("attempt")
    _need(aid in g.attempts, f"checkpoint {cid} from unknown attempt {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_ADVANCING, f"attempt {aid} is {a.status}, not advancing")
    _need(a.parent_commit == g.head_cp.commit, "CAS: the parent commit is no longer the head")
    _need(e.get("tree") == a.tree, "checkpoint tree differs from the verified tree")
    _need(cid == max(g.checkpoints) + 1, f"checkpoint ids are sequential (got {cid})")
    held = tuple(sorted({l.task for l in g.leases.values() if l.worker == a.worker} | set(a.tasks)))
    level = CONFIRMED if a.tier == "full" else PROVISIONAL
    cp = Checkpoint(cid, e.get("commit"), a.tree, g.head, e.seq, e.t, attempt=aid, tier=a.tier, trigger=a.trigger,
                    files=files, tasks=held, snapshot=a.snapshot, epoch=a.epoch, kind=a.kind, level=level,
                    confirmed_seq=e.seq if level == CONFIRMED else None,
                    label=(a.summary or "").strip().split("\n")[0][:300])      # 手动存档的 summary 就是里程碑标签
    g = replace(g, checkpoints=_put(g.checkpoints, cid, cp), head=cid,
                attempts=_put(g.attempts, aid, replace(a, status=ATT_CREATED, checkpoint=cid)))
    g = replace(g, confirmed=latest_confirmed_ancestor(g, cid))
    for tid in a.tasks:
        t = g.tasks[tid]
        if t.status == REVIEW and t.review_attempt == aid:
            g = _set_task(g, replace(t, review_checkpoint=cid))
    wip = g.wips.get(a.worker)
    if wip is not None and wip.tree == a.tree:      # 存进去的正是当前的 WIP：它相对新存档没有未验证的改动了
        g = replace(g, wips=_put(g.wips, a.worker, replace(wip, base=cid, files=(), last_rejection=None)))
    if a.kind in PROGRESS_KINDS:                    # 交接存档不算进展（确认与否都一样）
        g = _progress(g, e, a.worker)
    return _check_passes(g, e, a.tree)


def _checkpoint_rejected(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown attempt {aid}")
    a = g.attempts[aid]
    _need(a.status in (ATT_PENDING, ATT_ADVANCING), f"attempt {aid} is {a.status}")
    regs = tuple(e.get("regressions") or ())
    a2 = replace(a, status=ATT_REJECTED, regressions=regs, flaky=tuple(e.get("flaky") or ()), reason=e.get("reason"))
    g = replace(g, attempts=_put(g.attempts, aid, a2))
    wip = g.wips.get(a.worker)
    if wip is not None and a.lane == LANE_FG:      # 只记 worker 声明的存档被拒；步骤与交接是中间态，不推给 worker
        rej = {"attempt": aid, "seq": e.seq, "reason": e.get("reason"), "regressions": list(regs[:50]),
               "n_regressions": len(regs), "flaky": list(a2.flaky[:20]), "detail": e.get("detail", ""),
               "snapshot": a.snapshot, "kind": a.kind}
        g = replace(g, wips=_put(g.wips, a.worker, replace(wip, last_rejection=rej)))
    return g


def _checkpoint_confirmed(g: Graph, e: Event) -> Graph:
    _running(g)
    cid = int(e.get("checkpoint"))
    cp = g.checkpoints.get(cid)
    _need(cp is not None and not cp.abandoned, f"checkpoint {cid} is not on the chain")
    _need(cp.level != CONFIRMED and not cp.demoted, f"checkpoint {cid} is already {cp.level}")
    g = _set_cp(g, replace(cp, level=CONFIRMED, confirmed_seq=e.seq))
    g = replace(g, confirmed=latest_confirmed_ancestor(g, g.head))
    # 确认点前移算进展，但只对 worker 声明过的单元（手动、步骤、review、收尾）：否则一个不断写出能过门的半成品、
    # 却从不完成任何东西的 worker，会因为后台提升而永远“有进展”，基于进展的停滞检测就失效了
    return _progress(g, e, None) if cp.kind in PROGRESS_KINDS else g


def _checkpoint_demoted(g: Graph, e: Event) -> Graph:
    _running(g)
    cid = int(e.get("checkpoint"))
    cp = g.checkpoints.get(cid)
    _need(cp is not None and cp.id != 0, f"cannot demote checkpoint {cid}")
    _need(cp.level == PROVISIONAL and not cp.demoted, f"checkpoint {cid} cannot be demoted")
    g = _set_cp(g, replace(cp, demoted=True, demote_regressions=tuple(e.get("regressions") or ())))
    return replace(g, confirmed=latest_confirmed_ancestor(g, g.head))


def _checkpoint_marked(g: Graph, e: Event) -> Graph:
    """worker 声明的单元（手动存档、步骤、review）落在一个已有的自动 / 交接存档上：把它升级为里程碑。"""
    _running(g)
    cid = int(e.get("checkpoint"))
    cp = g.checkpoints.get(cid)
    kind = e.get("kind")
    _need(cp is not None and cp.id != 0 and not cp.abandoned, f"cannot mark checkpoint {cid}")
    _need(cp.kind in ("auto", "handoff") and kind in PROGRESS_KINDS, f"cannot mark {cp.kind} checkpoint {cid} {kind}")
    label = cp.label or str(e.get("label") or "").strip().split("\n")[0][:300]
    g = _set_cp(g, replace(cp, kind=kind, label=label))
    return _progress(g, e, e.get("worker"))


def _rollback(g: Graph, e: Event) -> Graph:
    _running(g)
    to = int(e.get("to"))
    _need(to in g.checkpoints and not g.checkpoints[to].abandoned, f"cannot roll back to {to}")
    _need(not any(a.status in (ATT_PENDING, ATT_ADVANCING) for a in g.attempts.values()),
          "rollback while an attempt is in progress")
    chain_ids = []
    cur = g.head
    while cur is not None and cur != to:
        chain_ids.append(cur)
        cur = g.checkpoints[cur].parent
    _need(cur == to, f"checkpoint {to} is not on the chain")
    _need(sorted(chain_ids) == sorted(int(x) for x in e.get("abandoned")), "abandoned list does not match the chain")
    cps = dict(g.checkpoints)
    for cid in chain_ids:
        cps[cid] = replace(cps[cid], abandoned=True)
    for t in g.tasks.values():
        _need(not (t.status in FINISHED and t.done_checkpoint in chain_ids),
              f"task {t.id} is done on abandoned checkpoint {t.done_checkpoint}; reopen it first")
        _need(not (t.status == REVIEW and t.review_checkpoint in chain_ids),
              f"task {t.id} is reviewed on abandoned checkpoint {t.review_checkpoint}; reopen it first")
    for s in g.steps.values():
        _need(not (s.status == STEP_ANCHORED and s.checkpoint in chain_ids),
              f"step {s.id} is anchored on abandoned checkpoint {s.checkpoint}; invalidate it first")
    epoch = g.epoch + 1
    g = replace(g, checkpoints=cps, head=to, epoch=epoch, epoch_base=_put(g.epoch_base, epoch, to))
    return replace(g, confirmed=latest_confirmed_ancestor(g, to))


# ======================================================================== 定位、诊断、复查

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


def _relation_learned(g: Graph, e: Event) -> Graph:
    rel = list(g.relations)
    for src, tf in e.get("pairs"):
        if (src, tf) not in rel:
            rel.append((src, tf))
    return replace(g, relations=tuple(rel))


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


def _review_started(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    _need(t.status in (DONE_UNVERIFIED, BLOCKED), f"cannot review {t.id} in status {t.status}")
    _need(t.review != "running", f"{t.id} is already under review")
    return _set_task(g, replace(t, review="running", review_missing=()))


def _review_recorded(g: Graph, e: Event) -> Graph:
    t = _task(g, e.get("task"))
    _need(t.review == "running", f"{t.id} has no review in progress")
    impl = e.get("implemented")
    _need(impl in ("yes", "partial", "no", "reading", "none", "failed"), f"bad review result {impl}")
    return _set_task(g, replace(t, review=impl, review_missing=tuple(e.get("missing") or ())[:30]))


def _checkpoint_labeled(g: Graph, e: Event) -> Graph:
    cid = int(e.get("checkpoint"))
    _need(cid in g.checkpoints, f"unknown checkpoint {cid}")
    return _set_cp(g, replace(g.checkpoints[cid], label=str(e.get("label"))[:300]))


HANDLERS: dict[str, Callable[[Graph, Event], Graph]] = {
    "run_started": _run_started, "runtime_recovered": _runtime_recovered, "clock_started": _clock_started,
    "run_suspended": _run_suspended,
    "deadline_reserve": _deadline_reserve, "finalize_started": _finalize_started, "delivered": _delivered,
    "plan_proposed": _plan_proposed, "requirement_frozen": _requirement_frozen, "task_added": _task_added,
    "task_split": _task_split, "task_claimed": _task_claimed, "task_released": _task_released,
    "review_requested": _review_requested, "task_done": _task_done, "task_blocked": _task_blocked,
    "task_reopened": _task_reopened,
    "steps_planned": _steps_planned, "step_started": _step_started, "step_done": _step_done,
    "step_anchored": _step_anchored, "step_invalidated": _step_invalidated,
    "session_started": _session_started, "session_resumed": _session_resumed, "session_ended": _session_ended,
    "compacted": _compacted, "note": _note, "snapshot_taken": _snapshot_taken, "stall_detected": _stall_detected,
    "job_started": _job_started, "job_preempted": _job_preempted, "job_finished": _job_finished,
    "baseline_recorded": _baseline_recorded,
    "checkpoint_attempted": _checkpoint_attempted, "attempt_superseded": _attempt_superseded,
    "checkpoint_advancing": _checkpoint_advancing, "checkpoint_created": _checkpoint_created,
    "checkpoint_rejected": _checkpoint_rejected, "checkpoint_confirmed": _checkpoint_confirmed,
    "checkpoint_demoted": _checkpoint_demoted, "checkpoint_marked": _checkpoint_marked, "rollback": _rollback,
    "persistent_regression": _persistent_regression, "locate_started": _locate_started,
    "locate_concluded": _locate_concluded, "regression_located": _regression_located,
    "relation_learned": _relation_learned, "diagnosis_requested": _diagnosis_requested,
    "diagnosis_recorded": _diagnosis_recorded, "review_started": _review_started,
    "review_recorded": _review_recorded, "checkpoint_labeled": _checkpoint_labeled,
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

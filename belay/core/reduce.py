"""视图的推导函数：apply(graph, event) -> graph'，replay(events) -> graph。

纯函数：不修改输入的图（只复制被修改的表），不读时钟，不做 IO。
reduce 是机械的，但它同时是状态机的最后一道防线：不合法的转换（例如 open → done、给冻结的需求加字段、
链头不对的存档）会抛 IllegalEvent。规则只产生合法的事件；一条被接受过的日志重放时永远不会抛出。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable

from belay.core.events import Event, actor_worker, validate
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_CREATED, ATT_PENDING, ATT_REJECTED, BLOCKED, DONE,
                              DONE_UNVERIFIED, FINISHED, JOB_FINISHED, JOB_RUNNING, OPEN, REVIEW, RUN_DONE,
                              RUN_INCOMPLETE, RUN_RUNNING, SPLIT, Attempt, Checkpoint, Compaction, Graph, Job, Lease,
                              Note, Requirement, Run, Session, Stall, Task, Wip, WorkerState)
from belay.core.queries import has_cycle, last_session


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


def _set_task(g: Graph, t: Task) -> Graph:
    return replace(g, tasks=_put(g.tasks, t.id, t))


def _progress(g: Graph, e: Event, worker: str | None) -> Graph:
    """新证据：记到这个 worker 最近的会话上（包括刚结束的：会话结束时 runtime 替它做的存档也算）。"""
    g = replace(g, last_progress_t=e.t, last_progress_seq=e.seq)
    if worker:
        s = last_session(g, worker)
        if s is not None and not s.progress:
            g = replace(g, sessions=_put(g.sessions, s.id, replace(s, progress=True)))
    return g


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
    run = replace(g.run, recoveries=g.run.recoveries + 1,
                  downtime_sec=g.run.downtime_sec + float(e.get("downtime_sec")))
    return replace(g, run=run)


def _deadline_reserve(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(not g.run.reserve, "deadline_reserve twice")
    return replace(g, run=replace(g.run, reserve=True, reserve_sec=float(e.get("reserve_sec"))))


def _delivered(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(e.get("checkpoint") == g.head, "delivered checkpoint must be the chain head")
    status = e.get("status")
    _need(status in ("DONE", "INCOMPLETE"), f"bad delivered status {status}")
    run = replace(g.run, status=RUN_DONE if status == "DONE" else RUN_INCOMPLETE, delivered=g.head)
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
                created_seq=seq)


def _task_added(g: Graph, e: Event) -> Graph:
    _running(g)
    _need(g.frozen, "tasks are added after requirements are frozen")
    t = _new_task(g, e.payload, e.seq, e.source)
    if t.discovered_from is not None:
        _need(t.discovered_from in g.tasks, f"discovered_from unknown task {t.discovered_from}")
    return _set_task(g, t)


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
    return _set_task(g, replace(parent, status=SPLIT, children=tuple(children)))


def _task_claimed(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    w = e.get("worker")
    _need(w in g.workers, f"unknown worker {w}")
    _need(t.status == OPEN, f"cannot claim {t.id} in status {t.status}")
    _need(t.id not in g.leases, f"{t.id} is already leased")
    _need(all(g.tasks[d].status in FINISHED for d in t.blocked_by), f"{t.id} has unfinished blockers")
    lease = Lease(t.id, w, e.t, float(e.get("expires_t")))
    g = replace(g, leases=_put(g.leases, t.id, lease))
    return _set_task(g, replace(t, status=ACTIVE))


def _task_released(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    lease = g.leases.get(t.id)
    _need(t.status == ACTIVE and lease is not None and lease.worker == e.get("worker"),
          f"{e.get('worker')} does not hold active task {t.id}")
    g = replace(g, leases=_drop(g.leases, t.id))
    return _set_task(g, replace(t, status=OPEN))


def _review_requested(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    lease = g.leases.get(t.id)
    _need(t.status == ACTIVE and lease is not None and lease.worker == e.get("worker"),
          f"{e.get('worker')} does not hold active task {t.id}")
    cp = e.get("checkpoint")
    if cp is not None:
        _need(cp == g.head, "review on a checkpoint that is not the head")
    return _set_task(g, replace(t, status=REVIEW, review_seq=e.seq, review_attempt=None, review_checkpoint=cp))


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
    g = _set_task(g, replace(t, status=DONE if verified else DONE_UNVERIFIED, done_checkpoint=cp, verified=verified,
                             last_failure=()))
    return _progress(g, e, worker)


def _task_blocked(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    _need(t.status in (OPEN, ACTIVE, REVIEW), f"cannot block {t.id} in status {t.status}")
    worker = actor_worker(e.actor)
    g = replace(g, leases=_drop(g.leases, t.id))
    g = _set_task(g, replace(t, status=BLOCKED, blocked_kind=e.get("kind"), blocked_reason=e.get("reason"),
                             blocked_quote=e.get("quote"), review_attempt=None, review_checkpoint=None))
    return _progress(g, e, worker)


def _task_reopened(g: Graph, e: Event) -> Graph:
    _running(g)
    t = _task(g, e.get("task"))
    _need(t.status in (REVIEW, DONE, DONE_UNVERIFIED, BLOCKED), f"cannot reopen {t.id} in status {t.status}")
    status = ACTIVE if (t.status == REVIEW and t.id in g.leases) else OPEN
    if status == OPEN:
        g = replace(g, leases=_drop(g.leases, t.id))
    t2 = replace(t, status=status, reopen_count=t.reopen_count + 1, reopen_reason=e.get("reason"),
                 last_failure=tuple(e.get("failures") or ()), review_attempt=None, review_checkpoint=None,
                 review_seq=None, done_checkpoint=None, verified=False, blocked_kind=None, blocked_reason=None,
                 blocked_quote=None)
    return _set_task(g, t2)


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


def _session_ended(g: Graph, e: Event) -> Graph:
    sid = e.get("session")
    _need(sid in g.sessions, f"unknown session {sid}")
    s = g.sessions[sid]
    _need(s.ended_t is None, f"session {sid} already ended")
    s2 = replace(s, ended_t=e.t, end_reason=e.get("reason"), peak_context=int(e.get("peak_context") or 0),
                 turns=int(e.get("turns") or 0), error=e.get("error"))
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


def _lease_renewed(g: Graph, e: Event) -> Graph:
    lease = g.leases.get(e.get("task"))
    _need(lease is not None and lease.worker == e.get("worker"), f"no lease on {e.get('task')} for {e.get('worker')}")
    return replace(g, leases=_put(g.leases, lease.task, replace(lease, expires_t=float(e.get("expires_t")))))


def _lease_expired(g: Graph, e: Event) -> Graph:
    tid = e.get("task")
    lease = g.leases.get(tid)
    _need(lease is not None and lease.worker == e.get("worker"), f"no lease on {tid} for {e.get('worker')}")
    g = replace(g, leases=_drop(g.leases, tid))
    t = g.tasks[tid]
    if t.status == ACTIVE:
        g = _set_task(g, replace(t, status=OPEN))
    return g


def _note(g: Graph, e: Event) -> Graph:
    w = e.get("worker")
    session = g.workers[w].session if w in g.workers else None
    session = e.get("session", session)
    return replace(g, notes=g.notes + (Note(e.seq, e.t, w, session, e.get("kind"), e.get("text")),))


def _wip_recorded(g: Graph, e: Event) -> Graph:
    w = e.get("worker")
    prev = g.wips.get(w)
    wip = Wip(worker=w, base=int(e.get("base")), tree=e.get("tree"), raw_tree=e.get("raw_tree", ""),
              files=tuple(tuple(f) for f in e.get("files")), dropped=tuple(e.get("dropped") or ()),
              diff=e.get("diff"), seq=e.seq, last_rejection=prev.last_rejection if prev else None)
    return replace(g, wips=_put(g.wips, w, wip))


def _stall_detected(g: Graph, e: Event) -> Graph:
    s = Stall(e.seq, e.t, e.get("kind"), e.get("action"), e.get("worker"), e.get("task"), e.get("detail", ""))
    return replace(g, stalls=g.stalls + (s,))


# ======================================================================== 验证与存档链

def _job_started(g: Graph, e: Event) -> Graph:
    jid, key = e.get("job"), e.get("key")
    _need(jid not in g.jobs, f"job {jid} exists")
    _need(key not in g.job_keys, f"job key {key} already has job {g.job_keys.get(key)}")
    sel = e.get("selection")
    job = Job(id=jid, key=key, tree=e.get("tree"), selection=None if sel is None else tuple(sel),
              purpose=e.get("purpose"), requested_by=e.actor, attempt=e.get("attempt"), live=bool(e.get("live")),
              tag=e.get("tag", ""), started_t=e.t, replaces=e.get("replaces"))
    g = replace(g, jobs=_put(g.jobs, jid, job), job_keys=_put(g.job_keys, key, jid))
    aid = e.get("attempt")
    if aid is not None:
        _need(aid in g.attempts, f"job for unknown attempt {aid}")
        a = g.attempts[aid]
        _need(a.status == ATT_PENDING, f"job for attempt {aid} in status {a.status}")
        jobs = tuple(j for j in a.jobs if j != e.get("replaces")) + (jid,)
        g = replace(g, attempts=_put(g.attempts, aid, replace(a, jobs=jobs)))
    return g


def _job_finished(g: Graph, e: Event) -> Graph:
    jid = e.get("job")
    _need(jid in g.jobs, f"unknown job {jid}")
    job = g.jobs[jid]
    _need(job.state == JOB_RUNNING, f"job {jid} already {job.state}")
    state = e.get("state")
    _need(state in ("finished", "unknown", "cancelled"), f"bad job state {state}")
    job2 = replace(job, state=state, results=dict(e.get("results") or {}), error=e.get("error", ""),
                   sec=float(e.get("sec") or 0.0), finished_t=e.t)
    keys = g.job_keys if state == JOB_FINISHED else _drop(g.job_keys, job.key)
    return replace(g, jobs=_put(g.jobs, jid, job2), job_keys=keys)


def _baseline_recorded(g: Graph, e: Event) -> Graph:
    _need(not g.baseline_ready, "baseline recorded twice")
    return replace(g, baseline=dict(e.get("classes")), baseline_ready=True,
                   baseline_sec=float(e.get("full_sec") or 0.0))


def _checkpoint_attempted(g: Graph, e: Event) -> Graph:
    _running(g)
    aid = e.get("attempt")
    _need(aid not in g.attempts, f"attempt {aid} exists")
    _need(g.baseline_ready, "checkpoint attempted before the baseline")
    _need(e.get("base") == g.head, "attempt base must be the chain head")
    _need(e.get("tree") != g.head_cp.tree, "attempt on the head tree (nothing to checkpoint)")
    for a in g.attempts.values():
        _need(not (a.worker == e.get("worker") and a.status in (ATT_PENDING, ATT_ADVANCING)),
              f"{e.get('worker')} already has attempt {a.id} in progress")
    tasks = tuple(e.get("tasks") or ())
    for tid in tasks:
        t = _task(g, tid)
        _need(t.status == REVIEW and t.review_attempt is None and t.review_checkpoint is None,
              f"attempt task {tid} is not awaiting review")
        g = _set_task(g, replace(t, review_attempt=aid))
    sel = e.get("selection")
    a = Attempt(id=aid, worker=e.get("worker"), trigger=e.get("trigger"), tree=e.get("tree"), base=e.get("base"),
                tier=e.get("tier"), selection=None if sel is None else tuple(sel), tasks=tasks,
                summary=e.get("summary", ""), raw_tree=e.get("raw_tree", ""), created_seq=e.seq)
    return replace(g, attempts=_put(g.attempts, aid, a))


def _checkpoint_advancing(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown attempt {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_PENDING, f"attempt {aid} is {a.status}")
    _need(a.base == g.head, f"attempt {aid} is based on {a.base}, head is {g.head}")
    _need(e.get("parent_commit") == g.head_cp.commit, "parent commit must be the head commit")
    _need(not any(x.status == ATT_ADVANCING for x in g.attempts.values()), "another attempt is advancing")
    a2 = replace(a, status=ATT_ADVANCING, parent_commit=e.get("parent_commit"), date=float(e.get("date")),
                 flaky=tuple(e.get("flaky") or ()))
    return replace(g, attempts=_put(g.attempts, aid, a2))


def _checkpoint_created(g: Graph, e: Event) -> Graph:
    cid = int(e.get("checkpoint"))
    _need(cid not in g.checkpoints, f"checkpoint {cid} exists")
    files = tuple(tuple(f) for f in (e.get("files") or ()))
    if cid == 0:
        _need(not g.checkpoints, "checkpoint 0 is the first checkpoint")
        cp = Checkpoint(0, e.get("commit"), e.get("tree"), None, e.seq, e.t)
        return replace(g, checkpoints={0: cp}, head=0)
    _running(g)
    aid = e.get("attempt")
    _need(aid in g.attempts, f"checkpoint {cid} from unknown attempt {aid}")
    a = g.attempts[aid]
    _need(a.status == ATT_ADVANCING, f"attempt {aid} is {a.status}, not advancing")
    _need(a.base == g.head, "CAS: attempt base is no longer the head")
    _need(e.get("tree") == a.tree, "checkpoint tree differs from the verified tree")
    _need(cid == max(g.checkpoints) + 1, f"checkpoint ids are sequential (got {cid})")
    held = tuple(sorted({l.task for l in g.leases.values() if l.worker == a.worker} | set(a.tasks)))
    cp = Checkpoint(cid, e.get("commit"), a.tree, g.head, e.seq, e.t, attempt=aid, tier=a.tier, trigger=a.trigger,
                    files=files, tasks=held)
    g = replace(g, checkpoints=_put(g.checkpoints, cid, cp), head=cid,
                attempts=_put(g.attempts, aid, replace(a, status=ATT_CREATED, checkpoint=cid)))
    for tid in a.tasks:
        t = g.tasks[tid]
        if t.status == REVIEW and t.review_attempt == aid:
            g = _set_task(g, replace(t, review_checkpoint=cid))
    wip = g.wips.get(a.worker)
    if wip is not None and wip.tree == a.tree:      # 存进去的正是当前的 WIP：它相对新存档没有未验证的改动了
        g = replace(g, wips=_put(g.wips, a.worker, replace(wip, base=cid, files=(), diff=None, last_rejection=None)))
    return _progress(g, e, a.worker)


def _checkpoint_rejected(g: Graph, e: Event) -> Graph:
    aid = e.get("attempt")
    _need(aid in g.attempts, f"unknown attempt {aid}")
    a = g.attempts[aid]
    _need(a.status in (ATT_PENDING, ATT_ADVANCING), f"attempt {aid} is {a.status}")
    regs = tuple(e.get("regressions") or ())
    a2 = replace(a, status=ATT_REJECTED, regressions=regs, flaky=tuple(e.get("flaky") or ()), reason=e.get("reason"))
    g = replace(g, attempts=_put(g.attempts, aid, a2))
    wip = g.wips.get(a.worker)
    if wip is not None:
        rej = {"attempt": aid, "seq": e.seq, "reason": e.get("reason"), "regressions": list(regs[:50]),
               "n_regressions": len(regs), "flaky": list(a2.flaky[:20]), "detail": e.get("detail", "")}
        g = replace(g, wips=_put(g.wips, a.worker, replace(wip, last_rejection=rej)))
    return g


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
    return replace(g, checkpoints=cps, head=to)


HANDLERS: dict[str, Callable[[Graph, Event], Graph]] = {
    "run_started": _run_started, "runtime_recovered": _runtime_recovered, "deadline_reserve": _deadline_reserve,
    "delivered": _delivered,
    "plan_proposed": _plan_proposed, "requirement_frozen": _requirement_frozen, "task_added": _task_added,
    "task_split": _task_split, "task_claimed": _task_claimed, "task_released": _task_released,
    "review_requested": _review_requested, "task_done": _task_done, "task_blocked": _task_blocked,
    "task_reopened": _task_reopened,
    "session_started": _session_started, "session_ended": _session_ended, "compacted": _compacted,
    "lease_renewed": _lease_renewed, "lease_expired": _lease_expired, "note": _note, "wip_recorded": _wip_recorded,
    "stall_detected": _stall_detected,
    "job_started": _job_started, "job_finished": _job_finished, "baseline_recorded": _baseline_recorded,
    "checkpoint_attempted": _checkpoint_attempted, "checkpoint_advancing": _checkpoint_advancing,
    "checkpoint_created": _checkpoint_created, "checkpoint_rejected": _checkpoint_rejected, "rollback": _rollback,
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

"""状态转换规则：输入（worker 请求、观察、时钟）→ 事件。全部是纯函数。

每个规则函数的第一个参数是 Tx：规则在 Tx 上 emit 事件，Tx 立刻把事件应用到自己的图副本上，
所以同一个规则里后面的判断看到的是前面事件之后的状态（例如“存档创建后立即判定待验证的任务”）。
runtime 在锁里调用规则，成功后把 tx.events 原样追加到日志；规则抛出 Rejected 时整个 Tx 被丢弃。

自述可以发起转换（认领、放弃、声明做完、报告受阻），但“完成”和“存档”只能由观察到的证据完成：
task_done 只由 evaluate_review 在存档那棵树的作业结果上判定，checkpoint_created 只在 CAS 成功后由观察写入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from belay.core.config import BelayConfig
from belay.core.events import COMPACTOR, LLM, OBSERVED, PLANNER, RULE, RUNTIME, SELF_REPORT, Event, worker_actor
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_PENDING, ATT_REJECTED, BLOCKED, DONE, DONE_UNVERIFIED,
                              FINISHED, JOB_CANCELLED, JOB_FINISHED, JOB_RUNNING, JOB_UNKNOWN, OPEN, REVIEW,
                              RUN_RUNNING, SPLIT, Graph)
from belay.core.plan import normalize_ws, quote_in_text, validate_split
from belay.core.queries import (chain, consecutive_crashes, held_tasks, last_session, next_id, num,
                                open_attempt, remaining_sec, requirements_covered, reserve_sec,
                                sessions_without_progress, workable)
from belay.core.reduce import apply
from belay.core.suggest import suggestion_rank
from belay.core.verify import (PASSED, classify_baseline, failure_signature, finished_covers, full_verified,
                               guard_in_selection, guard_set, head_full_ok, is_cmd, job_key, regression_ids,
                               regressions, related_units, results_for_tree, running_covers, test_files_of, units)

BLOCK_KINDS = ("insufficient_info", "environment", "check_conflict")


class Rejected(Exception):
    """请求不被接受：原因直接返回给 worker。"""


class Tx:
    def __init__(self, g: Graph, now: float, cfg: BelayConfig):
        self.g = g
        self.now = now
        self.cfg = cfg
        self.events: list[Event] = []

    def emit(self, type_: str, actor: str, source: str, **payload) -> Event:
        e = Event(self.g.seq + 1, self.now, type_, actor, source, payload)
        self.g = apply(self.g, e)
        self.events.append(e)
        return e


@dataclass(frozen=True)
class TreeObs:
    """外壳对工作区的一次观察（git 计算）。"""
    tree: str                                   # 剔除测试路径改动后的候选树
    raw_tree: str                               # 工作区原样的树
    files: tuple = ()                           # 候选相对链头的改动 ((路径, 增, 删), ...)
    dropped: tuple = ()                         # 被剔除的测试路径改动
    diff: Optional[str] = None                  # 完整 diff 的附件路径


# ======================================================================== 运行与准备

def start_run(tx: Tx, run_id: str, task: str, budget_sec: float, workers: Iterable[str] = ("w1",),
              public_checks: Iterable[str] = (), verifier: bool = True) -> None:
    tx.emit("run_started", RUNTIME, RULE, run_id=run_id, task=task, budget_sec=float(budget_sec),
            deadline_t=tx.now + float(budget_sec), workers=list(workers), public_checks=list(public_checks),
            verifier=verifier)


def create_base(tx: Tx, commit: str, tree: str) -> None:
    tx.emit("checkpoint_created", RUNTIME, OBSERVED, checkpoint=0, commit=commit, tree=tree, trigger="baseline")


def ensure_job(tx: Tx, tree: str, selection: Optional[Iterable[str]], purpose: str, actor: str = RUNTIME,
               attempt: Optional[str] = None, live: bool = False, tag: str = "") -> str:
    """同一 (树, 检查集合, 标签) 只跑一次：已有运行中或已完成的作业就复用。"""
    sel = None if selection is None else tuple(sorted(set(selection)))
    key = job_key(tree, sel, tag)
    existing = tx.g.job_keys.get(key)
    if existing is not None:
        return existing
    jid = next_id("J", tx.g.jobs)
    payload = dict(job=jid, key=key, tree=tree, selection=None if sel is None else list(sel), purpose=purpose,
                   live=live, tag=tag)
    if attempt is not None and tx.g.attempts.get(attempt) and tx.g.attempts[attempt].status == ATT_PENDING:
        payload["attempt"] = attempt
    tx.emit("job_started", actor, RULE, **payload)
    return jid


def record_baseline(tx: Tx, job1: Optional[str], job2: Optional[str], reason: str = "") -> None:
    """两次全量运行的结果归类；没有验证器或两次都没跑出结果时，基线为空（所有存档都“未验证”）。"""
    g = tx.g
    j1, j2 = g.jobs.get(job1) if job1 else None, g.jobs.get(job2) if job2 else None
    classes: dict[str, str] = {}
    errors = []
    if j1 is not None and j2 is not None:
        classes = classify_baseline(j1.results, j2.results)
        errors = [j.error for j in (j1, j2) if j.error]
    available = bool(classes)
    full_sec = max((j.sec for j in (j1, j2) if j is not None), default=0.0)
    tx.emit("baseline_recorded", RUNTIME, OBSERVED, classes=classes, available=available, full_sec=full_sec,
            reason=reason or ("; ".join(e[:300] for e in errors) if not available else ""))


def known_checks(g: Graph) -> set[str]:
    return set(g.baseline) | set(g.run.public_checks if g.run else ())


def propose_plan(tx: Tx, round_: int, proposal: dict, valid: bool, problems: list[str], source: str = LLM,
                 purpose: str = "initial", warnings: Optional[list[str]] = None) -> None:
    tx.emit("plan_proposed", PLANNER, source, round=round_, valid=valid, problems=list(problems)[:50],
            warnings=list(warnings or [])[:50], proposal=proposal, purpose=purpose)


def freeze_plan(tx: Tx, requirements: list[dict], tasks: list[dict], source: str = LLM) -> None:
    """需求一次性冻结；初始任务按拓扑顺序加入（调用方已校验）。"""
    tx.emit("requirement_frozen", PLANNER, RULE,
            requirements=[{**r, "origin": "llm" if source == LLM else "rule"} for r in requirements])
    for t in tasks:
        tx.emit("task_added", PLANNER, source, task=t["id"], title=t["title"], description=t.get("description", ""),
                links=list(t["links"]), blocked_by=list(t.get("blocked_by") or []),
                priority=int(t.get("priority") or 0), checks=list(t.get("checks") or []))


# ======================================================================== worker 的请求

def _held_active(g: Graph, worker: str, task_id: str):
    t = g.tasks.get(task_id)
    if t is None:
        raise Rejected(f"Unknown task {task_id}.")
    lease = g.leases.get(task_id)
    if lease is None or lease.worker != worker or t.status != ACTIVE:
        state = f"held by {lease.worker}" if lease else t.status
        raise Rejected(f"You do not hold {task_id} as an active task (it is {state}). Claim it first.")
    return t


def claim(tx: Tx, worker: str, task_id: str) -> str:
    g, cfg = tx.g, tx.cfg
    t = g.tasks.get(task_id)
    if t is None:
        raise Rejected(f"Unknown task {task_id}.")
    lease = g.leases.get(task_id)
    if lease is not None:
        if lease.worker == worker:
            return "already"
        raise Rejected(f"{task_id} is held by {lease.worker}.")
    if t.status == SPLIT:
        raise Rejected(f"{task_id} was split into {', '.join(t.children)}; claim one of those.")
    if t.status == DONE:
        raise Rejected(f"{task_id} is done: its checks passed on checkpoint {t.done_checkpoint}.")
    blockers = [d for d in t.blocked_by if g.tasks[d].status not in FINISHED]
    if blockers:
        raise Rejected(f"{task_id} is blocked by unfinished task(s) {', '.join(blockers)}.")
    rank = suggestion_rank(g, worker, task_id, tx.now, cfg)
    if t.status in (BLOCKED, DONE_UNVERIFIED):
        tx.emit("task_reopened", worker_actor(worker), RULE, task=task_id, reason="reclaimed")
    tx.emit("task_claimed", worker_actor(worker), RULE, task=task_id, worker=worker,
            expires_t=tx.now + cfg.lease_ttl_sec, suggested_rank=rank)
    return "claimed"


def release(tx: Tx, worker: str, task_id: str, note: str = "") -> None:
    _held_active(tx.g, worker, task_id)
    tx.emit("task_released", worker_actor(worker), RULE, task=task_id, worker=worker)
    if note.strip():
        tx.emit("note", worker_actor(worker), SELF_REPORT, worker=worker, kind="note", task=task_id,
                text=f"[released {task_id}] {note.strip()}")


def add_task(tx: Tx, worker: str, title: str, links: Iterable[str], description: str = "",
             blocked_by: Iterable[str] = (), discovered_from: Optional[str] = None,
             checks: Iterable[str] = ()) -> str:
    g = tx.g
    title = (title or "").strip()
    if not title:
        raise Rejected("A task needs a title.")
    links = [str(x) for x in links]
    if not links:
        raise Rejected("Link the task to at least one requirement (e.g. links=[\"R2\"]).")
    bad = [x for x in links if x not in g.requirements]
    if bad:
        raise Rejected(f"Unknown requirement(s) {bad}. Requirements: {', '.join(sorted(g.requirements, key=num))}.")
    blocked_by = [str(x) for x in blocked_by]
    bad = [x for x in blocked_by if x not in g.tasks]
    if bad:
        raise Rejected(f"Unknown task(s) in blocked_by: {bad}.")
    checks = [str(c) for c in checks]
    bad = [c for c in checks if c not in known_checks(g)]
    if bad:
        raise Rejected(f"Unknown check(s) {bad[:5]}: a task can only be verified by checks that exist on the "
                       "original code (tests you write yourself are development signals, not verification).")
    if discovered_from is None:
        held = [t.id for t in held_tasks(g, worker) if t.status == ACTIVE]
        discovered_from = held[0] if held else None
    elif discovered_from not in g.tasks:
        raise Rejected(f"Unknown task {discovered_from}.")
    tid = next_id("T", g.tasks)
    tx.emit("task_added", worker_actor(worker), SELF_REPORT, task=tid, title=title[:200],
            description=(description or "")[:2000], links=links, blocked_by=blocked_by,
            discovered_from=discovered_from, checks=checks)
    return tid


def split_task(tx: Tx, task_id: str, children: list[dict], actor: str = PLANNER, source: str = LLM) -> list[str]:
    g = tx.g
    parent = g.tasks.get(task_id)
    if parent is None or parent.status not in (OPEN, ACTIVE, BLOCKED):
        raise Rejected(f"{task_id} cannot be split now.")
    clean, problems = validate_split(parent.links, children, g.requirements, known_checks(g))
    if problems:
        raise Rejected("; ".join(problems))
    start = num(next_id("T", g.tasks))
    specs = []
    for i, c in enumerate(clean):
        specs.append({**c, "id": f"T{start + i}", "blocked_by": list(parent.blocked_by)})
    tx.emit("task_split", actor, source, task=task_id, children=specs)
    return [s["id"] for s in specs]


def note(tx: Tx, worker: str, text: str, kind: str = "note") -> None:
    text = (text or "").strip()
    if not text:
        raise Rejected("Empty note.")
    tx.emit("note", worker_actor(worker), SELF_REPORT, worker=worker, kind=kind, text=text[:4000])


def report_blocked(tx: Tx, worker: str, task_id: str, kind: str, reason: str, quote: Optional[str] = None) -> None:
    g = tx.g
    t = g.tasks.get(task_id)
    if t is None:
        raise Rejected(f"Unknown task {task_id}.")
    if kind not in BLOCK_KINDS:
        raise Rejected(f"kind must be one of {', '.join(BLOCK_KINDS)}.")
    if not (reason or "").strip():
        raise Rejected("Give a reason.")
    lease = g.leases.get(task_id)
    if lease is not None and lease.worker != worker:
        raise Rejected(f"{task_id} is held by {lease.worker}.")
    if t.status not in (OPEN, ACTIVE, REVIEW):
        raise Rejected(f"{task_id} is {t.status}; only unfinished tasks can be reported blocked.")
    if t.status == REVIEW and t.review_checkpoint is None and t.review_attempt is not None:
        raise Rejected(f"{task_id} is being checked right now; wait for the result.")
    if kind == "check_conflict":
        if not quote or not quote_in_text(quote, g.run.task):
            raise Rejected("A check_conflict report must quote the task text verbatim (quote=...). The conflict is "
                           "recorded, not waived: the regression gate does not change.")
    tx.emit("task_blocked", worker_actor(worker), SELF_REPORT, task=task_id, kind=kind, reason=reason.strip()[:2000],
            quote=normalize_ws(quote)[:1000] if quote else None)


# ======================================================================== 存档：验证后比较并交换

def record_wip(tx: Tx, worker: str, obs: TreeObs) -> None:
    g = tx.g
    w = g.wips.get(worker)
    if w is not None and w.tree == obs.tree and w.raw_tree == obs.raw_tree and w.base == g.head:
        return
    tx.emit("wip_recorded", RUNTIME, OBSERVED, worker=worker, base=g.head, tree=obs.tree, raw_tree=obs.raw_tree,
            files=[list(f) for f in obs.files], dropped=list(obs.dropped), diff=obs.diff)


def _selection(g: Graph, cfg: BelayConfig, tier: str, files: Iterable[str], extra_checks: Iterable[str]):
    """返回 (档位, 选择)。related 找不到相关测试或改动可能影响全局时升级为 full。"""
    if tier == "related":
        sel, _why = related_units(list(files), test_files_of(g.baseline))
        if sel is None:
            return "full", None
        cmd_guard = [c for c in guard_set(g.baseline) if is_cmd(c)]      # 公开检查通常便宜：总是带上
        return "related", tuple(sorted(set(sel) | set(units(extra_checks)) | set(cmd_guard)))
    return "full", None


def request_checkpoint(tx: Tx, worker: str, obs: TreeObs, trigger: str, tasks: Iterable[str] = (),
                       tier: Optional[str] = None, summary: str = "") -> Optional[str]:
    """发起一次存档尝试；候选与链头相同时返回 None（没有要存的东西）。"""
    g = tx.g
    if not g.baseline_ready or g.head is None:
        raise Rejected("The harness is still setting up; try again shortly.")
    if open_attempt(g, worker) is not None:
        raise Rejected("A checkpoint of your work is already in progress.")
    record_wip(tx, worker, obs)
    g = tx.g
    if obs.tree == g.head_cp.tree:
        return None
    tasks = tuple(tasks)
    extra = [c for tid in tasks for c in g.tasks[tid].checks]
    tier, selection = _selection(g, tx.cfg, tier or tx.cfg.checkpoint_tier, [f[0] for f in obs.files], extra)
    aid = next_id("A", g.attempts)
    tx.emit("checkpoint_attempted", worker_actor(worker) if trigger in ("worker", "review") else RUNTIME, RULE,
            attempt=aid, worker=worker, trigger=trigger, tree=obs.tree, raw_tree=obs.raw_tree, base=g.head,
            tier=tier, selection=None if selection is None else list(selection), tasks=list(tasks),
            summary=(summary or "")[:2000])
    advance_attempt(tx, aid)
    return aid


def _raw_results(g: Graph, tree: str) -> dict[str, str]:
    """不含确认重跑的结果（用来区分“确认过的回归”和“不稳定”）。"""
    out: dict[str, str] = {}
    for j in sorted(g.jobs.values(), key=lambda j: num(j.id)):
        if j.tree == tree and not j.live and j.state == JOB_FINISHED and j.tag != "confirm":
            out.update(j.results)
    return out


def _confirm_covers(g: Graph, tree: str, needed: list[str], state: str) -> bool:
    return any(j.tree == tree and j.tag == "confirm" and j.state == state and
               (j.selection is None or set(needed) <= set(j.selection)) for j in g.jobs.values())


def advance_attempt(tx: Tx, aid: str) -> None:
    """把一次存档尝试尽量往前推：缺结果就起作业；有回归先确认；然后决定推进或拒绝。"""
    g, cfg = tx.g, tx.cfg
    a = g.attempts[aid]
    if a.status != ATT_PENDING:
        return
    needed = list(a.selection) if a.selection is not None else None
    if needed is not None and not needed:
        return _decide(tx, aid, (), ())
    have = full_verified(g, a.tree) if needed is None else finished_covers(g, a.tree, needed)
    if not have:
        busy = any(j.tree == a.tree and j.state == JOB_RUNNING and not j.live and j.selection is None
                   for j in g.jobs.values()) if needed is None else running_covers(g, a.tree, needed)
        if not busy:
            ensure_job(tx, a.tree, a.selection, "verify", attempt=aid)
        return
    expected = guard_in_selection(guard_set(g.baseline), a.selection)
    regs_raw = regressions(expected, _raw_results(g, a.tree))
    if regs_raw and cfg.confirm_regressions:
        cu = list(units(regression_ids(regs_raw)))
        if not _confirm_covers(g, a.tree, cu, JOB_FINISHED):
            if not _confirm_covers(g, a.tree, cu, JOB_RUNNING):
                ensure_job(tx, a.tree, cu, "confirm", attempt=aid, tag="confirm")
            return
    regs = regressions(expected, results_for_tree(g, a.tree))
    flaky = sorted(set(regression_ids(regs_raw)) - set(regression_ids(regs)))
    _decide(tx, aid, regs, tuple(flaky))


def _decide(tx: Tx, aid: str, regs: tuple, flaky: tuple) -> None:
    g = tx.g
    a = g.attempts[aid]
    if regs:
        errors = sorted({j.error[:300] for j in g.jobs.values() if j.tree == a.tree and j.error and not j.live})
        tx.emit("checkpoint_rejected", RUNTIME, RULE, attempt=aid, regressions=list(regs), flaky=list(flaky),
                reason="regression", detail="; ".join(errors)[:1000])
        _reopen_attempt_tasks(tx, aid, "checkpoint_rejected", regs)
    else:
        tx.emit("checkpoint_advancing", RUNTIME, RULE, attempt=aid, parent_commit=g.head_cp.commit, date=tx.now,
                flaky=list(flaky))


def _reopen_attempt_tasks(tx: Tx, aid: str, reason: str, failures: Iterable[str]) -> None:
    a = tx.g.attempts[aid]
    for tid in a.tasks:
        t = tx.g.tasks[tid]
        if t.status == REVIEW and t.review_attempt == aid:
            tx.emit("task_reopened", RUNTIME, RULE, task=tid, reason=reason, failures=list(failures)[:50],
                    attempt=aid)


def ref_advanced(tx: Tx, aid: str, ok: bool, commit: str = "", files: Iterable = (), detail: str = "") -> None:
    """外壳做完 commit-tree + update-ref 之后的观察。重复到达（恢复时）会被忽略。"""
    a = tx.g.attempts.get(aid)
    if a is None or a.status != ATT_ADVANCING:
        return
    if ok:
        cid = max(tx.g.checkpoints) + 1
        tx.emit("checkpoint_created", RUNTIME, OBSERVED, checkpoint=cid, attempt=aid, commit=commit, tree=a.tree,
                files=[list(f) for f in files])
        for tid in a.tasks:
            evaluate_review(tx, tid)
    else:
        tx.emit("checkpoint_rejected", RUNTIME, OBSERVED, attempt=aid, regressions=[], reason="cas_conflict",
                detail=detail[:1000])
        _reopen_attempt_tasks(tx, aid, "checkpoint_rejected", [f"cas_conflict: {detail[:200]}"])


def abort_attempts(tx: Tx, reason: str) -> None:
    """截止时仍在验证的尝试：拒绝（链不动）。正在 CAS 的尝试不能中止，由外壳做完。"""
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING:
            tx.emit("checkpoint_rejected", RUNTIME, RULE, attempt=a.id, regressions=[], reason=reason)
            _reopen_attempt_tasks(tx, a.id, "checkpoint_rejected", [reason])


# ======================================================================== 待验证 → 完成

def request_review(tx: Tx, worker: str, task_id: str, obs: TreeObs, summary: str = "") -> Optional[str]:
    g = tx.g
    _held_active(g, worker, task_id)
    if not g.baseline_ready or g.head is None:
        raise Rejected("The harness is still setting up; try again shortly.")
    if open_attempt(g, worker) is not None:
        raise Rejected("A checkpoint of your work is already in progress.")
    record_wip(tx, worker, obs)
    if obs.tree == tx.g.head_cp.tree:
        tx.emit("review_requested", worker_actor(worker), RULE, task=task_id, worker=worker, checkpoint=tx.g.head)
        evaluate_review(tx, task_id)
        return None
    tx.emit("review_requested", worker_actor(worker), RULE, task=task_id, worker=worker)
    return request_checkpoint(tx, worker, obs, "review", tasks=(task_id,), summary=summary)


def evaluate_review(tx: Tx, task_id: str) -> None:
    """在任务的存档那棵树上看它的检查：全部 PASSED → done；失败 → 重开；缺结果 → 起 evidence 作业。"""
    g = tx.g
    t = g.tasks.get(task_id)
    if t is None or t.status != REVIEW or t.review_checkpoint is None:
        return
    cp = g.checkpoints[t.review_checkpoint]
    if not t.checks:
        tx.emit("task_done", RUNTIME, RULE, task=task_id, checkpoint=cp.id, verified=False)
        return
    res = results_for_tree(g, cp.tree)
    missing = [c for c in t.checks if c not in res]
    need = list(units(missing))
    if missing and not finished_covers(g, cp.tree, need):
        if not running_covers(g, cp.tree, need):
            ensure_job(tx, cp.tree, need, "evidence")
        return
    failing = [f"{c} ({res.get(c, 'MISSING')})" for c in t.checks if res.get(c) != PASSED]
    if failing:
        tx.emit("task_reopened", RUNTIME, RULE, task=task_id, reason="evidence_failed", failures=failing[:50],
                checkpoint=cp.id)
    else:
        tx.emit("task_done", RUNTIME, RULE, task=task_id, checkpoint=cp.id, verified=True,
                evidence={c: res[c] for c in t.checks})


# ======================================================================== 作业

def run_check(tx: Tx, worker: str, raw_tree: str, tests: Iterable[str] = (), full: bool = False,
              changed: Iterable[str] = ()) -> str:
    """worker 的开发检查：在活的工作区上跑（结果只是开发信号），同一 (树, 选择) 复用结果。"""
    g = tx.g
    tests = [str(x) for x in tests]
    if full:
        sel = None
    elif tests:
        sel = tuple(sorted(set(tests)))          # 开发检查可以直接给 node id
    else:
        sel, _ = related_units(list(changed), test_files_of(g.baseline))
        if sel == ():
            raise Rejected("No test is related to your changes; pass tests=[...] or full=true.")
    return ensure_job(tx, raw_tree, sel, "dev", actor=worker_actor(worker), live=True, tag="dev")


def job_finished(tx: Tx, job_id: str, state: str, results: dict, sec: float = 0.0, error: str = "") -> None:
    """作业结果（观察）→ 级联：重跑丢失的作业、推进在等结果的存档尝试、判定在等证据的任务。"""
    job = tx.g.jobs.get(job_id)
    if job is None or job.state != JOB_RUNNING:
        return
    tx.emit("job_finished", "verifier", OBSERVED, job=job_id, state=state, results=dict(results), sec=float(sec),
            error=(error or "")[:2000])
    job = tx.g.jobs[job_id]
    if tx.g.run is None or tx.g.run.status != RUN_RUNNING:
        return
    if state == JOB_CANCELLED:
        # 只在收尾时取消作业：依赖它的尝试不再重跑，直接拒绝（链不动）
        for a in list(tx.g.attempts.values()):
            if a.status == ATT_PENDING and a.tree == job.tree and not job.live:
                tx.emit("checkpoint_rejected", RUNTIME, RULE, attempt=a.id, regressions=[], reason="cancelled")
                _reopen_attempt_tasks(tx, a.id, "checkpoint_rejected", ["verification cancelled"])
        return
    if state == JOB_UNKNOWN and not job.live:
        att = job.attempt if job.attempt and tx.g.attempts[job.attempt].status == ATT_PENDING else None
        ensure_job(tx, job.tree, job.selection, job.purpose, attempt=att, tag=job.tag)
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING:
            advance_attempt(tx, a.id)
    for t in list(tx.g.tasks.values()):
        if t.status == REVIEW and t.review_checkpoint is not None:
            evaluate_review(tx, t.id)
    if tx.g.head_cp is not None and job.tree == tx.g.head_cp.tree and not job.live and job.purpose == "evidence":
        verify_head(tx)


def verify_head(tx: Tx) -> bool:
    """链头的全量验证（收尾时）。返回 True 表示还在等作业。"""
    g, cfg = tx.g, tx.cfg
    cp = g.head_cp
    guard = guard_set(g.baseline)
    if cp is None or cp.id == 0 or not guard:
        return False
    if not full_verified(g, cp.tree):
        busy = any(j.tree == cp.tree and j.state == JOB_RUNNING and j.selection is None and not j.live
                   for j in g.jobs.values())
        if not busy:
            ensure_job(tx, cp.tree, None, "evidence")
        return True
    regs_raw = regressions(sorted(guard), _raw_results(g, cp.tree))
    if regs_raw and cfg.confirm_regressions:
        cu = list(units(regression_ids(regs_raw)))
        if not _confirm_covers(g, cp.tree, cu, JOB_FINISHED):
            if not _confirm_covers(g, cp.tree, cu, JOB_RUNNING):
                ensure_job(tx, cp.tree, cu, "confirm", tag="confirm")
            return True
    return False


# ======================================================================== 回退

def rollback(tx: Tx, worker: str, to: Optional[int] = None) -> int:
    g = tx.g
    to = g.head if to is None else int(to)
    ids = [cp.id for cp in chain(g)]
    if to not in ids:
        raise Rejected(f"Checkpoint {to} is not on the checkpoint chain ({', '.join(map(str, ids))}).")
    if open_attempt(g) is not None:
        raise Rejected("A checkpoint is in progress; roll back after it finishes.")
    abandoned = ids[:ids.index(to)]
    for t in list(g.tasks.values()):
        if (t.status in FINISHED and t.done_checkpoint in abandoned) or \
                (t.status == REVIEW and t.review_checkpoint in abandoned):
            tx.emit("task_reopened", worker_actor(worker), RULE, task=t.id, reason="rolled_back",
                    failures=[f"checkpoint {t.done_checkpoint or t.review_checkpoint} was rolled back"])
    cp = tx.g.checkpoints[to]
    tx.emit("rollback", worker_actor(worker), RULE, worker=worker, to=to, abandoned=abandoned, tree=cp.tree,
            commit=cp.commit)
    return to


# ======================================================================== 心跳、时钟、停滞

def heartbeat(tx: Tx, worker: str) -> None:
    """任何工具调用都算心跳；租约剩余不到一半时才写续期事件，避免日志膨胀。"""
    ttl = tx.cfg.lease_ttl_sec
    for lease in list(tx.g.leases.values()):
        if lease.worker == worker and lease.expires_t - tx.now < ttl / 2:
            tx.emit("lease_renewed", RUNTIME, RULE, task=lease.task, worker=worker, expires_t=tx.now + ttl)


def tick(tx: Tx) -> None:
    g, cfg = tx.g, tx.cfg
    if g.run is None or g.run.status != RUN_RUNNING:
        return
    for lease in list(g.leases.values()):
        if lease.expires_t <= tx.now and g.tasks[lease.task].status == ACTIVE and \
                g.workers[lease.worker].session is None:
            tx.emit("lease_expired", RUNTIME, RULE, task=lease.task, worker=lease.worker)
    if not tx.g.run.reserve and g.frozen and remaining_sec(tx.g, tx.now) <= reserve_sec(tx.g, cfg):
        tx.emit("deadline_reserve", RUNTIME, RULE, reserve_sec=reserve_sec(tx.g, cfg))
    if cfg.stall:
        detect_stalls(tx)


def detect_stalls(tx: Tx) -> None:
    g, cfg = tx.g, tx.cfg
    since = [s for s in g.stalls if s.seq > g.last_progress_seq]
    for w, ws in sorted(g.workers.items()):
        if ws.session is None:
            continue
        held = [t.id for t in held_tasks(g, w) if t.status == ACTIVE]
        task = held[0] if held else None
        action = "replan" if (since and task) else "hint"
        idle_since = g.last_progress_t
        if tx.now - idle_since > cfg.stall_no_progress_sec and not any(x.kind == "no_progress" for x in since):
            tx.emit("stall_detected", RUNTIME, RULE, kind="no_progress", action=action, worker=w, task=task,
                    detail=f"no new checkpoint or finished task for {int((tx.now - idle_since) / 60)} min")
            return
        mine = sorted((a for a in g.attempts.values() if a.worker == w and a.status in (ATT_REJECTED, "created")),
                      key=lambda a: a.created_seq)[-cfg.stall_same_failure:]
        if len(mine) == cfg.stall_same_failure and all(a.status == ATT_REJECTED and a.regressions for a in mine):
            sigs = {failure_signature(a.regressions) for a in mine}
            if len(sigs) == 1:
                sig = sigs.pop()
                if not any(x.kind == "repeated_failure" and sig in x.detail for x in since):
                    tx.emit("stall_detected", RUNTIME, RULE, kind="repeated_failure", action=action, worker=w,
                            task=task, detail=f"signature {sig}: the same {len(mine[-1].regressions)} regression(s) "
                                              f"rejected {len(mine)} checkpoints in a row")
                    return


# ======================================================================== 会话

def session_reason(g: Graph, worker: str) -> str:
    s = last_session(g, worker)
    if s is None:
        return "first"
    return {"handoff": "handoff", "crash": "crash", "runtime_crash": "recover", "stuck": "restart"}.get(
        s.end_reason or "", "restart")


def start_session(tx: Tx, worker: str, reason: str, opening: dict, transcript: Optional[str] = None) -> str:
    sid = next_id("S", tx.g.sessions)
    tx.emit("session_started", RUNTIME, RULE, session=sid, worker=worker, reason=reason, opening=opening,
            transcript=transcript)
    for lease in list(tx.g.leases.values()):
        if lease.worker == worker:
            tx.emit("lease_renewed", RUNTIME, RULE, task=lease.task, worker=worker,
                    expires_t=tx.now + tx.cfg.lease_ttl_sec)
    return sid


def end_session(tx: Tx, worker: str, reason: str, peak_context: int = 0, turns: int = 0,
                error: Optional[str] = None, todos: Optional[list[dict]] = None) -> Optional[str]:
    ws = tx.g.workers.get(worker)
    if ws is None or ws.session is None:
        return None
    sid = ws.session
    if todos:
        mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
        text = "\n".join(f"{mark.get(t.get('status'), '[ ]')} {t.get('content', '')}" for t in todos)
        tx.emit("note", worker_actor(worker), SELF_REPORT, worker=worker, kind="todos", text=text[:4000])
    tx.emit("session_ended", RUNTIME, OBSERVED, session=sid, worker=worker, reason=reason,
            peak_context=int(peak_context), turns=int(turns), error=(error or None) and error[:2000])
    return sid


def record_compaction(tx: Tx, worker: str, level: int, before: int, after: int,
                      summary: Optional[str] = None) -> None:
    ws = tx.g.workers[worker]
    if ws.session is None:
        return
    tx.emit("compacted", COMPACTOR, LLM if summary else RULE, session=ws.session, level=level, before=int(before),
            after=int(after), summary=summary)


def recovered(tx: Tx, downtime_sec: float, reconciled: dict) -> None:
    tx.emit("runtime_recovered", RUNTIME, OBSERVED, downtime_sec=max(0.0, float(downtime_sec)), reconciled=reconciled)


# ======================================================================== 运行的结束

def next_step(g: Graph, worker: str, now: float, cfg: BelayConfig) -> tuple[str, str]:
    """会话结束不等于运行结束。返回 (动作, 理由)：stop | finalize | start_session | wait。"""
    if g.run is None or g.run.status != RUN_RUNNING:
        return "stop", "delivered"
    if g.run.reserve or remaining_sec(g, now) <= reserve_sec(g, cfg):
        return "finalize", "deadline"
    if not g.frozen or not g.baseline_ready:
        return "wait", "setup"
    if open_attempt(g) is not None:
        return "wait", "checkpoint in progress"
    if any(t.status == REVIEW for t in g.tasks.values()):
        return "wait", "evidence in progress"
    if consecutive_crashes(g, worker) >= cfg.max_crash_restarts:
        return "finalize", "crashes"
    if not workable(g):
        return ("finalize", "complete") if requirements_covered(g) else ("finalize", "no_workable")
    if sessions_without_progress(g, worker) >= cfg.max_idle_sessions:
        return "finalize", "no_progress"
    return "start_session", session_reason(g, worker)


def final_status(g: Graph) -> str:
    ok = requirements_covered(g) and not workable(g) and head_full_ok(g)
    return "DONE" if ok else "INCOMPLETE"


def deliver(tx: Tx, reason: str) -> str:
    abort_attempts(tx, "cancelled")
    status = final_status(tx.g)
    tx.emit("delivered", RUNTIME, RULE, checkpoint=tx.g.head, status=status, reason=reason,
            head_full_verified=full_verified(tx.g, tx.g.head_cp.tree) if tx.g.head_cp else False)
    return status


def stall_stop(tx: Tx, worker: str) -> None:
    tx.emit("stall_detected", RUNTIME, RULE, kind="sessions_no_progress", action="stop", worker=worker,
            detail=f"{sessions_without_progress(tx.g, worker)} sessions in a row without new evidence")

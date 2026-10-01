"""状态转换规则：输入（worker 请求、观察、时钟）→ 事件。全部是纯函数。

每个规则函数的第一个参数是 Tx：规则在 Tx 上 emit 事件，Tx 立刻把事件应用到自己的图副本上，
所以同一个规则里后面的判断看到的是前面事件之后的状态（例如“存档创建后立即判定待验证的任务”）。
runtime 在锁里调用规则，成功后把 tx.events 原样追加到日志；规则抛出 Rejected 时整个 Tx 被丢弃。

自述可以发起转换（认领、放弃、声明做完、报告受阻、声明步骤完成），但“完成”“存档”“提升”只能由观察到的证据完成：
task_done 只由 evaluate_review 在存档那棵树的作业结果上判定，checkpoint_created / checkpoint_confirmed 只在观察之后写入。
LLM（诊断者、复查者）只能解释、只能收紧（重开任务），不能放宽。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from belay.core.config import BelayConfig
from belay.core.events import (COMPACTOR, DIAGNOSER, LLM, OBSERVED, PLANNER, REVIEWER, RULE, RUNTIME, SELF_REPORT,
                               VERIFIER, Event, worker_actor)
from belay.core.model import (ACTIVE, ATT_ADVANCING, ATT_PENDING, ATT_REJECTED, BLOCKED, CONFIRMED, DONE,
                              DONE_UNVERIFIED, JOB_CANCELLED, JOB_FINISHED, JOB_RUNNING, JOB_UNKNOWN, KIND_AUTO,
                              KIND_FINAL, KIND_HANDOFF, KIND_MILESTONE, KIND_REVIEW, KIND_STEP, LANE_BG, LANE_FG,
                              OPEN, PROVISIONAL, REVIEW, RUN_RUNNING, SPLIT, STEP_ACTIVE, STEP_ANCHORED,
                              STEP_DECLARED, STEP_PLANNED, WHERE_LIVE, WHERE_SLOT, WHERE_WORKSPACE, Graph, Snapshot)
from belay.core.plan import normalize_ws, quote_in_text, validate_split
from belay.core.queries import (all_resolved, chain, chain_ids, consecutive_crashes, current_step, focus_task,
                                held_tasks, is_ancestor, last_session, latest_milestone, latest_snapshot, next_id, num,
                                open_attempt, remaining_sec, reserve_sec, sessions_without_progress,
                                snapshot_contained, snapshots_in_epoch, steps_of, unfinished_deps)
from belay.core.reduce import apply
from belay.core.suggest import suggestion_rank
from belay.core.verify import (PASSED, PT_FAIL, PT_PASS, PT_RUNNING, PT_UNTESTED, check_unit,
                               classify_baseline, failure_signature, finished_covers, full_verified, jobs_by_tree,
                               active_guard, guard_in_selection, guard_set, is_cmd, is_test_path, job_key,
                               point_status, regression_ids, regressions, related_units, results_for_tree,
                               running_covers, suite_layout, test_files_of, units)

BLOCK_KINDS = ("insufficient_info", "environment", "check_conflict")
TRIGGER_KIND = {"worker": KIND_MILESTONE, "review": KIND_REVIEW, "auto": KIND_AUTO, "step": KIND_STEP,
                "handoff": KIND_HANDOFF, "session_end": KIND_HANDOFF, "deadline": KIND_FINAL, "final": KIND_FINAL}
# 后台验证只取这些语义节点拍下的快照；其余快照（写操作、模型跑测试、恢复、回退……）只存不验，供恢复与二分使用。
# 为前台意图拍的快照（手动存档、review、按门自查、收尾）由发起者自己验证。
PRIORITY_SNAPSHOT_REASONS = {"step_done": "step", "handoff": "handoff", "session_end": "session_end"}
HANDOFF_REASONS = ("handoff", "session_end")
# worker 声明“做完了”的存档：只有它们被拒或被降级时才定位、诊断并通知 worker
DECLARED_KINDS = (KIND_MILESTONE, KIND_REVIEW)


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
class SnapObs:
    """外壳对工作区的一次快照（git 计算）。"""
    tree: str                                   # 剔除测试路径改动后的候选树
    raw_tree: str                               # 工作区原样的树
    files: tuple = ()                           # 候选相对链头的改动 ((路径, 增, 删), ...)
    dropped: tuple = ()                         # 被剔除的测试路径改动
    testable: bool = True                       # 预检通过
    commit: str = ""                            # 包住这张快照的提交
    precheck: str = ""
    tool_seq: int = 0
    session: Optional[str] = None


def _running_run(g: Graph) -> bool:
    return g.run is not None and g.run.status == RUN_RUNNING


def _background_ok(g: Graph) -> bool:
    """后台活动（语义节点的验证、提升、定位）只在正常运行、隔离有效时进行。"""
    return _running_run(g) and not g.run.finalizing and not g.run.reserve and g.baseline_ready and not g.degraded


# ======================================================================== 运行与准备

def start_run(tx: Tx, run_id: str, task: str, budget_sec: float, workers: Iterable[str] = ("w1",),
              public_checks: Iterable[str] = (), verifier: bool = True) -> None:
    tx.emit("run_started", RUNTIME, RULE, run_id=run_id, task=task, budget_sec=float(budget_sec),
            deadline_t=tx.now + float(budget_sec), workers=list(workers), public_checks=list(public_checks),
            verifier=verifier)


def start_clock(tx: Tx) -> None:
    """预算从现在开始：deadline = 现在 + 预算。"""
    tx.emit("clock_started", RUNTIME, RULE, deadline_t=tx.now + tx.g.run.budget_sec)


def create_base(tx: Tx, commit: str, tree: str) -> None:
    tx.emit("checkpoint_created", RUNTIME, OBSERVED, checkpoint=0, commit=commit, tree=tree, trigger="baseline")


def ensure_job(tx: Tx, tree: str, selection: Optional[Iterable[str]], purpose: str, actor: str = RUNTIME,
               attempt: Optional[str] = None, live: bool = False, tag: str = "", where: Optional[str] = None,
               locate: Optional[str] = None, checkpoint: Optional[int] = None) -> str:
    """同一 (树, 检查集合, 标签) 只跑一次：已有运行中或已完成的作业就复用。"""
    sel = None if selection is None else tuple(sorted(set(selection)))
    key = job_key(tree, sel, tag)
    existing = tx.g.job_keys.get(key)
    if existing is not None:
        return existing
    if where is None:
        where = WHERE_LIVE if live else (WHERE_WORKSPACE if tx.g.degraded else WHERE_SLOT)
    jid = next_id("J", tx.g.jobs)
    payload = dict(job=jid, key=key, tree=tree, selection=None if sel is None else list(sel), purpose=purpose,
                   live=live, tag=tag, where=where)
    if attempt is not None and tx.g.attempts.get(attempt) and tx.g.attempts[attempt].status == ATT_PENDING:
        payload["attempt"] = attempt
    if locate is not None:
        payload["locate"] = locate
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    tx.emit("job_started", actor, RULE, **payload)
    return jid


def record_baseline(tx: Tx, job1: Optional[str], job2: Optional[str], reason: str = "",
                    confirm: Optional[str] = None, isolation: Optional[dict] = None) -> None:
    """两次全量运行的结果归类（一次在工作区、一次在槽位；隔离无效时两次都在工作区）。

    confirm：槽位里对“工作区通过、槽位没通过”的测试的确认重跑，结果覆盖槽位那一次。
    没有验证器或两次都没跑出结果时，基线为空（所有存档都“未验证”）。
    """
    g = tx.g
    j1, j2 = g.jobs.get(job1) if job1 else None, g.jobs.get(job2) if job2 else None
    classes: dict[str, str] = {}
    errors = []
    if j1 is not None and j2 is not None:
        r2 = dict(j2.results)
        jc = g.jobs.get(confirm) if confirm else None
        if jc is not None and jc.state == JOB_FINISHED:
            r2.update(jc.results)
        classes = classify_baseline(j1.results, r2)
        errors = [j.error for j in (j1, j2) if j.error]
    available = bool(classes)
    full_sec = max((j.sec for j in (j1, j2) if j is not None), default=0.0)
    tx.emit("baseline_recorded", RUNTIME, OBSERVED, classes=classes, available=available, full_sec=full_sec,
            reason=reason or ("; ".join(e[:300] for e in errors) if not available else ""),
            isolation=dict(isolation or {}))


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


# ======================================================================== worker 的请求：任务

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
    """认领 = 设为当前焦点。依赖只是排序提示：依赖未完成也可以认领，回复里会提示。"""
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
    rank = suggestion_rank(g, worker, task_id, tx.now, cfg)
    if t.status in (BLOCKED, DONE_UNVERIFIED):
        tx.emit("task_reopened", worker_actor(worker), RULE, task=task_id, reason="reclaimed")
    tx.emit("task_claimed", worker_actor(worker), RULE, task=task_id, worker=worker, head=tx.g.head,
            suggested_rank=rank)
    return "claimed"


def claim_hint(g: Graph, task_id: str) -> str:
    deps = unfinished_deps(g, g.tasks[task_id])
    return f"Its dependencies {', '.join(deps)} are not finished yet." if deps else ""


def release(tx: Tx, worker: str, task_id: str, note: str = "") -> None:
    _held_active(tx.g, worker, task_id)
    tx.emit("task_released", worker_actor(worker), RULE, task=task_id, worker=worker)
    if note.strip():
        tx.emit("note", worker_actor(worker), SELF_REPORT, worker=worker, kind="released", task=task_id,
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
        f = focus_task(g, worker)
        discovered_from = f.id if f is not None and f.status == ACTIVE else None
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
    f = focus_task(tx.g, worker)
    tx.emit("note", worker_actor(worker), SELF_REPORT, worker=worker, kind=kind, text=text[:4000],
            task=f.id if f is not None else None)


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


def _observed_failing(g: Graph, test: str) -> bool:
    """这个检查在 worker 的某个候选树上失败过：只看非 live 的作业（候选树已剔除测试改动，worker 改不了测试本身）。"""
    base = g.checkpoints[0].tree if 0 in g.checkpoints else None
    index = jobs_by_tree(g)
    return any(tree != base and point_status(g, tree, test, index) == PT_FAIL for tree in index)


def waive_checks(tx: Tx, worker: str, task_id: str, tests: Iterable[str], quote: str, reason: str) -> list[str]:
    """worker 声明一些现有测试与任务原文明确要求的行为冲突：规则校验后把它们从回归门里去掉（check_waived）。

    校验：worker 持有这个任务；引文逐字出现在任务原文里（至少三个词）；每个检查都在守护集合里、不是公开检查
    （cmd:），并且确实在 worker 的某个候选树上失败过（不能预先豁免）；总数不超过 waive_max_tests。
    豁免只改变门检查什么，不改变任务的检查项；每一条都写进账本。返回新豁免的检查。"""
    g, cfg = tx.g, tx.cfg
    if not cfg.waivers:
        raise Rejected("Waivers are disabled for this run: keep the existing behaviour, or report the conflict with "
                       "report_blocked(kind=\"check_conflict\").")
    _held_active(g, worker, task_id)
    if not g.baseline_ready:
        raise Rejected("The harness is still setting up; try again shortly.")
    if not (reason or "").strip():
        raise Rejected("Give a reason: what the task asks for and how the test contradicts it.")
    q = normalize_ws(quote or "")
    if len(q.split()) < 3 or not quote_in_text(q, g.run.task):
        raise Rejected("quote must be at least three words copied verbatim from the task text that ask for the new "
                       "behaviour.")
    tests = list(dict.fromkeys(str(t).strip() for t in tests if str(t).strip()))
    if not tests:
        raise Rejected("Name the checks to waive (tests=[...], full node ids as the gate reports them).")
    guard = guard_set(g.baseline)
    problems = []
    for t in tests:
        if is_cmd(t):
            problems.append(f"{t}: public checks cannot be waived")
        elif t in g.waived:
            problems.append(f"{t}: already waived")
        elif t not in guard:
            problems.append(f"{t}: not in the regression gate")
        elif not _observed_failing(g, t):
            problems.append(f"{t}: the harness has not seen it fail on your changes; checkpoint first, or run "
                            "run_check(as_gate=true)")
    if problems:
        raise Rejected("Nothing was waived:\n" + "\n".join(f"- {p}" for p in problems[:20]))
    if len(g.waived) + len(tests) > cfg.waive_max_tests:
        raise Rejected(f"At most {cfg.waive_max_tests} checks can be waived in a run ({len(g.waived)} already are). "
                       "If this many existing tests contradict the task, the change is probably broader than the "
                       "task asks for.")
    tx.emit("check_waived", worker_actor(worker), RULE, task=task_id, tests=tests, quote=q[:1000],
            reason=reason.strip()[:2000])
    _cascade(tx)
    return tests


# ======================================================================== 步骤（模块 H）

_TODO_STATUS = {"pending": STEP_PLANNED, "in_progress": STEP_ACTIVE}


def _norm_title(s: str) -> str:
    return normalize_ws(s).lower()


def plan_steps(tx: Tx, worker: str, todos: list[dict], snapshot: Optional[int] = None,
               files: Iterable = ()) -> Optional[str]:
    """todo 列表 = 焦点任务的步骤计划（每次调用立即写入）。未持有任务时写成普通笔记。

    新标为 completed 的条目按 step_done 处理：锚点是调用方为它拍的快照（snapshot）。返回焦点任务 id。
    """
    g = tx.g
    t = focus_task(g, worker)
    if t is None or t.status != ACTIVE:
        if todos:
            mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
            text = "\n".join(f"{mark.get(x.get('status'), '[ ]')} {x.get('content', '')}" for x in todos)
            tx.emit("note", worker_actor(worker), SELF_REPORT, worker=worker, kind="todos", text=text[:4000])
        return None
    existing = steps_of(g, t.id)
    by_title = {_norm_title(s.title): s for s in existing}
    next_n = max([s.n for s in existing] + [0]) + 1
    specs, completed = [], []
    for order, item in enumerate(todos):
        title = str(item.get("content") or "").strip()[:300]
        if not title:
            continue
        cur = by_title.get(_norm_title(title))
        status = item.get("status", "pending")
        if cur is None:
            sid, n = f"{t.id}.{next_n}", next_n
            next_n += 1
        else:
            sid, n = cur.id, cur.n
        if cur is not None and cur.status in (STEP_DECLARED, STEP_ANCHORED):
            st = cur.status
        elif status == "completed":
            st = STEP_ACTIVE
            completed.append(sid)
        else:
            st = _TODO_STATUS.get(status, STEP_PLANNED)
        specs.append({"id": sid, "n": n, "title": title, "status": st, "order": order})
    old = {s.id: (s.title, s.status, s.order) for s in existing}
    new = {x["id"]: (x["title"], x["status"], x["order"]) for x in specs}
    dropped = [sid for sid, (_, st, _) in old.items() if sid not in new and st in (STEP_PLANNED, STEP_ACTIVE)]
    if dropped or any(old.get(sid) != v for sid, v in new.items()):
        tx.emit("steps_planned", worker_actor(worker), SELF_REPORT, worker=worker, task=t.id, steps=specs,
                diff={"added": sorted(set(new) - set(old)), "dropped": dropped,
                      "changed": sorted(sid for sid in new if sid in old and old[sid] != new[sid])})
    for sid in completed:
        if snapshot is not None:
            _declare_step(tx, worker, sid, snapshot, "", files)
    if completed:
        refresh_anchors(tx)
        schedule_background(tx)
    return t.id


def newly_completed(g: Graph, worker: str, todos: list[dict]) -> bool:
    """这次 todo 更新里有没有新标为 completed、还没声明完成的步骤（调用方据此先拍锚点快照）。"""
    t = focus_task(g, worker)
    if t is None or t.status != ACTIVE:
        return False
    by_title = {_norm_title(s.title): s for s in steps_of(g, t.id)}
    for item in todos:
        if item.get("status") == "completed":
            cur = by_title.get(_norm_title(str(item.get("content") or "")))
            if cur is None or cur.status in (STEP_PLANNED, STEP_ACTIVE):
                return True
    return False


def step_done(tx: Tx, worker: str, snapshot: int, summary: str = "", files: Iterable = ()) -> str:
    """标记当前步骤完成：锚点是调用方刚强制拍下的快照；worker 不等待验证。返回步骤 id。"""
    g = tx.g
    if snapshot is None or snapshot not in g.snapshots:
        raise Rejected("The harness is still setting up; try again shortly.")
    t = focus_task(g, worker)
    if t is None or t.status != ACTIVE:
        raise Rejected("step_done marks a step of the task you are working on; claim a task first.")
    cur = current_step(g, t.id)
    if cur is None:
        n = max([s.n for s in steps_of(g, t.id)] + [0]) + 1
        title = (summary or "").strip().split("\n")[0][:200] or f"step {n}"
        specs = [{"id": s.id, "n": s.n, "title": s.title, "status": s.status, "order": s.order}
                 for s in steps_of(g, t.id) if s.status in (STEP_PLANNED, STEP_ACTIVE)]
        sid = f"{t.id}.{n}"
        specs.append({"id": sid, "n": n, "title": title, "status": STEP_ACTIVE, "order": len(steps_of(g, t.id))})
        tx.emit("steps_planned", worker_actor(worker), SELF_REPORT, worker=worker, task=t.id, steps=specs,
                diff={"added": [sid]})
    else:
        sid = cur.id
    _declare_step(tx, worker, sid, snapshot, summary, files)
    refresh_anchors(tx)
    schedule_background(tx)
    return sid


def start_step(tx: Tx, worker: str, step_id: str) -> None:
    s = tx.g.steps.get(step_id)
    if s is not None and s.status == STEP_PLANNED:
        tx.emit("step_started", worker_actor(worker), SELF_REPORT, worker=worker, step=step_id)


def _declare_step(tx: Tx, worker: str, sid: str, snapshot: int, summary: str, files: Iterable) -> None:
    g = tx.g
    s = g.steps[sid]
    if s.status not in (STEP_PLANNED, STEP_ACTIVE):
        return
    snap = g.snapshots.get(snapshot)
    head = g.head_cp
    anchor, epoch = snapshot, (snap.epoch if snap else g.epoch)
    if snap is not None and head is not None and snap.tree == head.tree:
        anchor, epoch = head.snapshot, head.epoch      # 状态已经在链头上：锚点就是链头的快照
    tx.emit("step_done", worker_actor(worker), SELF_REPORT, worker=worker, step=sid, snapshot=anchor,
            anchor_epoch=epoch, summary=(summary or "")[:1000], files=[list(f) for f in files][:100])


def holder_of(g: Graph, task_id: str) -> Optional[str]:
    lease = g.leases.get(task_id)
    return lease.worker if lease else None


def _mark(tx: Tx, cid: int, kind: str, label: str = "", worker: Optional[str] = None) -> None:
    cp = tx.g.checkpoints.get(cid)
    if cp is not None and cp.id != 0 and not cp.abandoned and cp.kind in (KIND_AUTO, KIND_HANDOFF) and \
            kind in (KIND_MILESTONE, KIND_STEP, KIND_REVIEW, KIND_FINAL):
        tx.emit("checkpoint_marked", RUNTIME, RULE, checkpoint=cid, kind=kind, label=(label or "")[:300],
                worker=worker)


def mark_head(tx: Tx, worker: str, kind: str, label: str = "") -> None:
    """worker 要存的状态已经是链头（后台已经存过）：把链头升级为里程碑，回退的默认目标才会落在这里。"""
    _mark(tx, tx.g.head, kind, label, worker)


def refresh_anchors(tx: Tx) -> None:
    """已声明的步骤：锚点被链上某个同段存档包含时写 step_anchored。"""
    for s in sorted(tx.g.steps.values(), key=lambda s: (s.task, s.n)):
        if s.status == STEP_DECLARED:
            cid = snapshot_contained(tx.g, s.anchor_snapshot, s.anchor_epoch)
            if cid is not None:
                tx.emit("step_anchored", RUNTIME, RULE, step=s.id, checkpoint=cid)
                _mark(tx, cid, KIND_STEP, s.summary or s.title, holder_of(tx.g, s.task))


# ======================================================================== 快照（模块 B）

def record_snapshot(tx: Tx, worker: str, obs: SnapObs, reason: str) -> int:
    """记录一张快照；与这个 worker 上一张快照完全相同（同一段、同一链头）时不记，返回那一张的序号。
    例外：交接 / 会话结束是按原因认出的验证节点，上一张不是这类快照时照样记一张（树相同），否则这个节点会漏验。"""
    g = tx.g
    last = latest_snapshot(g, worker)
    same = last is not None and last.tree == obs.tree and last.raw_tree == obs.raw_tree and last.epoch == g.epoch \
        and last.testable == obs.testable
    if same and not (reason in HANDOFF_REASONS and last.reason not in HANDOFF_REASONS):
        return last.n
    n = g.last_snapshot + 1
    f = focus_task(g, worker)
    cur = current_step(g, f.id) if f is not None else None
    ws = g.workers.get(worker)
    tx.emit("snapshot_taken", RUNTIME, OBSERVED, snapshot=n, worker=worker, tree=obs.tree, raw_tree=obs.raw_tree,
            reason=reason, testable=bool(obs.testable), commit=obs.commit, base=g.head,
            files=[list(x) for x in obs.files][:500], dropped=list(obs.dropped)[:200],
            held=[t.id for t in held_tasks(g, worker)], step=cur.id if cur else None,
            session=obs.session or (ws.session if ws else None), tool_seq=obs.tool_seq, precheck=obs.precheck[:1000])
    schedule_background(tx)
    return n


def _priority_candidate(g: Graph, worker: str) -> Optional[tuple[Snapshot, str, str]]:
    """后台线要验证的快照：还没被包含的步骤锚点、比链头新的交接快照（最早的先）。返回（快照, 触发, 标签）。"""
    head = g.head_cp
    attempted = {a.snapshot for a in g.attempts.values()}
    out: list[tuple[int, Snapshot, str, str]] = []
    if not g.degraded:
        for s in g.steps.values():
            if s.status != STEP_DECLARED or s.anchor_snapshot not in g.snapshots:
                continue
            snap = g.snapshots[s.anchor_snapshot]
            if snap.worker == worker and snap.testable and snap.epoch == g.epoch and snap.n not in attempted \
                    and snap.tree != head.tree and not snap.lost:
                out.append((snap.n, snap, "step", s.summary or s.title))
    for snap in g.snapshots.values():
        if snap.worker == worker and snap.reason in HANDOFF_REASONS and snap.epoch == g.epoch and \
                snap.n not in attempted and snap.testable and snap.tree != head.tree and not snap.lost and \
                not (head.epoch == snap.epoch and head.snapshot >= snap.n):
            out.append((snap.n, snap, PRIORITY_SNAPSHOT_REASONS[snap.reason], ""))
    out = [x for x in out if not (head.epoch == x[1].epoch and head.snapshot >= x[0])]
    if not out:
        return None
    _, snap, trig, label = min(out, key=lambda x: x[0])
    return snap, trig, label


def schedule_background(tx: Tx) -> None:
    """后台验证线：只验证语义节点（步骤锚点、交接、会话结束）的快照，同一时刻每个 worker 最多一个后台尝试。
    没有基于时间或写操作次数的自动存档：普通快照只存不验。后台尝试被拒时链头不动，也不通知 worker、不定位。"""
    g = tx.g
    if not _running_run(g) or g.run.finalizing or g.run.reserve or not g.baseline_ready or g.head_cp is None:
        return
    for w in sorted(g.workers):
        g = tx.g
        if open_attempt(g, w, LANE_BG) is not None:
            continue
        prio = _priority_candidate(g, w)
        if prio is not None:
            snap, trig, label = prio
            request_checkpoint(tx, w, snap.n, trig, lane=LANE_BG, summary=label)


# ======================================================================== 存档：验证后比较并交换

def _selection(g: Graph, cfg: BelayConfig, tier: str, files: Iterable[str], extra_checks: Iterable[str]):
    """返回 (档位, 选择)。related 找不到相关测试、改动可能影响全局或无从判断时升级为 full。"""
    files = list(files)
    if tier == "related" and files:
        sel, _why = related_units(files, test_files_of(g.baseline), g.relations)
        if sel is None:
            return "full", None
        cmd_guard = [c for c in active_guard(g) if is_cmd(c)]           # 公开检查通常便宜：总是带上
        return "related", tuple(sorted(set(sel) | set(units(extra_checks)) | set(cmd_guard)))
    return "full", None


def request_checkpoint(tx: Tx, worker: str, snapshot: int, trigger: str, lane: str = LANE_FG,
                       tasks: Iterable[str] = (), tier: Optional[str] = None, summary: str = "") -> Optional[str]:
    """对一张快照发起存档尝试；它与链头相同时返回 None（没有要存的东西）。"""
    g = tx.g
    if not g.baseline_ready or g.head is None:
        raise Rejected("The harness is still setting up; try again shortly.")
    if open_attempt(g, worker, lane) is not None:
        if lane == LANE_FG:
            raise Rejected("A checkpoint of your work is already in progress.")
        return None
    snap = g.snapshots.get(snapshot)
    if snap is None:
        raise Rejected(f"Unknown snapshot {snapshot}.")
    if snap.tree == g.head_cp.tree:
        mark_head(tx, worker, TRIGGER_KIND.get(trigger, KIND_MILESTONE), summary)
        return None
    tasks = tuple(tasks)
    extra = [c for tid in tasks for c in g.tasks[tid].checks]
    tier, selection = _selection(g, tx.cfg, tier or tx.cfg.checkpoint_tier, [f[0] for f in snap.files], extra)
    aid = next_id("A", g.attempts)
    kind = TRIGGER_KIND.get(trigger, KIND_MILESTONE)
    tx.emit("checkpoint_attempted", worker_actor(worker) if trigger in ("worker", "review") else RUNTIME, RULE,
            attempt=aid, worker=worker, trigger=trigger, tree=snap.tree, raw_tree=snap.raw_tree, base=g.head,
            tier=tier, selection=None if selection is None else list(selection), tasks=list(tasks),
            summary=(summary or "")[:2000], snapshot=snap.n, lane=lane, kind=kind)
    if not snap.testable:
        tx.emit("checkpoint_rejected", RUNTIME, RULE, attempt=aid, regressions=[], reason="precheck",
                detail=snap.precheck[:1000] or "the changed files do not compile")
        _reopen_attempt_tasks(tx, aid, "checkpoint_rejected", [f"precheck: {snap.precheck[:200]}"])
        schedule_background(tx)
        return aid
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


def _older_than_head(g: Graph, a) -> bool:
    h = g.head_cp
    return h.id != 0 and h.epoch == a.epoch and h.snapshot >= a.snapshot


def advance_attempt(tx: Tx, aid: str) -> None:
    """把一次存档尝试尽量往前推：已被更新的快照超过就标为 superseded；缺结果就起作业；有回归先确认；
    然后决定推进或拒绝。"""
    g, cfg = tx.g, tx.cfg
    a = g.attempts[aid]
    if a.status != ATT_PENDING:
        return
    if _older_than_head(g, a) or a.tree == g.head_cp.tree:
        return supersede_attempt(tx, aid, "newer checkpoint")
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
    expected = guard_in_selection(active_guard(g), a.selection)
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
        if a.lane == LANE_FG:                       # 只有 worker 声明的存档被拒才定位与诊断；后台的只是链头不动
            loc = start_locate(tx, regression_ids(regs), {"tree": a.tree, "snapshot": a.snapshot}, "rejected",
                               ref=aid)
            if loc is None:
                maybe_diagnose(tx, "rejected", regression_ids(regs), None)
            _repeated_diagnosis(tx, aid)
        schedule_background(tx)
    elif not any(x.status == ATT_ADVANCING for x in g.attempts.values()):
        tx.emit("checkpoint_advancing", RUNTIME, RULE, attempt=aid, parent_commit=g.head_cp.commit, date=tx.now,
                flaky=list(flaky))
    # 另一个尝试正在推进：等它落地（ref_advanced 会再推进这一个；那时它多半已被新存档取代）


def supersede_attempt(tx: Tx, aid: str, reason: str) -> None:
    """取代一个还在等结果的尝试。它带着的待验证任务：链头已经包含它的快照时转到链头上判定，否则重开。"""
    g = tx.g
    a = g.attempts[aid]
    if a.status != ATT_PENDING:
        return
    review_tasks = [tid for tid in a.tasks if g.tasks[tid].status == REVIEW and g.tasks[tid].review_attempt == aid]
    contained = _older_than_head(g, a) or a.tree == g.head_cp.tree
    if review_tasks and not contained:
        _reopen_attempt_tasks(tx, aid, "checkpoint_rejected", [f"checkpoint attempt {aid} was cancelled ({reason})"])
        review_tasks = []
    tx.emit("attempt_superseded", RUNTIME, RULE, attempt=aid, reason=reason,
            review_checkpoint=tx.g.head if review_tasks else None)
    for tid in review_tasks:
        evaluate_review(tx, tid)


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
        for other in list(tx.g.attempts.values()):             # 新快照胜出：更旧的尝试不再进链
            if other.status == ATT_PENDING:
                advance_attempt(tx, other.id)
        refresh_anchors(tx)
        for t in list(tx.g.tasks.values()):
            if t.status == REVIEW and t.review_checkpoint is not None:
                evaluate_review(tx, t.id)
    else:
        tx.emit("checkpoint_rejected", RUNTIME, OBSERVED, attempt=aid, regressions=[], reason="cas_conflict",
                detail=detail[:1000])
        _reopen_attempt_tasks(tx, aid, "checkpoint_rejected", [f"cas_conflict: {detail[:200]}"])
    schedule_promotion(tx)
    schedule_background(tx)


def abort_attempts(tx: Tx, reason: str, lane: Optional[str] = None) -> None:
    """收尾时仍在验证的尝试：拒绝（链不动）。正在 CAS 的尝试不能中止，由外壳做完。"""
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING and (lane is None or a.lane == lane):
            tx.emit("checkpoint_rejected", RUNTIME, RULE, attempt=a.id, regressions=[], reason=reason)
            _reopen_attempt_tasks(tx, a.id, "checkpoint_rejected", [reason])


# ======================================================================== 两级存档链：提升与降级（模块 C）

def schedule_promotion(tx: Tx) -> None:
    """验证队列有空闲时，取链上最新的、还没有全量结果的暂存点跑全量；更老的暂存点跳过。"""
    g = tx.g
    if not _background_ok(g) or not guard_set(g.baseline):
        return
    cands = []
    for cp in chain(g):                             # 只看最新确认点与最近一次降级之后的暂存点
        if cp.level == CONFIRMED or cp.demoted:
            break
        cands.append(cp)
    if not cands:
        return
    target = cands[0]
    if full_verified(g, target.tree):
        return evaluate_promotion(tx, target.id)
    running_full = [j for j in g.jobs.values() if j.state == JOB_RUNNING and j.selection is None and not j.live]
    if any(j.tree == target.tree for j in running_full) or any(j.purpose == "promote" for j in running_full):
        return
    ensure_job(tx, target.tree, None, "promote", checkpoint=target.id)


def promote_now(tx: Tx, cid: Optional[int] = None) -> bool:
    """收尾时对链头（或给定的暂存点）做全量验证。返回 True 表示还在等作业。"""
    g = tx.g
    cid = g.head if cid is None else cid
    cp = g.checkpoints.get(cid)
    if cp is None or cp.level == CONFIRMED or cp.demoted or not guard_set(g.baseline):
        return False
    if not full_verified(g, cp.tree):
        if not any(j.tree == cp.tree and j.state == JOB_RUNNING and j.selection is None and not j.live
                   for j in g.jobs.values()):
            ensure_job(tx, cp.tree, None, "promote", checkpoint=cp.id)
        return True
    evaluate_promotion(tx, cid)
    cp = tx.g.checkpoints[cid]
    return cp.level != CONFIRMED and not cp.demoted


def evaluate_promotion(tx: Tx, cid: int) -> None:
    g, cfg = tx.g, tx.cfg
    cp = g.checkpoints.get(cid)
    if cp is None or cp.level != PROVISIONAL or cp.demoted or cp.abandoned or not full_verified(g, cp.tree):
        return
    guard = active_guard(g)
    regs_raw = regressions(sorted(guard), _raw_results(g, cp.tree))
    if regs_raw and cfg.confirm_regressions:
        cu = list(units(regression_ids(regs_raw)))
        if not _confirm_covers(g, cp.tree, cu, JOB_FINISHED):
            if not _confirm_covers(g, cp.tree, cu, JOB_RUNNING):
                ensure_job(tx, cp.tree, cu, "confirm", tag="confirm", checkpoint=cid)
            return
    regs = regressions(sorted(guard), results_for_tree(g, cp.tree))
    if not regs:
        tx.emit("checkpoint_confirmed", VERIFIER, OBSERVED, checkpoint=cid)
        schedule_promotion(tx)
        return
    tx.emit("checkpoint_demoted", RUNTIME, RULE, checkpoint=cid, regressions=list(regs)[:200],
            n_regressions=len(regs))
    if not _background_ok(tx.g) or cp.kind not in DECLARED_KINDS:
        schedule_promotion(tx)                      # 步骤、交接等中间节点：只降级（不再交付），不追查、不通知
        return
    # 问题是否还在：对最新的可测快照只跑这几个失败的测试
    ids = regression_ids(regs)
    snap = latest_snapshot(tx.g, None, testable=True)
    if snap is None or snap.tree == cp.tree or snap.epoch != tx.g.epoch:
        _demotion_persists(tx, cid, ids, snap.n if snap else cp.snapshot)
    else:
        ensure_job(tx, snap.tree, units(ids), "recheck", tag=f"recheck:{cid}", checkpoint=cid)
    schedule_promotion(tx)


def _evaluate_recheck(tx: Tx, jid: str) -> None:
    g = tx.g
    j = g.jobs[jid]
    cp = g.checkpoints.get(j.checkpoint) if j.checkpoint is not None else None
    if cp is None or j.state != JOB_FINISHED or not j.results:
        return
    ids = regression_ids(cp.demote_regressions)
    still = [t for t in ids if j.results.get(t) != PASSED and check_unit(t) in (j.selection or ())]
    if still:
        snap = next((s for s in g.snapshots.values() if s.tree == j.tree), None)
        _demotion_persists(tx, cp.id, still, snap.n if snap else cp.snapshot)


def _demotion_persists(tx: Tx, cid: int, tests: list[str], latest_n: int) -> None:
    cp = tx.g.checkpoints[cid]
    tx.emit("persistent_regression", RUNTIME, RULE, tests=list(tests)[:50], trigger="demoted", checkpoint=cid,
            since=cp.snapshot, epoch=cp.epoch, latest=latest_n)
    loc = start_locate(tx, tests, {"tree": cp.tree, "snapshot": cp.snapshot, "checkpoint": cid}, "demoted",
                       ref=f"cp:{cid}")
    if loc is None:
        maybe_diagnose(tx, "demoted", tests, None)


# ======================================================================== 快照二分定位（模块 D3）

def locate_points(g: Graph, lid: str) -> list[dict]:
    """区间内的点：段起点存档 + 同一段内坏端之前的可测快照（连续相同的树只取一张）+ 坏端。"""
    loc = g.locates[lid]
    base = g.checkpoints[loc.lower]
    pts = [{"kind": "checkpoint", "id": base.id, "tree": base.tree, "snapshot": base.snapshot if base.id else 0}]
    bad_n = loc.bad_snapshot
    last_tree = base.tree
    for s in snapshots_in_epoch(g, loc.epoch):
        if bad_n is not None and s.n >= bad_n:
            break
        if loc.epoch == base.epoch and s.n <= base.snapshot:
            continue
        if not s.testable or s.lost or s.tree == last_tree:
            continue
        pts.append({"kind": "snapshot", "id": s.n, "tree": s.tree, "snapshot": s.n})
        last_tree = s.tree
    pts.append({"kind": "snapshot" if bad_n is not None else "checkpoint",
                "id": bad_n if bad_n is not None else loc.bad_checkpoint, "tree": loc.bad_tree,
                "snapshot": bad_n})
    return pts


def _test_interval(g: Graph, pts: list[dict], test: str, index: Optional[dict] = None) -> dict:
    """一个测试在区间上的状态：最后一次已知通过（好端）与之后第一次已知失败（坏端）；中间还没测过的点。"""
    last = len(pts) - 1
    sts = []
    for i, p in enumerate(pts):
        if i == last:
            sts.append(PT_FAIL)
        elif i == 0 and p["kind"] == "checkpoint" and p["id"] == 0 and test in guard_set(g.baseline):
            sts.append(PT_PASS)                      # 守护测试在原始代码上两次都通过
        else:
            sts.append(point_status(g, p["tree"], test, index))
    passes = [i for i in range(last) if sts[i] == PT_PASS]
    if not passes:
        if sts[0] in (PT_UNTESTED, PT_RUNNING):
            return {"state": "need", "mid": 0, "running": sts[0] == PT_RUNNING, "gi": None, "bi": last}
        bi = next(i for i in range(last + 1) if sts[i] == PT_FAIL)
        return {"state": "done", "gi": 0, "bi": bi, "exact": False}
    gi = passes[-1]
    bi = next(i for i in range(gi + 1, last + 1) if sts[i] == PT_FAIL)
    between = [i for i in range(gi + 1, bi) if sts[i] in (PT_UNTESTED, PT_RUNNING)]
    if any(sts[i] == PT_RUNNING for i in between):
        return {"state": "wait", "gi": gi, "bi": bi}
    if not between:                                 # 中间只剩跑不出结果的点：给出区间
        return {"state": "done", "gi": gi, "bi": bi, "exact": bi == gi + 1}
    mid = between[len(between) // 2]
    return {"state": "need", "mid": mid, "gi": gi, "bi": bi, "running": False}


def start_locate(tx: Tx, tests: Iterable[str], bad: dict, trigger: str, ref: Optional[str] = None) -> Optional[str]:
    """按规则定位：从“最后一次已知通过”到坏端二分。返回定位 id；不做定位时返回 None。"""
    g, cfg = tx.g, tx.cfg
    tests = sorted(set(t for t in tests if not is_cmd(t)))
    if not cfg.locate or not _background_ok(g) or not tests or not guard_set(g.baseline):
        return None
    bad_n = bad.get("snapshot")
    snap = g.snapshots.get(bad_n) if bad_n is not None else None
    epoch = snap.epoch if snap is not None else g.epoch
    if epoch != g.epoch:
        return None
    for loc in g.locates.values():                  # 已有定位覆盖这些测试：复用
        if loc.epoch == epoch and set(tests) <= set(loc.tests):
            if loc.status == "running":
                return loc.id
            if loc.bad_snapshot is not None and bad_n is not None and loc.bad_snapshot <= bad_n and \
                    not any(s.epoch == epoch and loc.bad_snapshot < s.n <= bad_n and
                            point_status(g, s.tree, t) == PT_PASS for s in g.snapshots.values() for t in tests):
                return loc.id
    lid = next_id("L", g.locates)
    tx.emit("locate_started", RUNTIME, RULE, locate=lid, tests=tests[:50], bad=dict(bad), epoch=epoch,
            lower=g.epoch_base.get(epoch, 0), trigger=trigger, ref=ref)
    advance_locate(tx, lid)
    return lid


def advance_locate(tx: Tx, lid: str) -> None:
    for _ in range(64):                             # 复用已完成的作业时立即重算，直到需要等作业
        g, cfg = tx.g, tx.cfg
        loc = g.locates.get(lid)
        if loc is None or loc.status != "running" or not _running_run(g):
            return
        if any(j.locate == lid and j.state == JOB_RUNNING for j in g.jobs.values()):
            return
        pts = locate_points(g, lid)
        steps = sum(1 for j in g.jobs.values() if j.locate == lid)
        over = steps >= cfg.locate_max_steps or tx.now - loc.started_t > cfg.locate_max_sec or g.run.finalizing
        index = jobs_by_tree(g)
        iv = {t: _test_interval(g, pts, t, index) for t in loc.tests}
        pending = {t: v for t, v in iv.items() if v["state"] in ("need", "wait")}
        if not pending or over:
            return _conclude_locate(tx, lid, pts, iv)
        if all(v["state"] == "wait" or v.get("running") for v in pending.values()):
            return
        first = next(t for t in sorted(pending) if pending[t]["state"] == "need" and not pending[t].get("running"))
        mid = pending[first]["mid"]
        run = [t for t, v in pending.items() if v["state"] == "need" and
               (v["mid"] == mid or ((v["gi"] if v["gi"] is not None else -1) < mid < v["bi"])) and
               point_status(g, pts[mid]["tree"], t) == PT_UNTESTED]
        jid = ensure_job(tx, pts[mid]["tree"], units(run or [first]), "locate", locate=lid, tag="locate")
        if tx.g.jobs[jid].state == JOB_RUNNING and tx.g.jobs[jid].locate == lid:
            return
        if tx.g.jobs[jid].state == JOB_RUNNING:
            return                                  # 同一个作业正被别的定位使用：等它结束


def _attribution(g: Graph, p: dict) -> dict:
    if p.get("kind") == "snapshot" and p.get("id") in g.snapshots:
        s = g.snapshots[p["id"]]
        return {"snapshot": s.n, "held": list(s.held), "step": s.step, "session": s.session}
    return {"checkpoint": p.get("id")}


def _conclude_locate(tx: Tx, lid: str, pts: list[dict], iv: dict) -> None:
    groups: dict[tuple, dict] = {}
    for t, v in sorted(iv.items()):
        if v["state"] == "done":
            gi, bi, exact = v["gi"], v["bi"], v["exact"]
        else:                                       # 超出上限：给出已缩小的区间
            gi = v["gi"] if v.get("gi") is not None else 0
            bi, exact = v["bi"], False
        key = (gi, bi)
        grp = groups.setdefault(key, {"tests": [], "good": pts[gi], "bad": pts[bi], "exact": exact,
                                      "attribution": _attribution(tx.g, pts[bi])})
        grp["tests"].append(t)
        grp["exact"] = grp["exact"] and exact
    tx.emit("locate_concluded", RUNTIME, RULE, locate=lid, groups=list(groups.values()))


def record_located(tx: Tx, lid: str, group: int, files: Iterable, diff: Optional[str]) -> None:
    """外壳算出“好 → 坏”之间的改动之后的观察；随后学习相关性、请求诊断。"""
    g = tx.g
    loc = g.locates.get(lid)
    if loc is None or loc.status != "concluded" or group >= len(loc.groups) or not _running_run(g):
        return
    if any(r.get("group") == group for r in loc.results):
        return
    grp = loc.groups[group]
    files = [list(f) for f in files][:200]
    tx.emit("regression_located", VERIFIER, OBSERVED, locate=lid, group=group, tests=grp["tests"],
            good=grp["good"], bad=grp["bad"], exact=bool(grp["exact"]), files=files, diff=diff,
            attribution=grp.get("attribution") or {})
    if loc.trigger == "demoted" and grp["exact"]:
        pairs = sorted({(f[0], check_unit(t)) for f in files for t in grp["tests"]
                        if not is_test_path(f[0], suite_layout(tx.g)) and not is_cmd(t)})
        pairs = [p for p in pairs if p not in tx.g.relations]
        if pairs:
            tx.emit("relation_learned", RUNTIME, RULE, pairs=[list(p) for p in pairs][:100], locate=lid)
    if _running_run(tx.g) and not tx.g.run.finalizing:
        maybe_diagnose(tx, loc.trigger if loc.trigger != "step" else "rejected", grp["tests"], lid, group)


# ======================================================================== 诊断者（模块 E）

def _diag_key(tests: Iterable[str], locate: Optional[str], group: Optional[int], g: Graph) -> str:
    rng = ""
    if locate is not None and group is not None and locate in g.locates:
        grp = g.locates[locate].groups[group]
        rng = f"{grp['good'].get('id')}->{grp['bad'].get('id')}"
    return f"{failure_signature(tests)}:{rng}"


def maybe_diagnose(tx: Tx, trigger: str, tests: Iterable[str], locate: Optional[str], group: Optional[int] = None,
                   previous: Optional[str] = None) -> Optional[str]:
    g, cfg = tx.g, tx.cfg
    tests = sorted(set(tests))
    if not cfg.diagnoser or not tests or not _running_run(g) or g.run.finalizing:
        return None
    key = _diag_key(tests, locate, group, g)
    if trigger != "repeated" and any(d.key == key for d in g.diagnoses.values()):
        return None
    did = next_id("D", g.diagnoses)
    tx.emit("diagnosis_requested", RUNTIME, RULE, diagnosis=did, trigger=trigger, tests=tests[:50], key=key,
            locate=locate, group=group, previous=previous)
    return did


def _repeated_diagnosis(tx: Tx, aid: str) -> None:
    """同一回归签名第二次被拒：再诊断一次，带上前一次的结论。"""
    g = tx.g
    a = g.attempts[aid]
    sig = failure_signature(regression_ids(a.regressions))
    same = [x for x in g.attempts.values() if x.status == ATT_REJECTED and x.regressions and
            (x.lane == LANE_FG or x.kind == KIND_STEP) and failure_signature(regression_ids(x.regressions)) == sig]
    if len(same) != 2:
        return
    prev = [d for d in g.diagnoses.values() if d.status == "recorded" and
            failure_signature(d.tests) == sig]
    if prev:
        maybe_diagnose(tx, "repeated", regression_ids(a.regressions), prev[-1].locate, None, previous=prev[-1].id)


def record_diagnosis(tx: Tx, did: str, result: dict, failed: bool = False) -> None:
    """诊断结论（llm）入图。intentional=true 必须给出任务原文的逐字引文，规则校验，不过就丢弃这一项。"""
    g = tx.g
    d = g.diagnoses.get(did)
    if d is None or d.status != "requested" or not _running_run(g):
        return
    result = dict(result or {})
    inten = result.get("intentional")
    if isinstance(inten, dict) and inten.get("likely"):
        q = inten.get("quote") or ""
        if not q or g.run is None or not quote_in_text(q, g.run.task):
            result["intentional"] = {"likely": False, "requirement": None, "quote": None, "dropped": True}
    tx.emit("diagnosis_recorded", DIAGNOSER, LLM, diagnosis=did, result=result, failed=failed)


# ======================================================================== 待验证 → 完成；复查（模块 F）

def request_review(tx: Tx, worker: str, task_id: str, snapshot: int, summary: str = "") -> Optional[str]:
    g = tx.g
    _held_active(g, worker, task_id)
    if not g.baseline_ready or g.head is None:
        raise Rejected("The harness is still setting up; try again shortly.")
    if open_attempt(g, worker, LANE_FG) is not None:
        raise Rejected("A checkpoint of your work is already in progress.")
    snap = g.snapshots.get(snapshot)
    if snap is None or snap.tree == g.head_cp.tree:
        mark_head(tx, worker, KIND_REVIEW, summary or f"{task_id} finished")
        tx.emit("review_requested", worker_actor(worker), RULE, task=task_id, worker=worker, checkpoint=tx.g.head)
        evaluate_review(tx, task_id)
        return None
    tx.emit("review_requested", worker_actor(worker), RULE, task=task_id, worker=worker)
    return request_checkpoint(tx, worker, snapshot, "review", tasks=(task_id,), summary=summary)


def evaluate_review(tx: Tx, task_id: str) -> None:
    """在任务的存档那棵树上看它的检查：全部 PASSED → done；失败 → 重开；缺结果 → 起 evidence 作业。"""
    g = tx.g
    t = g.tasks.get(task_id)
    if t is None or t.status != REVIEW or t.review_checkpoint is None:
        return
    cp = g.checkpoints[t.review_checkpoint]
    if not t.checks:
        tx.emit("task_done", RUNTIME, RULE, task=task_id, checkpoint=cp.id, verified=False)
        maybe_review(tx, task_id, "done")
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


def _review_eligible(g: Graph, cfg: BelayConfig, task_id: str, phase: str) -> bool:
    t = g.tasks[task_id]
    if not cfg.reviewer or t.review is not None or t.review_reopens >= cfg.review_max_reopens:
        return False
    if phase == "done":
        return t.status == DONE_UNVERIFIED
    return t.status == BLOCKED and t.blocked_kind == "insufficient_info"


def maybe_review(tx: Tx, task_id: str, phase: str) -> bool:
    g = tx.g
    if not _running_run(g) or not _review_eligible(g, tx.cfg, task_id, phase):
        return False
    tx.emit("review_started", RUNTIME, RULE, task=task_id, phase=phase)
    return True


def reviews_needed(g: Graph, cfg: BelayConfig) -> list[tuple[str, str]]:
    """收尾前还没复查过的 done_unverified，以及以 insufficient_info 受阻的任务。"""
    out = []
    for t in sorted(g.tasks.values(), key=lambda t: num(t.id)):
        for phase in ("done", "blocked"):
            if _review_eligible(g, cfg, t.id, phase):
                out.append((t.id, phase))
    return out


def request_final_reviews(tx: Tx) -> int:
    n = 0
    for tid, phase in reviews_needed(tx.g, tx.cfg):
        n += maybe_review(tx, tid, phase)
    return n


def record_review(tx: Tx, task_id: str, phase: str, result: dict) -> None:
    """复查者（llm）只能收紧：no / partial → 重开；yes → 什么都不做（永远不能把任务提升为 done）。"""
    g = tx.g
    t = g.tasks.get(task_id)
    if t is None or t.review != "running" or not _running_run(g):
        return
    result = dict(result or {})
    impl = result.get("implemented")
    if phase == "blocked":
        impl = "reading" if result.get("reading") else "none"
    if impl not in ("yes", "partial", "no", "reading", "none", "failed"):
        impl = "failed"
    missing = [str(x)[:300] for x in (result.get("missing") or [])][:20]
    tx.emit("review_recorded", REVIEWER, LLM, task=task_id, phase=phase, implemented=impl, missing=missing,
            evidence=[str(x)[:300] for x in (result.get("evidence") or [])][:20],
            reading=str(result.get("reading") or "")[:1500] or None)
    t = tx.g.tasks[task_id]
    if tx.g.run.reserve or tx.g.run.finalizing:
        return                                     # 截止收尾时已经没有时间再做：只进账本
    if phase == "done" and impl in ("no", "partial") and t.status == DONE_UNVERIFIED:
        tx.emit("task_reopened", RUNTIME, RULE, task=task_id, reason="review_missing",
                failures=missing or [f"review: implemented={impl}"])
    elif phase == "blocked" and impl == "reading" and t.status == BLOCKED:
        tx.emit("task_reopened", RUNTIME, RULE, task=task_id, reason="review_reading",
                failures=[f"a reasonable reading: {result.get('reading')}"[:1500]])


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
        sel, _ = related_units(list(changed), test_files_of(g.baseline), g.relations)
        if sel == ():
            raise Rejected("No test is related to your changes; pass tests=[...] or full=true.")
    return ensure_job(tx, raw_tree, sel, "dev", actor=worker_actor(worker), live=True, tag="dev")


def gate_check(tx: Tx, worker: str, snapshot: int, tests: Iterable[str] = (), full: bool = False) -> str:
    """按门的口径自查：对当前工作区拍快照、剔除测试改动，在验证槽位里运行（第 2 档优先级）。"""
    g = tx.g
    snap = g.snapshots.get(snapshot)
    if snap is None:
        raise Rejected("No snapshot of your working tree yet.")
    tests = [str(x) for x in tests]
    if full:
        sel = None
    elif tests:
        sel = tuple(sorted(set(tests)))
    else:
        sel, _ = related_units([f[0] for f in snap.files], test_files_of(g.baseline), g.relations)
        if not sel:
            sel = None
    return ensure_job(tx, snap.tree, sel, "gate", actor=worker_actor(worker), tag="gate")


def job_preempted(tx: Tx, job_id: str) -> None:
    j = tx.g.jobs.get(job_id)
    if j is not None and j.state == JOB_RUNNING:
        tx.emit("job_preempted", VERIFIER, OBSERVED, job=job_id)


def job_finished(tx: Tx, job_id: str, state: str, results: dict, sec: float = 0.0, error: str = "",
                 reasons: Optional[dict] = None) -> None:
    """作业结果（观察）→ 级联：重跑丢失的作业、推进在等结果的尝试、判定证据、提升与降级、定位。"""
    job = tx.g.jobs.get(job_id)
    if job is None or job.state != JOB_RUNNING:
        return
    reasons = {k: str(v)[:400] for k, v in sorted((reasons or {}).items())[:50]}
    tx.emit("job_finished", "verifier", OBSERVED, job=job_id, state=state, results=dict(results), sec=float(sec),
            error=(error or "")[:2000], reasons=reasons)
    job = tx.g.jobs[job_id]
    if tx.g.run is None or tx.g.run.status != RUN_RUNNING:
        return
    if state == JOB_CANCELLED:
        if tx.g.run.finalizing or tx.g.run.reserve:
            for a in list(tx.g.attempts.values()):  # 收尾时取消的作业：依赖它的尝试直接拒绝（链不动）
                if a.status == ATT_PENDING and job_id in a.jobs:
                    tx.emit("checkpoint_rejected", RUNTIME, RULE, attempt=a.id, regressions=[], reason="cancelled")
                    _reopen_attempt_tasks(tx, a.id, "checkpoint_rejected", ["verification cancelled"])
            return
    elif state == JOB_UNKNOWN and not job.live:
        att = job.attempt if job.attempt and tx.g.attempts[job.attempt].status == ATT_PENDING else None
        ensure_job(tx, job.tree, job.selection, job.purpose, attempt=att, tag=job.tag, where=job.where,
                   locate=job.locate, checkpoint=job.checkpoint)
    _cascade(tx, job_id)


def _cascade(tx: Tx, job_id: Optional[str] = None) -> None:
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING:
            advance_attempt(tx, a.id)
    for t in list(tx.g.tasks.values()):
        if t.status == REVIEW and t.review_checkpoint is not None:
            evaluate_review(tx, t.id)
    job = tx.g.jobs.get(job_id) if job_id else None
    if job is not None and job.state == JOB_FINISHED and not job.live:
        for cp in chain(tx.g):
            if cp.tree == job.tree and cp.level == PROVISIONAL and not cp.demoted:
                evaluate_promotion(tx, cp.id)
        if job.tag.startswith("recheck:"):
            _evaluate_recheck(tx, job_id)
    for lid in [l.id for l in tx.g.locates.values() if l.status == "running"]:
        advance_locate(tx, lid)
    schedule_promotion(tx)
    schedule_background(tx)


# ======================================================================== 回退

def rollback(tx: Tx, worker: str, to: Optional[int] = None) -> int:
    """回退：默认退到最近的里程碑。先取消后台尝试；有前台尝试或有尝试正在推进时拒绝。"""
    g = tx.g
    to = latest_milestone(g) if to is None else int(to)
    ids = chain_ids(g)
    if to not in ids:
        raise Rejected(f"Checkpoint {to} is not on the checkpoint chain ({', '.join(map(str, ids))}).")
    if open_attempt(g, None, LANE_FG) is not None or any(a.status == ATT_ADVANCING for a in g.attempts.values()):
        raise Rejected("A checkpoint is in progress; roll back after it finishes.")
    for a in list(g.attempts.values()):
        if a.status == ATT_PENDING and a.lane == LANE_BG:
            supersede_attempt(tx, a.id, "rollback")
    g = tx.g
    abandoned = ids[:ids.index(to)]
    for t in list(g.tasks.values()):
        if (t.status in (DONE, DONE_UNVERIFIED) and t.done_checkpoint in abandoned) or \
                (t.status == REVIEW and t.review_checkpoint in abandoned):
            tx.emit("task_reopened", worker_actor(worker), RULE, task=t.id, reason="rolled_back",
                    failures=[f"checkpoint {t.done_checkpoint or t.review_checkpoint} was rolled back"])
    keep = [c for c in chain(tx.g) if c.id not in abandoned]
    for s in sorted(tx.g.steps.values(), key=lambda s: s.id):
        if s.status not in (STEP_DECLARED, STEP_ANCHORED):
            continue
        ok = any(c.epoch == s.anchor_epoch and c.snapshot >= (s.anchor_snapshot or 0) for c in keep)
        if not ok:
            tx.emit("step_invalidated", worker_actor(worker), RULE, step=s.id, reason="rolled_back")
    cp = tx.g.checkpoints[to]
    tx.emit("rollback", worker_actor(worker), RULE, worker=worker, to=to, abandoned=abandoned, tree=cp.tree,
            commit=cp.commit)
    return to


# ======================================================================== 时钟、停滞

def tick(tx: Tx) -> None:
    g, cfg = tx.g, tx.cfg
    if g.run is None or g.run.status != RUN_RUNNING:
        return
    if not tx.g.run.reserve and g.frozen and remaining_sec(tx.g, tx.now) <= reserve_sec(tx.g, cfg):
        tx.emit("deadline_reserve", RUNTIME, RULE, reserve_sec=reserve_sec(tx.g, cfg))
    if cfg.stall:
        detect_stalls(tx)
    for lid in [l.id for l in tx.g.locates.values() if l.status == "running"]:
        advance_locate(tx, lid)


def detect_stalls(tx: Tx) -> None:
    g, cfg = tx.g, tx.cfg
    since = [s for s in g.stalls if s.seq > g.last_progress_seq]
    for w, ws in sorted(g.workers.items()):
        if ws.session is None:
            continue
        f = focus_task(g, w)
        task = f.id if f is not None and f.status == ACTIVE else None
        action = "replan" if (since and task) else "hint"
        idle_since = g.last_progress_t
        if tx.now - idle_since > cfg.stall_no_progress_sec and not any(x.kind == "no_progress" for x in since):
            tx.emit("stall_detected", RUNTIME, RULE, kind="no_progress", action=action, worker=w, task=task,
                    detail=f"no progress for {int((tx.now - idle_since) / 60)} min")
            return
        mine = sorted((a for a in g.attempts.values() if a.worker == w and a.status in (ATT_REJECTED, "created")
                       and a.lane == LANE_FG),
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
    return {"handoff": "handoff", "crash": "crash", "runtime_crash": "recover", "stuck": "restart",
            "suspended": "resume", "rebuild": "rebuild"}.get(s.end_reason or "", "restart")


def start_session(tx: Tx, worker: str, reason: str, opening: dict, transcript: Optional[str] = None) -> str:
    sid = next_id("S", tx.g.sessions)
    tx.emit("session_started", RUNTIME, RULE, session=sid, worker=worker, reason=reason, opening=opening,
            transcript=transcript)
    return sid


def session_resumed(tx: Tx, session: str, mode: str, detail: str = "") -> None:
    tx.emit("session_resumed", RUNTIME, OBSERVED, session=session, mode=mode, detail=detail[:500])


def end_session(tx: Tx, worker: str, reason: str, peak_context: int = 0, turns: int = 0,
                error: Optional[str] = None, todos: Optional[list[dict]] = None) -> Optional[str]:
    ws = tx.g.workers.get(worker)
    if ws is None or ws.session is None:
        return None
    sid = ws.session
    if todos and focus_task(tx.g, worker) is None:
        plan_steps(tx, worker, todos)
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


def recovered(tx: Tx, downtime_sec: float, reconciled: dict, rebuilt: bool = False,
              lost_snapshots: Iterable[int] = (), isolation: Optional[dict] = None) -> None:
    tx.emit("runtime_recovered", RUNTIME, OBSERVED, downtime_sec=max(0.0, float(downtime_sec)),
            reconciled=reconciled, rebuilt=rebuilt, lost_snapshots=sorted(lost_snapshots),
            isolation=isolation or None)


def suspend(tx: Tx, reason: str = "suspend") -> None:
    tx.emit("run_suspended", RUNTIME, RULE, reason=reason)


def label_checkpoint(tx: Tx, cid: int, label: str) -> None:
    if cid in tx.g.checkpoints and label.strip():
        tx.emit("checkpoint_labeled", COMPACTOR, LLM, checkpoint=cid, label=label.strip()[:300])


# ======================================================================== 运行的结束

def next_step(g: Graph, worker: str, now: float, cfg: BelayConfig) -> tuple[str, str]:
    """会话结束不等于运行结束。返回 (动作, 理由)：stop | finalize | start_session | resume_session | review | wait。"""
    if g.run is None or g.run.status != RUN_RUNNING:
        return "stop", "delivered"
    if g.run.finalizing:
        return "finalize", g.run.finalize_reason or "final"
    if g.run.reserve or remaining_sec(g, now) <= reserve_sec(g, cfg):
        return "finalize", "deadline"
    if not g.frozen or not g.baseline_ready:
        return "wait", "setup"
    ws = g.workers.get(worker)
    if ws is not None and ws.session is not None:
        return "resume_session", ws.session
    if open_attempt(g, None, LANE_FG) is not None:
        return "wait", "checkpoint in progress"
    if g.degraded and open_attempt(g, None, LANE_BG) is not None:
        return "wait", "checkpoint in progress"     # 降级模式：切换工作区的验证结束前不能开会话
    if any(t.status == REVIEW for t in g.tasks.values()):
        return "wait", "evidence in progress"
    if consecutive_crashes(g, worker) >= cfg.max_crash_restarts:
        return "finalize", "crashes"
    if all_resolved(g):
        if reviews_needed(g, cfg):
            return "review", "final"
        if any(t.review == "running" for t in g.tasks.values()):
            return "wait", "review in progress"
        return "finalize", "complete"
    if sessions_without_progress(g, worker) >= cfg.max_idle_sessions:
        return "finalize", "no_progress"
    return "start_session", session_reason(g, worker)


def begin_finalize(tx: Tx, reason: str) -> None:
    """收尾开始：不再开新的后台验证；取消后台尝试。"""
    if tx.g.run is None or tx.g.run.status != RUN_RUNNING or tx.g.run.finalizing:
        return
    tx.emit("finalize_started", RUNTIME, RULE, reason=reason)
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING and a.lane == LANE_BG:
            supersede_attempt(tx, a.id, "finalize")


def final_status(g: Graph, delivered: Optional[int]) -> str:
    """DONE 的条件见 queries.status_reasons。"""
    from belay.core.queries import status_reasons
    return "INCOMPLETE" if status_reasons(g, delivered) else "DONE"


def deliver(tx: Tx, reason: str, checkpoint: Optional[int] = None, lag: Optional[dict] = None) -> str:
    from belay.core.queries import delivery_checkpoint, done_not_delivered
    abort_attempts(tx, "cancelled")
    g = tx.g
    cid = delivery_checkpoint(g, tx.cfg) if checkpoint is None else int(checkpoint)
    if not is_ancestor(g, cid, g.head):
        cid = delivery_checkpoint(g, tx.cfg)
    from belay.core.queries import status_reasons
    reasons = status_reasons(g, cid)
    status = "INCOMPLETE" if reasons else "DONE"
    cp = g.checkpoints[cid]
    behind = chain_ids(g).index(cid)
    tx.emit("delivered", RUNTIME, RULE, checkpoint=cid, status=status, reason=reason, level=cp.level,
            head=g.head, behind_head=behind, unconfirmed_policy=tx.cfg.deliver_unconfirmed,
            not_delivered=[t.id for t in done_not_delivered(g, cid)], lag=dict(lag or {}),
            full_verified=full_verified(g, cp.tree), status_reasons=reasons)
    return status


def stall_stop(tx: Tx, worker: str) -> None:
    tx.emit("stall_detected", RUNTIME, RULE, kind="sessions_no_progress", action="stop", worker=worker,
            detail=f"{sessions_without_progress(tx.g, worker)} sessions in a row without progress")

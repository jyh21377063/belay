"""状态转换规则：输入（worker 请求、观察、复核结论、时钟）→ 事件。全部是纯函数。

每个规则函数的第一个参数是 Tx：规则在 Tx 上 emit 事件，Tx 立刻把事件应用到自己的图副本上，
所以同一个规则里后面的判断看到的是前面事件之后的状态。runtime 在锁里调用规则，成功后把 tx.events 原样追加到日志；
规则抛出 Rejected 时整个 Tx 被丢弃。

v8：合并是唯一的正式关口，复核者是唯一的裁判。
  - 合并请求（merge_requested）：后台空闲时优先对最新的边界快照（勾掉 todo、交接）发起，很久没有边界快照时才兜底
    合并最新的可测快照；submit、收尾时也发起。
  - 回归门（有测试时，全量）：守护测试必须全过；回归只能由复核者裁决豁免（引文由规则逐字校验）。
  - 复核者：判定“不比上一个合并点差”，同一次复核里逐条判定需求并给出证据等级；规则校验证据等级
    （E3 要求引用的测试在这棵树上通过，E2 要求引用的命令确实执行过），并保证合并链单调：已完成的需求不退回、
    分数不下降。复核者不可用（失败重试后仍失败）时只按回归门合并，需求只由测试（E3）或自述（E0）记下。
  - 交付的永远是链头。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from belay.core.config import BelayConfig
from belay.core.events import (COMPACTOR, DIAGNOSER, LLM, OBSERVED, PLANNER, REVIEWER, RULE, RUNTIME, SELF_REPORT,
                               VERIFIER, Event, worker_actor)
from belay.core.model import (ACTIONABLE, ATT_ADVANCING, ATT_CREATED, ATT_PENDING, ATT_REJECTED, BY_CHECKS,
                              BY_REVIEW, BY_ROLLBACK, BY_SELF, E0, E1, E2, E3, J_BLOCKED, J_DONE, J_NOT_DONE,
                              J_PARTIAL, JOB_CANCELLED, JOB_FINISHED, JOB_RUNNING, JOB_UNKNOWN, LANE_BG, LANE_FG,
                              LEVEL_RANK, LEVELS, REQ_BLOCKED, REQ_DONE, REQ_OPEN, REV_DECIDED, REV_FAILED,
                              REV_RECORDED, REV_RUNNING, RUN_RUNNING, SUB_PENDING, TODO_ACTIVE, TODO_ANCHORED,
                              TODO_COMPLETED, TODO_PENDING, WHERE_LIVE, WHERE_SLOT, WHERE_WORKSPACE, Attempt, Graph,
                              Review, Snapshot)
from belay.core.plan import normalize_ws, quote_in_text
from belay.core.queries import (actionable, broken_requirements, chain, chain_ids, consecutive_crashes,
                                current_todo, delivery_checkpoint, evidence_checks, is_ancestor, judged_on_tree,
                                last_bg_review_t, last_score, last_session, latest_snapshot, mentioned_requirements,
                                next_id, num, open_attempt, open_persistent, open_requirements, open_submit,
                                remaining_sec, reserve_sec, running_review, sessions_without_progress,
                                snapshot_contained, snapshots_in_epoch, status_reasons, submit_accepted,
                                todos_in_order)
from belay.core.reduce import apply
from belay.core.verify import (B_PASS, PASSED, PT_FAIL, PT_PASS, PT_RUNNING, PT_UNTESTED, active_guard, check_unit,
                               classify_baseline, failure_signature, full_verified, guard_set, is_cmd, job_key,
                               jobs_by_tree, point_status, regression_ids, regressions, results_for_tree, units)

BLOCK_KINDS = ("insufficient_info", "environment", "check_conflict")
HANDOFF_REASONS = ("handoff", "session_end")
# 为前台意图拍的快照：由发起者自己发起合并请求，后台不取
FOREGROUND_REASONS = ("submit", "final", "deadline")
# 有人在等结论的合并请求（被拒时定位、诊断并告诉 worker）
DECLARED_TRIGGERS = ("submit",)


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
    tree: str
    raw_tree: str
    files: tuple = ()
    dropped: tuple = ()
    testable: bool = True
    commit: str = ""
    precheck: str = ""
    tool_seq: int = 0
    session: Optional[str] = None


def _running_run(g: Graph) -> bool:
    return g.run is not None and g.run.status == RUN_RUNNING


def _background_ok(g: Graph) -> bool:
    """后台活动（定位）只在正常运行、隔离有效时进行。"""
    return _running_run(g) and not g.run.finalizing and not g.run.reserve and g.baseline_ready and not g.degraded


# ======================================================================== 运行与准备

def start_run(tx: Tx, run_id: str, task: str, budget_sec: float, workers: Iterable[str] = ("w1",),
              public_checks: Iterable[str] = (), verifier: bool = True) -> None:
    from belay.core.model import VERSION
    tx.emit("run_started", RUNTIME, RULE, run_id=run_id, task=task, budget_sec=float(budget_sec),
            deadline_t=tx.now + float(budget_sec), workers=list(workers), public_checks=list(public_checks),
            verifier=verifier, version=VERSION)


def start_clock(tx: Tx) -> None:
    tx.emit("clock_started", RUNTIME, RULE, deadline_t=tx.now + tx.g.run.budget_sec)


def create_base(tx: Tx, commit: str, tree: str) -> None:
    tx.emit("merged", RUNTIME, OBSERVED, checkpoint=0, commit=commit, tree=tree, trigger="baseline")


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
    """两次全量运行的结果归类（一次在工作区、一次在槽位；隔离无效时两次都在工作区）。"""
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


def freeze_plan(tx: Tx, requirements: list[dict], source: str = LLM) -> None:
    tx.emit("requirement_frozen", PLANNER, RULE,
            requirements=[{**r, "origin": "llm" if source == LLM else "rule"} for r in requirements])


# ======================================================================== todo（模块 H）

def _norm_title(s: str) -> str:
    return normalize_ws(s).lower()


def update_todos(tx: Tx, worker: str, todos: list[dict], snapshot: Optional[int] = None) -> list[str]:
    """todo_write 的列表镜像到图上（按标题匹配保持 id 稳定）。新标为 completed 的条目记为 todo_completed，
    锚点是调用方为它强制拍下的快照。todo 只是线索：不改变任何需求的状态。返回新完成的 todo id。"""
    g = tx.g
    existing = todos_in_order(g)
    by_title = {_norm_title(t.title): t for t in existing}
    next_n = max([t.n for t in g.todos.values()] + [0]) + 1
    specs, completed, seen = [], [], set()
    for order, item in enumerate(todos):
        title = str(item.get("content") or "").strip()[:300]
        key = _norm_title(title)
        if not title or key in seen:
            continue
        seen.add(key)
        cur = by_title.get(key)
        status = item.get("status", "pending")
        if cur is None:
            tid, n = f"P{next_n}", next_n
            next_n += 1
        else:
            tid, n = cur.id, cur.n
        if cur is not None and cur.status in (TODO_COMPLETED, TODO_ANCHORED):
            st = cur.status
        elif status == "completed":
            st = TODO_ACTIVE
            completed.append(tid)
        else:
            st = TODO_ACTIVE if status == "in_progress" else TODO_PENDING
        specs.append({"id": tid, "n": n, "title": title, "status": st, "order": order,
                      "requirements": mentioned_requirements(g, title)})
    old = {t.id: (t.title, t.status, t.order, t.requirements) for t in existing}
    new = {x["id"]: (x["title"], x["status"], x["order"], tuple(x["requirements"])) for x in specs}
    dropped = [tid for tid, (_, st, _, _) in old.items() if tid not in new and st in (TODO_PENDING, TODO_ACTIVE)]
    if dropped or any(old.get(tid) != v for tid, v in new.items()):
        tx.emit("todos_updated", worker_actor(worker), SELF_REPORT, worker=worker, todos=specs,
                diff={"added": sorted(set(new) - set(old), key=num), "dropped": dropped,
                      "changed": sorted((t for t in new if t in old and old[t] != new[t]), key=num)})
    if completed and snapshot is not None:
        for tid in completed:
            _complete_todo(tx, worker, tid, snapshot)
        refresh_anchors(tx)
        schedule_background(tx)
    return completed if snapshot is not None else []


def newly_completed(g: Graph, todos: list[dict]) -> bool:
    by_title = {_norm_title(t.title): t for t in g.todos.values()}
    for item in todos:
        if item.get("status") == "completed":
            cur = by_title.get(_norm_title(str(item.get("content") or "")))
            if cur is None or cur.status in (TODO_PENDING, TODO_ACTIVE):
                return True
    return False


def _complete_todo(tx: Tx, worker: str, tid: str, snapshot: int) -> None:
    g = tx.g
    t = g.todos[tid]
    if t.status not in (TODO_PENDING, TODO_ACTIVE):
        return
    snap = g.snapshots.get(snapshot)
    head = g.head_cp
    anchor, epoch = snapshot, (snap.epoch if snap else g.epoch)
    if snap is not None and head is not None and snap.tree == head.tree:
        anchor, epoch = head.snapshot, head.epoch
    tx.emit("todo_completed", worker_actor(worker), SELF_REPORT, worker=worker, todo=tid, snapshot=anchor,
            anchor_epoch=epoch)


def refresh_anchors(tx: Tx) -> None:
    """勾掉的 todo：锚点被链上某个同段合并点包含时写 todo_anchored。"""
    for t in sorted(tx.g.todos.values(), key=lambda t: t.n):
        if t.status == TODO_COMPLETED:
            cid = snapshot_contained(tx.g, t.anchor_snapshot, t.anchor_epoch)
            if cid is not None:
                tx.emit("todo_anchored", RUNTIME, RULE, todo=t.id, checkpoint=cid)


# ======================================================================== 快照与后台合并请求（模块 B）

def record_snapshot(tx: Tx, worker: str, obs: SnapObs, reason: str) -> int:
    """记录一张快照；与这个 worker 上一张快照完全相同时不记，返回那一张的序号。
    例外：交接 / 会话结束是要发起合并请求的节点，上一张不是这类快照时照样记一张（树相同）。"""
    g = tx.g
    last = latest_snapshot(g, worker)
    same = last is not None and last.tree == obs.tree and last.raw_tree == obs.raw_tree and last.epoch == g.epoch \
        and last.testable == obs.testable
    if same and not (reason in HANDOFF_REASONS and last.reason not in HANDOFF_REASONS):
        return last.n
    n = g.last_snapshot + 1
    cur = current_todo(g)
    ws = g.workers.get(worker)
    tx.emit("snapshot_taken", RUNTIME, OBSERVED, snapshot=n, worker=worker, tree=obs.tree, raw_tree=obs.raw_tree,
            reason=reason, testable=bool(obs.testable), commit=obs.commit, base=g.head,
            files=[list(x) for x in obs.files][:500], dropped=list(obs.dropped)[:200],
            todo=cur.id if cur else None, session=obs.session or (ws.session if ws else None),
            tool_seq=obs.tool_seq, precheck=obs.precheck[:1000])
    if reason != "todo":                            # 勾掉 todo 的锚点快照：update_todos 记下完成之后再发起（触发是 todo）
        schedule_background(tx)
    return n


def _todos_covered(g: Graph, snap: Snapshot) -> list:
    """合并这张快照会一并锚定的、勾掉了还没锚定的 todo（锚点在链头之后、不晚于这张快照，同一段）。"""
    head = g.head_cp
    lo = head.snapshot if head.epoch == snap.epoch else 0
    return sorted((t for t in g.todos.values() if t.status == TODO_COMPLETED and t.anchor_epoch == snap.epoch
                   and t.anchor_snapshot is not None and lo < t.anchor_snapshot <= snap.n), key=lambda t: t.n)


def _background_candidate(g: Graph, worker: str, mode: str) -> Optional[tuple[Snapshot, str, str]]:
    """后台要合并的快照（这个 worker、同一段、可测、比链头新、比它最近一次合并请求的快照新、树没请求过）：
    优先最新的边界快照——交接 / 会话结束的快照，或勾掉 todo 的锚点快照：worker 自己停下来的完整节点，哪怕它之后又改了
    别的东西；没有边界快照时才取最新的快照（auto，间隔更长的兜底）。同一个 worker 的快照是累积的，较新的边界包含较早
    的边界，所以合并期间攒下的几个 todo 合成一次请求。不回退到最近一次请求之前的快照（被拒的由 worker 按反馈接着改），
    也不越过 worker 撤回定位到的坏改动的快照（revert）。auto 只取最新的可测快照，它不能合并时不退回更早的中间状态。
    mode=handoff 或降级模式只取交接 / 会话结束的快照。返回（快照, 触发, 标签）。"""
    head = g.head_cp
    only_handoff = g.degraded or mode == "handoff"
    tried = {a.tree for a in g.attempts.values() if a.epoch == g.epoch}
    floor = max((a.snapshot for a in g.attempts.values() if a.worker == worker and a.epoch == g.epoch), default=0)
    newest: Optional[Snapshot] = None
    auto_ok = not only_handoff
    for n in sorted(g.snapshots, reverse=True):
        snap = g.snapshots[n]
        if snap.worker != worker or snap.epoch != g.epoch or snap.lost:
            continue
        if (head.epoch == snap.epoch and head.snapshot >= snap.n) or snap.n <= floor:
            break
        if snap.reason in FOREGROUND_REASONS:
            break                                   # submit / 收尾的快照归前台处理
        if snap.testable and snap.tree != head.tree and snap.tree not in tried:
            if snap.reason in HANDOFF_REASONS:
                return snap, "handoff", ""
            if not only_handoff:
                if any(t.status == TODO_COMPLETED and t.anchor_snapshot == snap.n and t.anchor_epoch == snap.epoch
                       for t in g.todos.values()):
                    return snap, "todo", "; ".join(t.title for t in _todos_covered(g, snap))
                if auto_ok:
                    newest = snap
        if snap.testable:
            auto_ok = False                         # auto 只取最新的可测快照，不退回更早的中间状态
        if snap.reason == "revert":
            break                                   # worker 撤回了定位到的坏改动：更早的快照里还带着它
    return (newest, "auto", "") if newest is not None else None


def _bg_due(g: Graph, cfg: BelayConfig, now: float, trigger: str) -> bool:
    """后台复核的节流：距上一次后台复核开始，勾掉 todo 至少 merge_todo_interval_sec（只防连续勾掉琐碎条目时反复请
    复核者），兜底的 auto 至少 merge_min_interval_sec；交接不受限制。只按回归门被拒的请求没有复核，不计入间隔。
    没有复核者时不节流（回归门只花 CPU）。"""
    if not cfg.reviewer or trigger == "handoff":
        return True
    last = last_bg_review_t(g)
    if last is None:
        return True
    gap = cfg.merge_todo_interval_sec if trigger == "todo" else cfg.merge_min_interval_sec
    return now - last >= gap


def schedule_background(tx: Tx) -> None:
    """后台合并线：同一时刻每个 worker 最多一个合并请求，后到的不抢占进行中的（否则 worker 勾 todo 比复核快时链头
    永远不前进）；空闲时重新挑候选：最新的边界快照，没有时到了兜底间隔才取最新快照。submit 仍然取代后台请求。"""
    g, cfg = tx.g, tx.cfg
    if not _running_run(g) or g.run.finalizing or g.run.reserve or not g.baseline_ready or g.head_cp is None or \
            cfg.background == "off" or not g.frozen:
        return
    for w in sorted(g.workers):
        g = tx.g
        if open_attempt(g, w) is not None or open_submit(g, w) is not None:
            continue
        cand = _background_candidate(g, w, cfg.background)
        if cand is None:
            continue
        snap, trig, label = cand
        if not _bg_due(g, cfg, tx.now, trig):
            continue
        request_merge(tx, w, snap.n, trig, lane=LANE_BG, summary=label)


# ======================================================================== 合并请求：回归门 → 复核 → 比较并交换

def request_merge(tx: Tx, worker: str, snapshot: int, trigger: str, lane: str = LANE_FG, summary: str = "",
                  submit: Optional[dict] = None) -> Optional[str]:
    """对一张快照发起合并请求；它与链头相同时返回 None（没有要合并的东西）。
    submit：随这次请求判定的提交（submit_requested 的 payload），在请求有结果之前写入。"""
    g = tx.g
    if not g.baseline_ready or g.head is None:
        raise Rejected("The harness is still setting up; try again shortly.")
    if open_attempt(g, worker, lane) is not None:
        if lane == LANE_FG:
            raise Rejected("A merge request of your work is already in progress.")
        return None
    snap = g.snapshots.get(snapshot)
    if snap is None:
        raise Rejected(f"Unknown snapshot {snapshot}.")
    if snap.tree == g.head_cp.tree:
        return None
    aid = next_id("A", g.attempts)
    tx.emit("merge_requested", worker_actor(worker) if trigger == "submit" else RUNTIME, RULE,
            attempt=aid, worker=worker, trigger=trigger, tree=snap.tree, raw_tree=snap.raw_tree, base=g.head,
            selection=None if g.baseline else [], summary=(summary or "")[:2000], snapshot=snap.n, lane=lane)
    if submit is not None:
        tx.emit("submit_requested", worker_actor(worker), RULE, **submit, attempt=aid)
    if not snap.testable:
        tx.emit("merge_rejected", RUNTIME, RULE, attempt=aid, regressions=[], reason="precheck",
                detail=snap.precheck[:1000] or "the changed files do not compile")
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


def _older_than_head(g: Graph, a: Attempt) -> bool:
    h = g.head_cp
    return h.id != 0 and h.epoch == a.epoch and h.snapshot >= a.snapshot


def _has_gate(a: Attempt) -> bool:
    """这个合并请求有回归门可跑（selection=None 表示全量）；没有测试配置时 selection=()。"""
    return a.selection is None


def _gate(tx: Tx, a: Attempt) -> tuple[str, tuple, tuple]:
    """回归门（全量）：返回 ("wait"|"done", 回归, 不稳定)。没有测试可跑时直接通过。"""
    g, cfg = tx.g, tx.cfg
    if not _has_gate(a):
        return "done", (), ()
    if not full_verified(g, a.tree):
        busy = any(j.tree == a.tree and j.state == JOB_RUNNING and not j.live and j.selection is None
                   for j in g.jobs.values())
        if not busy:
            ensure_job(tx, a.tree, None, "gate", attempt=a.id)
        return "wait", (), ()
    expected = sorted(active_guard(g))
    regs_raw = regressions(expected, _raw_results(g, a.tree))
    if regs_raw and cfg.confirm_regressions:
        cu = list(units(regression_ids(regs_raw)))
        if not _confirm_covers(g, a.tree, cu, JOB_FINISHED):
            if not _confirm_covers(g, a.tree, cu, JOB_RUNNING):
                ensure_job(tx, a.tree, cu, "confirm", attempt=a.id, tag="confirm")
            return "wait", (), ()
    regs = regressions(expected, results_for_tree(g, a.tree))
    flaky = sorted(set(regression_ids(regs_raw)) - set(regression_ids(regs)))
    return "done", regs, tuple(flaky)


def _broken_regs(g: Graph, tree: str) -> tuple[str, ...]:
    """已完成（E3）的需求依据的测试在这棵树上不再通过：按单调规则不能合并（写成回归的格式，带需求编号）。"""
    res = results_for_tree(g, tree)
    return tuple(f"{t} ({res.get(t)}) [{rid} was done]" for rid, tests in broken_requirements(g, tree) for t in tests)


def advance_attempt(tx: Tx, aid: str) -> None:
    """把一个合并请求尽量往前推：被链头超过就取代；回归门缺结果就起作业；然后复核；然后合并或拒绝。"""
    g, cfg = tx.g, tx.cfg
    a = g.attempts[aid]
    if a.status != ATT_PENDING or not _running_run(g):
        return
    if _older_than_head(g, a) or a.tree == g.head_cp.tree:
        return supersede_attempt(tx, aid, "newer merge point")
    st, regs, flaky = _gate(tx, a)
    if st == "wait":
        return
    broken = _broken_regs(tx.g, a.tree)
    v = tx.g.reviews.get(a.review) if a.review else None
    if v is not None and v.status in (REV_RUNNING, REV_RECORDED):
        return
    if v is not None and v.status == REV_DECIDED:
        d = v.decision
        if not d.get("merge"):
            return _reject(tx, aid, "review", regs, flaky, detail="; ".join(d.get("reasons") or [])[:2000])
        if regs or broken:
            return _reject(tx, aid, "regression", regs + broken, flaky)
        return _advance(tx, aid, flaky)
    if broken:
        return _reject(tx, aid, "requirement_regression", regs + broken, flaky)
    failed = [x for x in a.reviews if tx.g.reviews[x].status == REV_FAILED]
    reviewable = cfg.reviewer and len(failed) <= cfg.review_retries and not (failed and tx.g.run.finalizing)
    if regs:
        sub = tx.g.submits.get(a.submit) if a.submit else None
        if reviewable and cfg.waivers and sub is not None and sub.waivers:
            return _request_review(tx, a, regs, flaky)     # worker 认为这些测试与任务原文冲突：由复核者裁决
        return _reject(tx, aid, "regression", regs, flaky)
    if reviewable:
        return _request_review(tx, a, regs, flaky)
    if cfg.reviewer and failed and not _has_gate(a) and a.trigger != "submit":
        # 复核者不可用、又没有回归门：没有任何东西能说明这张快照不比链头差（后台与截止时的快照常常改到一半），
        # 不合并；worker 自己提交的除外（它声明做完了，按自述记下）
        return _reject(tx, aid, "review", regs, flaky,
                       detail="the reviewer gave no verdict and there is no regression gate to fall back on")
    return _advance(tx, aid, flaky)                        # 没有复核者（或复核者不可用）：只按回归门合并


def _advance(tx: Tx, aid: str, flaky: tuple) -> None:
    g = tx.g
    if any(x.status == ATT_ADVANCING for x in g.attempts.values()):
        return                                             # 另一个正在推进：等它落地（ref_advanced 会再推进这一个）
    tx.emit("merge_advancing", RUNTIME, RULE, attempt=aid, parent_commit=g.head_cp.commit, date=tx.now,
            flaky=list(flaky))


def _reject(tx: Tx, aid: str, reason: str, regs: tuple, flaky: tuple, detail: str = "") -> None:
    g = tx.g
    a = g.attempts[aid]
    if reason in ("regression", "requirement_regression") and not detail:
        detail = "; ".join(sorted({j.error[:300] for j in g.jobs.values() if j.tree == a.tree and j.error
                                   and not j.live}))[:1000]
    tx.emit("merge_rejected", RUNTIME, RULE, attempt=aid, regressions=list(regs), flaky=list(flaky), reason=reason,
            detail=detail)
    if reason in ("regression", "requirement_regression") and regs:
        tests = regression_ids(regs)
        tests = [t.split(" [", 1)[0] for t in tests]
        if a.lane == LANE_FG:                       # worker 在等的请求被拒：立即定位与诊断
            loc = start_locate(tx, tests, {"tree": a.tree, "snapshot": a.snapshot}, "rejected", ref=aid)
            if loc is None:
                maybe_diagnose(tx, "rejected", tests, None)
            _repeated_diagnosis(tx, aid)
        else:                                       # 后台的：同一回归连续两个后台请求都在才定位与诊断
            _background_persists(tx, aid)


def supersede_attempt(tx: Tx, aid: str, reason: str) -> None:
    """取代一个还在等结果的合并请求（包括正在复核的：复核随之取消）。它带着的提交：链头已经包含它的快照时转到
    链头上判定，否则记为被拒（取消）。"""
    g = tx.g
    a = g.attempts[aid]
    if a.status != ATT_PENDING:
        return
    contained = _older_than_head(g, a) or a.tree == g.head_cp.tree
    sub = a.submit if a.submit is not None and g.submits[a.submit].status == SUB_PENDING else None
    tx.emit("merge_superseded", RUNTIME, RULE, attempt=aid, reason=reason,
            submit_checkpoint=tx.g.head if sub is not None and contained else None)
    _cancel_review(tx, a.review, f"merge request {aid} was superseded ({reason})")
    if sub is not None and contained:
        _judge_on_head(tx, sub)


def _cancel_review(tx: Tx, vid: Optional[str], reason: str) -> None:
    v = tx.g.reviews.get(vid) if vid else None
    if v is not None and v.status in (REV_RUNNING, REV_RECORDED):
        tx.emit("review_cancelled", RUNTIME, RULE, review=vid, reason=reason[:500])


def ref_advanced(tx: Tx, aid: str, ok: bool, commit: str = "", files: Iterable = (), detail: str = "") -> None:
    """外壳做完 commit-tree + update-ref 之后的观察。重复到达（恢复时）会被忽略。"""
    a = tx.g.attempts.get(aid)
    if a is None or a.status != ATT_ADVANCING:
        return
    if ok:
        cid = max(tx.g.checkpoints) + 1
        tx.emit("merged", RUNTIME, OBSERVED, checkpoint=cid, attempt=aid, commit=commit, tree=a.tree,
                files=[list(f) for f in files])
        _after_merge(tx, cid)
        refresh_anchors(tx)
    else:
        tx.emit("merge_rejected", RUNTIME, RULE, attempt=aid, regressions=[], reason="cas_conflict",
                detail=detail[:1000])
    _cascade(tx)


def abort_attempts(tx: Tx, reason: str, lane: Optional[str] = None) -> None:
    """收尾时仍在进行的合并请求：拒绝（链不动；复核随之取消）。正在 CAS 的不能中止，由外壳做完。"""
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING and (lane is None or a.lane == lane):
            tx.emit("merge_rejected", RUNTIME, RULE, attempt=a.id, regressions=[], reason=reason)
            _cancel_review(tx, a.review, reason)


# ======================================================================== 复核（模块 F）

def _focus(g: Graph, submit_id: Optional[str] = None) -> list[str]:
    """要求复核者判定的需求：还没完成的、只有自述的完成或受阻、worker 这次声明受阻的。"""
    out = {r.id for r in actionable(g) if r.status == REQ_OPEN or r.level == E0 or r.by == BY_SELF}
    s = g.submits.get(submit_id) if submit_id else None
    if s is not None:
        out |= {str(b.get("requirement")) for b in s.blocked}
    return sorted((x for x in out if x in g.requirements), key=num)


def _review_slot(tx: Tx, lane: str) -> bool:
    """同一时刻只有一个复核。前台（submit、收尾）需要复核者时取代正在复核的后台请求。"""
    cur = running_review(tx.g)
    if cur is None:
        return True
    if lane == LANE_FG and cur.attempt is not None:
        a = tx.g.attempts.get(cur.attempt)
        if a is not None and a.lane == LANE_BG and a.status == ATT_PENDING:
            supersede_attempt(tx, a.id, "the reviewer is needed for a foreground request")
            return running_review(tx.g) is None
    return False


def _request_review(tx: Tx, a: Attempt, regs: tuple, flaky: tuple) -> None:
    if not _review_slot(tx, a.lane):
        return                                     # 复核者在忙：复核结束时级联会再推进这个请求
    g = tx.g
    prev = g.reviews.get(a.review) if a.review else None
    vid = next_id("V", g.reviews)
    tx.emit("review_started", RUNTIME, RULE, review=vid, trigger=a.trigger, attempt=a.id, tree=a.tree,
            snapshot=a.snapshot, base=g.head, submit=a.submit, focus=_focus(g, a.submit),
            gate={"regressions": list(regs)[:200], "flaky": list(flaky)[:50], "available": bool(g.baseline)},
            retry_of=prev.id if prev is not None and prev.status == REV_FAILED else None)


def _as_list(v, n: int = 20, width: int = 300) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x)[:width] for x in v if x is not None and str(x).strip()][:n]


_STATUS_WORDS = {"done": J_DONE, "complete": J_DONE, "completed": J_DONE, "implemented": J_DONE, "yes": J_DONE,
                 "partial": J_PARTIAL, "partially": J_PARTIAL, "incomplete": J_PARTIAL,
                 "not_done": J_NOT_DONE, "notdone": J_NOT_DONE, "no": J_NOT_DONE, "missing": J_NOT_DONE,
                 "not_implemented": J_NOT_DONE, "open": J_NOT_DONE, "blocked": J_BLOCKED}


def _num_or_none(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _bool_or_none(x) -> Optional[bool]:
    if isinstance(x, bool):
        return x
    if isinstance(x, str) and x.strip().lower() in ("true", "yes", "merge"):
        return True
    if isinstance(x, str) and x.strip().lower() in ("false", "no", "reject"):
        return False
    return None


def clean_verdict(raw) -> dict:
    """复核者 verdict 的规整（格式，不判断真假）：截断、归一化状态与等级。"""
    v = raw if isinstance(raw, dict) else {}
    reqs = []
    for item in v.get("requirements") or []:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        st = str(item.get("status") or "").strip().lower().replace(" ", "_").replace("-", "_")
        lv = str(item.get("level") or "").strip().upper()
        reqs.append({"id": str(item["id"]).strip()[:20], "status": _STATUS_WORDS.get(st),
                     "level": lv if lv in LEVELS else None,
                     "evidence": _as_list(item.get("evidence"), 10), "tests": _as_list(item.get("tests"), 20, 500),
                     "runs": _as_list(item.get("runs"), 20, 20), "missing": _as_list(item.get("missing"), 10),
                     "regressed": bool(item.get("regressed")), "reason": str(item.get("reason") or "")[:600]})
    waivers = []
    for w in v.get("waivers") or []:
        if isinstance(w, dict):
            waivers.append({"tests": _as_list(w.get("tests"), 50, 500), "quote": str(w.get("quote") or "")[:1000],
                            "reason": str(w.get("reason") or "")[:1000],
                            "requirement": str(w.get("requirement") or "").strip()[:20] or None})
    return {"merge": _bool_or_none(v.get("merge")), "reason": str(v.get("reason") or "")[:1500],
            "summary": str(v.get("summary") or "").strip().split("\n")[0][:300], "requirements": reqs[:300],
            "waivers": waivers[:20], "score": _num_or_none(v.get("score")),
            "score_note": str(v.get("score_note") or "")[:500], "feedback": str(v.get("feedback") or "")[:4000]}


def _validated_level(g: Graph, r, item: dict, res: dict, run_ids: set[str]) -> tuple[str, list[str], list[str]]:
    """证据等级的校验：E3 要求引用的测试在这棵树上有结果（完成：全部通过，且至少一个在原始代码上不通过，
    否则证明不了新行为；没做完：至少一个没通过）；E2 要求引用的命令确实在这次复核里执行过。不够就降级。"""
    level = item.get("level") or E1
    tests = [t for t in item.get("tests") or [] if t in g.baseline]
    runs = [x for x in item.get("runs") or [] if x in run_ids]
    positive = item.get("status") == J_DONE
    if level == E3:
        if positive:
            ok = bool(tests) and all(res.get(t) == PASSED for t in tests) and \
                any(g.baseline.get(t) != B_PASS for t in tests)
        else:
            ok = any(t in res and res[t] != PASSED for t in tests)
        if not ok:
            level = E2
    if level == E2 and not runs:
        level = E1
    return level, (tests if level == E3 else []), (runs if level in (E2, E3) else [])


def decide_review(g: Graph, cfg: BelayConfig, v: Review, verdict: dict, runs: list[dict]) -> dict:
    """复核者结论的校验（纯函数）。返回 {merge, reasons, notes, judgements, mentioned, waivers, score, ...}。

    合并标准：回归门全过（复核者批准、规则校验过的豁免除外）；复核者认为没有破坏性改动；已完成的需求没有被
    这次改动弄坏（需要 E2 / E3 的证据）；分数不比链上最近一次测到的低（容差 score_tolerance）。
    合并不要求任何需求已经完成。"""
    task = g.run.task if g.run else ""
    res = results_for_tree(g, v.tree)
    run_ids = {str(x.get("id")) for x in runs}
    reasons: list[str] = []
    notes: list[str] = []
    # ---- 豁免
    granted: list[dict] = []
    regs = set(regression_ids(v.gate.get("regressions") or ())) if v.attempt else set()
    if regs:
        guard = guard_set(g.baseline)
        budget = cfg.waive_max_tests - len(g.waived)
        taken: set[str] = set()
        for w in verdict.get("waivers") or []:
            if not cfg.waivers:
                notes.append("waivers are disabled for this run")
                break
            q = normalize_ws(w.get("quote") or "")
            if len(q.split()) < 3 or not quote_in_text(q, task):
                notes.append(f"waiver of {', '.join(w['tests'][:3])} ignored: the quote is not verbatim task text")
                continue
            tests = [t for t in w.get("tests") or [] if t in regs and t in guard and not is_cmd(t)
                     and t not in g.waived and t not in taken]
            if len(tests) > budget:
                notes.append(f"at most {cfg.waive_max_tests} checks can be waived in a run")
                tests = tests[:max(0, budget)]
            if tests:
                rid = w.get("requirement") if w.get("requirement") in g.requirements else None
                granted.append({"tests": tests, "quote": q[:1000], "reason": (w.get("reason") or "")[:2000],
                                "requirement": rid})
                taken.update(tests)
                budget -= len(tests)
        left = sorted(regs - taken)
        if left:
            reasons.append(f"{len(left)} regression(s) are not waived: " + ", ".join(left[:8]))
    # ---- 需求
    judgements: list[dict] = []
    mentioned: list[str] = []
    declared: dict[str, dict] = {}
    s = g.submits.get(v.submit) if v.submit else None
    if s is not None:
        declared = {str(b.get("requirement")): dict(b) for b in s.blocked}
    for item in verdict.get("requirements") or []:
        r = g.requirements.get(item["id"])
        if r is None or r.kind != ACTIONABLE or item.get("status") is None or r.id in mentioned:
            continue
        mentioned.append(r.id)
        status = item["status"]
        level, tests, used_runs = _validated_level(g, r, item, res, run_ids)
        missing = list(item.get("missing") or [])
        evidence = list(item.get("evidence") or [])
        if status == J_DONE and level == E0:
            status, missing = J_PARTIAL, missing + ["no evidence beyond the agent's own claim"]
        if status == J_BLOCKED and r.id not in declared and r.status != REQ_BLOCKED:
            # 受阻要由 worker 声明、复核者认可：复核者自己认为做不了的，只告诉 worker（它可以在 submit 里声明）
            status = J_NOT_DONE
            missing = missing + [f"the reviewer thinks it cannot be done here: {(item.get('reason') or '')[:300]}"]
            notes.append(f"{r.id}: judged blocked without the agent declaring it; recorded as not done")
        base = {"requirement": r.id, "evidence": evidence, "missing": missing}
        if r.status == REQ_DONE and status != J_DONE:
            strong = level in (E2, E3)
            if r.level == E0 or strong:
                if item.get("regressed") and strong and v.attempt is not None:
                    reasons.append(f"{r.id} was done (merge point {r.checkpoint}) and this change breaks it"
                                   + (f": {'; '.join(missing[:3])}" if missing else ""))
                    continue
                judgements.append({**base, "status": REQ_OPEN, "judgement": status, "reason": "reassessed",
                                   "level": level, "runs": used_runs, "tests": tests})
            else:
                notes.append(f"{r.id} was judged {status} by reading only; it stays done ({r.level}, merge point "
                             f"{r.checkpoint}): changing a finished requirement needs a test or a command that "
                             "shows the problem")
            continue
        if r.status == REQ_DONE:                   # 仍然完成：证据等级只升不降
            if LEVEL_RANK[level] > LEVEL_RANK.get(r.level or E0, 0):
                judgements.append({**base, "status": REQ_DONE, "judgement": J_DONE, "level": level, "tests": tests,
                                   "runs": used_runs, "reason": "stronger evidence"})
            continue
        if status == J_DONE:
            judgements.append({**base, "status": REQ_DONE, "judgement": J_DONE, "level": level, "tests": tests,
                               "runs": used_runs, "reason": "review"})
        elif status == J_BLOCKED:
            d = declared.get(r.id) or {}
            judgements.append({**base, "status": REQ_BLOCKED, "judgement": J_BLOCKED, "reason": "review",
                               "blocked_kind": d.get("kind") or r.blocked_kind or "reviewer",
                               "blocked_reason": (item.get("reason") or d.get("reason") or "; ".join(missing))[:2000],
                               "blocked_quote": d.get("quote")})
        else:
            if r.id in declared and not missing and item.get("reason"):
                missing = [item["reason"]]
            judgements.append({**base, "missing": missing, "status": REQ_OPEN, "judgement": status,
                               "reason": "blocked_not_accepted" if r.id in declared or r.status == REQ_BLOCKED
                               else "review"})
    # ---- 分数
    score = verdict.get("score") if run_ids else None
    if verdict.get("score") is not None and not run_ids:
        notes.append("the score was not used: no command was run in this review")
    prev, prev_note, prev_cp = last_score(g)
    if v.attempt is not None and score is not None and prev is not None and \
            score < prev - cfg.score_tolerance * abs(prev):
        reasons.append(f"the score dropped from {prev:g} (merge point {prev_cp}) to {score:g}")
    # ---- 合并
    merge = None
    if v.attempt is not None:
        if not verdict.get("merge"):
            reasons.insert(0, "the reviewer did not approve the merge: " + (verdict.get("reason") or "no reason given"))
        merge = not reasons
    return {"merge": merge, "reasons": reasons, "notes": notes, "judgements": judgements, "mentioned": mentioned,
            "waivers": granted, "score": score, "score_note": verdict.get("score_note") or "",
            "label": verdict.get("summary") or "", "feedback": verdict.get("feedback") or ""}


def record_review(tx: Tx, vid: str, verdict: Optional[dict], runs: Iterable[dict] = (), failed: bool = False,
                  error: str = "", transcript: Optional[str] = None) -> None:
    """复核者会话结束（llm）：原样记下结论，再由规则校验，写豁免与决定；然后推进合并请求或判定提交。"""
    g = tx.g
    v = g.reviews.get(vid)
    if v is None or v.status != REV_RUNNING or not _running_run(g):
        return
    clean = clean_verdict(verdict) if verdict is not None and not failed else {}
    if not failed:
        if v.attempt is not None and clean.get("merge") is None:
            failed, error = True, error or "the verdict does not say whether to merge"
        elif v.attempt is None and not clean.get("requirements"):
            failed, error = True, error or "the verdict judges no requirement"
    runs = [{"id": str(x.get("id")), "cmd": str(x.get("cmd") or "")[:500],
             "rc": int(x["rc"]) if isinstance(x.get("rc"), int) else None} for x in runs or ()][:200]
    tx.emit("merge_reviewed", REVIEWER, LLM, review=vid, verdict=clean, runs=runs, failed=failed,
            error=(error or "")[:2000], transcript=transcript)
    if not failed:
        d = decide_review(tx.g, tx.cfg, tx.g.reviews[vid], clean, runs)
        waivers = d.pop("waivers")
        tx.emit("review_decided", RUNTIME, RULE, review=vid, waived=[t for w in waivers for t in w["tests"]], **d)
        for w in waivers:
            tx.emit("waiver_granted", REVIEWER, RULE, review=vid, **w)
        if v.attempt is None:                       # 只判定：判定落在被判定的合并点上
            _apply_judgements(tx, vid, v.checkpoint)
            if v.submit is not None:
                finish_submit(tx, v.submit)
    _cascade(tx)


def _apply_judgements(tx: Tx, vid: str, cid: int) -> None:
    g = tx.g
    if not _running_run(g) or cid not in g.checkpoints or not is_ancestor(g, cid, g.head):
        return
    for j in tx.g.reviews[vid].decision.get("judgements") or []:
        r = tx.g.requirements.get(j["requirement"])
        if r is None:
            continue
        if j["status"] == REQ_DONE and j.get("level") == E3:   # E3 的测试必须在这个合并点上通过
            res = results_for_tree(tx.g, tx.g.checkpoints[cid].tree)
            if not all(res.get(t) == PASSED for t in j.get("tests") or ()):
                continue
        tx.emit("requirement_judged", REVIEWER, RULE, requirement=r.id, status=j["status"],
                judgement=j.get("judgement"), level=j.get("level") if j["status"] == REQ_DONE else None,
                evidence=j.get("evidence") or [], tests=j.get("tests") or [], runs=j.get("runs") or [],
                missing=j.get("missing") or [], checkpoint=cid, review=vid, by=BY_REVIEW, reason=j.get("reason"),
                blocked_kind=j.get("blocked_kind"), blocked_reason=j.get("blocked_reason"),
                blocked_quote=j.get("blocked_quote"))


def _auto_checks(tx: Tx, cid: int) -> None:
    """规划器关联的证据检查（原始代码上不通过的已有测试）在合并点上全部通过 → 完成（E3，规则）。"""
    g = tx.g
    res = results_for_tree(g, g.checkpoints[cid].tree)
    for r in actionable(g):
        ev = evidence_checks(g, r)
        if not ev or not all(res.get(c) == PASSED for c in ev):
            continue
        if r.status == REQ_DONE and r.level == E3:
            continue
        tx.emit("requirement_judged", RUNTIME, RULE, requirement=r.id, status=REQ_DONE, judgement=J_DONE, level=E3,
                tests=ev, evidence=[f"{c} passes" for c in ev][:10], missing=[], checkpoint=cid, by=BY_CHECKS,
                reason="checks")


def _self_report(tx: Tx, sid: str, cid: int) -> None:
    """没有复核者（关闭或不可用）时的提交：受阻的声明记为自述受阻；证据检查没过的仍未完成；其余记为自述完成（E0）。"""
    g = tx.g
    s = g.submits[sid]
    declared = {str(b.get("requirement")): b for b in s.blocked}
    res = results_for_tree(g, g.checkpoints[cid].tree)
    w = worker_actor(s.worker)
    for r in open_requirements(g):
        ev = evidence_checks(g, r)
        if r.id in declared:
            b = declared[r.id]
            tx.emit("requirement_judged", w, SELF_REPORT, requirement=r.id, status=REQ_BLOCKED, judgement=J_BLOCKED,
                    checkpoint=cid, by=BY_SELF, reason="self_report", blocked_kind=b.get("kind"),
                    blocked_reason=b.get("reason"), blocked_quote=b.get("quote"), evidence=[], missing=[])
        elif ev and not all(res.get(c) == PASSED for c in ev):
            tx.emit("requirement_judged", RUNTIME, RULE, requirement=r.id, status=REQ_OPEN, judgement=J_NOT_DONE,
                    checkpoint=cid, by=BY_CHECKS, reason="checks fail",
                    missing=[f"{c} ({res.get(c, 'MISSING')})" for c in ev if res.get(c) != PASSED][:10])
        else:
            tx.emit("requirement_judged", w, SELF_REPORT, requirement=r.id, status=REQ_DONE, judgement=J_DONE,
                    level=E0, checkpoint=cid, by=BY_SELF, reason="self_report",
                    evidence=["the agent's submit summary (not verified)"], missing=[])


def _after_merge(tx: Tx, cid: int) -> None:
    """合并点落地：复核者的判定、测试的判定写进账本；提交得到结论。"""
    g = tx.g
    cp = g.checkpoints[cid]
    a = g.attempts[cp.attempt]
    if cp.review is not None:
        _apply_judgements(tx, cp.review, cid)
    _auto_checks(tx, cid)
    if a.submit is not None and tx.g.submits[a.submit].status == SUB_PENDING:
        if cp.review is None:
            _self_report(tx, a.submit, cid)
        finish_submit(tx, a.submit)


# ======================================================================== 持续性回归

def _background_persists(tx: Tx, aid: str) -> None:
    """后台合并请求被拒，且上一个被拒的后台请求（另一棵树、同一段）也有同样的回归：不是改到一半的临时状态，
    记为持续性回归，定位并诊断（结果在 worker 的下一轮作为提示送达，不打断它）。同一组测试只做一次。"""
    g = tx.g
    a = g.attempts[aid]
    snap = g.snapshots.get(a.snapshot)
    if snap is None or not _running_run(g) or g.run.finalizing:
        return
    prev = [x for x in g.attempts.values() if x.lane == LANE_BG and x.status == ATT_REJECTED and x.id != aid
            and x.regressions and x.created_seq < a.created_seq and x.tree != a.tree and x.snapshot in g.snapshots
            and g.snapshots[x.snapshot].epoch == snap.epoch]
    if not prev:
        return
    p = max(prev, key=lambda x: x.created_seq)
    common = sorted(set(regression_ids(a.regressions)) & set(regression_ids(p.regressions)))
    common = [t.split(" [", 1)[0] for t in common]
    common = [t for t in common if not is_cmd(t) and not open_persistent(g, t)]
    if not common:
        return
    tx.emit("persistent_regression", RUNTIME, RULE, tests=common[:50], trigger="background", checkpoint=None,
            since=p.snapshot, epoch=snap.epoch, latest=a.snapshot)
    loc = start_locate(tx, common, {"tree": a.tree, "snapshot": a.snapshot}, "background", ref=aid)
    if loc is None:
        maybe_diagnose(tx, "background", common, None)


# ======================================================================== 快照二分定位（模块 D3）

def locate_points(g: Graph, lid: str) -> list[dict]:
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
    last = len(pts) - 1
    sts = []
    for i, p in enumerate(pts):
        if i == last:
            sts.append(PT_FAIL)
        elif i == 0 and p["kind"] == "checkpoint" and p["id"] == 0 and test in guard_set(g.baseline):
            sts.append(PT_PASS)
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
    if not between:
        return {"state": "done", "gi": gi, "bi": bi, "exact": bi == gi + 1}
    mid = between[len(between) // 2]
    return {"state": "need", "mid": mid, "gi": gi, "bi": bi, "running": False}


def start_locate(tx: Tx, tests: Iterable[str], bad: dict, trigger: str, ref: Optional[str] = None) -> Optional[str]:
    """按规则定位：从“最后一次已知通过”到坏端二分。返回定位 id；不做定位时返回 None。"""
    g, cfg = tx.g, tx.cfg
    tests = sorted(set(t for t in tests if not is_cmd(t)))
    if not cfg.locate or not _background_ok(g) or not tests or not g.baseline:
        return None
    bad_n = bad.get("snapshot")
    snap = g.snapshots.get(bad_n) if bad_n is not None else None
    epoch = snap.epoch if snap is not None else g.epoch
    if epoch != g.epoch:
        return None
    for loc in g.locates.values():
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
    for _ in range(64):
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
        if tx.g.jobs[jid].state == JOB_RUNNING:
            return


def _attribution(g: Graph, p: dict) -> dict:
    if p.get("kind") == "snapshot" and p.get("id") in g.snapshots:
        s = g.snapshots[p["id"]]
        return {"snapshot": s.n, "todo": s.todo, "session": s.session}
    return {"checkpoint": p.get("id")}


def _conclude_locate(tx: Tx, lid: str, pts: list[dict], iv: dict) -> None:
    groups: dict[tuple, dict] = {}
    for t, v in sorted(iv.items()):
        if v["state"] == "done":
            gi, bi, exact = v["gi"], v["bi"], v["exact"]
        else:
            gi = v["gi"] if v.get("gi") is not None else 0
            bi, exact = v["bi"], False
        key = (gi, bi)
        grp = groups.setdefault(key, {"tests": [], "good": pts[gi], "bad": pts[bi], "exact": exact,
                                      "attribution": _attribution(tx.g, pts[bi])})
        grp["tests"].append(t)
        grp["exact"] = grp["exact"] and exact
    tx.emit("locate_concluded", RUNTIME, RULE, locate=lid, groups=list(groups.values()))


def record_located(tx: Tx, lid: str, group: int, files: Iterable, diff: Optional[str]) -> None:
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
    if _running_run(tx.g) and not tx.g.run.finalizing and loc.trigger != "review":
        maybe_diagnose(tx, loc.trigger, grp["tests"], lid, group)


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
    g = tx.g
    a = g.attempts[aid]
    sig = failure_signature(regression_ids(a.regressions))
    same = [x for x in g.attempts.values() if x.status == ATT_REJECTED and x.regressions and
            x.lane == LANE_FG and failure_signature(regression_ids(x.regressions)) == sig]
    if len(same) != 2:
        return
    prev = [d for d in g.diagnoses.values() if d.status == "recorded" and failure_signature(d.tests) == sig]
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


# ======================================================================== 提交（请求立即复核）

def _check_blocked(g: Graph, blocked: Iterable[dict]) -> list[dict]:
    out, problems = [], []
    for b in blocked or ():
        if not isinstance(b, dict):
            problems.append("each blocked entry is an object {requirement, kind, reason, quote}")
            continue
        rid = str(b.get("requirement") or "").strip()
        kind = str(b.get("kind") or "").strip()
        reason = str(b.get("reason") or "").strip()
        quote = b.get("quote")
        r = g.requirements.get(rid)
        if r is None or r.kind != ACTIONABLE:
            problems.append(f"{rid or '(no id)'}: not a requirement on the checklist")
        elif kind not in BLOCK_KINDS:
            problems.append(f"{rid}: kind must be one of {', '.join(BLOCK_KINDS)}")
        elif not reason:
            problems.append(f"{rid}: give a reason")
        elif kind == "check_conflict" and (not quote or not quote_in_text(str(quote), g.run.task)):
            problems.append(f"{rid}: a check_conflict must quote the task text verbatim (quote=...); for specific "
                            "gate tests that fail on your change, propose waivers instead")
        else:
            out.append({"requirement": rid, "kind": kind, "reason": reason[:2000],
                        "quote": normalize_ws(str(quote))[:1000] if quote else None})
    if problems:
        raise Rejected("Nothing was submitted:\n" + "\n".join(f"- {p}" for p in problems[:20]))
    return out


def _check_waivers(g: Graph, cfg: BelayConfig, waivers: Iterable[dict]) -> list[dict]:
    """worker 提议的豁免：只校验格式（引文逐字、测试在回归门里），是否采纳由复核者裁决。"""
    waivers = list(waivers or ())
    if not waivers:
        return []
    if not cfg.waivers:
        raise Rejected("Waivers are disabled for this run: keep the existing behaviour, or report the conflict in "
                       "submit(blocked=[{requirement, kind: \"check_conflict\", reason, quote}]).")
    out, problems = [], []
    guard = guard_set(g.baseline)
    for w in waivers:
        if not isinstance(w, dict):
            problems.append("each waiver is an object {tests, quote, reason}")
            continue
        tests = [str(t).strip() for t in (w.get("tests") or []) if str(t).strip()]
        q = normalize_ws(str(w.get("quote") or ""))
        reason = str(w.get("reason") or "").strip()
        bad = [t for t in tests if t not in guard or is_cmd(t)]
        if not tests:
            problems.append("name the tests (tests=[...], full ids as the gate reports them)")
        elif bad:
            problems.append(f"not in the regression gate: {', '.join(bad[:5])}")
        elif len(q.split()) < 3 or not quote_in_text(q, g.run.task):
            problems.append("quote must be at least three words copied verbatim from the task text that ask for the "
                            "new behaviour")
        elif not reason:
            problems.append("give a reason: how each test contradicts the task text")
        else:
            rid = str(w.get("requirement") or "").strip() or None
            out.append({"tests": tests[:50], "quote": q[:1000], "reason": reason[:2000],
                        "requirement": rid if rid in g.requirements else None})
    if problems:
        raise Rejected("Nothing was submitted:\n" + "\n".join(f"- {p}" for p in problems[:20]))
    return out


def request_submit(tx: Tx, worker: str, snapshot: int, summary: str = "", blocked: Iterable[dict] = (),
                   implicit: bool = False, waivers: Iterable[dict] = ()) -> str:
    """worker 请求立即复核：对调用方刚强制拍下的快照发起前台合并请求（取代后台的）；快照就是链头时只判定需求。"""
    g = tx.g
    if not g.baseline_ready or g.head is None or not g.frozen:
        raise Rejected("The harness is still setting up; try again shortly.")
    if open_submit(g, worker) is not None:
        raise Rejected("Your previous submit is still being reviewed; wait for its result.")
    if open_attempt(g, worker, LANE_FG) is not None:
        raise Rejected("A merge request of your work is already in progress.")
    snap = g.snapshots.get(snapshot)
    if snap is None:
        raise Rejected("No snapshot of your working tree yet.")
    clean = _check_blocked(g, blocked)
    props = _check_waivers(g, tx.cfg, waivers)
    sid = next_id("U", g.submits)
    payload = dict(submit=sid, worker=worker, snapshot=snap.n, summary=(summary or "")[:4000], blocked=clean,
                   waivers=props, implicit=implicit)
    for a in list(g.attempts.values()):              # 新的前台请求胜出：后台的请求让路（作业结果按树复用）
        if a.worker == worker and a.lane == LANE_BG and a.status == ATT_PENDING:
            supersede_attempt(tx, a.id, "a submit")
    if snap.tree == tx.g.head_cp.tree:
        tx.emit("submit_requested", worker_actor(worker), RULE, **payload, checkpoint=tx.g.head)
        _judge_on_head(tx, sid)
    else:
        request_merge(tx, worker, snap.n, "submit", summary=(summary or "").strip().split("\n")[0][:300],
                      submit=payload)
    return sid


def _judge_on_head(tx: Tx, sid: str) -> None:
    """提交的快照就是链头（没有新的改动）：还没有在这棵树上判定过的需求请复核者看一眼（只判定、不合并）；
    都判定过了就直接按账本回答。没有复核者（或复核者不可用）时按自述记下。"""
    g, cfg = tx.g, tx.cfg
    s = g.submits.get(sid)
    if s is None or s.status != SUB_PENDING or s.attempt is not None and g.attempts[s.attempt].status == ATT_PENDING:
        return
    head = g.head_cp
    prev = g.reviews.get(s.review) if s.review else None
    if prev is not None and prev.status in (REV_RUNNING, REV_RECORDED):
        return
    failed = [v for v in g.reviews.values() if v.submit == sid and v.attempt is None and v.status == REV_FAILED]
    declared = [str(b["requirement"]) for b in s.blocked if g.requirements[str(b["requirement"])].status != REQ_BLOCKED
                or g.requirements[str(b["requirement"])].by == BY_SELF]
    opens = [r.id for r in open_requirements(g)]
    if not opens and not declared:
        return finish_submit(tx, sid)
    if cfg.reviewer and len(failed) <= cfg.review_retries and not (failed and g.run.finalizing):
        judged = judged_on_tree(g, head.tree)
        need = [r for r in opens if r not in judged] + declared      # 新的受阻声明总要复核者看一眼
        if head.id == 0 or head.review is None:
            need = opens + declared
        if need:
            if not _review_slot(tx, LANE_FG):
                return                              # 复核者在忙：复核结束时级联会再来
            vid = next_id("V", tx.g.reviews)
            tx.emit("review_started", RUNTIME, RULE, review=vid, trigger="judge", checkpoint=tx.g.head, tree=head.tree,
                    snapshot=s.snapshot, base=tx.g.head, submit=sid, focus=_focus(tx.g, sid), gate={},
                    retry_of=failed[-1].id if failed else None)
            return
        return finish_submit(tx, sid)
    _self_report(tx, sid, g.head)
    finish_submit(tx, sid)


def finish_submit(tx: Tx, sid: str) -> None:
    """提交的结论：合并（或只判定）之后还有没完成的 actionable 需求 → 交还清单；没有 → 接受（运行可以收尾）。"""
    g = tx.g
    s = g.submits.get(sid)
    if s is None or s.status != SUB_PENDING:
        return
    if s.attempt is not None and g.attempts[s.attempt].status != ATT_CREATED and s.checkpoint is None:
        return
    if s.review is not None and g.reviews[s.review].status in (REV_RUNNING, REV_RECORDED):
        return
    left = [r.id for r in open_requirements(g)]
    tx.emit("submit_updated", RUNTIME, RULE, submit=sid, status="returned" if left else "accepted", open=left,
            checkpoint=s.checkpoint if s.checkpoint is not None else g.head)


# ======================================================================== 作业

def job_preempted(tx: Tx, job_id: str) -> None:
    j = tx.g.jobs.get(job_id)
    if j is not None and j.state == JOB_RUNNING:
        tx.emit("job_preempted", VERIFIER, OBSERVED, job=job_id)


def job_finished(tx: Tx, job_id: str, state: str, results: dict, sec: float = 0.0, error: str = "",
                 reasons: Optional[dict] = None) -> None:
    """作业结果（观察）→ 级联：重跑丢失的作业、推进在等结果的合并请求、定位。"""
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
            for a in list(tx.g.attempts.values()):
                if a.status == ATT_PENDING and job_id in a.jobs:
                    tx.emit("merge_rejected", RUNTIME, RULE, attempt=a.id, regressions=[], reason="cancelled")
                    _cancel_review(tx, a.review, "cancelled at the deadline")
            return
    elif state == JOB_UNKNOWN and not job.live:
        att = job.attempt if job.attempt and tx.g.attempts[job.attempt].status == ATT_PENDING else None
        ensure_job(tx, job.tree, job.selection, job.purpose, attempt=att, tag=job.tag, where=job.where,
                   locate=job.locate, checkpoint=job.checkpoint)
    _cascade(tx)


def _cascade(tx: Tx) -> None:
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING:
            advance_attempt(tx, a.id)
    for s in list(tx.g.submits.values()):            # 只判定的提交：复核者空出来了、或复核失败要重试
        if s.status != SUB_PENDING:
            continue
        a = tx.g.attempts.get(s.attempt) if s.attempt else None
        if a is None or (a.status not in (ATT_PENDING, ATT_ADVANCING, ATT_CREATED) and s.checkpoint is not None):
            _judge_on_head(tx, s.id)
        elif a.status == ATT_CREATED:
            finish_submit(tx, s.id)
    for lid in [l.id for l in tx.g.locates.values() if l.status == "running"]:
        advance_locate(tx, lid)
    schedule_background(tx)


# ======================================================================== 回退

def rollback(tx: Tx, worker: str, to: Optional[int] = None) -> int:
    """回退（只由恢复流程使用：容器重建后丢了链上的合并点）。"""
    g = tx.g
    to = g.head if to is None else int(to)
    ids = chain_ids(g)
    if to not in ids:
        raise Rejected(f"Merge point {to} is not on the chain ({', '.join(map(str, ids))}).")
    if open_attempt(g, None, LANE_FG) is not None or any(a.status == ATT_ADVANCING for a in g.attempts.values()):
        raise Rejected("A merge is in progress; roll back after it finishes.")
    if open_submit(g) is not None:
        raise Rejected("A submit is being reviewed; roll back after it finishes.")
    for a in list(g.attempts.values()):
        if a.status == ATT_PENDING and a.lane == LANE_BG:
            supersede_attempt(tx, a.id, "rollback")
    g = tx.g
    abandoned = ids[:ids.index(to)]
    for r in actionable(g):
        if r.status in (REQ_DONE, REQ_BLOCKED) and r.checkpoint in abandoned:
            tx.emit("requirement_judged", worker_actor(worker), RULE, requirement=r.id, status=REQ_OPEN,
                    judgement=None, by=BY_ROLLBACK, reason="rolled_back", checkpoint=to,
                    missing=[f"merge point {r.checkpoint} was rolled back"])
    keep = [c for c in chain(tx.g) if c.id not in abandoned]
    for t in sorted(tx.g.todos.values(), key=lambda t: t.n):
        if t.status not in (TODO_COMPLETED, TODO_ANCHORED):
            continue
        ok = any(c.epoch == t.anchor_epoch and c.snapshot >= (t.anchor_snapshot or 0) for c in keep)
        if not ok:
            tx.emit("todo_invalidated", worker_actor(worker), RULE, todo=t.id, reason="rolled_back")
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
    schedule_background(tx)                          # 到了间隔的后台合并请求


def _review_rejections_in_row(g: Graph, worker: str) -> list[Attempt]:
    """最近一次合并之后，连续被复核者拒绝的合并请求。"""
    out = []
    for a in sorted((a for a in g.attempts.values() if a.worker == worker and a.status in (ATT_REJECTED, ATT_CREATED)
                     and a.reason != "cancelled"), key=lambda a: a.created_seq, reverse=True):
        if a.status == ATT_CREATED:
            break
        if a.reason == "review":
            out.append(a)
        elif a.lane == LANE_FG:
            break
    return out


def detect_stalls(tx: Tx) -> None:
    g, cfg = tx.g, tx.cfg
    since = [s for s in g.stalls if s.seq > g.last_progress_seq]
    for w, ws in sorted(g.workers.items()):
        if ws.session is None:
            continue
        idle_since = g.last_progress_t
        if tx.now - idle_since > cfg.stall_no_progress_sec and not any(x.kind == "no_progress" for x in since):
            tx.emit("stall_detected", RUNTIME, RULE, kind="no_progress", action="hint", worker=w,
                    detail=f"no progress for {int((tx.now - idle_since) / 60)} min")
            return
        mine = sorted((a for a in g.attempts.values() if a.worker == w and a.status in (ATT_REJECTED, ATT_CREATED)
                       and a.lane == LANE_FG), key=lambda a: a.created_seq)[-cfg.stall_same_failure:]
        if len(mine) == cfg.stall_same_failure and all(a.status == ATT_REJECTED and a.regressions for a in mine):
            sigs = {failure_signature(a.regressions) for a in mine}
            if len(sigs) == 1:
                sig = sigs.pop()
                if not any(x.kind == "repeated_failure" and sig in x.detail for x in since):
                    tx.emit("stall_detected", RUNTIME, RULE, kind="repeated_failure", action="hint", worker=w,
                            detail=f"signature {sig}: the same {len(mine[-1].regressions)} regression(s) "
                                   f"rejected {len(mine)} submits in a row")
                    return
        rej = _review_rejections_in_row(g, w)
        if len(rej) >= cfg.stall_same_failure:
            last_merge = max((c.created_seq for c in g.checkpoints.values()), default=0)
            if not any(x.kind == "review_rejections" and x.seq > last_merge for x in g.stalls):
                latest = rej[0]
                tx.emit("stall_detected", RUNTIME, RULE, kind="review_rejections", action="hint", worker=w,
                        detail=f"{len(rej)} merge requests in a row were not approved by the reviewer; latest "
                               f"({latest.id}): {latest.detail[:600]}")
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
                error: Optional[str] = None) -> Optional[str]:
    ws = tx.g.workers.get(worker)
    if ws is None or ws.session is None:
        return None
    sid = ws.session
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


# ======================================================================== 运行的结束

def next_step(g: Graph, worker: str, now: float, cfg: BelayConfig) -> tuple[str, str]:
    """会话结束不等于运行结束。返回 (动作, 理由)：stop | finalize | start_session | resume_session | wait。"""
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
        return "wait", "merge in progress"
    if g.degraded and open_attempt(g, None, LANE_BG) is not None:
        return "wait", "merge in progress"
    if open_submit(g, worker) is not None:
        return "wait", "submit in progress"
    if submit_accepted(g, worker):
        return "finalize", "complete"
    if consecutive_crashes(g, worker) >= cfg.max_crash_restarts:
        return "finalize", "crashes"
    if sessions_without_progress(g, worker) >= cfg.max_idle_sessions:
        return "finalize", "no_progress"
    return "start_session", session_reason(g, worker)


def begin_finalize(tx: Tx, reason: str) -> None:
    """收尾开始：不再发起后台合并请求；取代正在进行的后台请求（复核随之取消）。"""
    if tx.g.run is None or tx.g.run.status != RUN_RUNNING or tx.g.run.finalizing:
        return
    tx.emit("finalize_started", RUNTIME, RULE, reason=reason)
    for a in list(tx.g.attempts.values()):
        if a.status == ATT_PENDING and a.lane == LANE_BG:
            supersede_attempt(tx, a.id, "finalize")
    _cascade(tx)                                    # 复核者空出来了：在等它的前台请求（submit）接着走


def final_status(g: Graph, delivered: Optional[int]) -> str:
    return "INCOMPLETE" if status_reasons(g, delivered) else "DONE"


def deliver(tx: Tx, reason: str, checkpoint: Optional[int] = None, lag: Optional[dict] = None) -> str:
    """交付链头（合并链单调，链头就是最好的结果）。"""
    abort_attempts(tx, "cancelled")
    v = running_review(tx.g)
    if v is not None:
        _cancel_review(tx, v.id, "the run was delivered")
    g = tx.g
    cid = delivery_checkpoint(g) if checkpoint is None else int(checkpoint)
    if not is_ancestor(g, cid, g.head):
        cid = delivery_checkpoint(g)
    reasons = status_reasons(g, cid)
    status = "INCOMPLETE" if reasons else "DONE"
    cp = g.checkpoints[cid]
    tx.emit("delivered", RUNTIME, RULE, checkpoint=cid, status=status, reason=reason, head=g.head,
            behind_head=chain_ids(g).index(cid), score=cp.score, lag=dict(lag or {}),
            status_reasons=reasons)
    return status


def stall_stop(tx: Tx, worker: str) -> None:
    tx.emit("stall_detected", RUNTIME, RULE, kind="sessions_no_progress", action="stop", worker=worker,
            detail=f"{sessions_without_progress(tx.g, worker)} sessions in a row without progress")

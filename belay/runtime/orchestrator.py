"""Orchestrator：单写者循环。取消息 → decide() → 落库 → 执行动作。

整个 Belay 跑在同一个 asyncio 事件循环里：Orchestrator 是唯一消费收件箱、唯一写状态的协程；
worker、作业、Test Author、Reviewer 都是后台协程，只通过收件箱交换消息，所以不需要锁。

  orch = Orchestrator(...)
  status = await orch.run()          # 直到 DONE / INCOMPLETE
  await orch.shutdown(deliver=True)  # 停止一切，把集成分支 HEAD 检出到工作区（交付物），导出账本与事件

WorkerRuntime 是工具看到的 RuntimeClient：在把请求放进收件箱之前，先取好需要 IO 的事实（工作区快照、候选提交）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from belay.config import RuntimeConfig, RuntimePaths
from belay.env import Env
from belay.graph.build import initial_state
from belay.graph.invariants import violations
from belay.graph.ledger import counts, ledger_text, statuses
from belay.graph.model import Event, GraphState, Put
from belay.graph.requirements import extract_requirements, notes_block
from belay.graph.store import Store, init_state
from belay.runtime.bootstrap import SetupInfo
from belay.runtime.decide import decide
from belay.runtime.effects import Effects
from belay.runtime.gitops import ShadowRepo
from belay.runtime.jobs import JobRunner
from belay.runtime.messages import (LedgerQuery, Message, ReportConflict, RunCheck, Start, Stop, Submit,
                                    Tick, Wait)

WorkerFactory = Callable[[str, "WorkerRuntime", Callable[[], Awaitable[str]], float], Any]


class WorkerRuntime:
    """一个 worker 的 RuntimeClient。"""

    def __init__(self, orch: "Orchestrator", work_id: str):
        self.o = orch
        self.work_id = work_id

    def drain_notices(self) -> list[str]:
        return self.o.notices.pop(self.work_id, [])

    async def request(self, kind: str, **p: Any) -> dict:
        o = self.o
        if o.finished.is_set():
            return {"text": "The run has finished; nothing more is accepted.", "finished": True}
        rid = uuid.uuid4().hex[:12]
        now = time.time()
        try:
            if kind == "run_check":
                work = o.state.work[self.work_id]
                tree = await o.repo.snapshot(work.workspace, o.index_name(self.work_id))
                changed = [] if (p.get("tests") or p.get("full") or p.get("command")) else \
                    await o.repo.changed(o.state.run.base_tree, tree)
                msg: Message = RunCheck(now, rid, self.work_id, tree, changed, list(p.get("tests") or []),
                                        bool(p.get("full")), p.get("command"))
            elif kind == "wait":
                msg = Wait(now, rid, self.work_id, list(p["job_ids"]), float(p.get("timeout") or 600))
            elif kind == "ledger":
                msg = LedgerQuery(now, rid, self.work_id, str(p.get("req_id") or "").strip())
            elif kind == "submit":
                facts = await o.candidate_facts(self.work_id)
                msg = Submit(time.time(), rid, self.work_id, str(p.get("summary") or ""), bool(p.get("final")),
                             **facts)
            elif kind == "report_conflict":
                msg = ReportConflict(now, rid, self.work_id, str(p.get("report_kind") or ""), p.get("req_id"),
                                     list(p.get("check_ids") or []), str(p.get("reason") or ""))
            else:
                return {"text": f"unknown runtime request {kind}", "error": True}
        except Exception as e:                      # noqa: BLE001 — 快照等 IO 失败：告诉模型，不中断 worker
            o.log(f"[belay] {kind} 失败：{type(e).__name__}: {e}")
            return {"text": f"The harness could not process the request: {type(e).__name__}: {str(e)[:300]}",
                    "error": True}
        fut = asyncio.get_running_loop().create_future()
        o.futures[rid] = fut
        o.post(msg)
        return await fut


class Orchestrator:
    def __init__(self, *, cfg: RuntimeConfig, paths: RuntimePaths, setup: SetupInfo, spec: dict | None,
                 instruction: str, root_env: Env, agent_env: Env, llm, worker_factory: WorkerFactory,
                 out_dir: str | Path, budget_sec: float, log: Callable[[str], None] = print,
                 requirements: list | None = None, plan_notes: list[str] | None = None):
        self.cfg = cfg
        self.paths = paths
        self.setup = setup
        self.spec = spec
        self.instruction = instruction
        self.root_env = root_env
        self.agent_env = agent_env
        self.llm = llm
        self.worker_factory = worker_factory
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.isolation = setup.isolation
        self.agent_user = setup.agent_user
        self.repo = ShadowRepo(root_env, paths)
        self.root_jobs = JobRunner(root_env)
        self.dev_jobs = JobRunner(agent_env)
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.futures: dict[str, asyncio.Future] = {}
        self.notices: dict[str, list[str]] = {}
        self.workers: dict[str, tuple[Any, asyncio.Task]] = {}
        self.tasks: set[asyncio.Task] = set()
        self.finished = asyncio.Event()
        self.final_status: str | None = None
        self.final_reason = ""
        self.effects = Effects(self)
        self._loop_task: asyncio.Task | None = None
        self._tick_task: asyncio.Task | None = None

        now = time.time()
        reserve = cfg.reserve_sec(setup.full_gate_sec, budget_sec, setup.gate_available)
        reqs = requirements if requirements else extract_requirements(instruction)
        self.store = Store(self.out_dir / "graph.sqlite")
        self.state: GraphState = initial_state(
            requirements=reqs, baseline=setup.baseline, now=now, deadline_t=now + budget_sec, reserve_sec=reserve,
            full_gate_sec=setup.full_gate_sec, base_commit=setup.base_commit, base_tree=setup.base_tree,
            workspace=setup.workspace, gate_available=setup.gate_available,
            test_author_available=setup.test_author_available and cfg.test_author,
            protect_tests=cfg.protect_tests and setup.gate_available, test_files=setup.test_files,
            notes=list(setup.notes) + list(plan_notes or []))
        init_state(self.store, self.state)
        self.log(f"[belay] {len(reqs)} 条需求，{len(setup.baseline)} 个基线测试，预留 {reserve:.0f}s 给最终门禁")

    # ---- 基础
    def post(self, msg: Message) -> None:
        self.inbox.put_nowait(msg)

    def spawn(self, coro, name: str, track: bool = True) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        if track:
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return task

    def client(self, work_id: str) -> WorkerRuntime:
        return WorkerRuntime(self, work_id)

    @staticmethod
    def index_name(work_id: str) -> str:
        return work_id.lower()

    def log_event(self, type_: str, **data) -> None:
        self.store.commit([Event(type_, time.time(), data)])

    def task_context(self) -> str:
        """给 Test Author 的任务背景：任务原文中需求列表之前的部分（仓库、版本说明）。"""
        head = self.instruction.split("<release_notes>")[0].strip()
        return head[:1500] if head and head != notes_block(self.instruction).strip() else ""

    def failure_text(self, check_ids: list[str]) -> str:
        """这些测试在最近一次作业里的失败原因（给 reviewer）。"""
        jobs = sorted((j for j in self.state.job.values() if j.state == "DONE" and not j.command),
                      key=lambda j: j.finished_t or 0, reverse=True)
        lines = []
        for c in check_ids:
            chk = self.state.check.get(c)
            if chk is not None and chk.source == "authored":         # 验收测试：最近一次门禁里的失败
                lines.append(f"{c} (acceptance test for {chk.req_id}): "
                             + (chk.last_failure or "no failure recorded"))
                continue
            for j in jobs:
                r = (j.result or {}).get("reasons", {}).get(c)
                st = (j.result or {}).get("tests", {}).get(c)
                if st:
                    lines.append(f"{c}: {st}" + (f" - {r}" if r else "") + f" (job {j.id})")
                    break
            else:
                lines.append(f"{c}: no result recorded yet")
        return "\n".join(lines)

    def review_record(self, rec: dict) -> None:
        with open(self.out_dir / "reviews.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    async def candidate_facts(self, work_id: str) -> dict:
        """工作区快照 → 剔除测试路径下的改动 → 以集成分支 HEAD 为父提交的候选提交。"""
        run = self.state.run
        work = self.state.work[work_id]
        tree = await self.repo.snapshot(work.workspace, self.index_name(work_id))
        dropped: list[str] = []
        if run.protect_tests:
            tree, dropped = await self.repo.strip_tests(run.base_tree, tree)
        if tree == run.head_tree:
            return dict(commit=run.head_commit, tree=tree, changed=[], changed_since_base=[], dropped_tests=dropped)
        n = len(self.state.candidate) + 1
        commit = await self.repo.commit(tree, run.head_commit, f"belay candidate from {work_id}\n\n"
                                                                f"Belay-Work: {work_id}\nBelay-Seq: {n}\n")
        changed = await self.repo.changed(run.head_tree, tree)
        since_base = await self.repo.changed(run.base_tree, tree)
        return dict(commit=commit, tree=tree, changed=changed, changed_since_base=since_base, dropped_tests=dropped)

    # ---- 主循环
    async def _loop(self) -> None:
        while True:
            msg = await self.inbox.get()
            try:
                changes, actions = decide(self.state, msg, self.cfg)
            except Exception as e:                  # noqa: BLE001 — 决策出错：记录，回复请求方，继续运行
                self.log(f"[belay] decide({type(msg).__name__}) 出错：{type(e).__name__}: {e}")
                self.log_event("decide_error", message=type(msg).__name__, error=f"{type(e).__name__}: {e}"[:500])
                rid = getattr(msg, "rid", None)
                if rid:
                    fut = self.futures.pop(rid, None)
                    if fut is not None and not fut.done():
                        fut.set_result({"text": f"Internal harness error: {type(e).__name__}: {e}", "error": True})
                continue
            self.state.apply(changes)
            self.store.commit(changes)
            if self.cfg.check_invariants:
                bad = violations(self.state, self.cfg.gate)
                if bad:
                    self.log(f"[belay] 不变量被破坏：{bad}")
                    self.log_event("invariant_violation", message=type(msg).__name__, violations=bad[:20])
            for a in actions:
                try:
                    self.effects.execute(a)
                except Exception as e:              # noqa: BLE001
                    self.log(f"[belay] 执行 {type(a).__name__} 出错：{type(e).__name__}: {e}")
                    self.log_event("effect_error", action=type(a).__name__, error=f"{e}"[:500])

    async def _ticker(self) -> None:
        while not self.finished.is_set():
            await asyncio.sleep(self.cfg.tick_sec)
            self.post(Tick(time.time()))

    async def run(self) -> str:
        self._loop_task = self.spawn(self._loop(), "orchestrator", track=False)
        self._tick_task = self.spawn(self._ticker(), "ticker", track=False)
        self.post(Start(time.time()))
        await self.finished.wait()
        return self.final_status or "INCOMPLETE"

    def stop(self, reason: str) -> None:
        self.post(Stop(time.time(), reason))

    async def shutdown(self, deliver: bool = True) -> None:
        """停止一切；交付 = 把集成分支 HEAD 检出到工作区；导出事件与账本。多次调用是安全的。"""
        if getattr(self, "_shut", False):
            return
        self._shut = True
        if self.state.run.phase != "finished":        # 例如被 Pier 超时取消：直接把状态记为结束
            run = replace(self.state.run, phase="finished", status="INCOMPLETE",
                          finish_reason=self.state.run.finish_reason or "stopped from outside")
            self.state.run = run
            self.store.commit([Put(run), Event("finish", time.time(), {"status": "INCOMPLETE",
                                                                         "reason": run.finish_reason})])
        for t in (self._tick_task,):
            if t:
                t.cancel()
        for work_id, (worker, task) in list(self.workers.items()):
            if not task.done():
                worker.config.deadline = time.monotonic()
                task.cancel()
        await asyncio.gather(*(t for _, t in self.workers.values()), return_exceptions=True)
        await self.effects.cancel_running_jobs()
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        for fut in self.futures.values():
            if not fut.done():
                fut.set_result({"text": "The run has finished.", "finished": True})
        ws = self.state.run.workspace
        try:
            cur = await self.repo.snapshot(ws, "final")
            diff = await self.repo.diff(self.state.run.base_tree, cur, max_chars=2_000_000, exclude_tests=False)
            (self.out_dir / "worktree.diff").write_text(diff, encoding="utf-8")
        except Exception as e:                      # noqa: BLE001
            self.log(f"[belay] 保存工作区 diff 失败：{e}")
        if deliver:
            try:
                await self.repo.checkout(ws, "final", self.state.run.head_tree)
                await self.root_env.run(f"rm -rf {ws}/.belay_checks", timeout=60, cwd="/")
                self.log(f"[belay] 交付集成分支 HEAD（{len(self.state.integration)} 次合并）")
            except Exception as e:                  # noqa: BLE001
                self.log(f"[belay] 交付 HEAD 失败，工作区保持原样：{e}")
                self.log_event("deliver_error", error=str(e)[:500])
        self.export()
        self.store.close()

    def export(self) -> None:
        sts = statuses(self.state, finished=True)
        reqs = self.state.requirement
        data = {"status": self.state.run.status, "reason": self.state.run.finish_reason,
                "counts": counts(sts), "merges": len(self.state.integration),
                "requirements": [{"id": s.id, "status": s.status, "why": s.why, "evidence": s.evidence,
                                  "kind": reqs[s.id].kind, "text": reqs[s.id].text} for s in sts],
                "notes": self.state.run.notes}
        (self.out_dir / "ledger.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.out_dir / "ledger.md").write_text(ledger_text(self.state, time.time()) + "\n", encoding="utf-8")
        self.store.export_events(self.out_dir / "events.jsonl")

    def summary(self) -> dict:
        """写进 Pier 的 context.metadata。"""
        st = self.state
        cands = list(st.candidate.values())
        return {"belay_status": st.run.status, "belay_reason": st.run.finish_reason,
                "ledger": counts(statuses(st, finished=True)), "merges": len(st.integration),
                "candidates": len(cands), "rejections": sum(c.verdict == "rejected" for c in cands),
                "dropped_test_files": sorted({p for c in cands for p in c.dropped_tests})[:50],
                "reports": {k: sum(r.kind == k for r in st.report.values())
                            for k in ("test_conflict", "wrong_test", "insufficient_info", "environment")},
                "reports_approved": sum(r.verdict == "approved" for r in st.report.values()),
                "acceptance_tests": {s: sum(c.status == s for c in st.authored(active_only=False))
                                     for s in ("active", "queued", "pending", "rejected", "withdrawn")},
                "final_bounces": sum(w.final_bounces for w in st.work.values()),
                "jobs": len(st.job), "isolation": self.isolation, "notes": st.run.notes}

"""BelayRun：一次运行的生命周期（命令式外壳）。

  start(task)   准备：run_started → 影子仓库与 0 号存档 → 基线（原始代码上两次全量）与规划并行 → 冻结需求
  _main()       循环问 next_step(图)：开会话 / 等 / 收尾。会话结束不等于运行结束。
  _finalize()   停 worker → 全量存档尝试 → 链头全量验证 → 交付最近的存档
  resume()      runtime 崩溃后：快照 + 重放 → 对账（recovery.py）→ 回到循环

副作用（作业、git CAS、补丁镜像、还原工作区、交付、重新规划、停 worker）都由已经写入日志的事件触发。
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import build_context
from belay.core.effects import Effect
from belay.core.model import ATT_ADVANCING, ATT_CREATED, ATT_REJECTED, JOB_RUNNING
from belay.core.plan import renumber, validate_plan
from belay.core.queries import next_id, open_attempt
from belay.core.render import ledger, ledger_markdown
from belay.core.rules import Rejected, TreeObs
from belay.core.verify import guard_set, is_test_path
from belay.env import Env
from belay.runtime import planner as P
from belay.runtime.gitops import ShadowRepo
from belay.runtime.port import WorkerPort
from belay.runtime.prompts import system_prompt
from belay.runtime.runtime import Runtime
from belay.runtime.session import BelaySession
from belay.runtime.store import EventStore
from belay.runtime.verifier import RunnerVerifier, VerifierSpec
from belay.tools import EXPLORE_TOOLS, Policy, get_belay_tools, get_tools
from belay.worker.transcript import Transcript


@dataclass
class RunSettings:
    run_dir: str
    budget_sec: float = 5400
    worker: str = "w1"
    git_dir: str = "/opt/belay/git"
    jobs_dir: str = "/opt/belay/jobs"
    tick_sec: float = 5.0
    stop_grace_sec: float = 30.0
    finalize_grace_sec: float = 60.0         # 截止之后最多再等这么久让最后的验证结束
    deliver_checkout: bool = True            # 结束时把工作区检出为交付的存档
    planner_rounds: int = 3
    crash_backoff_sec: float = 5.0
    policy: Optional[Policy] = None
    tools: Optional[list[str]] = None


@dataclass
class RunResult:
    status: str                              # DONE | INCOMPLETE
    checkpoint: Optional[int]
    run_dir: str
    ledger: dict = field(default_factory=dict)


class _Hooks:
    """会话循环的钩子（SessionHooks）。"""

    def __init__(self, run: "BelayRun", port: WorkerPort):
        self.run = run
        self.port = port

    def activity(self, busy: int = 0) -> None:
        self.run.last_activity = self.run.rt.now()
        self.run.tools_running += busy

    def heartbeat(self) -> None:
        self.run.spawn(self.run.rt.submit(R.heartbeat, self.run.w))

    def notices(self) -> list[str]:
        return self.port.drain_notices()

    def should_stop(self) -> bool:
        return self.run.stop_event.is_set()

    def store_blob(self, text: str) -> str:
        return self.run.store.put_blob(text)

    async def compaction_opening(self) -> str:
        return self.run.context(mode="compaction").text

    async def record_compaction(self, level: int, before: int, after: int, summary: Optional[str] = None) -> None:
        await self.run.rt.submit(R.record_compaction, self.run.w, level, before, after, summary)


class BelayRun:
    def __init__(self, llm, env: Env, settings: RunSettings, cfg: Optional[BelayConfig] = None,
                 verifier_spec: Optional[VerifierSpec] = None, planner_llm=None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = lambda m: None):
        self.llm = llm
        self.planner_llm = planner_llm if planner_llm is not None else llm
        self.env = env
        self.s = settings
        self.cfg = cfg or BelayConfig()
        self.spec = verifier_spec or VerifierSpec()
        self.clock = clock
        self.log = log
        self.w = settings.worker
        self.store = EventStore(settings.run_dir)
        self.repo = ShadowRepo(env, settings.git_dir, env.workdir)
        self.verifier = (RunnerVerifier(env, self.spec, env.workdir, settings.git_dir, settings.jobs_dir)
                         if self.spec.available else None)
        self.policy = settings.policy or Policy(protected_prefixes=(settings.git_dir, settings.jobs_dir, "/logs"))
        self.rt: Optional[Runtime] = None
        self.stop_event = asyncio.Event()
        self.restored = asyncio.Event()
        self.delivered = asyncio.Event()
        self.last_activity = clock()
        self.tools_running = 0
        self.session_task: Optional[asyncio.Task] = None
        self._cancel_reason: Optional[str] = None
        self._bg: set[asyncio.Task] = set()
        self.platform = "Linux"

    # ================================================================ 入口
    async def start(self, task: str, run_id: str = "run") -> RunResult:
        self.rt = Runtime(self.store, self.cfg, clock=self.clock, log=self.log)
        self.rt.effect_handler = self._effect
        await self._setup(task, run_id)
        return await self._main()

    async def resume(self) -> RunResult:
        from belay.runtime.recovery import reconcile
        self.rt = Runtime.open(self.store, self.cfg, clock=self.clock, log=self.log)
        self.rt.effect_handler = self._effect
        await reconcile(self)
        return await self._main()

    def spawn(self, coro) -> None:
        async def guarded():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:                   # 后台请求失败（例如 runtime 已关闭）不影响主循环
                self.log(f"background request failed: {type(e).__name__}: {e}")
        t = asyncio.create_task(guarded())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ================================================================ 准备
    async def _setup(self, task: str, run_id: str) -> None:
        rt = self.rt
        self.platform = (await self.env.run("uname -sm", timeout=30)).output.strip() or "Linux"
        await rt.submit(R.start_run, run_id, task, self.s.budget_sec, workers=[self.w],
                        public_checks=self.spec.check_ids, verifier=self.verifier is not None)
        await self.env.run(f"rm -rf {shlex.quote(self.s.git_dir)}", timeout=120, cwd="/")
        commit, tree = await self.repo.init()
        await rt.submit(R.create_base, commit, tree)
        listing = await self.env.run(f"{self.repo._git()} ls-tree -r --name-only {tree} | head -20000",
                                     timeout=120, cwd="/")
        test_files = sorted(p for p in listing.output.splitlines() if is_test_path(p))
        baseline, planning = await asyncio.gather(self._baseline(tree), self._plan(task, test_files))
        outcome: P.PlanOutcome = planning
        for i, r in enumerate(outcome.rounds):
            await rt.submit(R.propose_plan, i + 1, r.proposal, r.valid, r.problems, source=r.source,
                            warnings=r.warnings)
        known = sorted(R.known_checks(rt.graph))
        final = outcome.rounds[-1].proposal
        rep = validate_plan(task, final, known)
        if rep.ok:
            reqs, tasks = renumber(rep)
            source = outcome.source
        else:                                            # 只可能是原文异常；用规划器返回的结果
            reqs, tasks, source = outcome.requirements, outcome.tasks, outcome.source
        await rt.submit(R.freeze_plan, reqs, tasks, source=source)

    async def _baseline(self, tree: str) -> None:
        rt = self.rt
        if self.verifier is None:
            await rt.submit(R.record_baseline, None, None, reason="no checks are configured for this task")
            return
        j1 = await rt.submit(R.ensure_job, tree, None, "baseline", tag="baseline#1")
        j2 = await rt.submit(R.ensure_job, tree, None, "baseline", tag="baseline#2")
        await rt.wait_until(lambda g: all(g.jobs[j].state != JOB_RUNNING for j in (j1, j2)))
        await rt.submit(R.record_baseline, j1, j2)

    async def _plan(self, task: str, test_files: list[str]) -> P.PlanOutcome:
        return await P.plan(self.planner_llm, task, known_checks=[], rounds=self.s.planner_rounds, log=self.log,
                            test_files=test_files)

    # ================================================================ 主循环
    async def _main(self) -> RunResult:
        ticker = asyncio.create_task(self._ticker())
        try:
            while True:
                g = self.rt.graph
                action, reason = R.next_step(g, self.w, self.rt.now(), self.cfg)
                if action == "stop":
                    break
                if action == "wait":
                    await self.rt.changed(timeout=self.s.tick_sec)
                    continue
                if action == "finalize":
                    if reason == "no_progress":
                        await self.rt.submit(R.stall_stop, self.w)
                    await self._finalize(reason)
                    continue
                await self._run_session(reason)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        await self.rt.close()
        g = self.rt.graph
        return RunResult(status="DONE" if g.run.status == "done" else "INCOMPLETE", checkpoint=g.run.delivered,
                         run_dir=self.s.run_dir, ledger=ledger(g))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.s.tick_sec)
            try:
                await self.rt.submit(R.tick)
                self.store.set_meta("alive", self.rt.now())
                task = self.session_task
                if task is not None and not task.done() and self.tools_running <= 0 and \
                        self.rt.now() - self.last_activity > self.cfg.idle_timeout_sec:
                    self.log("worker looks stuck: cancelling its session")
                    self._cancel_reason = "stuck"
                    task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"tick failed: {type(e).__name__}: {e}")

    # ================================================================ 会话
    def blobs(self) -> dict[str, str]:
        w = self.rt.graph.wips.get(self.w)
        if w and w.diff:
            return {w.diff: self.store.read_blob(w.diff, self.cfg.context_diff_chars * 2)}
        return {}

    def context(self, mode: str):
        return build_context(self.rt.graph, self.w, self.cfg.opening_budget_tokens, self.rt.now(), self.cfg,
                             self.blobs(), mode=mode)

    def _ablation_opening(self) -> tuple[str, dict]:
        """消融“Belay − 图上下文”：开场只有任务原文 + 上一个会话的模型摘要。"""
        g = self.rt.graph
        text = f"<task>\n{g.run.task.strip()}\n</task>"
        summaries = [c.summary for c in g.compactions if c.worker == self.w and c.summary]
        if summaries:
            text += f"\n\nSummary of your previous session (model-written):\n{summaries[-1]}"
        return text, {"mode": "ablation", "tokens": len(text) // 4}

    async def _run_session(self, reason: str) -> None:
        rt = self.rt
        if self.cfg.graph_context:
            ctx = self.context(mode="first" if reason == "first" else "resume")
            opening, summary = ctx.text, ctx.summary()
        else:
            opening, summary = self._ablation_opening()
        sid = next_id("S", rt.graph.sessions)
        tpath = str(Path(self.s.run_dir) / "sessions" / f"{sid}.jsonl")
        await rt.submit(R.start_session, self.w, reason, summary, tpath)
        self.stop_event.clear()
        self._cancel_reason = None
        self.last_activity = rt.now()
        self.tools_running = 0
        port = WorkerPort(self, self.w)
        tools = get_belay_tools(self.s.tools)
        session = BelaySession(self.llm, self.env, tools,
                               system_prompt(self.env.workdir, self.platform, has_explore="explore" in
                                             [t.name for t in tools]),
                               opening, _Hooks(self, port), self.cfg, runtime_client=port, policy=self.policy,
                               transcript=Transcript(tpath), subagent=self._explore)
        self.session_task = asyncio.create_task(session.run())
        error = None
        try:
            out = await self.session_task
            end = out.reason
        except asyncio.CancelledError:
            if self._cancel_reason is None:
                raise
            end = self._cancel_reason
        except Exception as e:
            end, error = "crash", f"{type(e).__name__}: {e}"
            self.log(f"session {sid} crashed: {error}")
        finally:
            self.session_task = None
            port.close()
        await rt.submit(R.end_session, self.w, end, session.peak_context, session.turns, error, session.ctx.todos)
        if end != "deadline" and not rt.graph.run.reserve:
            await self.checkpoint_now("handoff" if end == "handoff" else "session_end")
        if end == "crash":
            await asyncio.sleep(self.s.crash_backoff_sec)

    async def _explore(self, description: str, question: str) -> str:
        from belay.worker.loop import Worker, WorkerConfig
        from belay.worker.prompts import EXPLORE_WRAPUP
        child = Worker(self.llm, self.env, tools=get_tools(EXPLORE_TOOLS), role="explore", policy=self.policy,
                       config=WorkerConfig(max_turns=40, reset_tokens=10 ** 12, clear_tokens=10 ** 12))
        res = await child.run(question)
        report = res.final_text
        if res.status == "max_turns":
            report = await child.conclude(EXPLORE_WRAPUP)
        return (report or "(The exploration returned no report.)")[:20000]

    # ================================================================ 观察
    async def observe_raw(self, worker: str) -> tuple[str, list[str]]:
        raw = await self.repo.snapshot(worker)
        head = self.rt.graph.head_cp.tree
        changed = [p for p, _a, _d in await self.repo.numstat(head, raw)] if raw != head else []
        return raw, changed

    async def observe(self, worker: str) -> TreeObs:
        g = self.rt.graph
        raw = await self.repo.snapshot(worker)
        base, head = g.checkpoints[0].tree, g.head_cp.tree
        if self.cfg.protect_tests and guard_set(g.baseline):
            tree, dropped = await self.repo.strip_tests(base, raw)
        else:
            tree, dropped = raw, []
        files = await self.repo.numstat(head, tree) if tree != head else []
        diff_path = None
        if files:
            diff = await self.repo.diff(head, tree, max_bytes=self.cfg.context_diff_chars * 2)
            diff_path = self.store.put_blob(diff, ".diff") if diff.strip() else None
        return TreeObs(tree, raw, tuple(files), tuple(dropped), diff_path)

    async def checkpoint_now(self, trigger: str, tier: Optional[str] = None,
                             timeout: Optional[float] = None) -> Optional[str]:
        """runtime 主动发起的存档尝试（会话结束、交接、截止、收尾）。"""
        try:
            obs = await self.observe(self.w)
            aid = await self.rt.submit(R.request_checkpoint, self.w, obs, trigger, tier=tier)
        except Rejected as e:
            self.log(f"checkpoint ({trigger}) not attempted: {e}")
            return None
        if aid is not None:
            await self.rt.wait_until(lambda g: g.attempts[aid].status in (ATT_CREATED, ATT_REJECTED), timeout=timeout)
        return aid

    # ================================================================ 收尾
    async def _finalize(self, reason: str) -> None:
        rt = self.rt
        self.stop_event.set()
        hard = rt.graph.run.deadline_t + self.s.finalize_grace_sec
        left = lambda: max(0.0, hard - rt.now())                        # noqa: E731
        # worker 停下时可能还有一次存档在验证（它是 worker 最后的状态）：先等它
        await rt.wait_until(lambda g: open_attempt(g) is None, timeout=left())
        await self.checkpoint_now("deadline" if reason == "deadline" else "final", tier="full", timeout=left())
        while left() > 0:
            if not await rt.submit(R.verify_head):
                break
            await rt.changed(timeout=min(self.s.tick_sec, left()))
        for j in list(rt.graph.jobs.values()):                          # 到点还没跑完的作业：取消
            if j.state == JOB_RUNNING:
                if self.verifier is not None:
                    await self.verifier.cancel(j.id)
                await rt.submit(R.job_finished, j.id, "cancelled", {}, 0.0, "cancelled at the deadline")
        # 正在 CAS 的尝试不能中止（git 引用与图必须一致）：等它做完，这一步只是几条 git 命令
        await rt.wait_until(lambda g: not any(a.status == ATT_ADVANCING for a in g.attempts.values()))
        self.delivered.clear()
        await rt.submit(R.deliver, reason)
        await self.delivered.wait()

    # ================================================================ 副作用
    async def _effect(self, eff: Effect) -> None:
        handler = getattr(self, f"_eff_{eff.kind}")
        await handler(**eff.args)

    async def _eff_launch_job(self, job: str) -> None:
        j = self.rt.graph.jobs[job]
        if self.verifier is None:
            await self.rt.submit(R.job_finished, job, "finished", {}, 0.0, "no checks are configured")
            return
        out = await self.verifier.run(j)
        await self.rt.submit(R.job_finished, job, out.state, out.results, out.sec, out.error)

    def commit_message(self, attempt_id: str) -> str:
        return f"belay: checkpoint from attempt {attempt_id}"

    async def advance(self, attempt_id: str) -> tuple[bool, str, list, str]:
        """commit-tree（确定的）+ CAS。重做安全：引用已经指向这个提交就算成功。"""
        g = self.rt.graph
        a = g.attempts[attempt_id]
        commit = await self.repo.commit(a.tree, a.parent_commit, self.commit_message(attempt_id), a.date)
        ref = await self.repo.read_ref()
        if ref == commit:
            ok, detail = True, "ref already advanced"
        elif ref == a.parent_commit:
            ok = await self.repo.cas(commit, a.parent_commit)
            detail = "" if ok else "update-ref failed"
        else:
            ok, detail = False, f"ref moved to {ref}"
        files = await self.repo.numstat(g.checkpoints[a.base].tree, a.tree) if ok else []
        return ok, commit, files, detail

    async def _eff_advance_ref(self, attempt: str) -> None:
        ok, commit, files, detail = await self.advance(attempt)
        await self.rt.submit(R.ref_advanced, attempt, ok, commit, files, detail)

    async def _eff_mirror_checkpoint(self, checkpoint: int) -> None:
        g = self.rt.graph
        patch = await self.repo.diff(g.checkpoints[0].tree, g.checkpoints[checkpoint].tree, binary=True)
        d = Path(self.s.run_dir) / "checkpoints"
        d.mkdir(exist_ok=True)
        (d / f"{checkpoint}.diff").write_text(patch, encoding="utf-8", errors="surrogateescape")

    async def _eff_restore_workspace(self, worker: str, checkpoint: int, reset_ref: bool) -> None:
        try:
            cp = self.rt.graph.checkpoints[checkpoint]
            await self.repo.checkout(cp.tree, worker)
            if reset_ref:
                await self.repo.set_ref(cp.commit)
        finally:
            self.restored.set()

    async def _eff_deliver(self, checkpoint: int, status: str) -> None:
        try:
            g = self.rt.graph
            d = Path(self.s.run_dir)
            base, cp = g.checkpoints[0], g.checkpoints[checkpoint]
            patch = await self.repo.diff(base.tree, cp.tree, binary=True)
            (d / "deliverable.diff").write_text(patch, encoding="utf-8", errors="surrogateescape")
            raw = await self.repo.snapshot(self.w)
            wt = await self.repo.diff(base.tree, raw, binary=True)
            (d / "worktree.diff").write_text(wt, encoding="utf-8", errors="surrogateescape")
            (d / "ledger.json").write_text(json.dumps(ledger(g), indent=1, ensure_ascii=False), encoding="utf-8")
            (d / "ledger.md").write_text(ledger_markdown(g), encoding="utf-8")
            if self.s.deliver_checkout:
                await self.repo.checkout(cp.tree, self.w)
        finally:
            self.delivered.set()

    async def _eff_replan(self, task: str, worker: Optional[str]) -> None:
        g = self.rt.graph
        t = g.tasks.get(task)
        if t is None or self.planner_llm is None:
            return
        reqs = {r: g.requirements[r].quote for r in t.links}
        w = g.wips.get(worker or self.w)
        failures = list(t.last_failure) + (list((w.last_rejection or {}).get("regressions") or []) if w else [])
        try:
            children = await P.propose_split(self.planner_llm, g.run.task,
                                             {"id": t.id, "title": t.title, "description": t.description,
                                              "links": list(t.links)}, reqs, failures)
            await self.rt.submit(R.split_task, task, children)
        except Rejected as e:
            await self.rt.submit(R.propose_plan, 1, {"task": task}, False, [str(e)], purpose="split")
        except Exception as e:
            self.log(f"replan failed: {type(e).__name__}: {e}")

    async def _eff_stop_workers(self, reason: str) -> None:
        self.stop_event.set()
        task = self.session_task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.s.stop_grace_sec)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            if not task.done():
                self._cancel_reason = reason
                task.cancel()

"""BelayRun：一次运行的生命周期（命令式外壳）。

  start(task)   准备：run_started → 影子仓库与 0 号存档 → 基线（工作区一次 + 验证槽位一次，比对导入隔离）与规划并行
                → 冻结需求
  _main()       循环问 next_step(图)：开会话 / 接上会话 / 复查 / 等 / 收尾。会话结束不等于运行结束。
  _finalize()   停 worker → 取消后台尝试 → 对当前 WIP 做一次全量存档尝试 → 交付最新的确认点
  resume()      runtime 崩溃后：快照 + 重放 → 对账（recovery.py）→ 回到循环；rebuild=True 时先从 git bundle 重建容器状态
  suspend()     外层调度挂起：强制快照 → 导出 bundle → 结束会话

副作用（作业、git CAS、镜像、还原工作区、交付、重新规划、停 worker、取消孤儿作业、定位 diff、诊断、复查、标签）
都由已经写入日志的事件触发。worker 与后台只通过事件日志交汇，后台从不碰 worker 的工作区（隔离无效时降级）。
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import build_context, resume_reminder
from belay.core.effects import Effect
from belay.core.model import (ATT_ADVANCING, ATT_PENDING, JOB_RUNNING, LANE_BG, LANE_FG, MILESTONE_KINDS,
                              STEP_ANCHORED, STEP_DECLARED, WHERE_SLOT, WHERE_WORKSPACE)
from belay.core.plan import renumber, validate_plan
from belay.core.queries import (active_step, declared_steps, delivery_checkpoint, focus_task,
                                last_ended_session, latest_snapshot, next_id, open_attempt, resume_point, steps_of,
                                task_files)
from belay.core.render import ledger, ledger_markdown
from belay.core.rules import Rejected, SnapObs
from belay.core.verify import (guard_set, is_test_path, job_priority, reasons_for_tree, suite_layout, test_files_of,
                               units)
from belay.env import Env
from belay.llm import Usage
from belay.runtime import planner as P
from belay.runtime.gitops import CP_REF, SNAP_REF, ShadowRepo
from belay.runtime.port import WorkerPort
from belay.runtime.prompts import (DIAGNOSE_SYSTEM, LABEL_SYSTEM, REVIEW_BLOCKED_SYSTEM,
                                   REVIEW_SYSTEM, system_prompt)
from belay.runtime.runtime import Runtime
from belay.runtime.session import BelaySession, ModelCallFailed, load_transcript_messages
from belay.runtime.store import EventStore
from belay.runtime.verifier import RunnerVerifier, VerifierSpec
from belay.tools import EXPLORE_TOOLS, Policy, get_belay_tools, get_tools
from belay.worker.transcript import Transcript

TEST_CMD = re.compile(r"\b(pytest|py\.test|nosetests|tox|cargo\s+test|go\s+test|mvn\s+(\S+\s+)*test|gradle\w*\s+test|"
                      r"npm\s+(run\s+)?test|yarn\s+test|jest|mocha|make(\s+\S+)*\s+(test|check)|ctest|unittest)\b")


@dataclass
class RunSettings:
    run_dir: str
    budget_sec: float = 5400
    worker: str = "w1"
    git_dir: str = "/opt/belay/git"
    jobs_dir: str = "/opt/belay/jobs"
    verify_dir: Optional[str] = None         # 默认与 jobs_dir 同级的 verify/
    tick_sec: float = 5.0
    stop_grace_sec: float = 30.0
    finalize_grace_sec: float = 60.0         # 截止之后最多再等这么久让最后的验证结束
    deliver_checkout: bool = True            # 结束时把工作区检出为交付的存档
    planner_rounds: int = 3
    crash_backoff_sec: float = 5.0
    retry_backoff_sec: float = 5.0           # 模型接口失败后内存重试前的等待
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

    def notices(self) -> list[str]:
        return self.port.drain_notices()

    def should_stop(self) -> bool:
        return self.run.stop_event.is_set()

    def store_blob(self, text: str) -> str:
        return self.run.store.put_blob(text)

    async def compaction_opening(self) -> str:
        return (await self.run.context(mode="compaction")).text

    async def record_compaction(self, level: int, before: int, after: int, summary: Optional[str] = None) -> None:
        await self.run.rt.submit(R.record_compaction, self.run.w, level, before, after, summary)

    async def before_tool(self, tu: dict) -> None:
        """模型自己跑测试或构建前拍一张（只存，不验证：调试中的中间态测不过是常态）。"""
        if tu.get("name") == "bash" and TEST_CMD.search(str((tu.get("input") or {}).get("command") or "")):
            await self.run.take_snapshot("model_test")

    async def after_tools(self, tool_uses: list[dict], results: list, todos) -> None:
        """存：编辑类工具之后一定拍；bash 不一定写文件，累计 snapshot_bash_every 次再拍。树没变时不记新快照。"""
        run = self.run
        ok = [tu.get("name") for tu, (_out, err) in zip(tool_uses, results) if not err]
        edits = sum(1 for n in ok if n in ("edit_file", "write_file"))
        run.tool_seq += len(tool_uses)
        run.bash_since += sum(1 for n in ok if n == "bash")
        if todos is not None:
            await run.update_steps(todos)
        if edits or run.bash_since >= run.cfg.snapshot_bash_every:
            await run.take_snapshot("writes")

    def write_guard(self):
        return self.run.write_guard()

    def step_count(self) -> int:
        return declared_steps(self.run.rt.graph, self.run.w)

    def has_active_step(self) -> bool:
        return active_step(self.run.rt.graph, self.run.w) is not None


class _WriteGuard:
    def __init__(self, run: "BelayRun"):
        self.run = run
        self.held = False

    async def __aenter__(self):
        v = self.run.verifier
        if v is not None and self.run.rt.graph.degraded:
            await v.workspace_lock.acquire(False)
            self.held = True
        return self

    async def __aexit__(self, *exc):
        if self.held:
            await self.run.verifier.workspace_lock.release(False)
        return False


class BelayRun:
    def __init__(self, llm, env: Env, settings: RunSettings, cfg: Optional[BelayConfig] = None,
                 verifier_spec: Optional[VerifierSpec] = None, planner_llm=None, aux_llm=None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = lambda m: None):
        self.llm = llm
        self.planner_llm = planner_llm if planner_llm is not None else llm
        self.aux_llm = aux_llm if aux_llm is not None else self.planner_llm    # 诊断者、复查者、标签（可用便宜模型）
        self.env = env
        self.s = settings
        self.cfg = cfg or BelayConfig()
        self.spec = verifier_spec or VerifierSpec()
        self.clock = clock
        self.log = log
        self.w = settings.worker
        self.store = EventStore(settings.run_dir)
        self.repo = ShadowRepo(env, settings.git_dir, env.workdir)
        self.verifier = (RunnerVerifier(env, self.spec, env.workdir, settings.git_dir, settings.jobs_dir,
                                        settings.verify_dir, slots=self.cfg.verify_slots,
                                        nice=self.cfg.background_nice, cpu_limit=self.cfg.background_cpu_limit)
                         if self.spec.available else None)
        state_dirs = (settings.git_dir, settings.jobs_dir, "/logs") + \
            ((self.verifier.verify_dir,) if self.verifier is not None else ())
        self.policy = settings.policy or Policy(protected_prefixes=state_dirs)
        self.rt: Optional[Runtime] = None
        self.stop_event = asyncio.Event()
        self.restored = asyncio.Event()
        self.delivered = asyncio.Event()
        self.last_activity = clock()
        self.tools_running = 0
        self.tool_seq = 0
        self.bash_since = 0
        self.session_task: Optional[asyncio.Task] = None
        self.session: Optional[BelaySession] = None
        self.usage = Usage()                       # 全部会话累计的用量（评测框架的 context 用）
        self.turns = 0
        self.peak_context = 0
        self._cancel_reason: Optional[str] = None
        self._bg: set[asyncio.Task] = set()
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._snap_lock = asyncio.Lock()
        self._mirror_lock = asyncio.Lock()
        self.platform = "Linux"

    # ================================================================ 入口
    async def start(self, task: str, run_id: str = "run") -> RunResult:
        self.rt = Runtime(self.store, self.cfg, clock=self.clock, log=self.log)
        self._wire()
        await self._setup(task, run_id)
        return await self._main()

    async def prepare(self, task: str, run_id: str = "run") -> None:
        """只做准备（影子仓库、基线双跑与导入隔离、规划与冻结需求），然后关闭。评测框架在 setup 阶段调用它，
        不占 agent 的预算；之后用一个新的 BelayRun 对同一个 run_dir 调 run_prepared()。"""
        self.rt = Runtime(self.store, self.cfg, clock=self.clock, log=self.log)
        self._wire()
        try:
            await self._setup(task, run_id)
        finally:
            await self._drain_background()
            await self.rt.close()

    async def run_prepared(self) -> RunResult:
        """接着 prepare() 运行：预算从现在开始计时（clock_started），然后进入主循环。"""
        self.rt = Runtime.open(self.store, self.cfg, clock=self.clock, log=self.log)
        self._wire()
        self._load_isolation()
        self.platform = (await self.env.run("uname -sm", timeout=30)).output.strip() or "Linux"
        if not self.rt.graph.sessions:
            await self.rt.submit(R.start_clock)
        return await self._main()

    def prepared(self, task: str) -> bool:
        """run_dir 里有一次针对这段任务原文、已经冻结需求、还没开始会话的准备。"""
        g = self.store.events()
        from belay.core.reduce import replay
        graph = replay(g) if g else None
        return bool(graph and graph.run and graph.frozen and graph.baseline_ready and not graph.sessions and
                    " ".join(graph.run.task.split()) == " ".join(task.split()))

    async def emergency_deliver(self) -> Optional[int]:
        """被外部取消（评测框架超时）时的兜底：停下 worker 与作业，按图把工作区检出为交付点，写账本。
        不经过规则（runtime 可能已经停了）；交付点的选取与正常收尾相同（最新的确认点）。"""
        if self.rt is None or self.rt.graph.head_cp is None:
            return None
        self.stop_event.set()
        if self.session_task is not None and not self.session_task.done():
            self._cancel_reason = "deadline"
            self.session_task.cancel()
        g = self.rt.graph
        if g.run is not None and g.run.delivered is not None:
            return g.run.delivered
        if self.verifier is not None:
            for j in [j for j in g.jobs.values() if j.state == JOB_RUNNING]:
                try:
                    await self.verifier.cancel(j.id)
                except Exception:
                    pass
        cid = delivery_checkpoint(g, self.cfg)
        try:
            await self.repo.checkout(g.checkpoints[cid].tree, self.w)
        except Exception as e:
            self.log(f"emergency delivery: checkout failed: {type(e).__name__}: {e}")
        d = Path(self.s.run_dir)
        try:
            L = ledger(g)
            L.update(status="incomplete", delivered_checkpoint=cid, emergency=True)
            (d / "ledger.json").write_text(json.dumps(L, indent=1, ensure_ascii=False), encoding="utf-8")
            (d / "ledger.md").write_text(ledger_markdown(g) + f"\n(emergency delivery of checkpoint {cid} after "
                                         "an external cancellation)\n", encoding="utf-8")
        except Exception as e:
            self.log(f"emergency delivery: ledger failed: {type(e).__name__}: {e}")
        return cid

    async def resume(self, rebuild: bool = False) -> RunResult:
        from belay.runtime.recovery import reconcile
        self.rt = Runtime.open(self.store, self.cfg, clock=self.clock, log=self.log)
        self._wire()
        self._load_isolation()
        await reconcile(self, rebuild=rebuild)
        return await self._main()

    def _wire(self) -> None:
        self.rt.effect_handler = self._effect
        if self.verifier is not None:
            self.verifier.priority_of = lambda job: job_priority(self.rt.graph, job)
            self.verifier.on_preempt = lambda jid: self.rt.submit(R.job_preempted, jid)
            self.verifier.seed_index = self.repo.index(self.w)

    def spawn(self, coro) -> asyncio.Task:
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
        return t

    def write_guard(self) -> _WriteGuard:
        return _WriteGuard(self)

    # ================================================================ 准备
    async def _setup(self, task: str, run_id: str) -> None:
        rt = self.rt
        self.platform = (await self.env.run("uname -sm", timeout=30)).output.strip() or "Linux"
        await rt.submit(R.start_run, run_id, task, self.s.budget_sec, workers=[self.w],
                        public_checks=self.spec.check_ids, verifier=self.verifier is not None)
        await self.env.run(f"rm -rf {shlex.quote(self.s.git_dir)}", timeout=120, cwd="/")
        if self.verifier is not None:
            await self.env.run(f"rm -rf {shlex.quote(self.verifier.verify_dir)}", timeout=600, cwd="/")
        commit, tree = await self.repo.init()
        await rt.submit(R.create_base, commit, tree)
        await self.repo.set_cp_ref(0, commit)
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

    async def _wait_jobs(self, *jids: str) -> None:
        await self.rt.wait_until(lambda g: all(g.jobs[j].state != JOB_RUNNING for j in jids if j))

    async def _baseline(self, tree: str) -> None:
        """两次全量：一次在工作区（此时还没有 worker）、一次在验证槽位；两边“通过”集合的差决定导入隔离是否有效。"""
        rt = self.rt
        if self.verifier is None:
            await rt.submit(R.record_baseline, None, None, reason="no checks are configured for this task",
                            isolation={"valid": True, "reason": "no verifier"})
            return
        await self.verifier.setup()
        iso: dict = {}
        sys_path = await self.verifier.probe_sys_path()
        self.verifier.pythonpath_rel = self.verifier.map_sys_path(sys_path)
        iso["pythonpath"] = self.verifier.pythonpath_rel
        hard = self.spec.mentions(self.env.workdir)
        j1 = await rt.submit(R.ensure_job, tree, None, "baseline", tag="baseline#ws", where=WHERE_WORKSPACE)
        await self._wait_jobs(j1)
        j2 = await rt.submit(R.ensure_job, tree, None, "baseline", tag="baseline#slot", where=WHERE_SLOT)
        await self._wait_jobs(j2)
        g = rt.graph
        r1, r2 = g.jobs[j1].results, g.jobs[j2].results
        missing = sorted(t for t, st in r1.items() if st == "PASSED" and r2.get(t) != "PASSED")
        confirm = None
        if missing and self.spec.test_cmd:
            confirm = await rt.submit(R.ensure_job, tree, units(missing), "baseline", tag="baseline#slot-confirm",
                                      where=WHERE_SLOT)
            await self._wait_jobs(confirm)
            rc = rt.graph.jobs[confirm].results
            missing = [t for t in missing if rc.get(t) != "PASSED"]
        guard_files = test_files_of({t: "pass" for t, st in r1.items() if st == "PASSED"})
        probe = await self.verifier.isolation_probe(guard_files, tree) if self.spec.test_cmd else \
            {"ok": True, "inconclusive": True, "reason": "no test command"}
        reasons = []
        if hard:
            reasons.append("commands name the working-tree path: " + ", ".join(hard))
        if len(missing) > self.cfg.isolation_max_diff:
            reasons.append(f"{len(missing)} test(s) pass in the working tree but not in the verification directory: "
                           + ", ".join(missing[:5]))
        if not probe.get("ok"):
            reasons.append(f"isolation probe: {probe.get('reason')}")
        iso.update(valid=not reasons, reason="; ".join(reasons) or probe.get("reason", ""), diff=len(missing),
                   probe=probe)
        self.store.set_meta("isolation", iso)
        if reasons:                                      # 降级：两次全量都在工作区上跑
            self.log(f"import isolation is not effective ({iso['reason']}); verification falls back to switching "
                     "the working tree and background verification is off")
            j3 = await rt.submit(R.ensure_job, tree, None, "baseline", tag="baseline#ws2", where=WHERE_WORKSPACE)
            await self._wait_jobs(j3)
            await rt.submit(R.record_baseline, j1, j3, isolation=iso)
        else:
            await rt.submit(R.record_baseline, j1, j2, confirm=confirm, isolation=iso)

    def _load_isolation(self) -> None:
        iso = self.store.get_meta("isolation", None)
        if iso and self.verifier is not None:
            self.verifier.pythonpath_rel = list(iso.get("pythonpath") or [])

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
                if action == "review":
                    await self.rt.submit(R.request_final_reviews)
                    continue
                if action == "finalize":
                    if reason == "no_progress":
                        await self.rt.submit(R.stall_stop, self.w)
                    await self._finalize(reason)
                    continue
                if action == "resume_session":
                    await self._run_session("resume", resume=reason)
                    continue
                await self._run_session(reason)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        await self._drain_background()
        await self.rt.close()
        g = self.rt.graph
        return RunResult(status="DONE" if g.run.status == "done" else "INCOMPLETE", checkpoint=g.run.delivered,
                         run_dir=self.s.run_dir, ledger=ledger(g))

    async def _drain_background(self) -> None:
        """运行结束：还在导出的 bundle 等它做完（有上限），其余后台任务取消。"""
        for t in list(self._job_tasks.values()):
            t.cancel()
        await asyncio.gather(*self._job_tasks.values(), return_exceptions=True)
        pending = [t for t in self._bg if not t.done()]
        if pending:
            _done, still = await asyncio.wait(pending, timeout=60)
            for t in still:
                t.cancel()
            await asyncio.gather(*still, return_exceptions=True)

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

    # ================================================================ 快照（模块 B）
    async def take_snapshot(self, reason: str) -> Optional[int]:
        """在工具边界拍快照（何时拍由调用方决定，见 after_tools；没有时间限流）。快照用于恢复、回退与事后二分；
        只有原因是语义节点（步骤完成、交接、会话结束）的快照才进后台验证。树与上一张相同时不记新快照，
        返回那一张的序号。"""
        async with self._snap_lock:
            g = self.rt.graph
            if g.head_cp is None or not g.baseline_ready:
                return None
            obs = await self._observe()
            n = await self.rt.submit(R.record_snapshot, self.w, obs, reason)
            self.bash_since = 0
            g = self.rt.graph
            exported = (self.store.get_meta("mirror", {}) or {}).get("snap", 0)
            if n is not None and n - exported >= self.cfg.mirror_every:
                self.spawn(self.mirror("snapshots"))
            return n

    async def _observe(self) -> SnapObs:
        g = self.rt.graph
        raw = await self.repo.snapshot(self.w)
        last = latest_snapshot(g, self.w) or latest_snapshot(g)
        if last is not None and last.raw_tree == raw and last.epoch == g.epoch:      # 没有改动：直接沿用上一张
            return SnapObs(tree=last.tree, raw_tree=raw, files=last.files, dropped=last.dropped,
                           testable=last.testable, commit=last.commit, precheck=last.precheck, tool_seq=self.tool_seq)
        base, head = g.checkpoints[0].tree, g.head_cp.tree
        if self.cfg.protect_tests and guard_set(g.baseline):
            layout = suite_layout(g)
            tree, dropped = await self.repo.strip_tests(base, raw, lambda p: is_test_path(p, layout))
        else:
            tree, dropped = raw, []
        files = await self.repo.numstat(head, tree) if tree != head else []
        testable, why = await self._precheck(files)
        parent = last.commit if last is not None and last.commit and not last.lost else g.checkpoints[0].commit
        n = g.last_snapshot + 1
        commit = await self.repo.snapshot_commit(n, raw, tree, parent, self.rt.now())
        ws = g.workers.get(self.w)
        return SnapObs(tree=tree, raw_tree=raw, files=tuple(files), dropped=tuple(dropped), testable=testable,
                       commit=commit, precheck=why, tool_seq=self.tool_seq, session=ws.session if ws else None)

    async def _precheck(self, files) -> tuple[bool, str]:
        """廉价预检：改动的 .py 文件能否编译（不写 .pyc）；非 Python 项目可配 precheck_cmd。"""
        py = [p for p, _a, _d in files if p.endswith(".py")]
        if py and self.cfg.precheck_python:
            code = ("import sys\nbad=[]\nfor p in sys.argv[1:]:\n    try:\n        compile(open(p,'rb').read(), p, "
                    "'exec')\n    except SyntaxError as e:\n        bad.append('%s:%s: %s' % (p, e.lineno, e.msg))\n"
                    "    except Exception:\n        pass\nprint('\\n'.join(bad))\nsys.exit(3 if bad else 0)\n")
            args = " ".join(shlex.quote(p) for p in py[:200])
            pre = self.spec.prelude or "true"
            res = await self.env.run(f"{pre}; (command -v python >/dev/null && python -c {shlex.quote(code)} {args}) "
                                     f"|| (command -v python >/dev/null || exit 0)", timeout=120)
            if res.return_code == 3:
                return False, res.output.strip()[-1000:]
        if self.cfg.precheck_cmd:
            res = await self.env.run(self.cfg.precheck_cmd, timeout=600)
            if res.return_code != 0:
                return False, res.output.strip()[-1000:]
        return True, ""

    async def step_files(self, n: Optional[int]) -> list:
        """相邻锚点之间的改动（观察）：上一个完成步骤的锚点（或认领时的链头）→ 这张快照。"""
        g = self.rt.graph
        t = focus_task(g, self.w)
        if n is None or n not in g.snapshots or t is None:
            return []
        prev = [s for s in steps_of(g, t.id) if s.status in (STEP_DECLARED, STEP_ANCHORED) and s.anchor_snapshot]
        if prev:
            last = max(prev, key=lambda s: s.anchor_snapshot)
            a = g.snapshots[last.anchor_snapshot].tree if last.anchor_snapshot in g.snapshots else g.head_cp.tree
        else:
            base = t.claimed_head if t.claimed_head in g.checkpoints else 0
            a = g.checkpoints[base].tree
        b = g.snapshots[n].tree
        return await self.repo.numstat(a, b) if a != b else []

    async def update_steps(self, todos: list[dict]) -> None:
        g = self.rt.graph
        if R.newly_completed(g, self.w, todos):
            n = await self.take_snapshot("step_done")
            files = await self.step_files(n)
            await self.rt.submit(R.plan_steps, self.w, todos, n, files)
        else:
            await self.rt.submit(R.plan_steps, self.w, todos)

    async def observe_raw(self, worker: str) -> tuple[str, list[str]]:
        raw = await self.repo.snapshot(worker)
        head = self.rt.graph.head_cp.tree
        changed = [p for p, _a, _d in await self.repo.numstat(head, raw)] if raw != head else []
        return raw, changed

    # ================================================================ 会话
    async def context(self, mode: str, away=(), recent_calls=(), extra: Optional[dict] = None):
        blobs = {**await self._context_blobs(mode), **(extra or {})}
        return build_context(self.rt.graph, self.w, self.cfg.opening_budget_tokens, self.rt.now(), self.cfg,
                             blobs, mode=mode, away=away, recent_calls=recent_calls)

    async def _context_blobs(self, mode: str) -> dict[str, str]:
        """当前步骤的部分改动 diff（恢复点：基底存档 → 最新快照的原样树）。"""
        g = self.rt.graph
        out: dict[str, str] = {}
        rp = resume_point(g, self.w)
        snap = latest_snapshot(g, self.w)
        if mode != "first" and snap is not None and rp.get("base") in g.checkpoints:
            base = g.checkpoints[rp["base"]].tree
            if base != snap.raw_tree:
                try:
                    diff = await self.repo.diff(base, snap.raw_tree)
                    if diff.strip():
                        out["partial_diff"] = diff
                except Exception as e:
                    self.log(f"partial diff failed: {e}")
        return out

    def _ablation_opening(self) -> tuple[str, dict]:
        """消融“Belay − 图上下文”：开场只有任务原文 + 上一个会话的模型摘要。"""
        g = self.rt.graph
        text = f"<task>\n{g.run.task.strip()}\n</task>"
        summaries = [c.summary for c in g.compactions if c.worker == self.w and c.summary]
        if summaries:
            text += f"\n\nSummary of your previous session (model-written):\n{summaries[-1]}"
        return text, {"mode": "ablation", "tokens": len(text) // 4}

    def _away_events(self):
        prev = last_ended_session(self.rt.graph, self.w)
        return self.store.events(after=prev.ended_seq) if prev and prev.ended_seq else []

    def _recent_calls(self, sid: Optional[str]) -> list[str]:
        """崩溃恢复时：中断前最后几个工具调用及结果摘要（这些动作不重做）。"""
        s = self.rt.graph.sessions.get(sid) if sid else None
        if s is None or not s.transcript:
            return []
        out = []
        try:
            with open(s.transcript, encoding="utf-8") as f:
                recs = [json.loads(x) for x in f if x.strip()]
        except (OSError, ValueError):
            return []
        for r in recs[-40:]:
            if r.get("type") == "tool_result":
                for x in r.get("results") or []:
                    out.append(f"{x.get('name')} -> {'error' if x.get('error') else 'ok'}: "
                               f"{str(x.get('output'))[:160]}")
        return out[-6:]

    async def _start_opening(self, reason: str) -> tuple[str, dict, list[str]]:
        rt = self.rt
        prev = last_ended_session(rt.graph, self.w)
        crashed = reason in ("crash", "recover", "rebuild", "restart", "resume")
        if crashed and rt.graph.baseline_ready:
            await self.take_snapshot("recover")                      # G7：恢复的第一步是补拍快照
        if not self.cfg.graph_context:
            text, summary = self._ablation_opening()
            return text, summary, []
        away = self._away_events() if reason != "first" else []
        calls = self._recent_calls(prev.id) if crashed and prev else []
        extra = await self._away_files(prev.id) if prev is not None and reason != "first" else {}
        ctx = await self.context(mode="first" if reason == "first" else "resume", away=away, recent_calls=calls,
                                 extra=extra)
        pre: list[str] = []
        if reason != "first":                                          # 预读当前步骤涉及的文件
            g = rt.graph
            snap = latest_snapshot(g, self.w)
            rp = resume_point(g, self.w)
            if snap is not None and rp.get("base") in g.checkpoints:
                base = g.checkpoints[rp["base"]].tree
                if base != snap.raw_tree:
                    pre = [p for p, _a, _d in await self.repo.numstat(base, snap.raw_tree)][:3]
            step = g.steps.get(rp.get("step") or "")
            if step is not None:                                        # 步骤描述中提到且存在的路径
                for tok in re.findall(r"[\w./-]+\.\w+|[\w.-]+/[\w./-]+", f"{step.title} {step.summary}"):
                    if tok not in pre and len(pre) < self.cfg.l2_reread_files and \
                            (await self.env.run(f"test -f {shlex.quote(tok)}", timeout=10)).return_code == 0:
                        pre.append(tok)
        return ctx.text, ctx.summary(), pre

    async def _away_files(self, prev_sid: str) -> dict[str, str]:
        """G1：上一个会话结束时的快照与现在的快照相比，工作区有没有变（回退、撤销、重建都可能改动它）。"""
        g = self.rt.graph
        before = [s for s in g.snapshots.values() if s.session == prev_sid and s.worker == self.w and not s.lost]
        now = latest_snapshot(g, self.w)
        if not before or now is None:
            return {}
        last = max(before, key=lambda s: s.n)
        if last.n == now.n or last.raw_tree == now.raw_tree:
            return {}
        files = await self.repo.numstat(last.raw_tree, now.raw_tree)
        text = "\n".join(f"  {p} (+{a} -{d})" for p, a, d in files[:30])
        return {"away_files": text} if text else {}

    def _make_session(self, opening: str, port: WorkerPort, tpath: str,
                      messages: Optional[list[dict]] = None) -> BelaySession:
        tools = get_belay_tools(self.s.tools)
        return BelaySession(self.llm, self.env, tools,
                            system_prompt(self.env.workdir, self.platform,
                                          has_explore="explore" in [t.name for t in tools]),
                            opening, _Hooks(self, port), self.cfg, runtime_client=port, policy=self.policy,
                            transcript=Transcript(tpath), subagent=self._explore, messages=messages)

    async def _run_session(self, reason: str, resume: Optional[str] = None) -> None:
        rt = self.rt
        port = WorkerPort(self, self.w)
        if resume is not None:                                         # G2：读盘重放（runtime 崩溃、容器还在）
            sid = resume
            tpath = rt.graph.sessions[sid].transcript or str(Path(self.s.run_dir) / "sessions" / f"{sid}.jsonl")
            messages = load_transcript_messages(tpath, self.store.read_blob)
            if messages is None:
                port.close()
                await rt.submit(R.end_session, self.w, "runtime_crash")
                return
            session = self._make_session("", port, tpath, messages)
            await self.take_snapshot("recover")
            blobs = await self._context_blobs("resume")
            session.append_reminder(resume_reminder(rt.graph, self.w, rt.now(), self.cfg, blobs,
                                                    self.store.events(after=rt.graph.sessions[sid].started_seq)
                                                    [-200:]))
        else:
            opening, summary, pre = await self._start_opening(reason)
            sid = next_id("S", rt.graph.sessions)
            tpath = str(Path(self.s.run_dir) / "sessions" / f"{sid}.jsonl")
            session = self._make_session(opening, port, tpath)
            if pre and self.cfg.graph_context:                         # 读过的文件记下 digest，可以直接编辑
                reread = await session.preread(pre)
                if reread:
                    session.messages[0]["content"] += \
                        f"\n## Files of the current step (re-read by the harness)\n{reread}\n"
            await rt.submit(R.start_session, self.w, reason, summary, tpath)
        self.session = session
        self.stop_event.clear()
        self._cancel_reason = None
        self.last_activity = rt.now()
        self.tools_running = 0
        error = None
        memory_retries = 0
        try:
            while True:
                self.session_task = asyncio.create_task(session.run())
                try:
                    out = await self.session_task
                    end = out.reason
                    break
                except asyncio.CancelledError:
                    if self._cancel_reason is None:
                        raise
                    end = self._cancel_reason
                    break
                except ModelCallFailed as e:
                    # G2：会话崩溃（模型接口多次重试仍失败），runtime 仍在 → 内存重试，原样再调用模型
                    if e.context_problem or memory_retries >= self.cfg.resume_max_failures or \
                            self.stop_event.is_set():
                        end, error = "crash", str(e)
                        self.log(f"session {sid} crashed: {error}")
                        break
                    memory_retries += 1
                    mark = rt.graph.seq
                    await asyncio.sleep(self.s.retry_backoff_sec)
                    await rt.submit(R.session_resumed, sid, "memory", str(e)[:300])
                    away = self.store.events(after=mark)
                    if away:
                        blobs = await self._context_blobs("resume")
                        session.append_reminder(resume_reminder(rt.graph, self.w, rt.now(), self.cfg, blobs, away))
                    self.log(f"session {sid}: model call failed ({e}); retrying in memory ({memory_retries})")
                except Exception as e:
                    end, error = "crash", f"{type(e).__name__}: {e}"
                    self.log(f"session {sid} crashed: {error}")
                    break
        finally:
            self.session_task = None
            self.session = None
            port.close()
        self.usage.add(session.usage)
        self.turns += session.turns
        self.peak_context = max(self.peak_context, session.peak_context)
        await rt.submit(R.end_session, self.w, end, session.peak_context, session.turns, error, session.ctx.todos)
        if end != "deadline" and not rt.graph.run.reserve and not rt.graph.run.finalizing:
            await self.take_snapshot("handoff" if end == "handoff" else "session_end")
            if rt.graph.degraded:                                      # 切换工作区的验证结束前不能开新会话
                await rt.wait_until(lambda g: open_attempt(g, None, LANE_BG) is None)
            self.spawn(self.mirror("session_end"))
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

    @staticmethod
    def _transcript_tail(path: Optional[str], max_chars: int, since_t: float = 0.0) -> str:
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8") as f:
                recs = [json.loads(x) for x in f if x.strip()]
        except (OSError, ValueError):
            return ""
        parts = []
        for r in recs:
            if r.get("t", 0) < since_t:
                continue
            if r.get("type") == "assistant":
                for b in r.get("content") or []:
                    if b.get("type") == "text" and b.get("text"):
                        parts.append(f"assistant: {b['text'][:600]}")
                    elif b.get("type") == "tool_use":
                        parts.append(f"tool {b.get('name')}: {json.dumps(b.get('input'))[:300]}")
            elif r.get("type") == "tool_result":
                for x in r.get("results") or []:
                    parts.append(f"result {x.get('name')}: {str(x.get('output'))[:300]}")
        text = "\n".join(parts)
        return text[-max_chars:]

    # ================================================================ 收尾
    async def _finalize(self, reason: str) -> None:
        rt = self.rt
        self.stop_event.set()
        hard = rt.graph.run.deadline_t + self.s.finalize_grace_sec
        left = lambda: max(0.0, hard - rt.now())                        # noqa: E731
        await rt.submit(R.begin_finalize, reason)
        # worker 停下时可能还有一次前台存档在验证（它是 worker 最后的状态）：先等它
        await rt.wait_until(lambda g: open_attempt(g, None, LANE_FG) is None, timeout=left())
        trigger = "deadline" if reason == "deadline" else "final"
        try:
            n = await self.take_snapshot(trigger)
            aid = await rt.submit(R.request_checkpoint, self.w, n, trigger, tier="full") if n else None
        except Rejected as e:
            self.log(f"final checkpoint not attempted: {e}")
            aid = None
        if aid is not None:
            await rt.wait_until(lambda g: g.attempts[aid].status not in (ATT_PENDING, ATT_ADVANCING), timeout=left())
        while left() > 0:                                               # 链头仍是暂存点：有时间就提升它
            if not await rt.submit(R.promote_now):
                break
            await rt.changed(timeout=min(self.s.tick_sec, left()))
        for j in list(rt.graph.jobs.values()):                          # 到点还没跑完的作业：取消
            if j.state == JOB_RUNNING:
                await self._cancel_job(j.id, "cancelled at the deadline")
        # 正在 CAS 的尝试不能中止（git 引用与图必须一致）：等它做完，这一步只是几条 git 命令
        await rt.wait_until(lambda g: not any(a.status == ATT_ADVANCING for a in g.attempts.values()))
        g = rt.graph
        cid = delivery_checkpoint(g, self.cfg)
        lag = await self._lag(cid)
        self.delivered.clear()
        await rt.submit(R.deliver, reason, cid, lag)
        await self.delivered.wait()

    async def _lag(self, cid: int) -> dict:
        """交付点相对链头、相对工作区落后多少（文件与行数）。"""
        g = self.rt.graph
        out: dict = {}
        try:
            tree = g.checkpoints[cid].tree
            if g.head_cp and g.head_cp.tree != tree:
                files = await self.repo.numstat(tree, g.head_cp.tree)
                out["vs_head"] = {"files": [f[0] for f in files][:100], "lines": sum(a + d for _, a, d in files)}
            snap = latest_snapshot(g, self.w)
            if snap is not None and snap.tree != tree:
                files = await self.repo.numstat(tree, snap.tree)
                out["vs_worktree"] = {"files": [f[0] for f in files][:100], "lines": sum(a + d for _, a, d in files)}
        except Exception as e:
            out["error"] = str(e)[:300]
        return out

    async def _cancel_job(self, jid: str, why: str) -> None:
        j = self.rt.graph.jobs.get(jid)
        if j is None or j.state != JOB_RUNNING:
            return
        # 先记为取消：之后进程被终止时报回来的（空）结果会被忽略，不会被当成“测试漏跑”
        await self.rt.submit(R.job_finished, jid, "cancelled", {}, 0.0, why)
        if self.verifier is not None:
            if await self.verifier.cancel(jid) == "queued":
                t = self._job_tasks.get(jid)
                if t is not None:
                    t.cancel()

    # ================================================================ 外层调度：挂起
    async def suspend(self) -> None:
        """强制快照 → 导出 bundle → 会话以 suspended 结束 → run_suspended。之后在任何主机上 resume 即可接回。"""
        await self.take_snapshot("suspend")
        await self.mirror("suspend")
        self._cancel_reason = "suspended"
        if self.session_task is not None and not self.session_task.done():
            self.session_task.cancel()
        await self.rt.submit(R.suspend, "suspend")

    # ================================================================ 镜像（G3）
    async def mirror(self, reason: str) -> None:
        """把影子仓库新增的对象导出为增量 git bundle，写到宿主机 run_dir/git/<n>.bundle。

        快照提交是一条链（父提交是上一张快照），所以最新快照的 ref 覆盖全部快照；存档 ref 只多出提交对象。
        增量 bundle 累积到 mirror_consolidate 份时合并成一份完整的（以 0 号基线为前提），旧文件删掉：
        一天的运行不会在宿主机上留下成千上万个小文件，重建时也不用逐个 unbundle。"""
        async with self._mirror_lock:
            g = self.rt.graph
            meta = dict(self.store.get_meta("mirror", {}) or {})
            bundles = list(meta.get("bundles") or [])
            seq = int(meta.get("seq") or len(bundles)) + 1
            snaps = [s for s in g.snapshots.values() if s.commit and not s.lost]
            latest = max(snaps, key=lambda s: s.n) if snaps else None
            done_cps = set(meta.get("cps") or [])
            cps = [c for c in g.checkpoints.values() if c.id > 0]
            full = len(bundles) >= max(2, self.cfg.mirror_consolidate)
            new_cps = cps if full else [c for c in cps if c.id not in done_cps]
            new_snap = latest is not None and (full or latest.n > int(meta.get("snap") or 0))
            tips = [f"{SNAP_REF}{latest.n}"] if new_snap else []
            for c in new_cps:                                          # bundle 记录的是引用名，不能只给提交哈希
                await self.repo.set_cp_ref(c.id, c.commit)
                tips.append(f"{CP_REF}{c.id}")
            if not tips:
                return
            exclude = [g.checkpoints[0].commit]
            if not full:
                exclude += ([meta["snap_commit"]] if meta.get("snap_commit") else []) + \
                    [g.checkpoints[c].commit for c in done_cps if c in g.checkpoints]
            name = f"{seq}-full.bundle" if full else f"{seq}.bundle"
            remote = f"{self.s.git_dir}.bundles/{name}"
            d = Path(self.s.run_dir) / "git"
            try:
                await self.env.run(f"mkdir -p {shlex.quote(self.s.git_dir)}.bundles", timeout=30, cwd="/")
                if not await self.repo.bundle_create(remote, tips, exclude):
                    return
                data = await self.env.read_bytes(remote)
                d.mkdir(exist_ok=True)
                tmp = d / f"{name}.tmp"
                tmp.write_bytes(data)
                tmp.replace(d / name)
                await self.env.run(f"rm -f {shlex.quote(remote)}", timeout=30, cwd="/")
            except Exception as e:
                self.log(f"mirror ({reason}) failed: {type(e).__name__}: {e}")
                return
            meta["bundles"] = [name] if full else bundles + [name]
            meta["seq"] = seq
            if latest is not None:
                meta["snap"], meta["snap_commit"] = max(latest.n, int(meta.get("snap") or 0)), latest.commit
            meta["cps"] = sorted(done_cps | {c.id for c in new_cps})
            self.store.set_meta("mirror", meta)              # 先记下新的列表，再删旧文件（中途崩溃只会多留几个文件）
            if full:
                for old in bundles:
                    (d / old).unlink(missing_ok=True)

    # ================================================================ 副作用
    async def _effect(self, eff: Effect) -> None:
        handler = getattr(self, f"_eff_{eff.kind}")
        await handler(**eff.args)

    async def _eff_launch_job(self, job: str) -> None:
        j = self.rt.graph.jobs[job]
        if self.verifier is None:
            await self.rt.submit(R.job_finished, job, "finished", {}, 0.0, "no checks are configured")
            return
        self._job_tasks[job] = asyncio.current_task()
        try:
            out = await self.verifier.run(j)
        except asyncio.CancelledError:
            return
        finally:
            self._job_tasks.pop(job, None)
        await self.rt.submit(R.job_finished, job, out.state, out.results, out.sec, out.error, out.reasons)

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
        parent_tree = g.head_cp.tree
        files = await self.repo.numstat(parent_tree, a.tree) if ok else []
        return ok, commit, files, detail

    async def _eff_advance_ref(self, attempt: str) -> None:
        ok, commit, files, detail = await self.advance(attempt)
        await self.rt.submit(R.ref_advanced, attempt, ok, commit, files, detail)

    async def _eff_mirror_checkpoint(self, checkpoint: int) -> None:
        """存档 ref 每个都设（便宜）；补丁镜像与 bundle 只在里程碑上做。非里程碑存档（交接）的提交是确定的，
        它的树就是某张快照的候选树：只要快照已导出，重建时可以原样重做（recovery.rebuild_container）。"""
        g = self.rt.graph
        cp = g.checkpoints[checkpoint]
        await self.repo.set_cp_ref(checkpoint, cp.commit)
        if cp.kind not in MILESTONE_KINDS:
            return
        patch = await self.repo.diff(g.checkpoints[0].tree, cp.tree, binary=True)
        d = Path(self.s.run_dir) / "checkpoints"
        d.mkdir(exist_ok=True)
        (d / f"{checkpoint}.diff").write_text(patch, encoding="utf-8", errors="surrogateescape")
        await self.mirror("milestone")

    async def _eff_label_checkpoint(self, checkpoint: int) -> None:
        """没有步骤时的兜底：里程碑存档（以及每 label_every 个其他存档）生成一行“这段做了什么”（llm）。"""
        g = self.rt.graph
        cp = g.checkpoints.get(checkpoint)
        if cp is None or cp.label or not self.cfg.labeler or self.aux_llm is None:
            return
        if any(steps_of(g, t) for t in cp.tasks):
            return
        if cp.kind not in MILESTONE_KINDS and checkpoint % max(1, self.cfg.label_every):
            return
        parent = g.checkpoints.get(cp.parent) if cp.parent is not None else None
        sess = g.sessions.get(g.workers[self.w].session or "") or next(
            (s for s in sorted(g.sessions.values(), key=lambda s: -s.started_seq)), None)
        tail = self._transcript_tail(sess.transcript if sess else None, 8000, parent.created_t if parent else 0.0)
        if not tail:
            return
        try:
            resp = await self.aux_llm.call(LABEL_SYSTEM, [], [{"role": "user", "content": tail}])
            await self.rt.submit(R.label_checkpoint, checkpoint, resp.text.strip().splitlines()[0][:300]
                                 if resp.text.strip() else "")
        except Exception as e:
            self.log(f"labelling checkpoint {checkpoint} failed: {type(e).__name__}: {e}")

    async def _eff_restore_workspace(self, worker: str, checkpoint: int, reset_ref: bool) -> None:
        try:
            cp = self.rt.graph.checkpoints[checkpoint]
            await self.repo.checkout(cp.tree, worker)
            if reset_ref:
                await self.repo.set_ref(cp.commit)
            await self.take_snapshot("rollback")
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
            await self.mirror("deliver")
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

    async def _eff_cancel_orphans(self, attempt: str) -> None:
        """被取代的尝试留下的作业：没有别的尝试、定位或提升在用，就取消（释放槽位）。"""
        g = self.rt.graph
        a = g.attempts.get(attempt)
        if a is None:
            return
        for jid in a.jobs:
            j = g.jobs.get(jid)
            if j is None or j.state != JOB_RUNNING:
                continue
            # 作业按 (树, 选择) 去重：别的尝试可能复用它而不在自己的 jobs 里，所以按树判断，宁可不取消
            used = any(o.tree == j.tree for o in g.attempts.values() if o.id != attempt and
                       o.status in (ATT_PENDING, ATT_ADVANCING)) or \
                any(cp.tree == j.tree and cp.level != "confirmed" for cp in g.checkpoints.values())
            if not used:
                await self._cancel_job(jid, f"attempt {attempt} was superseded")

    async def _eff_locate_diff(self, locate: str, groups: int) -> None:
        g = self.rt.graph
        loc = g.locates.get(locate)
        if loc is None:
            return
        for i, grp in enumerate(loc.groups):
            a, b = grp["good"]["tree"], grp["bad"]["tree"]
            try:
                layout = suite_layout(g)
                files = [f for f in await self.repo.numstat(a, b) if not is_test_path(f[0], layout)]
                diff = await self.repo.diff(a, b)
                path = self.store.put_blob(diff, ".diff") if diff.strip() else None
            except Exception as e:
                self.log(f"locate diff failed: {e}")
                files, path = [], None
            await self.rt.submit(R.record_located, locate, i, files, path)

    async def _eff_diagnose(self, diagnosis: str) -> None:
        g = self.rt.graph
        d = g.diagnoses.get(diagnosis)
        if d is None:
            return
        try:
            body = await self._diagnosis_input(diagnosis)
            if self.aux_llm is None:
                raise RuntimeError("no model for the diagnoser")
            resp = await self.aux_llm.call(DIAGNOSE_SYSTEM, [], [{"role": "user", "content": body}])
            result = P.extract_json(resp.text) or {}
            await self.rt.submit(R.record_diagnosis, diagnosis, result, not result)
        except Exception as e:
            self.log(f"diagnosis {diagnosis} failed: {type(e).__name__}: {e}")
            await self.rt.submit(R.record_diagnosis, diagnosis, {"error": str(e)[:300]}, True)

    async def _diagnosis_input(self, did: str) -> str:
        """诊断者的输入全部来自对图的查询：失败原因、测试源码、定位出的 diff、当时的意图（任务、步骤、笔记、摘要）。"""
        g = self.rt.graph
        d = g.diagnoses[did]
        budget = self.cfg.diagnose_input_tokens * self.cfg.chars_per_token
        parts = [f"<task_statement>\n{g.run.task.strip()[:6000]}\n</task_statement>"]
        rec = None
        if d.locate and d.locate in g.locates:
            recs = g.locates[d.locate].results
            rec = recs[-1] if recs else None
        bad_tree = rec["bad"]["tree"] if rec else (g.head_cp.tree if g.head_cp else "")
        reasons = reasons_for_tree(g, bad_tree)
        for t in d.tests[:5]:
            parts.append(f"## Failing test {t}\nreason: {reasons.get(t, '(no reason recorded)')}")
            src = await self.repo.show(bad_tree, t.split("::")[0], max_chars=6000)
            fn = t.split("::")[-1].split("[")[0]
            m = re.search(rf"^(\s*)def {re.escape(fn)}\(.*?(?=^\1\S|\Z)", src, re.M | re.S)
            parts.append("```python\n" + (m.group(0) if m else src[:3000]) + "\n```")
        if rec:
            diff = self.store.read_blob(rec["diff"], 20000) if rec.get("diff") else ""
            parts.append(f"## Located change ({rec['good'].get('id')} -> {rec['bad'].get('id')})\n```diff\n{diff}\n```")
            att = rec.get("attribution") or {}
            for tid in att.get("held") or []:
                t = g.tasks.get(tid)
                if t is None:
                    continue
                parts.append(f"## Task being worked on then: {t.id} {t.title}\n{t.description[:1000]}")
                for rid in t.links:
                    parts.append(f"- {rid}: \"{g.requirements[rid].quote[:800]}\"")
                for s in steps_of(g, tid):
                    parts.append(f"- step {s.id} [{s.status}] {s.title} {s.summary}")
                for n in [n for n in g.notes if n.task == tid][-8:]:
                    parts.append(f"- note (self-reported): {n.text[:500]}")
            sess = att.get("session")
            for c in [c for c in g.compactions if c.session == sess and c.summary][-1:]:
                parts.append(f"## Summary written in that session (model-written)\n{c.summary[:3000]}")
        else:
            conf = g.checkpoints.get(g.confirmed or 0)
            if conf is not None and g.head_cp is not None:
                diff = await self.repo.diff(conf.tree, g.head_cp.tree, max_bytes=20000)
                parts.append(f"## Changes since the latest confirmed checkpoint\n```diff\n{diff}\n```")
        if d.previous and d.previous in g.diagnoses:
            parts.append("## A previous diagnosis of the same failure (give a different hypothesis)\n"
                         + json.dumps(g.diagnoses[d.previous].result)[:3000])
        text = "\n\n".join(parts)
        return text[:int(budget)]

    async def _eff_review(self, task: str, phase: str) -> None:
        g = self.rt.graph
        t = g.tasks.get(task)
        if t is None:
            return
        try:
            if self.aux_llm is None:
                raise RuntimeError("no model for the reviewer")
            quotes = "\n".join(f"- {r}: \"{g.requirements[r].quote}\"" for r in t.links)
            if phase == "blocked":
                body = (f"<task_statement>\n{g.run.task.strip()[:8000]}\n</task_statement>\n\nTask {t.id}: {t.title}\n"
                        f"{t.description}\nLinked requirements:\n{quotes}\nThe agent's reason: {t.blocked_reason}")
                resp = await self.aux_llm.call(REVIEW_BLOCKED_SYSTEM, [], [{"role": "user", "content": body}])
            else:
                files = task_files(g, task)
                cp = g.checkpoints.get(t.done_checkpoint)
                base = g.checkpoints.get(t.claimed_head if t.claimed_head in g.checkpoints else 0)
                diff = await self.repo.diff(base.tree, cp.tree, max_bytes=40000) if cp and base else ""
                steps = "\n".join(f"- {s.id} {s.title}: {s.summary}" for s in steps_of(g, task))
                notes = "\n".join(n.text[:400] for n in g.notes if n.task == task)[-3000:]
                body = (f"Task {t.id}: {t.title}\n{t.description}\nRequirements:\n{quotes}\nSteps:\n{steps}\n"
                        f"The agent's notes:\n{notes}\nFiles: {', '.join(f[0] for f in files[:40])}\n"
                        f"```diff\n{diff}\n```")
                resp = await self.aux_llm.call(REVIEW_SYSTEM, [], [{"role": "user", "content": body}])
            result = P.extract_json(resp.text) or {"implemented": "failed"}
        except Exception as e:
            self.log(f"review of {task} failed: {type(e).__name__}: {e}")
            result = {"implemented": "failed"}
        await self.rt.submit(R.record_review, task, phase, result)

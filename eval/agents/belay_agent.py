"""Belay 组：自研 worker + 证据图 runtime。Belay 特有的接入逻辑只在这一个文件里（依赖规则 3）。

setup 阶段（不计入 90 分钟预算，与 A-gate 相同；runs.yaml 的 setup_timeout_min）：
  补丁快照（与所有组相同）→ belay.runtime.bootstrap：影子仓库、基线（两次）、原始代码副本、低权限用户。
run 阶段：Orchestrator 派发 worker、调度作业、推进集成分支，直到 DONE / INCOMPLETE 或预算用完。
结束时（asyncio.shield，Pier 超时取消也会执行）：停止一切 → 把集成分支 HEAD 检出到工作目录 → 照常导出补丁。
所以交付的是最近一个经过门禁的状态，补丁里没有测试路径下的改动。

runs.yaml 中除 FlatAgent 的参数外还可用：
  gate_spec   由 runner 按任务目录的 gate.json 传入（agent 定义里写 gate: true）
  runtime     belay.config.RuntimeConfig 的字段，例如 {gate: advise, test_author: false}
  workers     M5 起生效
  paths       belay.config.RuntimePaths 的字段（容器内的状态目录等），只有本地测试需要改
日志（trial 的 agent 日志目录下 belay/）：graph.sqlite、events.jsonl、ledger.json / ledger.md、setup.json、
worktree.diff（结束时工作区相对原始代码的完整改动，调试用）、test_author-*.jsonl、reviews.jsonl。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from belay.config import RuntimeConfig, RuntimePaths
from belay.env import PierEnv
from belay.runtime.bootstrap import SetupInfo, setup_container, verify_agent_user
from belay.runtime.orchestrator import Orchestrator
from belay.runtime.prompts import BELAY_RULES
from belay.tools import BELAY_TOOLS, Policy, get_belay_tools
from belay.worker import Worker, WorkerConfig
from belay.worker.transcript import Transcript
from eval.agents.flat_agent import FlatAgent


class BelayAgent(FlatAgent):
    default_policy = Policy.strict()
    time_reminders = True

    def __init__(self, *args, workers: int = 1, gate_spec: str | dict | None = None, runtime: dict | None = None,
                 paths: dict | None = None, **kwargs):
        spec = json.loads(gate_spec) if isinstance(gate_spec, str) and gate_spec else gate_spec
        self.gate_spec: dict | None = spec or None
        self.runtime_cfg = RuntimeConfig.from_dict({**(runtime or {}), "workers": int(workers)})
        self.paths = RuntimePaths(**(paths or {}))         # 容器内路径；只有本地测试需要改
        self.setup_info: SetupInfo | None = None
        self.orch: Orchestrator | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "belay"

    def version(self) -> str:
        return "0.2"

    @property
    def belay_dir(self) -> Path:
        d = Path(self.logs_dir) / "belay"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def setup(self, environment) -> None:
        await super().setup(environment)                    # 补丁快照
        workdir = await self._workdir(environment)
        root = PierEnv(environment, workdir, user="root")
        info = await setup_container(root, workdir, self.gate_spec, self.runtime_cfg, self.paths,
                                     log=self.logger.info)
        if info.isolation:
            problem = await verify_agent_user(PierEnv(environment, workdir, user=info.agent_user), workdir,
                                              self.paths)
            if problem:
                info.isolation, info.agent_user = False, None
                info.notes.append(problem + "; running the worker as root")
        self.setup_info = info
        (self.belay_dir / "setup.json").write_text(json.dumps(json.loads(info.to_json()), indent=1,
                                                              ensure_ascii=False), encoding="utf-8")
        self.logger.info(f"[belay] setup 完成（{info.sec:.0f}s）：gate={info.gate_available} "
                         f"test_author={info.test_author_available} isolation={info.isolation} notes={info.notes}")

    def make_belay_worker(self, llm, env: PierEnv, context, work_id: str, runtime, task_refresh,
                          deadline: float) -> Worker:
        names = [n for n in BELAY_TOOLS if self.explore or n != "explore"]
        return Worker(llm, env, tools=get_belay_tools(names),
                      config=WorkerConfig(deadline=deadline, clear_tokens=self.clear_tokens,
                                          reset_tokens=self.reset_tokens, time_reminders=self.time_reminders,
                                          extra_rules=BELAY_RULES),
                      policy=self.policy, transcript=Transcript(Path(self.logs_dir) / "transcript.jsonl"),
                      on_progress=self._progress(context), runtime=runtime, work_id=work_id,
                      task_refresh=task_refresh)

    async def run(self, instruction, environment, context) -> None:
        budget = self.budget_min * 60 - 30
        status = "error"
        try:
            info = self.setup_info
            if info is None:                                # setup 没有跑（例如本地调试）
                await self.setup(environment)
                info = self.setup_info
            root = PierEnv(environment, info.workspace, user="root")
            agent_env = PierEnv(environment, info.workspace, user=info.agent_user) if info.isolation else root
            llm = self._make_llm()                          # worker、Test Author、Reviewer 共用（录制在同一文件）

            def factory(work_id, runtime, task_refresh, deadline):
                self.worker = self.make_belay_worker(llm, agent_env, context, work_id, runtime, task_refresh,
                                                     deadline)
                return self.worker

            self.orch = Orchestrator(cfg=self.runtime_cfg, paths=self.paths, setup=info, spec=self.gate_spec,
                                     instruction=instruction, root_env=root, agent_env=agent_env, llm=llm,
                                     worker_factory=factory, out_dir=self.belay_dir, budget_sec=budget,
                                     log=self.logger.info)
            status = await self.orch.run()
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            await asyncio.shield(self._finish(environment, context, status))

    async def _finish(self, environment, context, status: str) -> None:
        t0 = time.time()
        try:
            if self.orch is not None:
                await self.orch.shutdown(deliver=True)
        except Exception as e:                               # noqa: BLE001 — 交付失败也要导出补丁
            self.logger.error(f"[belay] shutdown 失败：{type(e).__name__}: {e}")
        meta = dict(context.metadata or {}, worker_status=status)
        if self.worker is not None:
            self._progress(context)(self.worker)
            violations: dict[str, int] = {}
            for e in self.worker.ctx.events:
                if e.get("kind") == "violation":
                    violations[e["category"]] = violations.get(e["category"], 0) + 1
            meta.update(submitted=self.worker.ctx.submitted, resets=self.worker.resets, violations=violations)
        if self.orch is not None:
            meta.update(self.orch.summary())
        meta["belay_shutdown_sec"] = round(time.time() - t0, 1)
        context.metadata = meta
        await self._cleanup_and_export(environment)

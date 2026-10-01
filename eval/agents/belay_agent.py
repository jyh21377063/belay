"""Belay 组：自研 worker + 任务状态图 runtime（v6，`belay.runtime.driver.BelayRun`）。
Belay 特有的接入逻辑只在这一个文件里。

setup 阶段（不计入 agent 预算，runs.yaml 的 setup_timeout_min）：
  补丁快照（与所有组相同）→ 有任务原文时（pass_instruction: true）BelayRun.prepare：影子仓库与 0 号存档、
  基线双跑（工作区一次、验证槽位一次）与导入隔离判定、规划并冻结需求。状态全部写进宿主机上的 run_dir。
run 阶段：对同一个 run_dir 新建 BelayRun（不跨事件循环复用对象）→ run_prepared：预算从这里开始计时，会话循环、
  后台存档、收尾，交付最新的确认点（工作区被检出为交付点）。没有在 setup 阶段准备时，在 run() 里完整地 start()，
  准备耗时计入预算。
结束时（asyncio.shield，评测框架超时取消也会执行）：被取消就走 emergency_deliver（停 worker 与作业、把工作区检出为
  交付点、写账本）→ 照常导出补丁。交付的永远是通过回归门的存档，补丁里没有测试路径下的改动。

runs.yaml 中除 FlatAgent 的参数外还可用：
  gate_spec         由 runner 按任务目录的 gate.json 传入（agent 定义里写 gate: true）；没有时运行不带测试验证
  task_instruction  由 runner 在 setup 前传入的任务原文（agent 定义里写 pass_instruction: true）
  runtime           belay.core.config.BelayConfig 的字段（未知字段报错），例如 {locate: false}
  aux_model         诊断者、复查者、存档标签用的模型（默认与 worker 相同）
  state_dir         容器内的状态目录（影子仓库、作业、验证槽位），默认 /opt/belay；必须在仓库之外
  finalize_margin_sec   预算里留给收尾的余量（截止之后 runtime 还会等最后的验证一小段时间），默认 150
日志（trial 的 agent 日志目录下 belay/）：events.sqlite / events.jsonl（唯一真相）、sessions/S*.jsonl、
ledger.json / ledger.md、deliverable.diff、worktree.diff、checkpoints/、git/*.bundle、setup.json、llm_record.jsonl。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

from belay.core.config import BelayConfig
from belay.env import PierEnv
from belay.runtime.driver import BelayRun, RunSettings
from belay.runtime.verifier import VerifierSpec
from belay.tools import BELAY_TOOLS, Policy
from eval.agents.flat_agent import FlatAgent


class BelayAgent(FlatAgent):
    default_policy = Policy.strict()

    def __init__(self, *args, gate_spec: str | dict | None = None, runtime: dict | None = None,
                 task_instruction: str | None = None, aux_model: str | None = None, state_dir: str = "/opt/belay",
                 finalize_margin_sec: float = 150, **kwargs):
        spec = json.loads(gate_spec) if isinstance(gate_spec, str) and gate_spec else gate_spec
        self.gate_spec: dict = dict(spec or {})
        self.cfg = BelayConfig.from_dict(runtime or {})
        self.task_instruction = task_instruction
        self.aux_model = aux_model
        self.state_dir = state_dir.rstrip("/")
        self.finalize_margin_sec = float(finalize_margin_sec)
        self.run_obj: BelayRun | None = None
        self.prepared_sec: float | None = None
        super().__init__(*args, **kwargs)
        if self.state_dir != "/opt/belay":                  # 状态目录对 worker 不可见（不修改共享的默认策略对象）
            self.policy = replace(self.policy, protected_prefixes=(self.state_dir, "/logs"))

    @staticmethod
    def name() -> str:
        return "belay"

    def version(self) -> str:
        return "0.6"

    @property
    def belay_dir(self) -> Path:
        d = Path(self.logs_dir) / "belay"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------------------------------------------------------------- 组装
    def _settings(self) -> RunSettings:
        budget = max(300.0, self.budget_min * 60 - self.finalize_margin_sec)
        tools = [n for n in BELAY_TOOLS if self.explore or n != "explore"]
        return RunSettings(run_dir=str(self.belay_dir), budget_sec=budget, git_dir=f"{self.state_dir}/git",
                           jobs_dir=f"{self.state_dir}/jobs", verify_dir=f"{self.state_dir}/verify",
                           finalize_grace_sec=min(60.0, self.finalize_margin_sec / 2), policy=self.policy,
                           tools=tools)

    def _aux_llm(self):
        if not self.aux_model or self.replay:
            return None
        llm = self._make_llm()
        llm.model = self.aux_model
        llm.record_path = None
        return llm

    async def _make_run(self, environment) -> BelayRun:
        workdir = self.gate_spec.get("workdir") or await self._workdir(environment)
        env = PierEnv(environment, workdir, user="root")
        llm = self._make_llm()
        return BelayRun(llm, env, self._settings(), self.cfg, VerifierSpec.from_dict(self.gate_spec),
                        aux_llm=self._aux_llm(), log=self.logger.info)

    # ---------------------------------------------------------------- setup（不计入预算）
    async def setup(self, environment) -> None:
        await super().setup(environment)                    # 补丁快照
        info = {"gate": bool(self.gate_spec), "prepared": False}
        if self.task_instruction:
            t0 = time.time()
            run = await self._make_run(environment)
            try:
                await run.prepare(self.task_instruction, run_id=Path(self.logs_dir).parent.name or "trial")
                g = run.rt.graph
                info.update(prepared=True, sec=round(time.time() - t0, 1), requirements=len(g.requirements),
                            tasks=len(g.tasks), guard_checks=sum(1 for c in g.baseline.values() if c == "pass"),
                            isolation=g.isolation)
                self.prepared_sec = info["sec"]
                self.logger.info(f"[belay] 准备完成（{info['sec']:.0f}s）：{info['requirements']} 条需求，"
                                 f"{info['tasks']} 个任务，回归门 {info['guard_checks']} 个检查，"
                                 f"隔离{'有效' if g.isolation.get('valid', True) else '无效（降级）'}")
            except Exception as e:                           # 准备失败：run() 里从头 start()
                info.update(error=f"{type(e).__name__}: {e}")
                self.logger.error(f"[belay] 准备失败，将在 run() 里重新开始：{info['error']}")
            finally:
                run.store.close()
        (self.belay_dir / "setup.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")

    # ---------------------------------------------------------------- run
    async def run(self, instruction, environment, context) -> None:
        status = "error"
        try:
            self.run_obj = run = await self._make_run(environment)
            if run.prepared(instruction):
                res = await run.run_prepared()
            else:
                if run.store.events():                       # 准备过但不能用（原文不同、准备失败）：换一个目录
                    run.store.close()
                    stale = self.belay_dir.with_name(f"belay-stale-{int(time.time())}")
                    self.belay_dir.rename(stale)
                    self.run_obj = run = await self._make_run(environment)
                res = await run.start(instruction, run_id=Path(self.logs_dir).parent.name or "trial")
            status = res.status
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            await asyncio.shield(self._finish(environment, context, status))

    async def _finish(self, environment, context, status: str) -> None:
        t0 = time.time()
        run = self.run_obj
        meta = dict(context.metadata or {}, worker_status=status)
        if run is not None:
            try:
                if status in ("cancelled", "error"):
                    cid = await asyncio.wait_for(run.emergency_deliver(), timeout=180)
                    meta["belay_emergency_checkpoint"] = cid
            except Exception as e:                           # noqa: BLE001 — 交付失败也要导出补丁
                self.logger.error(f"[belay] 兜底交付失败：{type(e).__name__}: {e}")
            u = run.usage
            if run.session is not None:                      # 被取消时当前会话的用量还没累计进去
                u.add(run.session.usage)
            context.n_input_tokens = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
            context.n_cache_tokens = u.cache_read_tokens
            context.n_output_tokens = u.output_tokens
            context.n_agent_steps = run.turns + (run.session.turns if run.session is not None else 0)
            context.peak_context_tokens = run.peak_context
            if run.rt is not None:
                g = run.rt.graph
                from belay.core.render import ledger
                L = ledger(g)
                context.summarization_count = sum(len(s.compactions) for s in g.sessions.values())
                meta.update(belay_status="DONE" if g.run and g.run.status == "done" else "INCOMPLETE",
                            belay_status_reasons=L["status_reasons"], delivered=L["delivered_checkpoint"],
                            delivered_level=L["delivered_level"], head=L["head"], confirmed=L["confirmed"],
                            degraded=L["degraded"], categories=L["categories"], sessions=len(g.sessions),
                            snapshots=L["snapshots"], checkpoints=len(g.checkpoints) - 1,
                            prepared_in_setup=self.prepared_sec is not None)
        meta["belay_shutdown_sec"] = round(time.time() - t0, 1)
        context.metadata = meta
        await self._cleanup_and_export(environment)

"""B 组：自研执行器（参照 Claude Code 重新实现的单 worker），没有证据图。

循环跑在宿主机进程里，通过 environment.exec 在任务容器中执行工具；模型调用也从宿主机发出，
所以容器不需要任何网络（network_allowlist 为空）。继承 PatchCaptureMixin 后，补丁导出与评分路径
和 A 组完全一致。

runs.yaml 中可用的 kwargs：
  repo_dir        容器内仓库路径（默认自动探测，非 git 目录时用容器的工作目录）
  max_tokens      单次回复的最大输出 token（含思考），默认 64000
  effort          DeepSeek 的 output_config.effort，默认 max（与 A 组的 --effort max 对应）
  thinking        是否开启思考模式，默认 true
  budget_min      预算分钟数，worker 在截止前 30 秒自行结束；Pier 的超时仍是最终兜底
  clear_tokens / reset_tokens   上下文管理阈值，默认 50 万 / 75 万，与 A 组的压缩点对齐
  explore         是否提供只读探索子 agent（explore 工具），默认 true；模型可以不用
  policy          行动边界，如 {git_write: deny}；默认只记录不拦截，与 Claude Code 可比
  record          是否录制模型回复（写到 trial 日志目录的 llm_record.jsonl）
  replay          回放文件路径：不调用模型，用于调试
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from pier.agents.base import BaseAgent

from belay.env import PierEnv
from belay.llm import LLM, ReplayLLM
from belay.tools import DEFAULT_TOOLS, Policy, get_tools
from belay.worker.transcript import Transcript
from belay.worker import Worker, WorkerConfig
from eval.agents.patch_capture import PatchCaptureMixin

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"


class FlatAgent(PatchCaptureMixin, BaseAgent):
    default_policy = Policy()
    time_reminders = False

    def __init__(self, logs_dir, model_name=None, repo_dir=None, extra_env=None, max_tokens: int = 64000,
                 effort: str | None = "max", thinking: bool = True, budget_min: float = 90,
                 clear_tokens: int = 500_000, reset_tokens: int = 750_000, policy: dict | None = None,
                 record: bool = False, replay: str | None = None, explore: bool = True, **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.repo_dir = repo_dir
        self.extra_env = extra_env or {}          # runs.yaml 中的 env（如 DEEPSEEK_API_KEY）
        self.max_tokens = int(max_tokens)
        self.effort = effort
        self.thinking = bool(thinking)
        self.budget_min = float(budget_min)
        self.clear_tokens = int(clear_tokens)
        self.reset_tokens = int(reset_tokens)
        self.policy = Policy.from_dict(policy) if policy else self.default_policy
        self.record = bool(record)
        self.replay = replay
        self.explore = bool(explore)
        self.worker: Worker | None = None

    @staticmethod
    def name() -> str:
        return "belay-flat"

    def version(self) -> str:
        return "0.1"

    async def setup(self, environment) -> None:
        await self._belay_snapshot(environment)

    # ---- 组装
    def _env_var(self, *names: str) -> str | None:
        for n in names:
            v = self.extra_env.get(n) or os.environ.get(n)
            if v:
                return v
        return None

    def _make_llm(self):
        if self.replay:
            return ReplayLLM(self.replay)
        key = self._env_var("DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("缺少 API key：在 runs.yaml 的 env 中设置 DEEPSEEK_API_KEY")
        return LLM(self.model_name or "deepseek-flash", key,
                   base_url=self._env_var("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL,
                   max_tokens=self.max_tokens, effort=self.effort, thinking=self.thinking,
                   record_path=Path(self.logs_dir) / "llm_record.jsonl" if self.record else None,
                   log=self.logger.info)

    async def _workdir(self, environment) -> str:
        repo = await self._belay_find_repo(environment, required=False)
        if repo:
            return repo
        res = await environment.exec(command="pwd")
        return (res.stdout or "/").strip().splitlines()[-1]

    def _progress(self, context):
        def update(w: Worker) -> None:            # 每轮更新，超时被取消时 Pier 也能拿到用量
            u = w.usage
            context.n_input_tokens = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
            context.n_cache_tokens = u.cache_read_tokens
            context.n_output_tokens = u.output_tokens
            context.n_agent_steps = w.turns
            context.peak_context_tokens = w.peak_context
            context.summarization_count = w.resets
        return update

    def make_worker(self, env: PierEnv, context, deadline: float) -> Worker:
        tools = get_tools([n for n in DEFAULT_TOOLS if self.explore or n != "explore"])
        return Worker(self._make_llm(), env, tools=tools,
                      config=WorkerConfig(deadline=deadline, clear_tokens=self.clear_tokens,
                                          reset_tokens=self.reset_tokens, time_reminders=self.time_reminders),
                      policy=self.policy, transcript=Transcript(Path(self.logs_dir) / "transcript.jsonl"),
                      on_progress=self._progress(context))

    # ---- 运行
    async def run(self, instruction, environment, context) -> None:
        deadline = time.monotonic() + self.budget_min * 60 - 30
        status = "error"
        try:
            env = PierEnv(environment, await self._workdir(environment))
            self.worker = self.make_worker(env, context, deadline)
            result = await self.worker.run(instruction)
            status = result.status
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            if self.worker:
                self._progress(context)(self.worker)
                violations: dict[str, int] = {}
                for e in self.worker.ctx.events:
                    if e.get("kind") == "violation":
                        violations[e["category"]] = violations.get(e["category"], 0) + 1
                context.metadata = dict(context.metadata or {}, worker_status=status,
                                        submitted=self.worker.ctx.submitted, resets=self.worker.resets,
                                        violations=violations)
            await asyncio.shield(self._cleanup_and_export(environment))

    async def _cleanup_and_export(self, environment) -> None:
        # 结束 run_in_background 启动的进程组，再导出补丁
        await self._belay_exec(environment, "for f in /tmp/belay-bg/*.pid; do [ -f \"$f\" ] && "
                                            "kill -9 -\"$(cat \"$f\")\" 2>/dev/null; done; true", check=False)
        await self._belay_export_patch(environment)

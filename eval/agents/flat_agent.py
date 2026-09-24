"""B 组骨架：自研执行器如何接入。

循环跑在宿主机进程里，通过 environment.exec 在任务容器中执行工具；
继承 PatchCaptureMixin 后，补丁导出和 A 组完全一致，评分路径也完全一致。
"""
from __future__ import annotations

import asyncio

from pier.agents.base import BaseAgent

from eval.agents.patch_capture import PatchCaptureMixin


class FlatAgent(PatchCaptureMixin, BaseAgent):
    def __init__(self, logs_dir, model_name=None, repo_dir=None, extra_env=None, **kwargs):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.repo_dir = repo_dir
        self.extra_env = extra_env or {}          # runs.yaml 中的 env（如 DEEPSEEK_API_KEY）

    @staticmethod
    def name() -> str:
        return "belay-flat"

    def version(self) -> str:
        return "0.1"

    async def setup(self, environment) -> None:
        await self._belay_snapshot(environment)

    async def run(self, instruction, environment, context) -> None:
        try:
            # TODO(M1)：from belay.executor.loop import run_attempt
            #   工具（view / search / str_replace / bash ...）全部用 environment.exec 实现；
            #   逐轮消息写到 self.logs_dir / "messages.jsonl"；
            #   结束时把 token 用量写回 context.n_input_tokens / n_output_tokens。
            raise NotImplementedError("FlatAgent 尚未实现")
        finally:
            await asyncio.shield(self._belay_export_patch(environment))

"""A 组：Pier 自带的 Claude Code agent + 结束时导出补丁。

模型走 DeepSeek 的 Anthropic 兼容接口，只需在 runs.yaml 里配置 ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN 和 model。Pier 的 ClaudeCode 会：
  - 把 ANTHROPIC_MODEL 以及 haiku/sonnet/opus/subagent 的默认模型都设成这个 model；
  - 把 ANTHROPIC_BASE_URL 的域名加入网络白名单（任务需 allow_internet = false）；
  - 关闭非必要流量、禁用 plan mode，并记录 ATIF 轨迹。
"""
from __future__ import annotations

import asyncio

from pier.agents.installed.claude_code import ClaudeCode

from eval.agents.patch_capture import PatchCaptureMixin


class ClaudeCodeWithPatch(PatchCaptureMixin, ClaudeCode):
    def __init__(self, *args, repo_dir: str | None = None, **kwargs):
        self.repo_dir = repo_dir
        super().__init__(*args, **kwargs)

    async def setup(self, environment) -> None:
        await super().setup(environment)
        await self._belay_snapshot(environment)

    async def run(self, instruction, environment, context) -> None:
        try:
            await super().run(instruction, environment, context)
        finally:
            await asyncio.shield(self._belay_export_patch(environment, kill_pattern="claude"))

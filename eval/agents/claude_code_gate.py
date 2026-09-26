"""A-gate 组：Claude Code + 确定性回归门（Stop hook）。

setup 阶段（不计入 agent 预算）：
  1. 与 A 组相同：安装 Claude Code，给仓库拍快照；
  2. 上传 gate_script.py 与该题的门禁配置（由转换器生成的 gate.json，runner 通过 gate_spec 参数传入）；
  3. 在原始代码上跑两次相关测试，记录两次都通过的测试（基线，放在 /opt/belay-gate，不在工作区内）；
  4. 在 Claude Code 的用户配置（$CLAUDE_CONFIG_DIR/settings.json）中注册 Stop hook。

运行时：Claude Code 想结束时，hook 重跑测试；存在"原来通过、现在失败"的测试就返回 block 与失败列表，
Claude Code 继续工作；最多拦截 max_blocks 次后放行。没有例外流程：需求要求的合法行为变化也会被拦到上限。
hook 配置不进仓库，不影响补丁。

题目没有门禁配置（例如 LHTB 没有公开测试）时，不注册 hook，行为与 A 组相同，并在 /logs/agent/gate/ 记录原因。
"""
from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path

from eval.agents.claude_code import ClaudeCodeWithPatch

GATE_DIR = "/opt/belay-gate"
SCRIPT = Path(__file__).with_name("gate_script.py")


class ClaudeCodeGate(ClaudeCodeWithPatch):
    def __init__(self, *args, gate_spec: str | dict | None = None, max_blocks: int = 5, **kwargs):
        spec = json.loads(gate_spec) if isinstance(gate_spec, str) and gate_spec else gate_spec
        self.gate_spec = dict(spec, max_blocks=int(max_blocks)) if spec else None
        super().__init__(*args, **kwargs)

    async def setup(self, environment) -> None:
        await super().setup(environment)
        agent_dir = environment.env_paths.agent_dir
        await self._belay_exec(environment, f"mkdir -p {agent_dir}/gate && chmod 777 {agent_dir}/gate", check=False)
        if not self.gate_spec:
            await self._belay_exec(environment, f"echo 'no gate spec for this task; running without the Stop hook' "
                                                f"> {agent_dir}/gate/disabled.txt", check=False)
            return

        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(self.gate_spec, indent=1))
            await self._belay_exec(environment, f"mkdir -p {GATE_DIR}")
            await environment.upload_file(SCRIPT, f"{GATE_DIR}/gate_script.py")
            await environment.upload_file(spec_path, f"{GATE_DIR}/spec.json")
        await self._belay_exec(environment, f"cp {GATE_DIR}/spec.json {agent_dir}/gate/spec.json")

        # 基线：在原始代码上跑两次（setup 阶段，不计入 agent 预算）
        await self._belay_exec(environment, f"cd {shlex.quote(self.gate_spec['workdir'])} && "
                                            f"python3 {GATE_DIR}/gate_script.py baseline "
                                            f"> {agent_dir}/gate/baseline_run.log 2>&1", check=False)
        await self._belay_exec(environment, f"cp {GATE_DIR}/baseline.json {agent_dir}/gate/baseline.json 2>/dev/null; "
                                            f"chmod -R a+rX {GATE_DIR}", check=False)

        # 注册 Stop hook（Pier 把 CLAUDE_CONFIG_DIR 设为 /logs/agent/sessions）
        timeout = int(self.gate_spec.get("timeout_sec", 2400)) + 120
        settings = {"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": f"python3 {GATE_DIR}/gate_script.py check", "timeout": timeout}]}]}}
        cfg_dir = f"{agent_dir}/sessions"
        await self._belay_exec(environment, f"mkdir -p {cfg_dir} && echo {shlex.quote(json.dumps(settings))} "
                                            f"> {cfg_dir}/settings.json && chmod -R a+rwX {cfg_dir}")

"""A-gate 组：Claude Code + 确定性回归门（Stop hook）。

setup 阶段（不计入 agent 预算）：
  1. 与 A 组相同：安装 Claude Code，给仓库拍快照；
  2. 上传 gate_script.py 与该题的门禁配置（由转换器生成的 gate.json，runner 通过 gate_spec 参数传入）；
  3. 在原始代码上跑两次相关测试，记录两次都通过的测试（基线，放在 /opt/belay-gate，不在工作区内）；
  4. 在 Claude Code 的用户配置（$CLAUDE_CONFIG_DIR/settings.json）中注册 Stop hook。

门禁的配置、基线与记录都放在 /opt/belay-gate，运行期间不写入 agent 可见的 /logs/agent，结束时再复制到
/logs/agent/gate/。门禁配置中不含评分阶段才新增的测试文件（由转换器去除），避免泄露隐藏测试的文件名。

运行时：Claude Code 想结束时，hook 重跑测试；存在"原来通过、现在失败"的测试就返回 block 与失败列表，
Claude Code 继续工作；最多拦截 max_blocks 次后放行。没有例外流程：需求要求的合法行为变化也会被拦到上限。
hook 配置不进仓库，不影响补丁。

题目没有门禁配置（例如 LHTB 没有公开测试）时，不注册 hook，行为与 A 组相同，并记录原因。
"""
from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path

from eval.agents.claude_code import ClaudeCodeWithPatch
from eval.agents.patch_capture import STATE

GATE_DIR = "/opt/belay-gate"
SCRIPT = Path(__file__).with_name("gate_script.py")


class ClaudeCodeGate(ClaudeCodeWithPatch):
    def __init__(self, *args, gate_spec: str | dict | None = None, max_blocks: int = 5, **kwargs):
        spec = json.loads(gate_spec) if isinstance(gate_spec, str) and gate_spec else gate_spec
        self.gate_spec = dict(spec, max_blocks=int(max_blocks)) if spec else None
        super().__init__(*args, **kwargs)

    async def setup(self, environment) -> None:
        await super().setup(environment)          # 安装、补丁快照、写入禁用 WebSearch / WebFetch 的配置
        if not self.gate_spec:
            await self._belay_exec(environment, f"mkdir -p {GATE_DIR}/log && echo 'no gate spec for this task; "
                                                f"running without the Stop hook' > {GATE_DIR}/log/disabled.txt", check=False)
            return

        # 门禁的配置、基线、日志都放在 /opt/belay-gate，不写入 agent 可见的 /logs/agent（结束时再复制出来）
        spec = dict(self.gate_spec, base_tree_file=f"{STATE}/base_tree")
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "spec.json"
            spec_path.write_text(json.dumps(spec, indent=1))
            await self._belay_exec(environment, f"mkdir -p {GATE_DIR}/log {GATE_DIR}/state")
            await environment.upload_file(SCRIPT, f"{GATE_DIR}/gate_script.py")
            await environment.upload_file(spec_path, f"{GATE_DIR}/spec.json")

        # 基线：在原始代码上跑两次（setup 阶段，不计入 agent 预算）
        await self._belay_exec(environment, f"cd {shlex.quote(spec['workdir'])} && "
                                            f"python3 {GATE_DIR}/gate_script.py baseline "
                                            f"> {GATE_DIR}/log/baseline_run.log 2>&1", check=False)
        await self._belay_exec(environment, f"chmod -R a+rX {GATE_DIR} && chmod -R a+rwX {GATE_DIR}/log {GATE_DIR}/state",
                               check=False)

        # 注册 Stop hook（与禁用工具的配置写在同一个 settings.json 中）
        timeout = int(spec.get("timeout_sec", 2400)) + 120
        self._belay_settings["hooks"] = {"Stop": [{"hooks": [{
            "type": "command", "command": f"python3 {GATE_DIR}/gate_script.py check", "timeout": timeout}]}]}
        await self._belay_write_settings(environment)

    async def _belay_export_patch(self, environment, kill_pattern: str | None = None) -> None:
        """结束时把门禁的配置、基线与记录复制到 /logs/agent/gate（同步到宿主机）。"""
        agent_dir = environment.env_paths.agent_dir
        await self._belay_exec(environment, f"mkdir -p {agent_dir}/gate && cp -r {GATE_DIR}/log/. {agent_dir}/gate/ 2>/dev/null; "
                                            f"cp {GATE_DIR}/spec.json {GATE_DIR}/baseline.json {agent_dir}/gate/ 2>/dev/null; true",
                               check=False)
        await super()._belay_export_patch(environment, kill_pattern=kill_pattern)

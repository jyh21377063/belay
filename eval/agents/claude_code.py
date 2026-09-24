"""A 组：Pier 自带的 Claude Code agent + 结束时导出补丁。

模型走 DeepSeek 的 Anthropic 兼容接口，只需在 runs.yaml 里配置 ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN 和 model。Pier 的 ClaudeCode 会：
  - 设置 ANTHROPIC_MODEL；runs.yaml 的 env 最后合并，可覆盖各模型别名；
  - 把 ANTHROPIC_BASE_URL 的域名加入网络白名单（任务需 allow_internet = false）；
  - 关闭非必要流量、禁用 plan mode，并记录 ATIF 轨迹。

安装方式：Pier 默认从 https://claude.ai/install.sh 安装，部分云服务器会被 claude.ai 拒绝（403）。
这里改为从 npm 安装：npm 包的原生程序作为平台子包一起下载，postinstall 只做本地复制，不访问 claude.ai。
安装后的 claude 是原生可执行文件，运行时不需要 Node.js；安装用的临时 Node.js 装完即删，
不改动题目镜像原有的 Node 环境。
"""
from __future__ import annotations

import asyncio

from pier.agents.installed.claude_code import ClaudeCode
from pier.models.agent.install import AgentInstallSpec, InstallStep

from eval.agents.patch_capture import PatchCaptureMixin

NODE_VERSION = "22.12.0"          # @anthropic-ai/claude-code 要求 node >= 22

INSTALL_ROOT = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v curl >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y --no-install-recommends curl ca-certificates
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache curl ca-certificates
  elif command -v yum >/dev/null 2>&1; then yum install -y curl ca-certificates
  fi
fi

# 临时 Node.js，只用于执行 npm install
if command -v apk >/dev/null 2>&1; then
  apk add --no-cache nodejs npm
  NPM=npm
else
  case "$(uname -m)" in x86_64) A=x64 ;; aarch64|arm64) A=arm64 ;; *) echo "不支持的架构 $(uname -m)"; exit 1 ;; esac
  mkdir -p /opt/belay-node
  curl -fsSL "{node_mirror}/v{node_version}/node-v{node_version}-linux-$A.tar.gz" | tar xz -C /opt/belay-node --strip-components=1
  NPM=/opt/belay-node/bin/npm
  export PATH=/opt/belay-node/bin:$PATH
fi

# 安装到独立目录，再链接到 /usr/local/bin（对所有用户可见）
"$NPM" install -g --prefix /opt/claude-code --registry "{npm_registry}" --no-fund --no-audit "@anthropic-ai/claude-code{pkg_version}"
ln -sf /opt/claude-code/bin/claude /usr/local/bin/claude
rm -rf /opt/belay-node /root/.npm
/usr/local/bin/claude --version
"""


class ClaudeCodeWithPatch(PatchCaptureMixin, ClaudeCode):
    def __init__(self, *args, repo_dir: str | None = None,
                 node_mirror: str = "https://nodejs.org/dist",
                 npm_registry: str = "https://registry.npmjs.org",
                 **kwargs):
        self.repo_dir = repo_dir
        self.node_mirror = node_mirror.rstrip("/")
        self.npm_registry = npm_registry
        super().__init__(*args, **kwargs)

    def install_spec(self) -> AgentInstallSpec:
        root_run = INSTALL_ROOT.format(
            node_mirror=self.node_mirror, node_version=NODE_VERSION, npm_registry=self.npm_registry,
            pkg_version=f"@{self._version}" if self._version else "",
        )
        return AgentInstallSpec(
            agent_name=self.name(),
            version=self._version,
            steps=[
                InstallStep(user="root", env={"DEBIAN_FRONTEND": "noninteractive"}, run=root_run),
                InstallStep(user="agent", run="claude --version"),
            ],
            verification_command=self.get_version_command(),
        )

    async def setup(self, environment) -> None:
        await super().setup(environment)
        await self._belay_snapshot(environment)

    async def run(self, instruction, environment, context) -> None:
        try:
            await super().run(instruction, environment, context)
        finally:
            await asyncio.shield(self._belay_export_patch(environment, kill_pattern="claude"))

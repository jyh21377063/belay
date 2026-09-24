"""所有"会产出代码"的 agent 共用：setup 时给仓库拍快照，run 结束时导出 patch.diff。

用单独的 GIT_INDEX_FILE 做快照，不改动 agent 能看到的 git 状态；
镜像里原本就存在的未跟踪文件会记入基线，不会混进补丁；.gitignore 的文件不进补丁。
补丁写到 /logs/agent/patch.diff，Pier 会把该目录同步到宿主机的 trial 目录。
"""
from __future__ import annotations

import shlex
from pathlib import Path

GIT = "git -c safe.directory='*'"
STATE = "/opt/belay-capture"              # 容器内的快照状态目录
EXCLUDES = "':(exclude).claude' ':(exclude).belay'"


class PatchCaptureMixin:
    repo_dir: str | None = None
    _belay_repo: str | None = None

    async def _belay_exec(self, environment, cmd: str, check: bool = True):
        res = await environment.exec(command=f"bash -lc {shlex.quote(cmd)}", user="root")
        if check and res.return_code != 0:
            raise RuntimeError(f"[patch-capture] 命令失败 rc={res.return_code}: {cmd}\n{res.stdout}\n{res.stderr}")
        return res

    async def _belay_find_repo(self, environment) -> str:
        if self._belay_repo:
            return self._belay_repo
        start = shlex.quote(self.repo_dir) if self.repo_dir else "."
        res = await self._belay_exec(environment, f"cd {start} && {GIT} rev-parse --show-toplevel", check=False)
        repo = (res.stdout or "").strip().splitlines()[-1:] if res.return_code == 0 else []
        if not repo:
            raise RuntimeError("[patch-capture] 容器工作目录不在 git 仓库中；请在 agent kwargs 中设置 repo_dir")
        self._belay_repo = repo[0]
        return self._belay_repo

    async def _belay_snapshot(self, environment) -> None:
        repo = await self._belay_find_repo(environment)
        await self._belay_exec(environment, f"""
            set -e; cd {shlex.quote(repo)}; mkdir -p {STATE}
            export GIT_INDEX_FILE={STATE}/index
            cp "$({GIT} rev-parse --git-dir)/index" "$GIT_INDEX_FILE" 2>/dev/null || true
            {GIT} add -A
            {GIT} write-tree > {STATE}/base_tree
            echo {shlex.quote(repo)} > {STATE}/repo
        """)

    async def _belay_export_patch(self, environment, kill_pattern: str | None = None) -> None:
        """在 finally 里调用：无论 agent 正常结束、超时还是报错，都导出当前改动。"""
        agent_dir = environment.env_paths.agent_dir
        if kill_pattern:     # 超时后 agent 进程可能还在写文件，先停掉再取 diff
            await self._belay_exec(environment, f"pkill -f {shlex.quote(kill_pattern)} || true; sleep 1", check=False)
        repo = await self._belay_find_repo(environment)
        await self._belay_exec(environment, f"""
            set -e; cd {shlex.quote(repo)}
            export GIT_INDEX_FILE={STATE}/index
            BASE=$(cat {STATE}/base_tree)
            {GIT} add -A
            {GIT} diff-index --cached -p --binary "$BASE" -- . {EXCLUDES} > {agent_dir}/patch.diff
            {GIT} diff-index --cached --numstat "$BASE" -- . {EXCLUDES} > {agent_dir}/patch.numstat
        """)
        if not environment.capabilities.mounted:      # 非挂载环境（如 modal）需要手动下载
            for name in ("patch.diff", "patch.numstat"):
                await environment.download_file(f"{agent_dir}/{name}", Path(self.logs_dir) / name)

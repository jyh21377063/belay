"""影子仓库与集成分支：git 写操作只由 runtime 执行（不变量 3、4）。

影子仓库的 GIT_DIR 在 /opt/belay/git（只属于 root），work tree 按需指向各个工作区；仓库自己的 .git
不受影响，agent 用 git diff 看到的仍是相对原始提交的改动。git 仓库与 LHTB 的非 git 目录走同一套代码。

  init        给原始代码拍快照 → 基线提交；refs/heads/integration 指向它
  snapshot    工作区当前内容 → 树（每个工作区一个持久的索引文件，重复快照只重新哈希改动过的文件）
  candidate   快照 → 剔除测试路径下的改动 → 以集成分支 HEAD 为父提交的候选提交
  advance     update-ref 比较并交换推进集成分支
  checkout    把工作区精确切换为某个树（交付时用：工作区 = 集成分支 HEAD）
"""
from __future__ import annotations

import asyncio
import json
import shlex

from belay.config import RuntimePaths
from belay.env import Env

REF = "refs/heads/integration"
EXCLUDES = [".belay_checks/", "__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
            ".hypothesis/", "*.belay-tmp"]
LARGE_FILE_MB = 20


class GitError(RuntimeError):
    pass


class ShadowRepo:
    def __init__(self, env: Env, paths: RuntimePaths):
        self.env = env                       # 以 root 运行
        self.paths = paths
        self._locks: dict[str, asyncio.Lock] = {}

    def git(self, work_tree: str | None = None, index: str | None = None) -> str:
        env = f"GIT_INDEX_FILE={shlex.quote(index)} " if index else ""
        wt = f" --work-tree={shlex.quote(work_tree)}" if work_tree else ""
        return (f"{env}git -c safe.directory='*' -c core.autocrlf=false -c core.quotepath=off "
                f"-c user.name=belay -c user.email=belay@localhost --git-dir={shlex.quote(self.paths.git_dir)}{wt}")

    async def _run(self, cmd: str, timeout: float = 300) -> str:
        res = await self.env.run(cmd, timeout=timeout, cwd="/")
        if res.return_code != 0:
            raise GitError(f"rc={res.return_code}: {cmd[:300]}\n{res.output[-1500:]}")
        return res.output.strip()

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def init(self, workspace: str) -> tuple[str, str]:
        g = self.paths.git_dir
        excludes = "\n".join(EXCLUDES)
        await self._run(f"mkdir -p {shlex.quote(g)} {shlex.quote(self.paths.index(''))} && "
                        f"{self.git(workspace)} init -q && {self.git()} config core.bare false && "
                        f"printf '%s\\n' {shlex.quote(excludes)} > {shlex.quote(g)}/info/exclude")
        await self._refresh_large_excludes(workspace)
        tree = await self.snapshot(workspace, "base")
        commit = await self.commit(tree, None, "belay: original code")
        await self._run(f"{self.git()} update-ref {REF} {commit}")
        return commit, tree

    async def _refresh_large_excludes(self, workspace: str) -> None:
        """大文件（构建产物、数据）不进影子仓库：写入 info/exclude 的末尾一段。"""
        g = shlex.quote(self.paths.git_dir)
        marker = "# belay: large files"
        cmd = (f"cd {shlex.quote(workspace)} && sed -i '/^{marker}$/,$d' {g}/info/exclude && "
               f"echo '{marker}' >> {g}/info/exclude && "
               f"find . -path ./.git -prune -o -type f -size +{LARGE_FILE_MB}M -print 2>/dev/null | head -2000 | "
               f"sed 's|^\\./|/|' >> {g}/info/exclude")
        await self._run(cmd, timeout=300)

    async def snapshot(self, workspace: str, index_name: str) -> str:
        async with self._lock(index_name):
            git = self.git(workspace, self.paths.index(index_name))
            return (await self._run(f"cd {shlex.quote(workspace)} && {git} add -A . && {git} write-tree",
                                    timeout=600)).splitlines()[-1]

    async def commit(self, tree: str, parent: str | None, message: str) -> str:
        p = f" -p {parent}" if parent else ""
        out = await self._run(f"printf '%s' {shlex.quote(message)} | {self.git()} commit-tree {tree}{p}")
        return out.splitlines()[-1]

    async def head(self) -> str:
        return (await self._run(f"{self.git()} rev-parse {REF}")).splitlines()[-1]

    async def changed(self, a: str, b: str) -> list[str]:
        out = await self._run(f"{self.git()} diff-tree -r --no-renames --name-only {a} {b}")
        return [line for line in out.splitlines() if line]

    async def strip_tests(self, base_tree: str, tree: str) -> tuple[str, list[str]]:
        out = await self._run(f"python3 {shlex.quote(self.paths.runner)} strip-tests "
                              f"{shlex.quote(self.paths.git_dir)} {base_tree} {tree}")
        data = json.loads(out.splitlines()[-1])
        return data["tree"], data["dropped"]

    async def advance(self, new: str, expected: str) -> bool:
        res = await self.env.run(f"{self.git()} update-ref {REF} {new} {expected}", timeout=60, cwd="/")
        return res.return_code == 0

    async def checkout(self, workspace: str, index_name: str, target_tree: str) -> None:
        """把工作区切换为 target_tree：先快照（索引 = 当前内容），再两树合并更新工作区，
        删除 target 中没有的已跟踪文件；被忽略的文件（构建产物等）保持不动。"""
        current = await self.snapshot(workspace, index_name)
        if current == target_tree:
            return
        async with self._lock(index_name):
            git = self.git(workspace, self.paths.index(index_name))
            await self._run(f"cd {shlex.quote(workspace)} && {git} read-tree -m -u {current} {target_tree}",
                            timeout=600)

    async def diff(self, a: str, b: str, max_chars: int = 60000, exclude_tests: bool = True) -> str:
        spec = " -- . ':(exclude)*test*'" if exclude_tests else ""
        out = await self._run(f"{self.git()} diff --no-color {a} {b}{spec} | head -c {max_chars}", timeout=120)
        return out

    async def show(self, tree: str, path: str, max_chars: int = 20000) -> str:
        res = await self.env.run(f"{self.git()} show {tree}:{shlex.quote(path)} | head -c {max_chars}",
                                 timeout=60, cwd="/")
        return res.output if res.return_code == 0 else ""

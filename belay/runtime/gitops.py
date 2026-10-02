"""影子仓库：合并链对应的 git 提交。只有 runtime 写它；worker 看不到（路径在 Policy.protected_prefixes 里）。

GIT_DIR 在工作区之外，work tree 指向工作区；仓库自己的 .git 不受影响（worker 用 git diff 看到的仍是相对原始
提交的改动）。git 仓库与不是 git 仓库的目录走同一套代码。

  init        原始代码 → 基线提交；引用 refs/heads/belay 指向它
  snapshot    工作区当前内容 → 树（持久的索引文件，重复快照只重新哈希改动过的文件）
  strip_tests 把测试路径下的改动恢复为基线版本 → 候选树
  commit      确定的提交（日期取事件时间，所以重做得到同一个提交）
  cas         update-ref <新> <旧>：比较并交换推进合并链（合并提交的说明就是复核者写的一行标签）
  checkout    把工作区精确切换为某棵树（回退、交付、重建）
  snapshot_commit  把一张快照（原样树 + 候选树）包成一个确定的提交，ref 为 refs/belay/snap/<n>（防 gc、便于导出）
  bundle      增量 git bundle：宿主机上的镜像由它恢复（G3）
  export_to   把一棵树导出到复核目录（复核者在那里读代码、运行程序）；交付时 refs/belay/delivered 指向交付的合并点
  revert_files     只撤销“好 → 坏”之间、限定文件的改动（逐文件三方合并；有冲突就什么都不改）
"""
from __future__ import annotations

import base64
import posixpath
import shlex
from typing import Callable

from belay.core.verify import is_test_path
from belay.env import Env

REF = "refs/heads/belay"
SNAP_REF = "refs/belay/snap/"
CP_REF = "refs/belay/cp/"
DELIVERED_REF = "refs/belay/delivered"
EXCLUDES = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".hypothesis/",
            "*.belay-tmp", ".belay_checks/"]
LARGE_FILE_MB = 20


class GitError(RuntimeError):
    pass


class ShadowRepo:
    def __init__(self, env: Env, git_dir: str, workspace: str):
        self.env = env
        self.git_dir = git_dir
        self.workspace = workspace

    def _git(self, work_tree: bool = False, index: str | None = None, date: float | None = None) -> str:
        pre = ""
        if index:
            pre += f"GIT_INDEX_FILE={shlex.quote(index)} "
        if date is not None:
            d = f"@{int(date)} +0000"
            pre += f"GIT_AUTHOR_DATE='{d}' GIT_COMMITTER_DATE='{d}' "
        wt = f" --work-tree={shlex.quote(self.workspace)}" if work_tree else ""
        return (f"{pre}git -c safe.directory='*' -c core.autocrlf=false -c core.quotepath=off "
                f"-c user.name=belay -c user.email=belay@localhost --git-dir={shlex.quote(self.git_dir)}{wt}")

    def index(self, name: str) -> str:
        return posixpath.join(self.git_dir, f"belay-index-{name}")

    async def _run(self, cmd: str, timeout: float = 300, cwd: str = "/") -> str:
        res = await self.env.run(cmd, timeout=timeout, cwd=cwd)
        if res.return_code != 0:
            raise GitError(f"rc={res.return_code}: {cmd[:300]}\n{res.output[-1500:]}")
        return res.output.strip()

    # ---- 初始化
    async def exists(self) -> bool:
        res = await self.env.run(f"test -f {shlex.quote(self.git_dir)}/HEAD", timeout=30, cwd="/")
        return res.return_code == 0

    async def init(self) -> tuple[str, str]:
        g = shlex.quote(self.git_dir)
        await self._run(f"mkdir -p {g} && {self._git()} init -q && {self._git()} config core.bare false && "
                        f"printf '%s\\n' {shlex.quote(chr(10).join(EXCLUDES))} > {g}/info/exclude")
        await self._run(f"cd {shlex.quote(self.workspace)} && "
                        f"find . -path ./.git -prune -o -type f -size +{LARGE_FILE_MB}M -print 2>/dev/null | "
                        f"head -2000 | sed 's|^\\./|/|' >> {g}/info/exclude", timeout=300)
        tree = await self.snapshot("base")
        commit = await self.commit(tree, None, "belay: original code", 0)
        await self._run(f"{self._git()} update-ref {REF} {commit}")
        return commit, tree

    # ---- 观察
    async def snapshot(self, index_name: str = "w1") -> str:
        git = self._git(work_tree=True, index=self.index(index_name))
        out = await self._run(f"cd {shlex.quote(self.workspace)} && {git} add -A . && {git} write-tree", timeout=600)
        return out.splitlines()[-1].strip()

    async def _diff_entries(self, a: str, b: str) -> list[tuple[str, str, str, str]]:
        """[(status, path, old_mode, old_sha)]"""
        out = await self._run(f"{self._git()} diff-tree -r --no-renames -z {a} {b} | base64 | tr -d '\\n'")
        raw = base64.b64decode(out).decode("utf-8", "surrogateescape") if out else ""
        items = raw.split("\0")
        res, i = [], 0
        while i < len(items) - 1:
            meta, path = items[i], items[i + 1]
            i += 2
            if not meta.startswith(":"):
                continue
            m1, _m2, s1, _s2, st = meta[1:].split()
            res.append((st[0], path, m1, s1))
        return res

    async def strip_tests(self, base_tree: str, tree: str,
                          is_test: Callable[[str], bool] = is_test_path) -> tuple[str, list[str]]:
        """把 tree 中测试路径下的改动恢复为 base_tree 的版本。返回（新树，被剔除的路径）。
        is_test：测试路径的判断（runtime 传入按基线测试布局判断的版本，见 verify.suite_layout）。"""
        lines, dropped = [], []
        for st, path, m1, s1 in await self._diff_entries(base_tree, tree):
            if not is_test(path):
                continue
            dropped.append(path)
            lines.append(f"0 {'0' * 40}\t{path}" if st == "A" else f"{m1} {s1}\t{path}")
        if not dropped:
            return tree, []
        idx = self.index("strip")
        data = base64.b64encode(("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")).decode()
        git = self._git(index=idx)
        out = await self._run(f"rm -f {shlex.quote(idx)} && {git} read-tree {tree} && "
                              f"printf %s {shlex.quote(data)} | base64 -d | {git} update-index --index-info && "
                              f"{git} write-tree && rm -f {shlex.quote(idx)}")
        return out.splitlines()[-1].strip(), sorted(dropped)

    async def numstat(self, a: str, b: str) -> list[tuple[str, int, int]]:
        out = await self._run(f"{self._git()} diff-tree -r --no-renames --numstat {a} {b}")
        res = []
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                add, dele, path = parts
                res.append((path, int(add) if add.isdigit() else 0, int(dele) if dele.isdigit() else 0))
        return sorted(res)

    async def diff(self, a: str, b: str, binary: bool = False, max_bytes: int | None = None) -> str:
        cap = f" | head -c {int(max_bytes)}" if max_bytes else ""
        flag = "--binary --full-index" if binary else ""
        res = await self.env.run(f"{self._git()} diff --no-color {flag} {a} {b}{cap}", timeout=300, cwd="/")
        if res.return_code != 0:
            raise GitError(res.output[-1000:])
        return res.output

    # ---- 提交与引用
    async def commit(self, tree: str, parent: str | None, message: str, date: float) -> str:
        p = f" -p {parent}" if parent else ""
        out = await self._run(f"printf '%s' {shlex.quote(message)} | {self._git(date=date)} commit-tree {tree}{p}")
        return out.splitlines()[-1].strip()

    async def read_ref(self) -> str | None:
        res = await self.env.run(f"{self._git()} rev-parse --verify -q {REF}", timeout=60, cwd="/")
        return res.output.strip().splitlines()[-1] if res.return_code == 0 and res.output.strip() else None

    async def cas(self, new: str, expected: str) -> bool:
        res = await self.env.run(f"{self._git()} update-ref {REF} {new} {expected}", timeout=60, cwd="/")
        return res.return_code == 0

    async def set_ref(self, commit: str) -> None:
        await self._run(f"{self._git()} update-ref {REF} {commit}")

    async def tree_of(self, commit: str) -> str:
        return (await self._run(f"{self._git()} rev-parse {commit}^{{tree}}")).splitlines()[-1].strip()

    # ---- 工作区
    async def checkout(self, target_tree: str, index_name: str = "w1") -> None:
        """把工作区切换为 target_tree：已跟踪文件按 target 更新或删除；被忽略的文件（构建产物）不动。"""
        current = await self.snapshot(index_name)
        if current == target_tree:
            return
        git = self._git(work_tree=True, index=self.index(index_name))
        await self._run(f"cd {shlex.quote(self.workspace)} && {git} read-tree -m -u {current} {target_tree}",
                        timeout=600, cwd=self.workspace)

    async def show(self, tree: str, path: str, max_chars: int = 20000) -> str:
        res = await self.env.run(f"{self._git()} show {tree}:{shlex.quote(path)} | head -c {max_chars}",
                                 timeout=60, cwd="/")
        return res.output if res.return_code == 0 else ""

    # ---- 复核目录（模块 F）
    async def export_to(self, target: str, tree: str, seed_index: str | None = None) -> None:
        """把 tree 增量导出到 target：只改动与上次导出不同的文件；未跟踪的文件（上次复核运行留下的输出）清掉，
        被忽略的文件（构建缓存）保留。第一次导出时从工作区复制被忽略的文件作为构建缓存种子（尽力而为）。"""
        t = target.rstrip("/")
        q, marker = shlex.quote(t), shlex.quote(t + ".seeded")
        git = self._git(index=t + ".index") + f" --work-tree={q}"
        # 上次复核的命令改过的已跟踪文件：read-tree 只比较索引，所以先按工作树把它们找出来恢复
        await self._run(f"mkdir -p {q} && {git} read-tree --reset -u {tree} && "
                        f"{{ {git} update-index -q --refresh >/dev/null 2>&1; "
                        f"{git} diff-files --name-only -z | xargs -0 -r env {git} checkout-index -f --; }} && "
                        f"{git} clean -f -d -q", timeout=900)
        if seed_index:
            ws = shlex.quote(self.workspace)
            lister = self._git(work_tree=True, index=seed_index)
            await self.env.run(f"[ -f {marker} ] && exit 0; cd {ws} && {lister} ls-files -z -o -i --exclude-standard "
                               f"--directory 2>/dev/null | xargs -0 -r cp -a --reflink=auto --parents -t {q} 2>/dev/null; "
                               f"touch {marker}", timeout=1800, cwd="/")

    # ---- 快照提交与引用（模块 B / G3）
    async def snapshot_commit(self, n: int, raw_tree: str, cand_tree: str, parent: str | None, date: float) -> str:
        """一张快照 = 一个提交：它的树是 {raw: 原样树, cand: 候选树}；父提交是上一张快照，所以一条链就能增量导出。"""
        entries = f"040000 tree {raw_tree}\traw\n040000 tree {cand_tree}\tcand\n"
        data = base64.b64encode(entries.encode()).decode()
        tree = await self._run(f"printf %s {shlex.quote(data)} | base64 -d | {self._git()} mktree")
        commit = await self.commit(tree.splitlines()[-1].strip(), parent, f"belay: snapshot {n}", date)
        await self._run(f"{self._git()} update-ref {SNAP_REF}{int(n)} {commit}")
        return commit

    async def set_cp_ref(self, k: int, commit: str) -> None:
        await self._run(f"{self._git()} update-ref {CP_REF}{int(k)} {commit}")

    async def has_objects(self, shas: list[str]) -> dict[str, bool]:
        if not shas:
            return {}
        script = "; ".join(f"{self._git()} cat-file -e {s} 2>/dev/null && echo {s}=1 || echo {s}=0" for s in shas)
        out = (await self.env.run(script, timeout=300, cwd="/")).output
        got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return {s: got.get(s) == "1" for s in shas}

    async def bundle_create(self, path: str, tips: list[str], exclude: list[str]) -> bool:
        """增量 bundle：包含 tips 可达、exclude 不可达的对象。没有新东西时返回 False。"""
        revs = " ".join(shlex.quote(t) for t in tips) + " " + " ".join(f"^{e}" for e in exclude if e)
        res = await self.env.run(f"{self._git()} bundle create {shlex.quote(path)} {revs}", timeout=1800, cwd="/")
        if res.return_code != 0:
            if "empty bundle" in res.output.lower() or "refusing to create empty bundle" in res.output.lower():
                return False
            raise GitError(f"bundle create failed: {res.output[-800:]}")
        return True

    async def bundle_unbundle(self, path: str) -> None:
        await self._run(f"{self._git()} bundle unbundle {shlex.quote(path)} > /dev/null", timeout=1800)

    async def update_ref(self, ref: str, commit: str) -> None:
        await self._run(f"{self._git()} update-ref {ref} {commit}")

    # ---- 只撤销这一段（D4）
    async def revert_files(self, good_tree: str, bad_tree: str, paths: list[str]) -> tuple[bool, str]:
        """对每个文件做三方合并：ours = 工作区，base = 坏端，theirs = 好端，即在当前状态上反向应用“好 → 坏”的改动。
        全部干净合并才写回；任何冲突就什么都不改。返回（成功，说明）。"""
        ws = self.workspace.rstrip("/")
        tmp = f"/tmp/belay-revert-{abs(hash((good_tree, bad_tree))) % 10 ** 8}"
        plan: list[tuple[str, str]] = []            # (路径, 动作 write|delete)
        await self._run(f"rm -rf {tmp} && mkdir -p {tmp}")
        try:
            for i, path in enumerate(paths):
                q = shlex.quote(path)
                full = shlex.quote(f"{ws}/{path}")
                probe = await self.env.run(
                    f"{self._git()} cat-file -e {good_tree}:{q} 2>/dev/null && echo G; "
                    f"{self._git()} cat-file -e {bad_tree}:{q} 2>/dev/null && echo B; test -f {full} && echo W",
                    timeout=60, cwd="/")
                has_g, has_b, has_w = ("G" in probe.output.split(), "B" in probe.output.split(),
                                       "W" in probe.output.split())
                if has_b and has_g:
                    if not has_w:
                        return False, f"{path} was deleted in your working tree"
                    res = await self.env.run(
                        f"{self._git()} show {bad_tree}:{q} > {tmp}/{i}.base && "
                        f"{self._git()} show {good_tree}:{q} > {tmp}/{i}.theirs && cp {full} {tmp}/{i}.ours && "
                        f"git merge-file -q {tmp}/{i}.ours {tmp}/{i}.base {tmp}/{i}.theirs", timeout=120, cwd="/")
                    if res.return_code != 0:
                        return False, f"{path} conflicts with later changes"
                    plan.append((path, "write"))
                elif has_b and not has_g:          # 这一段里新增的文件：内容没再变过才删除
                    same = await self.env.run(f"{self._git()} show {bad_tree}:{q} | cmp -s - {full}", timeout=60,
                                              cwd="/")
                    if has_w and same.return_code != 0:
                        return False, f"{path} was added in that change and has been modified since"
                    if has_w:
                        plan.append((path, "delete"))
                elif has_g and not has_b:          # 这一段里删除的文件：工作区里没有才恢复
                    if has_w:
                        return False, f"{path} was deleted in that change and exists again now"
                    await self._run(f"{self._git()} show {good_tree}:{q} > {tmp}/{i}.ours")
                    plan.append((path, "write"))
            for i, path in enumerate(paths):
                act = dict(plan).get(path)
                full = shlex.quote(f"{ws}/{path}")
                if act == "write":
                    await self._run(f"mkdir -p \"$(dirname {full})\" && cat {tmp}/{i}.ours > {full}")
                elif act == "delete":
                    await self._run(f"rm -f {full}")
            return True, f"reverted {len(plan)} file(s)"
        finally:
            await self.env.run(f"rm -rf {tmp}", timeout=60, cwd="/")

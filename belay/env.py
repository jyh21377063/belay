"""执行环境：worker 的所有工具都通过 Env.run 在任务容器里执行。

循环本身跑在宿主机进程里（与 eval/agents/flat_agent.py 的约定一致），容器里不需要 Python，
只依赖 bash 与 coreutils（head / sed / wc / base64 / timeout / find / grep）。

三个实现共用同一套读写逻辑：
  PierEnv    评测时使用，包装 Pier 的 environment.exec
  LocalEnv   本地开发与单元测试，在本机目录上用 subprocess 执行
  DockerEnv  调试用，对着一个保留下来的容器（keep_containers）执行
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shlex
import signal
from dataclasses import dataclass

TIMEOUT_RC = 124            # GNU timeout 超时时的退出码
_CHUNK = 96 * 1024          # 写文件时每条命令携带的 base64 长度


@dataclass
class ExecOutput:
    output: str             # stdout 与 stderr 合并后的输出（保持交错顺序）
    return_code: int

    @property
    def timed_out(self) -> bool:
        return self.return_code == TIMEOUT_RC


class FileMissing(Exception):
    pass


class FileTooLarge(Exception):
    pass


class Env:
    """子类只需实现 _exec（执行一条 bash 命令，返回合并输出与退出码）。"""

    workdir: str

    async def _exec(self, command: str, timeout: float) -> ExecOutput:
        raise NotImplementedError

    async def run(self, command: str, timeout: float = 120, cwd: str | None = None) -> ExecOutput:
        """在 cwd（默认 workdir）下执行命令，stderr 合并进 stdout。

        超时由容器内的 GNU timeout 负责（杀掉整个进程组），外层再留 30 秒兜底。
        """
        inner = f"cd {shlex.quote(cwd or self.workdir)} && {command}"
        wrapped = f"timeout -k 5 {int(max(1, timeout))} bash -c {shlex.quote(inner)} 2>&1"
        return await self._exec(wrapped, timeout + 30)

    # ---- 文件读写：全部经由 run，因此三种环境行为一致
    # 读取时在同一条命令里取回整个文件的 sha256，供"读后被改"检测使用，不额外增加 exec。
    async def read_text(self, path: str, max_bytes: int = 8 * 1024 * 1024) -> tuple[str, str]:
        """读取整个文件（编辑前用），返回（内容，sha256）。

        非 UTF-8 字节用 surrogateescape 保留，写回时原样还原。
        """
        q = shlex.quote(path)
        res = await self.run(f"test -f {q} || {{ echo __BELAY_NOFILE__; exit 3; }}; "
                             f"size=$(wc -c < {q}); echo $size; [ $size -le {max_bytes} ] || exit 4; "
                             f"sha256sum {q} | cut -c1-64; base64 < {q} | tr -d '\\n'", timeout=60)
        if res.return_code == 3 and "__BELAY_NOFILE__" in res.output:
            raise FileMissing(path)
        if res.return_code == 4:                     # 截断后写回会丢内容，所以直接拒绝
            raise FileTooLarge(f"{path} is {res.output.strip()} bytes; files over {max_bytes} bytes cannot be edited")
        if res.return_code != 0:
            raise OSError(f"read failed rc={res.return_code}: {res.output[-500:]}")
        _size, digest, rest = (res.output.strip().split("\n", 2) + ["", ""])[:3]
        return base64.b64decode(rest.strip()).decode("utf-8", errors="surrogateescape"), digest.strip()

    async def read_lines(self, path: str, start: int, count: int) -> tuple[int, str, str]:
        """返回（文件总行数，第 start 行起 count 行的文本，整个文件的 sha256）。行号从 1 开始。"""
        q = shlex.quote(path)
        end = start + count - 1
        res = await self.run(f"test -f {q} || {{ echo __BELAY_NOFILE__; exit 3; }}; "
                             f"wc -l < {q}; sha256sum {q} | cut -c1-64; "
                             f"sed -n '{start},{end}p' {q} | head -c 2000000 | base64 | tr -d '\\n'",
                             timeout=60)
        if res.return_code == 3 and "__BELAY_NOFILE__" in res.output:
            raise FileMissing(path)
        if res.return_code != 0:
            raise OSError(f"read failed rc={res.return_code}: {res.output[-500:]}")
        lines = res.output.strip().split("\n", 2)
        total = int(lines[0].strip() or 0)
        digest = lines[1].strip() if len(lines) > 1 else ""
        rest = lines[2].strip() if len(lines) > 2 else ""
        text = base64.b64decode(rest).decode("utf-8", errors="replace") if rest else ""
        return total, text, digest

    async def digest(self, path: str) -> str | None:
        """文件的 sha256；文件不存在时返回 None。"""
        q = shlex.quote(path)
        res = await self.run(f"test -f {q} || exit 3; sha256sum {q} | cut -c1-64", timeout=30)
        if res.return_code == 3:
            return None
        if res.return_code != 0:
            raise OSError(f"digest failed rc={res.return_code}: {res.output[-500:]}")
        return res.output.strip()

    async def write_text(self, path: str, text: str) -> str:
        """写文件：先写到临时文件再 cat 覆盖目标，保留目标文件原有的权限与属主。返回写入内容的 sha256。"""
        raw = text.encode("utf-8", errors="surrogateescape")
        data = base64.b64encode(raw).decode()
        q = shlex.quote(path)
        tmp = shlex.quote(f"{path}.belay-tmp")
        res = await self.run(f"mkdir -p \"$(dirname {q})\" && : > {tmp}", timeout=30)
        if res.return_code != 0:
            raise OSError(f"write failed: {res.output[-500:]}")
        for i in range(0, len(data), _CHUNK):
            chunk = data[i:i + _CHUNK]
            res = await self.run(f"printf %s {shlex.quote(chunk)} | base64 -d >> {tmp}", timeout=60)
            if res.return_code != 0:
                await self.run(f"rm -f {tmp}", timeout=30)
                raise OSError(f"write failed: {res.output[-500:]}")
        res = await self.run(f"cat {tmp} > {q} && rm -f {tmp}", timeout=30)
        if res.return_code != 0:
            raise OSError(f"write failed: {res.output[-500:]}")
        return hashlib.sha256(raw).hexdigest()

    async def exists(self, path: str) -> bool:
        return (await self.run(f"test -e {shlex.quote(path)}", timeout=30)).return_code == 0


class PierEnv(Env):
    """评测时的环境：包装 Pier 的 BaseEnvironment。"""

    def __init__(self, environment, workdir: str, user: str | int | None = None):
        self.environment = environment
        self.workdir = workdir
        self.user = user

    async def _exec(self, command: str, timeout: float) -> ExecOutput:
        try:
            res = await self.environment.exec(command=f"bash -c {shlex.quote(command)}",
                                              timeout_sec=int(timeout), user=self.user)
        except asyncio.CancelledError:
            raise
        except Exception as e:                     # Pier 层面的超时或连接错误，按超时处理
            return ExecOutput(f"[belay] exec failed: {type(e).__name__}: {e}", TIMEOUT_RC)
        return ExecOutput((res.stdout or "") + (res.stderr or ""), res.return_code)


class _SubprocessEnv(Env):
    async def _spawn(self, argv: list[str], timeout: float) -> ExecOutput:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            return ExecOutput("[belay] outer timeout, killed", TIMEOUT_RC)
        return ExecOutput(out.decode("utf-8", errors="replace"), proc.returncode or 0)


class LocalEnv(_SubprocessEnv):
    """本地开发与单元测试：直接在本机目录执行。"""

    def __init__(self, workdir: str):
        self.workdir = os.path.abspath(workdir)

    async def _exec(self, command: str, timeout: float) -> ExecOutput:
        return await self._spawn(["bash", "-c", command], timeout)


class DockerEnv(_SubprocessEnv):
    """调试用：对着一个已有容器执行（例如 keep_containers 保留下来的题目容器）。"""

    def __init__(self, container: str, workdir: str, user: str | None = None):
        self.container = container
        self.workdir = workdir
        self.user = user

    async def _exec(self, command: str, timeout: float) -> ExecOutput:
        argv = ["docker", "exec"] + (["-u", self.user] if self.user else []) + [self.container, "bash", "-c", command]
        return await self._spawn(argv, timeout)

"""验证器：在任务容器里以独立进程组运行检查作业，写完成标记；宿主机分段等待标记出现。

每个作业一个目录（jobs_dir/<作业 id>/）：
  spec.json    runner.py 的输入        pid   进程组 id（取消时 kill 整个进程组）
  out / log    包装层输出 / 测试输出    rc / done   退出码与完成标记（崩溃恢复时按它对账）
  result.json  runner.py 的结构化结果

非 live 的作业（基线、验证、确认、证据）让 runner 临时把工作区切换为作业的树（测试路径是原始版本），结束后恢复；
它们与 live 的开发检查之间用读写锁隔开：验证独占工作区，开发检查可以并行。
"""
from __future__ import annotations

import asyncio
import json
import posixpath
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from belay.core.model import Job
from belay.core.verify import is_cmd
from belay.env import Env

RUNNER_SOURCE = Path(__file__).resolve().parents[1] / "container" / "runner.py"
POLL_ROUND_SEC = 60


@dataclass
class VerifierSpec:
    """检查的配置。兼容 eval 的 gate.json（workdir / prelude / commands / test_cmd / parser / timeout_sec）。"""
    test_cmd: Optional[str] = None
    parser: str = "parse_log_pytest"
    prelude: str = ""
    commands: list[str] = field(default_factory=list)
    timeout_sec: int = 3600
    public_checks: dict[str, str] = field(default_factory=dict)     # 名字 → 命令；检查 id 为 cmd:<名字>

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "VerifierSpec":
        d = dict(d or {})
        return cls(test_cmd=d.get("test_cmd"), parser=d.get("parser") or "parse_log_pytest",
                   prelude=d.get("prelude") or "", commands=list(d.get("commands") or []),
                   timeout_sec=int(d.get("timeout_sec") or d.get("timeout") or 3600),
                   public_checks=dict(d.get("public_checks") or {}))

    @property
    def available(self) -> bool:
        return bool(self.test_cmd or self.public_checks)

    @property
    def check_ids(self) -> list[str]:
        return [f"cmd:{k}" for k in sorted(self.public_checks)]


@dataclass
class JobOutcome:
    state: str                  # finished | unknown | cancelled
    results: dict
    sec: float
    error: str = ""


class RWLock:
    def __init__(self):
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False

    async def acquire(self, exclusive: bool) -> None:
        async with self._cond:
            if exclusive:
                await self._cond.wait_for(lambda: not self._writer and self._readers == 0)
                self._writer = True
            else:
                await self._cond.wait_for(lambda: not self._writer)
                self._readers += 1

    async def release(self, exclusive: bool) -> None:
        async with self._cond:
            if exclusive:
                self._writer = False
            else:
                self._readers -= 1
            self._cond.notify_all()


class RunnerVerifier:
    def __init__(self, env: Env, spec: VerifierSpec, workspace: str, git_dir: str, jobs_dir: str,
                 runner_path: Optional[str] = None, poll_sec: float = 0.5):
        self.env = env
        self.spec = spec
        self.workspace = workspace
        self.git_dir = git_dir
        self.jobs_dir = jobs_dir
        self.runner_path = runner_path or posixpath.join(jobs_dir, "runner.py")
        self.poll_sec = poll_sec
        self.lock = RWLock()
        self._uploaded = False

    async def setup(self) -> None:
        if self._uploaded:
            return
        await self._must(f"mkdir -p {shlex.quote(self.jobs_dir)}")
        await self.env.write_text(self.runner_path, RUNNER_SOURCE.read_text(encoding="utf-8"))
        self._uploaded = True

    def job_dir(self, job_id: str) -> str:
        return posixpath.join(self.jobs_dir, job_id)

    def runner_spec(self, job: Job) -> dict:
        sel = job.selection
        tests = [] if sel is None else [u for u in sel if not is_cmd(u)]
        checks = [{"id": f"cmd:{k}", "command": v} for k, v in sorted(self.spec.public_checks.items())
                  if sel is None or f"cmd:{k}" in sel]
        spec = {"workspace": self.workspace, "prelude": self.spec.prelude, "commands": self.spec.commands,
                "test_cmd": self.spec.test_cmd, "parser": self.spec.parser, "select": tests,
                "timeout": self.spec.timeout_sec, "checks": checks,
                "skip_tests": sel is not None and not tests}
        if not job.live:
            spec.update(tree=job.tree, git_dir=self.git_dir, index=posixpath.join(self.job_dir(job.id), "index"))
        return spec

    async def run(self, job: Job) -> JobOutcome:
        exclusive = not job.live
        await self.lock.acquire(exclusive)
        try:
            await self.setup()
            t0 = time.time()
            await self.launch(job)
            return await self.wait(job.id, t0)
        finally:
            await self.lock.release(exclusive)

    async def launch(self, job: Job) -> None:
        d = shlex.quote(self.job_dir(job.id))
        await self._must(f"rm -rf {d} && mkdir -p {d}")
        await self.env.write_text(f"{self.job_dir(job.id)}/spec.json", json.dumps(self.runner_spec(job)))
        inner = f"python3 {shlex.quote(self.runner_path)} run {d}/spec.json {d}"
        wrapped = f"timeout -k 10 {int(self.spec.timeout_sec) + 120} bash -c {shlex.quote(inner)}"
        script = f"{wrapped} > {d}/out 2>&1; echo $? > {d}/rc; touch {d}/done"
        await self._must(f"cd / && setsid nohup bash -c {shlex.quote(script)} > /dev/null 2>&1 < /dev/null & "
                         f"echo $! > {d}/pid")

    async def wait(self, job_id: str, t0: float) -> JobOutcome:
        d = shlex.quote(self.job_dir(job_id))
        n = max(1, int(POLL_ROUND_SEC / self.poll_sec))
        while True:
            res = await self.env.run(f"for i in $(seq {n}); do [ -f {d}/done ] && exit 0; sleep {self.poll_sec}; "
                                     f"done; exit 7", timeout=POLL_ROUND_SEC + 30, cwd="/")
            if res.return_code == 0:
                break
            if res.return_code != 7:
                await asyncio.sleep(2)
        out = await self.collect(job_id)
        if out is None:
            return JobOutcome("unknown", {}, time.time() - t0, "no completion marker")
        return out

    async def collect(self, job_id: str) -> Optional[JobOutcome]:
        """有完成标记就读取结果；没有返回 None（恢复对账用）。"""
        d = shlex.quote(self.job_dir(job_id))
        res = await self.env.run(f"[ -f {d}/done ] || exit 3; cat {d}/result.json 2>/dev/null", timeout=60, cwd="/")
        if res.return_code == 3:
            return None
        try:
            data = json.loads(res.output)
        except ValueError:
            tail = (await self.env.run(f"tail -c 2000 {d}/out 2>/dev/null", timeout=30, cwd="/")).output
            return JobOutcome("unknown", {}, 0.0, f"runner produced no result: {tail[-800:]}")
        err = data.get("error") or ""
        return JobOutcome("finished", dict(data.get("tests") or {}), float(data.get("sec") or 0.0), err)

    async def cancel(self, job_id: str) -> None:
        d = shlex.quote(self.job_dir(job_id))
        # 先 TERM：runner 收到后杀掉测试进程并恢复工作区；再 KILL 兜底
        await self.env.run(f"[ -f {d}/pid ] && kill -TERM -$(cat {d}/pid) 2>/dev/null; "
                           f"for i in $(seq 30); do [ -f {d}/done ] && break; sleep 0.5; done; "
                           f"[ -f {d}/pid ] && kill -9 -$(cat {d}/pid) 2>/dev/null; true", timeout=60, cwd="/")

    async def _must(self, cmd: str) -> None:
        res = await self.env.run(cmd, timeout=60, cwd="/")
        if res.return_code != 0:
            raise RuntimeError(f"verifier command failed rc={res.return_code}: {cmd[:200]}\n{res.output[-800:]}")

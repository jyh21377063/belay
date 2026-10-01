"""验证器：在任务容器里以独立进程组运行检查作业，写完成标记；宿主机分段等待标记出现。

每个作业一个目录（jobs_dir/<作业 id>/）：
  spec.json    runner.py 的输入        pid   进程组 id（取消时 kill 整个进程组；重启后按它重新接上）
  out / log    包装层输出 / 测试输出    rc / done   退出码与完成标记（崩溃恢复时按它对账）
  result.json  runner.py 的结构化结果

模块 A：所有非 live 的作业（基线、存档、确认、证据、定位、提升）不再切换 worker 的工作区，而是在验证槽位
（verify_dir/<槽位>/）里导出候选树后运行。槽位由一个可抢占的优先级队列分配：第 1、2 档作业到达而槽位被第 3、4 档
占着时，取消低档作业并重新排队（记为 preempted，不算丢失）。

降级模式（导入隔离无效）下回到切换工作区的方式（where=workspace）：这些作业独占工作区，worker 的写类工具在
workspace_lock 上共享等待；live 的开发检查与之互斥。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from belay.core.model import WHERE_LIVE, WHERE_SLOT, WHERE_WORKSPACE, Job
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

    def mentions(self, path: str) -> list[str]:
        """命令里写死了工作区绝对路径的地方（这些命令在槽位里仍会读写工作区）。"""
        p = path.rstrip("/")
        pat = re.compile(rf"(^|[\s'\"=:]){re.escape(p)}(/|\b|$)")
        out = []
        for name, text in [("test_cmd", self.test_cmd or ""), ("prelude", self.prelude)] + \
                [(f"commands[{i}]", c) for i, c in enumerate(self.commands)] + \
                [(f"public_checks.{k}", v) for k, v in sorted(self.public_checks.items())]:
            if pat.search(text):
                out.append(name)
        return out


@dataclass
class JobOutcome:
    state: str                  # finished | unknown | cancelled | preempted
    results: dict
    sec: float
    error: str = ""
    reasons: dict = field(default_factory=dict)


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


@dataclass
class Slot:
    name: str
    dir: str
    index: str
    job: Optional[str] = None
    priority: int = 9
    preempting: bool = False


@dataclass
class _Waiter:
    priority: int
    order: int
    job: str


class RunnerVerifier:
    def __init__(self, env: Env, spec: VerifierSpec, workspace: str, git_dir: str, jobs_dir: str,
                 verify_dir: Optional[str] = None, slots: int = 1, runner_path: Optional[str] = None,
                 poll_sec: float = 0.5, nice: int = 10, cpu_limit: int = 0):
        self.env = env
        self.spec = spec
        self.workspace = workspace
        self.git_dir = git_dir
        self.jobs_dir = jobs_dir
        self.verify_dir = verify_dir or posixpath.join(posixpath.dirname(jobs_dir.rstrip("/")), "verify")
        self.runner_path = runner_path or posixpath.join(jobs_dir, "runner.py")
        self.poll_sec = poll_sec
        self.nice = nice
        self.cpu_limit = cpu_limit
        self.slots = [Slot(f"s{i}", posixpath.join(self.verify_dir, f"s{i}"),
                           posixpath.join(self.verify_dir, f"s{i}.index")) for i in range(max(1, slots))]
        self.pythonpath_rel: list[str] = []          # 工作区 sys.path 中位于工作区之下的条目（相对路径，按原顺序）
        self.workspace_lock = RWLock()               # 降级模式：切换工作区的作业独占；worker 的写类工具共享
        self.seed_index: Optional[str] = None        # 列出工作区被忽略文件时用的索引（worker 的快照索引）
        self.priority_of: Callable[[Job], int] = lambda job: 2
        self.on_preempt: Optional[Callable[[str], Awaitable[None]]] = None
        self._cond = asyncio.Condition()
        self._queue: list[_Waiter] = []
        self._order = itertools.count()
        self._uploaded = False

    async def setup(self) -> None:
        if self._uploaded:
            return
        await self._must(f"mkdir -p {shlex.quote(self.jobs_dir)} {shlex.quote(self.verify_dir)}")
        await self.env.write_text(self.runner_path, RUNNER_SOURCE.read_text(encoding="utf-8"))
        self._uploaded = True

    def job_dir(self, job_id: str) -> str:
        return posixpath.join(self.jobs_dir, job_id)

    # ---------------------------------------------------------------- 规格
    def slot_pythonpath(self, slot: Slot) -> list[str]:
        out = []
        for rel in self.pythonpath_rel:
            p = slot.dir if rel in ("", ".") else posixpath.join(slot.dir, rel)
            if p not in out:
                out.append(p)
        return out

    def runner_spec(self, job: Job, slot: Optional[Slot] = None, priority: int = 2) -> dict:
        sel = job.selection
        tests = [] if sel is None else [u for u in sel if not is_cmd(u)]
        checks = [{"id": f"cmd:{k}", "command": v} for k, v in sorted(self.spec.public_checks.items())
                  if sel is None or f"cmd:{k}" in sel]
        spec = {"workspace": self.workspace, "prelude": self.spec.prelude, "commands": self.spec.commands,
                "test_cmd": self.spec.test_cmd, "parser": self.spec.parser, "select": tests,
                "timeout": self.spec.timeout_sec, "checks": checks,
                "skip_tests": sel is not None and not tests}
        where = WHERE_LIVE if job.live else job.where
        if where == WHERE_SLOT and slot is not None:
            spec.update(tree=job.tree, git_dir=self.git_dir, slot=slot.dir, slot_index=slot.index,
                        seed_from=self.workspace, seed_marker=slot.dir + ".seeded", seed_index=self.seed_index,
                        pythonpath_entries=self.slot_pythonpath(slot), slot_name=slot.name)
        elif where == WHERE_WORKSPACE:
            spec.update(tree=job.tree, git_dir=self.git_dir, index=posixpath.join(self.job_dir(job.id), "index"))
        if priority >= 3:
            spec["nice"] = self.nice
            if self.cpu_limit > 0:
                n = str(self.cpu_limit)
                spec["env"] = {"PYTEST_XDIST_AUTO_NUM_WORKERS": n, "OMP_NUM_THREADS": n, "MAKEFLAGS": f"-j{n}",
                               "CARGO_BUILD_JOBS": n}
        return spec

    # ---------------------------------------------------------------- 运行
    async def run(self, job: Job) -> JobOutcome:
        await self.setup()
        where = WHERE_LIVE if job.live else job.where
        if where == WHERE_SLOT:
            return await self._run_in_slot(job)
        exclusive = where == WHERE_WORKSPACE
        await self.workspace_lock.acquire(exclusive)
        try:
            t0 = time.time()
            await self.launch(job, self.runner_spec(job))
            return await self.wait(job.id, t0)
        finally:
            await self.workspace_lock.release(exclusive)

    async def _run_in_slot(self, job: Job) -> JobOutcome:
        while True:
            prio = self.priority_of(job)
            slot = await self._acquire(job.id, prio)
            try:
                t0 = time.time()
                await self.launch(job, self.runner_spec(job, slot, prio))
                out = await self.wait(job.id, t0)
                preempted = slot.preempting
            finally:
                await self._release(slot)
            if not preempted:
                return out
            if self.on_preempt is not None:
                try:
                    await self.on_preempt(job.id)
                except Exception:
                    pass

    async def _acquire(self, job_id: str, prio: int) -> Slot:
        w = _Waiter(prio, next(self._order), job_id)
        async with self._cond:
            self._queue.append(w)
            try:
                while True:
                    self._queue.sort(key=lambda x: (x.priority, x.order))
                    free = [s for s in self.slots if s.job is None]
                    if free and self._queue[0] is w:
                        self._queue.remove(w)
                        slot = free[0]
                        slot.job, slot.priority, slot.preempting = job_id, prio, False
                        return slot
                    if not free and prio <= 2:
                        victims = [s for s in self.slots if s.job and s.priority >= 3 and not s.preempting]
                        if victims and not any(s.preempting for s in self.slots):
                            v = max(victims, key=lambda s: s.priority)
                            v.preempting = True
                            asyncio.get_running_loop().create_task(self._terminate(v.job))
                    await self._cond.wait()
            except BaseException:
                if w in self._queue:
                    self._queue.remove(w)
                self._cond.notify_all()
                raise

    async def _release(self, slot: Slot) -> None:
        async with self._cond:
            slot.job, slot.priority = None, 9
            self._cond.notify_all()

    def slot_of(self, job_id: str) -> Optional[Slot]:
        return next((s for s in self.slots if s.job == job_id), None)

    async def launch(self, job: Job, spec: dict) -> None:
        d = shlex.quote(self.job_dir(job.id))
        await self._must(f"rm -rf {d} && mkdir -p {d}")
        await self.env.write_text(f"{self.job_dir(job.id)}/spec.json", json.dumps(spec))
        inner = f"python3 {shlex.quote(self.runner_path)} run {d}/spec.json {d}"
        # --foreground：timeout 不另起进程组，取消时 kill -TERM -<进程组> 才能到达 runner（它负责杀掉测试进程组）
        wrapped = f"timeout --foreground -k 10 {int(self.spec.timeout_sec) + 120} bash -c {shlex.quote(inner)}"
        # 进程组 id 由作业自己写（$$ 就是 setsid 之后的会话首进程）；外层 shell 用 trap 挡住 TERM，保证写完成标记。
        # 启动命令先把自己的输出换成 /dev/null，否则后台的子 shell 会一直占着 exec 的输出管道，
        # launch 要等作业跑完才返回（重启后也就无从“重新接上”）
        script = f"trap 'true' TERM; echo $$ > {d}/pid.tmp && mv {d}/pid.tmp {d}/pid; {wrapped} > {d}/out 2>&1; " \
                 f"echo $? > {d}/rc; touch {d}/done"
        await self._must(f"exec >/dev/null 2>&1 </dev/null; setsid nohup bash -c {shlex.quote(script)} &")
        await self._must(f"for i in $(seq 100); do [ -f {d}/pid ] && exit 0; sleep 0.05; done; exit 1")

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
        reasons = {k: str(v)[:400] for k, v in list((data.get("reasons") or {}).items())[:50]}
        return JobOutcome("finished", dict(data.get("tests") or {}), float(data.get("sec") or 0.0), err, reasons)

    async def _terminate(self, job_id: str) -> None:
        d = shlex.quote(self.job_dir(job_id))
        # 先 TERM：runner 收到后杀掉测试进程（并恢复切换过的工作区）；再 KILL 兜底
        await self.env.run(f"[ -f {d}/pid ] && kill -TERM -$(cat {d}/pid) 2>/dev/null; "
                           f"for i in $(seq 30); do [ -f {d}/done ] && break; sleep 0.5; done; "
                           f"[ -f {d}/done ] || {{ [ -f {d}/pid ] && kill -9 -$(cat {d}/pid) 2>/dev/null; "
                           f"echo 137 > {d}/rc; touch {d}/done; }}; true", timeout=60, cwd="/")

    async def cancel(self, job_id: str) -> str:
        """还在排队的作业返回 "queued"（调用方取消等待它的协程即可）；在跑的发 TERM，返回 "terminated"。"""
        async with self._cond:
            if any(w.job == job_id for w in self._queue):
                return "queued"
        await self._terminate(job_id)
        return "terminated"

    # ---------------------------------------------------------------- 重启后重新接上（G5）
    async def alive(self, job_id: str) -> bool:
        d = shlex.quote(self.job_dir(job_id))
        res = await self.env.run(f"[ -f {d}/done ] && exit 3; [ -f {d}/pid ] && kill -0 -$(cat {d}/pid) 2>/dev/null",
                                 timeout=30, cwd="/")
        return res.return_code == 0

    async def reattach(self, job: Job) -> JobOutcome:
        """进程组还活着的作业：占住它的槽位，继续等完成标记。"""
        await self.setup()
        slot = None
        if job.where == WHERE_SLOT:
            res = await self.env.run(f"cat {shlex.quote(self.job_dir(job.id))}/spec.json", timeout=30, cwd="/")
            try:
                name = json.loads(res.output).get("slot_name")
            except ValueError:
                name = None
            async with self._cond:
                slot = next((s for s in self.slots if s.name == name and s.job is None), None)
                if slot is not None:
                    slot.job, slot.priority, slot.preempting = job.id, self.priority_of(job), False
        try:
            return await self.wait(job.id, time.time())
        finally:
            if slot is not None:
                await self._release(slot)

    # ---------------------------------------------------------------- 导入隔离（基线阶段）
    async def probe_sys_path(self) -> Optional[list[str]]:
        await self.setup()
        path = posixpath.join(self.jobs_dir, "probe.json")
        spec = {"workspace": self.workspace, "prelude": self.spec.prelude, "commands": self.spec.commands}
        await self.env.write_text(path, json.dumps(spec))
        res = await self.env.run(f"python3 {shlex.quote(self.runner_path)} probe {shlex.quote(path)}", timeout=300,
                                 cwd="/")
        try:
            return json.loads(res.output.strip().splitlines()[-1]).get("sys_path")
        except (ValueError, IndexError):
            return None

    def map_sys_path(self, sys_path: Optional[list[str]]) -> list[str]:
        """sys.path 中位于工作区之下的条目 → 相对路径（按原顺序）；再补上根目录与 src/（若有）。"""
        ws = self.workspace.rstrip("/")
        out: list[str] = []
        for e in sys_path or []:
            if not e:
                continue
            e = e.rstrip("/")
            if e == ws or e.startswith(ws + "/"):
                rel = posixpath.relpath(e, ws)
                if rel not in out:
                    out.append(rel)
        for extra in (".", "src"):
            if extra not in out:
                out.append(extra)
        return out

    async def isolation_probe(self, candidates: list[str], tree: str) -> dict:
        """在第一个槽位里跑破坏探针（先把 tree 导出到槽位）。"""
        await self.setup()
        slot = self.slots[0]
        async with self._cond:
            await self._cond.wait_for(lambda: slot.job is None)
            slot.job, slot.priority = "isolation-probe", 1
        try:
            return await self._isolation_probe(slot, candidates, tree)
        finally:
            await self._release(slot)

    async def _isolation_probe(self, slot: Slot, candidates: list[str], tree: str) -> dict:
        d = posixpath.join(self.jobs_dir, "isolation")
        spec = {"workspace": slot.dir, "prelude": self.spec.prelude, "commands": self.spec.commands,
                "test_cmd": self.spec.test_cmd, "parser": self.spec.parser, "timeout": min(600, self.spec.timeout_sec),
                "pythonpath_entries": self.slot_pythonpath(slot), "candidates": candidates[:20],
                "tree": tree, "git_dir": self.git_dir, "slot": slot.dir, "slot_index": slot.index,
                "seed_from": self.workspace, "seed_marker": slot.dir + ".seeded", "seed_index": self.seed_index}
        await self._must(f"mkdir -p {shlex.quote(d)}")
        await self.env.write_text(f"{d}/spec.json", json.dumps(spec))
        res = await self.env.run(f"python3 {shlex.quote(self.runner_path)} isolation-probe {shlex.quote(d)}/spec.json "
                                 f"{shlex.quote(d)}", timeout=900, cwd="/")
        try:
            return json.loads(res.output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "inconclusive": False, "reason": f"probe failed: {res.output[-500:]}"}

    # ---------------------------------------------------------------- 失败日志（D1）
    async def read_log(self, job_id: str, max_bytes: int = 4_000_000) -> str:
        d = shlex.quote(self.job_dir(job_id))
        res = await self.env.run(f"tail -c {int(max_bytes)} {d}/log 2>/dev/null", timeout=60, cwd="/")
        return res.output if res.return_code == 0 else ""

    async def _must(self, cmd: str) -> None:
        res = await self.env.run(cmd, timeout=60, cwd="/")
        if res.return_code != 0:
            raise RuntimeError(f"verifier command failed rc={res.return_code}: {cmd[:200]}\n{res.output[-800:]}")


def extract_failure(log: str, test: str, max_chars: int = 8000) -> str:
    """从 pytest 输出里截出某个测试的 traceback 段落（FAILURES / ERRORS 下以 ___ name ___ 开头的一段）。"""
    name = test.split("::", 1)[1] if "::" in test else test
    candidates = {name, name.replace("::", "."), name.split("::")[-1]}
    header = re.compile(r"^_{3,} (.+?) _{3,}\s*$", re.M)
    heads = list(header.finditer(log))
    for i, m in enumerate(heads):
        title = m.group(1).strip()
        if title.startswith("ERROR at setup of ") or title.startswith("ERROR at teardown of "):
            title = title.split(" of ", 1)[1]
        if title in candidates or any(title.endswith(c) for c in candidates if c):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(log)
            stop = re.search(r"^={3,}", log[m.start():end], re.M)
            if stop and stop.start() > 0:
                end = m.start() + stop.start()
            seg = log[m.start():end].rstrip()
            return seg if len(seg) <= max_chars else seg[:max_chars // 2] + "\n[...]\n" + seg[-max_chars // 2:]
    lines = [ln for ln in log.splitlines() if test in ln or name in ln]
    return "\n".join(lines[-20:])

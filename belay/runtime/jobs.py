"""Job Runner：在容器内以独立进程组运行作业，写完成标记；宿主机分段等待标记出现。

每个作业一个目录（门禁与验证在 /opt/belay/jobs，只属于 root；开发检查在 /tmp/belay-jobs，属于 worker 用户）：
  spec.json   runner.py 的输入（pytest 作业）
  pid         进程组 id（setsid 启动，取消时 kill 整个进程组）
  out         包装层的输出；log 为测试输出
  rc / done   包装层写入的退出码与完成标记（崩溃恢复时按它对账）
  result.json runner.py 的结构化结果
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass

from belay.env import Env

POLL_ROUND_SEC = 120                 # 每次 exec 最多等这么久，然后再发起下一次（避免单次 exec 过长）


@dataclass
class JobSpec:
    id: str
    dir: str
    workspace: str
    timeout: int
    runner: str | None = None        # pytest 作业：runner.py 路径
    spec: dict | None = None         # pytest 作业：runner 的 SPEC
    command: str | None = None       # 命令作业


@dataclass
class JobOutcome:
    state: str                       # DONE | TIMEOUT | ERROR | CANCELLED
    result: dict
    sec: float
    log: str


class JobRunner:
    def __init__(self, env: Env):
        self.env = env

    async def launch(self, js: JobSpec) -> None:
        d = shlex.quote(js.dir)
        await self._must(f"rm -rf {d} && mkdir -p {d}")
        if js.spec is not None:
            await self.env.write_text(f"{js.dir}/spec.json", json.dumps(js.spec))
            inner = f"python3 {shlex.quote(js.runner)} pytest {d}/spec.json {d}"
        else:
            inner = f"cd {shlex.quote(js.workspace)} && ({js.command}) > {d}/log 2>&1"
        wrapped = f"timeout -k 10 {int(js.timeout) + 60} bash -c {shlex.quote(inner)}"
        script = f"{wrapped} > {d}/out 2>&1; echo $? > {d}/rc; touch {d}/done"
        await self._must(f"cd / && setsid nohup bash -c {shlex.quote(script)} > /dev/null 2>&1 < /dev/null & "
                         f"echo $! > {d}/pid")

    async def wait(self, js: JobSpec, t0: float) -> JobOutcome:
        d = shlex.quote(js.dir)
        n = POLL_ROUND_SEC // 2
        while True:
            res = await self.env.run(f"for i in $(seq {n}); do [ -f {d}/done ] && exit 0; sleep 2; done; exit 7",
                                     timeout=POLL_ROUND_SEC + 30, cwd="/")
            if res.return_code == 0:
                break
            if res.return_code != 7 and "exec failed" in res.output:
                await asyncio.sleep(5)               # exec 本身失败（连接问题）：稍后再试
        return await self.collect(js, time.time() - t0)

    async def collect(self, js: JobSpec, sec: float) -> JobOutcome:
        d = js.dir
        rc_res = await self.env.run(f"cat {shlex.quote(d)}/rc 2>/dev/null", timeout=30, cwd="/")
        rc = rc_res.output.strip()
        log = f"{d}/log"
        if js.spec is not None:
            res = await self.env.run(f"cat {shlex.quote(d)}/result.json 2>/dev/null", timeout=60, cwd="/")
            try:
                result = json.loads(res.output)
            except ValueError:
                tail = (await self.env.run(f"tail -c 2000 {shlex.quote(d)}/out", timeout=30, cwd="/")).output
                return JobOutcome("ERROR", {"status": "error", "error": f"runner produced no result (rc={rc}): "
                                                                          f"{tail}"}, sec, log)
            state = {"ok": "DONE", "timeout": "TIMEOUT"}.get(result.get("status"), "DONE")
            return JobOutcome(state, result, result.get("sec") or sec, log)
        tail = (await self.env.run(f"tail -c 4000 {shlex.quote(log)}", timeout=30, cwd="/")).output
        timed_out = rc in ("124", "137")
        return JobOutcome("TIMEOUT" if timed_out else "DONE",
                          {"status": "timeout" if timed_out else "ok", "rc": int(rc) if rc.lstrip("-").isdigit()
                           else None, "tail": tail}, sec, log)

    async def cancel(self, js: JobSpec) -> None:
        d = shlex.quote(js.dir)
        # 先 TERM：runner 收到后杀掉测试进程并恢复工作区（门禁可能临时改过测试文件）；再 KILL 兜底
        await self.env.run(f"[ -f {d}/pid ] && kill -TERM -$(cat {d}/pid) 2>/dev/null; "
                           f"for i in $(seq 15); do [ -f {d}/result.json ] && break; sleep 1; done; "
                           f"[ -f {d}/pid ] && kill -9 -$(cat {d}/pid) 2>/dev/null; true", timeout=60, cwd="/")

    async def _must(self, cmd: str) -> None:
        res = await self.env.run(cmd, timeout=60, cwd="/")
        if res.return_code != 0:
            raise RuntimeError(f"job command failed rc={res.return_code}: {cmd[:200]}\n{res.output[-800:]}")

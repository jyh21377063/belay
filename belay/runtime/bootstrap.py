"""setup 阶段（不计入 agent 预算，与 A-gate 相同）：在任务容器里准备 runtime 需要的一切。

  1. 上传 runner.py；建 root 专属的状态目录（0700）
  2. 影子仓库：给原始代码拍快照，得到基线提交；集成分支指向它
  3. 基线：在原始代码上跑两次测试配置里的全部测试，两次都通过的进入 guards，不一致的标 flaky
     （gate: off 时也跑：run_check 仍按基线归类结果，只是提交不过门禁）
  4. 原始代码副本（Test Author 读它、独立测试在它上面验证），并确认测试确实 import 到这份副本
  5. OS 层隔离：建低权限用户，工作区交给它；失败时退回 root 并记录原因

每一步失败都只关闭对应的机制并写进 notes，不让整个运行失败。
"""
from __future__ import annotations

import json
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from belay.config import RuntimeConfig, RuntimePaths
from belay.env import Env
from belay.graph.evidence import baseline_status
from belay.runtime.gitops import ShadowRepo

RUNNER_SRC = Path(__file__).resolve().parents[1] / "container" / "runner.py"


@dataclass
class SetupInfo:
    workspace: str
    base_commit: str = ""
    base_tree: str = ""
    baseline: dict[str, str] = field(default_factory=dict)
    full_gate_sec: float = 0.0
    test_files: list[str] = field(default_factory=list)
    gate_available: bool = False
    test_author_available: bool = False
    isolation: bool = False
    agent_user: str | None = None
    notes: list[str] = field(default_factory=list)
    sec: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "SetupInfo":
        return cls(**json.loads(text))


def _path_tokens(test_cmd: str) -> list[str]:
    out = []
    for tok in shlex.split(test_cmd):
        if not tok.startswith("-") and ("/" in tok or tok.split("::")[0].endswith(".py")):
            out.append(tok)
    return out


def pytest_spec(spec: dict, workspace: str, **kw) -> dict:
    """runner.py 的 SPEC：测试配置（gate.json）+ 本次运行的参数。"""
    return {"workspace": workspace, "prelude": spec.get("prelude") or "", "commands": spec.get("commands") or [],
            "test_cmd": spec["test_cmd"], "parser": spec.get("parser") or "parse_log_pytest",
            "timeout": int(spec.get("timeout_sec") or 3600), **kw}


async def _must(env: Env, cmd: str, timeout: float = 120) -> str:
    res = await env.run(cmd, timeout=timeout, cwd="/")
    if res.return_code != 0:
        raise RuntimeError(f"rc={res.return_code}: {cmd[:200]}\n{res.output[-1000:]}")
    return res.output


async def run_pytest_once(env: Env, paths: RuntimePaths, spec: dict, out_dir: str, timeout: float) -> dict:
    await env.write_text(f"{out_dir}/spec.json", json.dumps(spec))
    await env.run(f"python3 {shlex.quote(paths.runner)} pytest {shlex.quote(out_dir)}/spec.json "
                  f"{shlex.quote(out_dir)} > {shlex.quote(out_dir)}/out 2>&1", timeout=timeout, cwd="/")
    res = await env.run(f"cat {shlex.quote(out_dir)}/result.json", timeout=60, cwd="/")
    try:
        return json.loads(res.output)
    except ValueError:
        tail = (await env.run(f"tail -c 1500 {shlex.quote(out_dir)}/out", timeout=30, cwd="/")).output
        return {"status": "error", "error": f"runner produced no result: {tail}", "tests": {}}


async def setup_container(root: Env, workspace: str, spec: dict | None, cfg: RuntimeConfig, paths: RuntimePaths,
                          log=print) -> SetupInfo:
    t0 = time.time()
    info = SetupInfo(workspace=workspace)
    st, b = shlex.quote(paths.state), shlex.quote(paths.bin)
    await _must(root, f"mkdir -p {st}/jobs {st}/checks {st}/idx {b} && chmod 700 {st} && chmod 755 {b}")
    await root.write_text(paths.runner, RUNNER_SRC.read_text(encoding="utf-8"))
    await _must(root, f"chmod 755 {shlex.quote(paths.runner)}")
    if (await root.run("command -v python3 >/dev/null && command -v git >/dev/null", timeout=30)).return_code != 0:
        info.notes.append("python3 or git is missing in the container; the runtime cannot run checks")

    repo = ShadowRepo(root, paths)
    info.base_commit, info.base_tree = await repo.init(workspace)
    log(f"[belay] 影子仓库：基线提交 {info.base_commit[:10]}")

    # ---- 基线
    if spec:
        runs, secs, dropped = [], [], []
        for i in range(2):
            out = f"{paths.state}/baseline/{i + 1}"
            await _must(root, f"rm -rf {shlex.quote(out)} && mkdir -p {shlex.quote(out)}")
            timeout = int(spec.get("timeout_sec") or 3600) + 120
            res = await run_pytest_once(root, paths, pytest_spec(spec, workspace), out, timeout)
            runs.append(res.get("tests") or {})
            secs.append(float(res.get("sec") or 0))
            dropped = res.get("dropped") or dropped
            log(f"[belay] 基线第 {i + 1} 次：{res.get('status')}，{len(runs[-1])} 个测试，{secs[-1]:.0f}s")
            if res.get("status") != "ok":
                info.notes.append(f"baseline run {i + 1}: {res.get('status')} {str(res.get('error'))[:300]}")
        await repo.checkout(workspace, "base-restore", info.base_tree)   # 测试可能在工作区里留下文件
        if any(runs):
            info.baseline = baseline_status(runs)
            info.full_gate_sec = max(secs)
            info.test_files = [t for t in _path_tokens(spec["test_cmd"]) if t not in dropped]
            info.gate_available = True
        else:
            info.notes.append("the baseline produced no test results; the gate is disabled")
    else:
        info.notes.append("no test configuration for this task; submissions are merged without a gate")

    # ---- 原始代码副本（Test Author）
    if cfg.test_author and info.gate_available:
        try:
            o = shlex.quote(paths.orig)
            await _must(root, f"rm -rf {o} && mkdir -p {o} && cd {shlex.quote(workspace)} && "
                              f"tar --exclude=./.git -cf - . | tar -xf - -C {o}", timeout=900)
            probe_spec = pytest_spec(spec, paths.orig, pythonpath=True)
            await root.write_text(f"{paths.state}/probe.json", json.dumps(probe_spec))
            out = await _must(root, f"python3 {shlex.quote(paths.runner)} probe {shlex.quote(paths.state)}/probe.json",
                              timeout=300)
            sys_path = json.loads(out.strip().splitlines()[-1]).get("sys_path") or []
            norm = [p.rstrip("/") for p in sys_path]
            orig_i = next((i for i, p in enumerate(norm) if p == paths.orig), None)
            ws_i = next((i for i, p in enumerate(norm) if p == workspace.rstrip("/")), None)
            if orig_i is not None and (ws_i is None or orig_i < ws_i):
                info.test_author_available = True
            else:
                info.notes.append(f"imports in the original-code copy would resolve to {workspace} first "
                                  f"(sys.path={norm[:6]}); independent tests are disabled")
        except Exception as e:                      # noqa: BLE001 — 只关闭这个机制
            info.notes.append(f"original-code copy failed: {e}"[:400])

    # ---- OS 层隔离
    if cfg.isolation:
        u = shlex.quote(cfg.agent_user)
        ws = shlex.quote(workspace)
        dj = shlex.quote(paths.dev_jobs)
        res = await root.run(
            f"(id -u {u} >/dev/null 2>&1 || useradd -m -s /bin/bash {u} >/dev/null 2>&1 || "
            f"adduser -D -s /bin/bash {u} >/dev/null 2>&1) && id -u {u} >/dev/null && "
            f"chown -R {u}:{u} {ws} && mkdir -p {dj} && chown {u}:{u} {dj} && chmod 700 {shlex.quote(paths.state)} && "
            f"{{ [ -d /logs/verifier ] && chmod 700 /logs/verifier; true; }}", timeout=900, cwd="/")
        if res.return_code == 0:
            info.isolation, info.agent_user = True, cfg.agent_user
        else:
            info.notes.append(f"could not create the agent user; running the worker as root: {res.output[-300:]}")
    if not info.isolation:
        await root.run(f"mkdir -p {shlex.quote(paths.dev_jobs)}", timeout=30, cwd="/")

    info.sec = round(time.time() - t0, 1)
    await root.write_text(f"{paths.state}/setup.json", info.to_json())
    return info


async def verify_agent_user(agent_env: Env, workspace: str, paths: RuntimePaths) -> str | None:
    """以 worker 用户试一下：能在工作区写文件、能跑 python3、读不到 runtime 的状态。返回失败原因或 None。"""
    probe = shlex.quote(f"{workspace}/.belay_probe")
    setup = shlex.quote(f"{paths.state}/setup.json")
    res = await agent_env.run(f"touch {probe} && rm -f {probe} && python3 -c 'print(1)' >/dev/null && "
                              f"{{ cat {setup} >/dev/null 2>&1 && echo LEAK || echo OK; }}", timeout=60)
    if res.return_code != 0 or "OK" not in res.output:
        return f"agent user check failed: {res.output[-300:]}"
    return None

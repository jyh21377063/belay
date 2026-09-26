"""和 Pier 打交道的唯一模块：生成 job 配置 → 调 `pier run -c` → 解析 trial 结果。

每次调用只跑 1 个 task × 1 次，这样结果目录、断点续跑、超时控制都由我们自己掌握；
并发由 runner 的线程池负责。将来换成 Harbor 或自己的容器管理，只需要替换这个文件。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from eval.config import PROJECT_ROOT

REPLAY_AGENT = "eval.agents.replay:PatchReplayAgent"


def pier_command() -> list[str]:
    """优先用与当前解释器同一个 venv 里的 pier。

    自定义 agent 通过 import_path 加载，Pier 必须能 import 到本项目的 eval 包，
    所以 Pier 要装在项目自己的 venv 里，而不是 `uv tool install` 的隔离环境。
    """
    local = Path(sys.executable).parent / "pier"
    if local.exists():
        return [str(local)]
    found = shutil.which("pier")
    if not found:
        raise RuntimeError("找不到 pier，请在项目 venv 中 `pip install datacurve-pier`")
    return [found]


def agent_config(agent: dict, timeout_min: int | None, extra_kwargs: dict | None = None) -> dict:
    """runs.yaml 的 agent 定义 → Pier 的 AgentConfig（pier.models.trial.config）。"""
    cfg: dict = {}
    if agent.get("import_path"):
        cfg["import_path"] = agent["import_path"]
    else:
        cfg["name"] = agent["name"]
    if agent.get("model"):
        cfg["model_name"] = agent["model"]
    if agent.get("env"):
        cfg["env"] = dict(agent["env"])       # ${VAR} 原样保留，Pier 运行时展开，密钥不落盘
    kwargs = {**(agent.get("kwargs") or {}), **(extra_kwargs or {})}
    if kwargs:
        cfg["kwargs"] = kwargs
    if timeout_min:
        cfg["override_timeout_sec"] = timeout_min * 60
    if agent.get("setup_timeout_min"):      # A-gate 在 setup 阶段跑两次基线测试
        cfg["override_setup_timeout_sec"] = agent["setup_timeout_min"] * 60
    return cfg


def job_config(*, job_name: str, jobs_dir: Path, task_dir: Path, agent_cfg: dict,
               environment: str, keep_container: bool, verify: bool) -> dict:
    """Pier 的 JobConfig（pier.models.job.config）。"""
    return {
        "job_name": job_name,
        "jobs_dir": str(jobs_dir),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "quiet": True,
        "environment": {"type": environment, "delete": not keep_container},
        "verifier": {"disable": not verify},
        "agents": [agent_cfg],
        "tasks": [{"path": str(task_dir)}],
    }


@dataclass
class PierOutcome:
    ok: bool                    # 是否产出了 trial 级 result.json
    returncode: int
    trial_dir: Path | None
    result: dict | None
    log_path: Path


def find_trial_dir(job_dir: Path) -> Path | None:
    hits = sorted(p.parent for p in job_dir.glob("*/result.json"))
    return hits[-1] if hits else None


def run_job(cfg: dict, cfg_path: Path, log_path: Path, timeout_sec: int) -> PierOutcome:
    job_dir = Path(cfg["jobs_dir"]) / cfg["job_name"]
    if job_dir.exists():                      # 上次中断留下的半成品：整体重跑
        shutil.rmtree(job_dir)
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PROJECT_ROOT), env.get("PYTHONPATH")]))
    cmd = [*pier_command(), "run", "-c", str(cfg_path), "-y"]
    with open(log_path, "w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        try:
            rc = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout_sec).returncode
        except subprocess.TimeoutExpired:
            rc = -9
            log.write(f"\n[eval] pier 超过 {timeout_sec}s 未结束，已终止；请用 docker ps 检查残留容器\n")

    trial = find_trial_dir(job_dir)
    result = json.loads((trial / "result.json").read_text()) if trial else None
    return PierOutcome(result is not None, rc, trial, result, log_path)


# ---------------------------------------------------------------- 解析 trial 结果
def _secs(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return round((dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds(), 1)


def summarize_result(r: dict) -> dict:
    """从 Pier 的 TrialResult 中取出我们关心的字段。"""
    ar = r.get("agent_result") or {}
    ae = r.get("agent_execution") or {}
    exc = r.get("exception_info") or {}
    return {
        "agent_version": (r.get("agent_info") or {}).get("version"),
        "agent_sec": _secs(ae.get("started_at"), ae.get("finished_at")),
        "total_sec": _secs(r.get("started_at"), r.get("finished_at")),
        "n_input_tokens": ar.get("n_input_tokens"),
        "n_cache_tokens": ar.get("n_cache_tokens"),
        "n_output_tokens": ar.get("n_output_tokens"),
        "cost_usd": ar.get("cost_usd"),
        "n_agent_steps": ar.get("n_agent_steps") or r.get("n_agent_steps"),
        "peak_context_tokens": ar.get("peak_context_tokens"),
        "summarization_count": ar.get("summarization_count"),
        "rewards": (r.get("verifier_result") or {}).get("rewards"),
        "exception": exc.get("exception_type"),
    }


def interpret_rewards(rewards: dict | None, threshold: float = 1.0) -> tuple[bool | None, float | None, float | None]:
    """返回 (resolved, fix_rate, score)。

    约定：verifier 写 reward.json / reward.txt，含 resolved 或 Harbor 默认的 reward，可选 fix_rate。
    score 为连续分数（LHTB 的 reward 在 0–1 之间）；resolved = score ≥ threshold（LHTB 取 0.95）。
    """
    if not rewards:
        return None, None, None
    raw = rewards.get("resolved", rewards.get("reward"))
    score = None if rewards.get("reward") is None else float(rewards["reward"])
    fix = rewards.get("fix_rate")
    return (None if raw is None else float(raw) >= threshold), (None if fix is None else float(fix)), score

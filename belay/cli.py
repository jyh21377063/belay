"""本地调试入口：不经过 Pier，直接对一个目录或一个保留下来的容器运行。

  python -m belay.cli run    --workdir /path/to/repo --task-file task.md [--gate gate.json] [--run-dir runs/x]
  python -m belay.cli run    --docker <容器> --workdir /testbed --task-file task.md --gate gate.json --record rec.jsonl
  python -m belay.cli resume --run-dir runs/x                 # runtime 崩溃后：重放事件 → 对账 → 继续
  python -m belay.cli resume --run-dir runs/x --rebuild [--docker <新容器>]
                                                              # 容器 / 工作区 / 影子仓库丢了：从 git bundle 重建
  python -m belay.cli ledger --run-dir runs/x                 # 从事件库重放出账本
  python -m belay.cli handoff --run-dir runs/x                # 随时导出交接上下文：新会话开场会看到的内容（账本 + todo
                                                              # + 交接摘要 + 待处理的问题），可以直接交给下一个会话
  python -m belay.cli flat   --workdir /path/to/repo --task-file task.md   # B 组：同一个 worker，不用图

模型配置从环境变量读取：DEEPSEEK_API_KEY（或 ANTHROPIC_API_KEY）、ANTHROPIC_BASE_URL、BELAY_MODEL；
复核者与诊断者可以单独指定模型（--aux-model / BELAY_AUX_MODEL，默认与 worker 相同）。
--gate 兼容 eval 的 gate.json（test_cmd / parser / prelude / commands / timeout_sec，可加 public_checks）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path


def _llm(args, record: str | None, model: str | None = None):
    from belay.llm import LLM, ReplayLLM
    if getattr(args, "replay", None):
        return ReplayLLM(args.replay)
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("需要设置 DEEPSEEK_API_KEY")
    return LLM(model or args.model, key,
               base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"),
               effort=args.effort or None, thinking=not args.no_thinking, record_path=record, log=print)


def _env(docker: str | None, workdir: str):
    from belay.env import DockerEnv, LocalEnv
    return DockerEnv(docker, workdir) if docker else LocalEnv(workdir)


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--model", default=os.environ.get("BELAY_MODEL", "deepseek-flash"))
    ap.add_argument("--effort", default="max")
    ap.add_argument("--no-thinking", action="store_true")
    ap.add_argument("--record", help="把模型回复录制到该文件")
    ap.add_argument("--replay", help="回放录制的模型回复，不调用模型")
    ap.add_argument("--aux-model", default=os.environ.get("BELAY_AUX_MODEL"),
                    help="复核者与诊断者用的模型（默认与 worker 相同）")


def _run_belay(args, resume: bool, rebuild: bool = False) -> None:
    from belay.core.config import BelayConfig
    from belay.runtime.driver import BelayRun, RunSettings
    from belay.runtime.verifier import VerifierSpec

    run_dir = Path(args.run_dir)
    saved = run_dir / "cli.json"
    if resume:
        conf = json.loads(saved.read_text())
        if rebuild and getattr(args, "docker", None):          # 在新容器上重建
            conf["docker"] = args.docker
            saved.write_text(json.dumps(conf, indent=1))
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        state = "/opt/belay" if args.docker else str((run_dir / "state").resolve())
        conf = {"workdir": args.workdir, "docker": args.docker, "budget_sec": args.budget_min * 60,
                "git_dir": f"{state}/git", "jobs_dir": f"{state}/jobs", "verify_dir": f"{state}/verify",
                "gate": json.loads(Path(args.gate).read_text()) if args.gate else {},
                "config": json.loads(Path(args.config).read_text()) if args.config else {}}
        saved.write_text(json.dumps(conf, indent=1))
    env = _env(conf["docker"], conf["workdir"])
    record = args.record or str(run_dir / "llm_record.jsonl")
    settings = RunSettings(run_dir=str(run_dir), budget_sec=conf["budget_sec"], git_dir=conf["git_dir"],
                           jobs_dir=conf["jobs_dir"], verify_dir=conf.get("verify_dir"))
    llm = _llm(args, record)
    aux = _llm(args, None, args.aux_model) if args.aux_model and not getattr(args, "replay", None) else None
    run = BelayRun(llm, env, settings, BelayConfig.from_dict(conf["config"]),
                   VerifierSpec.from_dict(conf["gate"]), aux_llm=aux, log=print)
    if resume:
        res = asyncio.run(run.resume(rebuild=rebuild))
    else:
        task = Path(args.task_file).read_text() if args.task_file else args.task
        if not task:
            raise SystemExit("需要 --task 或 --task-file")
        res = asyncio.run(run.start(task, run_id=run_dir.name))
    print(f"\nstatus={res.status} delivered merge point={res.checkpoint}\nledger: {run_dir / 'ledger.md'}\n"
          f"deliverable: {run_dir / 'deliverable.diff'}")


def _ledger(args) -> None:
    from belay.core.reduce import replay
    from belay.core.render import ledger_markdown
    from belay.runtime.store import EventStore
    store = EventStore(args.run_dir)
    print(ledger_markdown(replay(store.events())))


def _handoff(args) -> None:
    """不需要容器：只从事件库重放出图，生成新会话开场会看到的上下文（不含需要 git 的改动 diff）。"""
    from belay.core.config import BelayConfig
    from belay.core.context import build_context
    from belay.core.reduce import replay
    from belay.runtime.store import EventStore
    store = EventStore(args.run_dir)
    try:
        g = replay(store.events())
    finally:
        store.close()
    cfg = BelayConfig()
    worker = args.worker or (next(iter(g.workers)) if g.workers else "w1")
    ctx = build_context(g, worker, cfg.opening_budget_tokens, time.time(), cfg, mode="resume")
    print(ctx.text)


def _flat(args) -> None:
    from belay.tools import Policy
    from belay.worker import Worker, WorkerConfig
    from belay.worker.transcript import Transcript
    task = Path(args.task_file).read_text() if args.task_file else args.task
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    worker = Worker(_llm(args, args.record), _env(args.docker, args.workdir),
                    config=WorkerConfig(deadline=time.monotonic() + args.budget_min * 60),
                    policy=Policy.strict() if args.strict else Policy(), transcript=Transcript(out / "transcript.jsonl"),
                    on_progress=lambda w: print(f"[turn {w.turns}] context={w.last_context}", flush=True))
    result = asyncio.run(worker.run(task))
    print(f"\nstatus={result.status} turns={result.turns} resets={result.resets} peak_context={result.peak_context}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Belay 本地调试入口")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="运行 Belay（runtime + worker）")
    run.add_argument("--workdir", required=True)
    run.add_argument("--docker")
    run.add_argument("--task")
    run.add_argument("--task-file")
    run.add_argument("--gate", help="检查配置（gate.json）")
    run.add_argument("--config", help="BelayConfig 的 JSON")
    run.add_argument("--run-dir", default="belay-run")
    run.add_argument("--budget-min", type=float, default=90)
    _common(run)
    res = sub.add_parser("resume", help="runtime 崩溃后继续")
    res.add_argument("--run-dir", required=True)
    res.add_argument("--rebuild", action="store_true", help="容器、工作区或影子仓库丢失：从宿主机上的 git bundle 重建")
    res.add_argument("--docker", help="重建时使用的新容器（从原始镜像启动）")
    _common(res)
    led = sub.add_parser("ledger", help="从事件库重放出账本")
    led.add_argument("--run-dir", required=True)
    hand = sub.add_parser("handoff", help="导出交接上下文（新会话的开场）")
    hand.add_argument("--run-dir", required=True)
    hand.add_argument("--worker")
    flat = sub.add_parser("flat", help="B 组：只有 worker，不用图")
    flat.add_argument("--workdir", required=True)
    flat.add_argument("--docker")
    flat.add_argument("--task")
    flat.add_argument("--task-file")
    flat.add_argument("--budget-min", type=float, default=90)
    flat.add_argument("--strict", action="store_true")
    flat.add_argument("--out", default="belay-flat-run")
    _common(flat)
    args = ap.parse_args()
    if args.cmd == "run":
        _run_belay(args, resume=False)
    elif args.cmd == "resume":
        _run_belay(args, resume=True, rebuild=args.rebuild)
    elif args.cmd == "ledger":
        _ledger(args)
    elif args.cmd == "handoff":
        _handoff(args)
    else:
        _flat(args)


if __name__ == "__main__":
    main()

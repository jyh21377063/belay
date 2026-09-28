"""本地调试入口：不经过 Pier，直接对一个目录或一个保留下来的容器运行 worker。

  python -m belay.cli --workdir /path/to/repo --task-file task.md
  python -m belay.cli --docker <容器> --workdir /testbed --task "..." --record run.jsonl
  python -m belay.cli --workdir /tmp/repo --task-file task.md --replay run.jsonl   # 回放，不调用模型

模型配置从环境变量读取：DEEPSEEK_API_KEY（或 ANTHROPIC_API_KEY）、ANTHROPIC_BASE_URL、BELAY_MODEL。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from belay.env import DockerEnv, LocalEnv
from belay.llm import LLM, ReplayLLM
from belay.tools import Policy
from belay.worker.transcript import Transcript
from belay.worker import Worker, WorkerConfig


def main() -> None:
    ap = argparse.ArgumentParser(description="运行单个 Belay worker（本地调试）")
    ap.add_argument("--workdir", required=True, help="仓库目录（--docker 时为容器内路径）")
    ap.add_argument("--docker", help="容器名或 ID；不填则在本机目录上执行")
    ap.add_argument("--task", help="任务文本")
    ap.add_argument("--task-file", help="任务文件")
    ap.add_argument("--model", default=os.environ.get("BELAY_MODEL", "deepseek-flash"))
    ap.add_argument("--effort", default="max")
    ap.add_argument("--no-thinking", action="store_true")
    ap.add_argument("--budget-min", type=float, default=90)
    ap.add_argument("--strict", action="store_true", help="使用 Belay 的严格行动边界")
    ap.add_argument("--record", help="把模型回复录制到该文件")
    ap.add_argument("--replay", help="回放录制的模型回复，不调用模型")
    ap.add_argument("--out", default="belay-local-run", help="轨迹输出目录")
    args = ap.parse_args()

    task = Path(args.task_file).read_text() if args.task_file else args.task
    if not task:
        ap.error("需要 --task 或 --task-file")
    env = DockerEnv(args.docker, args.workdir) if args.docker else LocalEnv(args.workdir)
    if args.replay:
        llm = ReplayLLM(args.replay)
    else:
        key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            ap.error("需要设置 DEEPSEEK_API_KEY")
        llm = LLM(args.model, key, base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"),
                  effort=args.effort or None, thinking=not args.no_thinking, record_path=args.record, log=print)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def progress(w: Worker) -> None:
        print(f"[turn {w.turns}] context={w.last_context} out_tokens={w.usage.output_tokens}", flush=True)

    worker = Worker(llm, env, config=WorkerConfig(deadline=time.monotonic() + args.budget_min * 60),
                    policy=Policy.strict() if args.strict else Policy(),
                    transcript=Transcript(out / "transcript.jsonl"), on_progress=progress)
    result = asyncio.run(worker.run(task))
    print(f"\nstatus={result.status} turns={result.turns} resets={result.resets} peak_context={result.peak_context}")
    print(f"summary: {result.summary}")


if __name__ == "__main__":
    main()

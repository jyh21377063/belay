"""统一入口。

  python -m eval.run --profile cc-pilot                                    # 调试集全部题
  python -m eval.run --profile cc-pilot --tasks koota-query-predicates    # 只跑一道
  python -m eval.run --profile cc-pilot --benchmarks deepswe --repeats 2
  python -m eval.run --profile cc-test                                     # 评测集，A 组
  python -m eval.run --profile flat-test --tasks promax/nasa__fprime-3642
  python -m eval.run --profile gold-check --split test                     # 验证评测集题目环境
  python -m eval.run --profile regrade --source-run cc-test-claude-code-test-20261001
  python -m eval.run --profile cc-pilot --dry-run                          # 只打印计划和 Pier 配置
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from eval import pier_backend as pb
from eval.config import DEFAULT_RUNS, ConfigError, RunPlan, build_plan, missing_env_vars


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python -m eval.run", description="长程任务评测统一入口")
    p.add_argument("--runs", default=str(DEFAULT_RUNS), help="运行编排文件（默认 belay/runs.yaml）")
    p.add_argument("--profile", required=True)
    p.add_argument("--split", choices=["test", "dev"])
    p.add_argument("--benchmarks", help="all 或逗号分隔，如 promax,deepswe")
    p.add_argument("--tasks", help="all 或逗号分隔；可写短 id 或 benchmark/id")
    p.add_argument("--agent", help="覆盖 profile 中的 agent（runs.yaml 的 agents 键名）")
    p.add_argument("--model", help="覆盖 agent 的模型名")
    p.add_argument("--repeats", type=int)
    p.add_argument("--concurrency", type=int)
    p.add_argument("--timeout-min", type=int, dest="timeout_min")
    p.add_argument("--run-id", dest="run_id", help="自定义 run_id；与已有 run_id 相同则断点续跑")
    p.add_argument("--source-run", dest="source_run", help="regrade 时读取补丁的 run_id")
    p.add_argument("--keep-containers", action="store_true", default=None, dest="keep_containers")
    p.add_argument("-y", "--yes", action="store_true", help="不询问，直接开始")
    p.add_argument("--dry-run", action="store_true", help="只打印计划和生成的 Pier 配置")
    return p.parse_args(argv)


def print_plan(plan: RunPlan) -> None:
    print(f"profile={plan.profile}  split={plan.split}  并发={plan.concurrency}  "
          f"单次上限={plan.timeout_min}min  保留容器={plan.keep_containers}")
    for s in plan.steps:
        who = s.agent.get("name") or s.agent.get("import_path")
        print(f"  step: agent={s.agent_key} ({who})  model={s.agent.get('model') or '-'}  "
              f"repeats={s.repeats}  评分={s.grade_mode}"
              + (f"  期望={s.expect}" if s.expect else "") + f"\n        run_id={s.run_id}")
    print(f"本次将运行的题目（{len(plan.tasks)} 道）：")
    for t in plan.tasks:
        print(f"  - {t.key:<60} {t.lang:<11} {t.repo}")


def main(argv=None) -> int:
    args = parse_args(argv)
    overrides = {k: getattr(args, k) for k in
                 ("split", "benchmarks", "tasks", "agent", "model", "repeats", "concurrency",
                  "timeout_min", "run_id", "source_run", "keep_containers")}
    try:
        plan = build_plan(args.runs, args.profile, overrides)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    print_plan(plan)

    missing = missing_env_vars(plan)
    if missing:
        print(f"\n缺少环境变量：{missing}（例如 export {missing[0]}=...）", file=sys.stderr)
        if not args.dry_run:
            return 2

    if args.dry_run:
        from eval.runner import preflight
        s, t = plan.steps[0], plan.tasks[0]
        cfg = pb.job_config(job_name="agent", jobs_dir=plan.trial_dir(s, t, 1) / "pier",
                            task_dir=plan.task_dir(t), agent_cfg=pb.agent_config(s.agent, plan.timeout_min),
                            environment=plan.environment, keep_container=plan.keep_containers,
                            verify=plan.inline_verify or s.grade_mode == "inline")
        print(f"\n第一个 trial 将生成的 Pier 配置：\n---\n{yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)}")
        for p in preflight(plan):
            print(f"[preflight] {p}")
        return 0

    from eval.runner import execute, preflight
    problems = preflight(plan)
    if problems:
        print("\n运行前检查未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    if not args.yes and input("\n确认开始？[y/N] ").strip().lower() != "y":
        print("已取消")
        return 1
    return 0 if execute(plan) else 1


if __name__ == "__main__":
    sys.exit(main())

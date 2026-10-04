"""给任意一份补丁单独评分：在全新容器里应用补丁，按题目自己的评分方式打分（SWE-EVO 的 F2P / P2P、LHTB 的官方评分都行）。
用来评某个合并点（POLISH 前后对比），不调用模型，不改 runs.yaml。

放在 belay/ 目录下运行：
  python grade_patch.py commit0-multilib-tdd /path/to/checkpoints/7.diff --run-id cp7-commit0
  python task_runs.py commit0-multilib-tdd --runs cp7-commit0,v10-improve-commit0

结果写到 <results_root>/<run_id>/<benchmark>/<题目>/1/（run.json、grade.json、patch.diff），task_runs.py 能直接读到。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from eval.config import DEFAULT_RUNS, ConfigError, build_plan
from eval.runner import grade_phase


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", help="题目 id（短 id，如 commit0-multilib-tdd）")
    ap.add_argument("patch", help="补丁文件（如 checkpoints/7.diff）")
    ap.add_argument("--run-id", required=True, help="结果目录名；task_runs.py 里显示为这一行的 run_id")
    ap.add_argument("--split", default="test", choices=["test", "dev"])
    a = ap.parse_args(argv)
    patch = Path(a.patch).resolve()
    if not patch.is_file():
        print(f"找不到补丁：{patch}", file=sys.stderr)
        return 2
    try:
        plan = build_plan(DEFAULT_RUNS, "belay-test", {"split": a.split, "tasks": a.task, "run_id": a.run_id})
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    t = plan.tasks[0]
    d = plan.results_root / a.run_id / t.benchmark / t.id / "1"
    if (d / "grade.json").exists():
        print(f"{d} 已经评过分；要重评先删掉这个目录", file=sys.stderr)
        return 1
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(patch, d / "patch.diff")
    (d / "run.json").write_text(json.dumps({"task": t.key, "agent": "patch", "source": str(patch),
                                            "status": "done", "grade_mode": "replay"},
                                           ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评分中：{t.key} ← {patch}（全新容器应用补丁后跑题目的 verifier，可能要几十分钟）")
    grade = grade_phase(plan, t, d, d / "patch.diff")
    (d / "grade.json").write_text(json.dumps(grade, ensure_ascii=False, indent=2), encoding="utf-8")
    if grade.get("status") != "done":
        print(f"评分失败：{grade.get('error')}", file=sys.stderr)
        return 1
    print(f"得分={grade.get('score')}  fix_rate={grade.get('fix_rate')}  补丁应用={grade.get('apply_method')}  → {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

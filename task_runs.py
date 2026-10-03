"""同一道题的所有运行横向对比：每次运行的分数、F2P / P2P 明细、用时，以及每个测试在各次运行里过了几次。

放在 belay/ 目录下运行（需要项目的 venv）：
  python task_runs.py conan-io__conan_2.0.2_2.0.3
  python task_runs.py conan-io__conan_2.0.2_2.0.3 --runs v9-conan,v8-conan   # 只看这几个 run_id
  python task_runs.py spot-scheduler-traces                                  # LHTB：只有连续得分

数据来源：<results_root>/<run_id>/<benchmark>/<题目>/<k>/ 下的 run.json、grade.json，以及评分容器的测试输出
（pier/grade/*/verifier/test_output.txt，由 eval.belay_report.grade_details 解析）。不改任何文件。
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from eval.belay_report import _root, grade_details


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fmt_min(sec) -> str:
    return f"{sec / 60:.0f}" if isinstance(sec, (int, float)) else "-"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", help="题目 id（短 id）")
    ap.add_argument("--runs", help="只看这些 run_id，逗号分隔")
    a = ap.parse_args(argv)
    root = _root("results_root")
    only = set(a.runs.split(",")) if a.runs else None
    trials = sorted(p for p in root.glob(f"*/*/{a.task}/*") if p.is_dir() and p.name.isdigit()
                    and (only is None or p.parts[-4] in only))
    if not trials:
        print(f"在 {root} 下没有找到 {a.task} 的运行")
        return 1

    rows, per_test, n_graded = [], defaultdict(lambda: ["", 0]), 0
    for t in trials:
        run_id, k = t.parts[-4], t.name
        run, grade = _json(t / "run.json"), _json(t / "grade.json")
        rw = grade.get("rewards") or run.get("rewards") or {}
        score = grade.get("score", run.get("inline_score"))
        fix = grade.get("fix_rate", run.get("inline_fix_rate"))
        gd = grade_details(t) if grade else None
        f2p = p2p_bad = "-"
        bad_names = []
        if gd and "error" not in gd:
            n_graded += 1
            f2p = f"{gd['n_f2p'] - len(gd['f2p_bad'])}/{gd['n_f2p']}"
            p2p_bad = str(len(gd["p2p_bad"]))
            bad_names = gd["p2p_bad"]
            for name in gd["f2p_bad"]:
                per_test[name][0], per_test[name][1] = "F2P", per_test[name][1] + 1
            for name in gd["p2p_bad"]:
                per_test[name][0], per_test[name][1] = "P2P", per_test[name][1] + 1
        elif rw:
            f2p = f"{rw.get('f2p_success', '-')}/{(rw.get('f2p_success') or 0) + (rw.get('f2p_failure') or 0)}" \
                if "f2p_success" in rw else "-"
            p2p_bad = str(rw.get("p2p_failure", "-"))
        rows.append([run_id, k, run.get("agent") or "-", fmt_min(run.get("agent_sec")),
                     "-" if fix is None else f"{fix:.3f}", "-" if score is None else f"{score:.4f}",
                     f2p, p2p_bad, "; ".join(n.split("::")[-1] for n in bad_names)[:80] or "-",
                     run.get("status") or "-"])

    head = ["run_id", "#", "agent", "用时min", "fix_rate", "score", "F2P 通过", "P2P 失败", "失败的 P2P", "状态"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")

    if per_test:
        print(f"\n在 {n_graded} 次有测试明细的运行里，失败过的测试（失败次数 / 有明细的运行数）：\n")
        for name, (kind, bad) in sorted(per_test.items(), key=lambda kv: (-kv[1][1], kv[0])):
            print(f"  {bad}/{n_graded}  {kind}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

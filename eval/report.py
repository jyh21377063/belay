"""把一个 run_id 下所有 trial 的 run.json / grade.json 汇总成 summary.csv 和 summary.md。

也可以单独调用：python -m eval.report <results_root>/<run_id>
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

COLUMNS = ["task", "repeat", "status", "resolved", "fix_rate", "inline_resolved", "mismatch",
           "apply_ok", "exception", "agent_min", "n_input_tokens", "n_cache_tokens",
           "n_output_tokens", "cost_usd", "n_agent_steps", "summarization_count",
           "patch_files", "patch_loc", "error"]


def _read(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def collect_dir(root: Path, grade_mode: str = "replay") -> list[dict]:
    rows = []
    for run_json in sorted(root.glob("*/*/*/run.json")):
        d = run_json.parent
        rec, grade = _read(run_json), _read(d / "grade.json")
        resolved = grade.get("resolved") if grade_mode == "replay" else rec.get("inline_resolved")
        inline = rec.get("inline_resolved")
        rows.append({
            "task": f"{d.parent.parent.name}/{d.parent.name}",
            "repeat": int(d.name) if d.name.isdigit() else d.name,
            "status": rec.get("status") if grade_mode != "replay" or rec.get("status") != "done"
            else grade.get("status", "ungraded"),
            "resolved": resolved,
            "fix_rate": grade.get("fix_rate") if grade_mode == "replay" else rec.get("inline_fix_rate"),
            "inline_resolved": inline,
            # 同一补丁"原容器评分"与"全新容器重放评分"不一致，说明补丁导出或环境有问题
            "mismatch": grade_mode == "replay" and None not in (inline, resolved) and inline != resolved,
            "apply_ok": grade.get("apply_ok"),
            "exception": rec.get("exception"),
            "agent_min": round(rec["agent_sec"] / 60, 1) if rec.get("agent_sec") else None,
            **{k: rec.get(k) for k in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd",
                                       "n_agent_steps", "summarization_count", "patch_files", "patch_loc")},
            "error": rec.get("error") or grade.get("error"),
        })
    return rows


def collect(plan, step) -> list[dict]:
    root = plan.results_root / step.run_id
    keep = {t.key for t in plan.tasks}
    return [r for r in collect_dir(root, step.grade_mode) if r["task"] in keep]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def write(root: Path, rows: list[dict]) -> None:
    with open(root / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    by_bm = defaultdict(list)
    for r in rows:
        by_bm[r["task"].split("/")[0]].append(r)

    lines = [f"# {root.name}", "", "| benchmark | trials | resolved | 解决率 | 平均 Fix Rate | "
             "agent 用时中位数(min) | 平均输入 token | 平均输出 token | 未完成/出错 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for bm, rs in sorted(by_bm.items()):
        n = len(rs)
        ok = sum(1 for r in rs if r["resolved"] is True)
        bad = sum(1 for r in rs if r["resolved"] is None)
        lines.append(f"| {bm} | {n} | {ok} | {ok / n:.0%} | {_mean(r['fix_rate'] for r in rs) or '—'} | "
                     f"{_median(r['agent_min'] for r in rs) or '—'} | "
                     f"{_mean(r['n_input_tokens'] for r in rs) or '—'} | "
                     f"{_mean(r['n_output_tokens'] for r in rs) or '—'} | {bad} |")

    lines += ["", "| 题目 | # | 结果 | Fix Rate | 用时(min) | 输入/输出 token | 补丁文件数 | 异常 | 备注 |",
              "|---|---:|:-:|---:|---:|---|---:|---|---|"]
    for r in rows:
        mark = {True: "✅", False: "❌", None: "⚠️"}[r["resolved"]]
        note = "; ".join(filter(None, ["inline≠replay" if r["mismatch"] else "",
                                       "补丁应用失败" if r["apply_ok"] is False else "",
                                       (r["error"] or "")[:80]]))
        lines.append(f"| {r['task']} | {r['repeat']} | {mark} | {r['fix_rate'] if r['fix_rate'] is not None else ''} | "
                     f"{r['agent_min'] or ''} | {r['n_input_tokens'] or ''}/{r['n_output_tokens'] or ''} | "
                     f"{r['patch_files'] if r['patch_files'] is not None else ''} | {r['exception'] or ''} | {note} |")
    (root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    write(target, collect_dir(target))
    print((target / "summary.md").read_text())

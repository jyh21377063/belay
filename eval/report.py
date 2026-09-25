"""把一个 run_id 下所有 trial 的 run.json / grade.json 汇总成 summary.csv 和 summary.md。

也可以单独调用：python -m eval.report <results_root>/<run_id>
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

COLUMNS = ["task", "repeat", "status", "resolved", "score", "fix_rate", "inline_resolved", "mismatch",
           "apply_ok", "exception", "agent_min", "trial_min", "grade_min",
           "n_input_tokens", "n_cache_tokens", "cache_hit_rate", "n_output_tokens", "cost_est",
           "n_agent_steps", "summarization_count", "patch_files", "patch_loc", "test_files_changed",
           "f2p_passed", "f2p_total", "p2p_regressions", "error"]

# 判断补丁中哪些是测试文件（各语言常见约定）
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|(^|/)conftest\.py$")


def patch_test_files(patch: Path) -> int:
    if not patch.exists():
        return 0
    files = re.findall(r"^diff --git a/(\S+) b/", patch.read_text(errors="ignore"), flags=re.M)
    return sum(1 for f in files if _TEST_PATH.search(f))


def estimate_cost(rec: dict, pricing: dict | None) -> float | None:
    """Pier 的 n_input_tokens 已包含缓存命中部分；n_cache_tokens 为缓存命中数。单价按每百万 token。"""
    if not pricing or rec.get("n_input_tokens") is None:
        return None
    try:
        miss, hit, out = (float(pricing[k]) for k in ("input_miss_per_m", "input_hit_per_m", "output_per_m"))
    except (KeyError, TypeError, ValueError):
        return None
    total_in, cached = rec.get("n_input_tokens") or 0, rec.get("n_cache_tokens") or 0
    return round(((total_in - cached) * miss + cached * hit + (rec.get("n_output_tokens") or 0) * out) / 1e6, 4)


def _read(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def f2p_p2p(rewards: dict | None) -> tuple[int | None, int | None, int | None]:
    """从 reward 中取出 F2P 通过数 / 总数、P2P 失败数。兼容 SWE-EVO（我们的 grade.py）与 DeepSWE 的字段名。"""
    rw = rewards or {}
    if "f2p_success" in rw:
        ok, bad = rw.get("f2p_success", 0), rw.get("f2p_failure", 0)
        return int(ok), int(ok + bad), int(rw.get("p2p_failure", 0))
    if "f2p_total" in rw:
        return (int(rw.get("f2p_passed", 0)), int(rw["f2p_total"]),
                int(rw.get("p2p_total", 0) - rw.get("p2p_passed", 0)))
    return None, None, None


def collect_dir(root: Path, grade_mode: str = "replay", pricing: dict | None = None) -> list[dict]:
    rows = []
    for run_json in sorted(root.glob("*/*/*/run.json")):
        d = run_json.parent
        rec, grade = _read(run_json), _read(d / "grade.json")
        grade_mode_row = rec.get("grade_mode") or grade_mode
        resolved = grade.get("resolved") if grade_mode_row == "replay" else rec.get("inline_resolved")
        score = grade.get("score") if grade_mode_row == "replay" else rec.get("inline_score")
        inline = rec.get("inline_resolved")
        rows.append({
            "task": f"{d.parent.parent.name}/{d.parent.name}",
            "repeat": int(d.name) if d.name.isdigit() else d.name,
            "status": rec.get("status") if grade_mode_row != "replay" or rec.get("status") != "done"
            else grade.get("status", "ungraded"),
            "resolved": resolved,
            "score": score,
            "fix_rate": grade.get("fix_rate") if grade_mode_row == "replay" else rec.get("inline_fix_rate"),
            "inline_resolved": inline,
            # 同一补丁"原容器评分"与"全新容器重放评分"不一致，说明补丁导出或环境有问题
            "mismatch": grade_mode_row == "replay" and None not in (inline, resolved) and inline != resolved,
            "apply_ok": grade.get("apply_ok"),
            "exception": rec.get("exception"),
            "agent_min": round(rec["agent_sec"] / 60, 1) if rec.get("agent_sec") else None,
            "trial_min": round(rec["total_sec"] / 60, 1) if rec.get("total_sec") else None,
            "grade_min": round(grade["verify_sec"] / 60, 1) if grade.get("verify_sec") else None,
            "cache_hit_rate": (round(rec["n_cache_tokens"] / rec["n_input_tokens"], 3)
                               if rec.get("n_input_tokens") and rec.get("n_cache_tokens") is not None else None),
            "cost_est": estimate_cost(rec, pricing),
            "test_files_changed": patch_test_files(d / "patch.diff"),
            **dict(zip(("f2p_passed", "f2p_total", "p2p_regressions"),
                       f2p_p2p(grade.get("rewards") if grade_mode_row == "replay" else rec.get("rewards")))),
            **{k: rec.get(k) for k in ("n_input_tokens", "n_cache_tokens", "n_output_tokens",
                                       "n_agent_steps", "summarization_count", "patch_files", "patch_loc")},
            "error": rec.get("error") or grade.get("error"),
            "_currency": (pricing or {}).get("currency"),
        })
    return rows


def collect(plan, step) -> list[dict]:
    """汇总该 run_id 下的全部 trial（包括之前分批运行的题目），保证 summary 完整。"""
    root = plan.results_root / step.run_id
    return collect_dir(root, step.grade_mode, step.agent.get("pricing"))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def _d(v):
    """None 显示为 —；0 照常显示。"""
    return "—" if v is None else v


def write(root: Path, rows: list[dict]) -> None:
    with open(root / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    by_bm = defaultdict(list)
    for r in rows:
        by_bm[r["task"].split("/")[0]].append(r)

    cur = next((r.get("_currency") for r in rows if r.get("_currency")), "")
    lines = [f"# {root.name}", "",
             "| benchmark | trials | resolved | 解决率 | 平均得分 | 平均 Fix Rate | agent 用时中位数(min) | "
             "单题总耗时中位数(min) | 平均轮数 | 平均缓存命中率 | 平均成本" + (f"({cur})" if cur else "") + " | 未完成/出错 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for bm, rs in sorted(by_bm.items()):
        n = len(rs)
        ok = sum(1 for r in rs if r["resolved"] is True)
        bad = sum(1 for r in rs if r["resolved"] is None)
        total_min = [((r["trial_min"] or 0) + (r["grade_min"] or 0)) or None for r in rs]
        lines.append(f"| {bm} | {n} | {ok} | {ok / n:.0%} | {_d(_mean(r['score'] for r in rs))} | {_d(_mean(r['fix_rate'] for r in rs))} | "
                     f"{_d(_median(r['agent_min'] for r in rs))} | {_d(_median(total_min))} | "
                     f"{_d(_mean(r['n_agent_steps'] for r in rs))} | {_d(_mean(r['cache_hit_rate'] for r in rs))} | "
                     f"{_d(_mean(r['cost_est'] for r in rs))} | {bad} |")

    lines += ["", "| 题目 | # | 结果 | 得分 | Fix Rate | F2P 通过（不清零） | P2P 回归 | agent(min) | 总耗时(min) | 轮数 | "
              "输入 / 缓存命中 / 输出 token | 成本 | 补丁文件数（其中测试） | 异常 | 备注 |",
              "|---|---:|:-:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---|"]
    fmt = lambda v: f"{v:,}" if isinstance(v, int) else ("" if v is None else str(v))
    for r in rows:
        mark = {True: "✅", False: "❌", None: "⚠️"}[r["resolved"]]
        note = "; ".join(filter(None, ["inline≠replay" if r["mismatch"] else "",
                                       "补丁应用失败" if r["apply_ok"] is False else "",
                                       (r["error"] or "")[:80]]))
        total = round((r["trial_min"] or 0) + (r["grade_min"] or 0), 1) or ""
        f2p = f"{r['f2p_passed']}/{r['f2p_total']}" if r.get("f2p_total") else ""
        lines.append(f"| {r['task']} | {r['repeat']} | {mark} | {fmt(r['score'])} | {fmt(r['fix_rate'])} | {f2p} | {fmt(r.get('p2p_regressions'))} | "
                     f"{fmt(r['agent_min'])} | {total} | "
                     f"{fmt(r['n_agent_steps'])} | {fmt(r['n_input_tokens'])} / {fmt(r['n_cache_tokens'])} / "
                     f"{fmt(r['n_output_tokens'])} | {fmt(r['cost_est'])} | "
                     f"{fmt(r['patch_files'])}（{r['test_files_changed']}） | {r['exception'] or ''} | {note} |")
    (root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    plan = _read(target / "plan.json")
    agent = plan.get("agent") or {}
    write(target, collect_dir(target, agent.get("grade", "replay"), agent.get("pricing")))
    print((target / "summary.md").read_text())

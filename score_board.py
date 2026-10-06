"""所有题目的历史评分总表：每道题、每个组（agent）跑过的全部运行，一张表看完，并列出还缺哪些组。

放在 belay/ 目录下运行（需要项目的 venv）：
  python score_board.py                                  # 正式集，全部历史运行
  python score_board.py --split dev                      # 调试集
  python score_board.py --runs belay-final,cc-test-v3    # 只看这几个 run_id（重评、单独评分会跟着来源一起算）
  python score_board.py --exclude-runs 'diag-*,v9-*'     # 排除一批 run（支持通配符）
  python score_board.py --need claude-code,belay-polish-task   # 缺少检查按这几个组（默认就是这两个）
  python score_board.py --out /data/results/score_board.md --csv /data/results/score_board.csv

怎么认组：
  普通运行       run.json 的 agent（claude-code、belay-polish-task、belay-polish-improve ……）
  regrade 重评   run.json 的 source 指向原运行的补丁 → 记在原运行的组下，标“重评”；
                 同一次原运行既有原评分又有重评时，只用重评（SWE-EVO 的测试补丁问题就是这样修正的）
  grade_patch    agent 为 patch：记为“单独评分”，标出来源（哪个运行的哪个合并点 / worktree）
  oracle / nop   只取 runs.yaml 的 gold_run（及同名的 -nop），作为参考列

分数：
  LHTB      题目评分器的 reward
  SWE-EVO   以参考解为准的 F2P 通过数 / 可过数 · P2P 回归数（eval/gold_ref.py）；
            测试补丁没打上（test_patch_applied=0）的运行显示“未运行测试”，需要重评
不改任何文件（除非给了 --out / --csv）。
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
from collections import defaultdict
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_tasks_yaml, load_yaml, resolve_path

LABELS = {
    "claude-code": "CC", "claude-code-gate": "CC+gate", "claude-code-pee": "CC+PEE", "flat": "flat",
    "belay": "Belay(不进POLISH)", "belay-polish-task": "Belay(按题POLISH)", "belay-polish": "Belay(POLISH auto)",
    "belay-polish-verify": "Belay(verify)", "belay-polish-improve": "Belay(improve)", "belay-improve": "Belay(旧improve)",
    "patch": "单独评分", "oracle": "参考解", "nop": "空补丁",
}
ORDER = ["claude-code", "claude-code-gate", "claude-code-pee", "flat", "belay", "belay-polish-task",
         "belay-polish-verify", "belay-polish-improve", "belay-polish", "belay-improve", "patch", "oracle", "nop"]


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _source_run(src: str, root: Path) -> tuple[str | None, str]:
    """run.json 的 source（补丁路径）→（来源 run_id，来源说明）。"""
    try:
        rel = Path(src).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None, Path(src).name
    parts = rel.parts
    what = Path(src).name
    if "checkpoints" in parts:
        what = f"合并点 #{Path(src).stem}"
    elif what in ("worktree.diff", "deliverable.diff"):
        what = {"worktree.diff": "worker 最终工作区", "deliverable.diff": "交付版本"}[what]
    elif what == "patch.diff":
        what = "补丁"
    return parts[0], what


def collect(root: Path, task_dirs: Path, tasks: list[tuple[str, str]], gold_run: str | None,
            only: set[str] | None, exclude: list[str]) -> list[dict]:
    from eval.gold_ref import GoldRef
    gold = GoldRef(root / gold_run, task_dirs) if gold_run and (root / gold_run).is_dir() else None
    nop_run = gold_run[: -len("-oracle")] + "-nop" if gold_run and gold_run.endswith("-oracle") else None
    agent_of_run: dict[str, str] = {}

    def run_agent(run_id: str, bm: str, tid: str) -> str:
        if run_id not in agent_of_run:
            a = _json(root / run_id / "plan.json").get("agent_key")
            if not a:
                for rj in sorted((root / run_id / bm / tid).glob("*/run.json")):
                    a = _json(rj).get("agent")
                    if a:
                        break
            agent_of_run[run_id] = a or "?"
        return agent_of_run[run_id]

    rows = []
    for bm, tid in tasks:
        for d in sorted(root.glob(f"*/{bm}/{tid}/*")):
            if not (d.is_dir() and d.name.isdigit() and (d / "run.json").exists()):
                continue
            run_id = d.parts[-4]
            if any(fnmatch.fnmatch(run_id, pat) for pat in exclude):
                continue
            run, grade = _json(d / "run.json"), _json(d / "grade.json")
            agent, regrade, source, source_run = run.get("agent"), False, "", None
            if run.get("source"):
                source_run, source = _source_run(run["source"], root)
                if agent != "patch":                         # regrade：记到原运行的组下
                    regrade = True
                    agent = run_agent(source_run, bm, tid) if source_run else "?"
            if agent in ("oracle", "nop") and run_id not in (gold_run, nop_run):
                continue
            if only and run_id not in only and (source_run not in only):
                continue
            agent_sec = run.get("agent_sec")
            if regrade and not agent_sec:                    # 重评没有 agent 阶段：用时取原运行的
                agent_sec = _json(Path(run["source"]).parent / "run.json").get("agent_sec")
            rw = grade.get("rewards") or run.get("rewards") or {}
            mode = run.get("grade_mode") or ("replay" if grade else "inline")
            score = grade.get("score") if mode == "replay" else run.get("inline_score")
            r = {"task": f"{bm}/{tid}", "bm": bm, "run_id": run_id, "repeat": d.name, "agent": agent or "?",
                 "regrade": regrade, "source_run": source_run, "source": source,
                 "agent_min": round(agent_sec / 60) if agent_sec else None,
                 "exception": run.get("exception"), "status": grade.get("status") or run.get("status"),
                 "score": score, "test_not_run": rw.get("test_patch_applied") == 0,
                 "f2p": None, "f2p_total": None, "p2p_reg": None, "mtime": (d / "run.json").stat().st_mtime}
            if bm == "swe_evo":
                if r["test_not_run"]:
                    pass
                elif gold is not None:
                    adj = gold.adjust(tid, d)
                    r["f2p"], r["f2p_total"], r["p2p_reg"] = (adj["f2p_passed_ref"], adj["f2p_total_ref"],
                                                              adj["p2p_regressions_ref"])
                elif "f2p_success" in rw:
                    r["f2p"], r["f2p_total"] = rw["f2p_success"], rw["f2p_success"] + rw.get("f2p_failure", 0)
                    r["p2p_reg"] = rw.get("p2p_failure")
            rows.append(r)
    # 同一次原运行既有原评分又有重评：只留重评（最新的那次）
    regraded = {(r["task"], r["source_run"], r["repeat"]) for r in rows if r["regrade"]}
    rows = [r for r in rows if r["regrade"] or (r["task"], r["run_id"], r["repeat"]) not in regraded]
    return sorted(rows, key=lambda r: r["mtime"])


def value(r: dict) -> str:
    if r["bm"] == "swe_evo":
        if r["test_not_run"]:
            return "未运行测试"
        if r["f2p_total"] is None:
            return "—"
        return f"{r['f2p']}/{r['f2p_total']}·{'—' if r['p2p_reg'] is None else r['p2p_reg']}"
    return "—" if r["score"] is None else f"{float(r['score']):.3f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python score_board.py")
    ap.add_argument("--split", default="test", choices=["test", "dev"])
    ap.add_argument("--runs", help="只看这些 run_id（逗号分隔）")
    ap.add_argument("--exclude-runs", default="", help="排除的 run_id，逗号分隔，支持通配符")
    ap.add_argument("--need", default="claude-code,belay-polish-task", help="缺少检查的组（agent 键名）")
    ap.add_argument("--out", type=Path, help="同时写 Markdown 到这个文件")
    ap.add_argument("--csv", type=Path, help="逐次运行的明细写成 CSV")
    a = ap.parse_args(argv)

    runs = load_yaml(DEFAULT_RUNS)
    base = DEFAULT_RUNS.parent
    root = resolve_path(base, runs["results_root"])
    task_dirs = resolve_path(base, runs["task_dirs"])
    ty = load_tasks_yaml(resolve_path(base, runs["tasks_file"]))
    entries = [(bm, e) for bm, lst in (ty.get(a.split) or {}).items() for e in (lst or [])]
    tasks = [(bm, e["id"]) for bm, e in entries]
    gold_run = (runs.get("defaults") or {}).get("gold_run")
    only = set(a.runs.split(",")) if a.runs else None
    exclude = [p.strip() for p in a.exclude_runs.split(",") if p.strip()]
    rows = collect(root, task_dirs, tasks, gold_run, only, exclude)

    agents = sorted({r["agent"] for r in rows}, key=lambda x: (ORDER.index(x) if x in ORDER else 50, x))
    by = defaultdict(list)
    for r in rows:
        by[(r["task"], r["agent"])].append(r)

    out = [f"# 评分总表（{a.split}）", "",
           "LHTB 为 reward；SWE-EVO 为“F2P 通过/参考解可过·P2P 回归”（以参考解为准，gold_run = "
           f"`{gold_run}`）。同一格多次运行按时间先后用 / 分隔，r = 重评。", ""]
    head = ["题目"] + [LABELS.get(x, x) for x in agents]
    out += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for bm, e in entries:
        key = f"{bm}/{e['id']}"
        marks = ("（负对照）" if e.get("role") == "negative_control" else "") + ("（开发期看过）" if e.get("seen_in_dev") else "")
        cells = [" / ".join(value(r) + ("r" if r["regrade"] else "") + (f"（{r['source']}）" if ag == "patch" else "")
                            for r in by.get((key, ag), [])) or "" for ag in agents]
        out.append(f"| {e['id']}{marks} | " + " | ".join(cells) + " |")

    need = [x.strip() for x in a.need.split(",") if x.strip()]
    missing = [(f"{bm}/{e['id']}", [LABELS.get(n, n) for n in need if not by.get((f"{bm}/{e['id']}", n))])
               for bm, e in entries]
    missing = [(t, m) for t, m in missing if m]
    out += ["", "## 缺少的组", ""]
    out += [f"- {t}：缺 {'、'.join(m)}" for t, m in missing] or ["（都有）"]

    attention = [r for r in rows if r["test_not_run"] or r["exception"] or r["status"] not in ("done", None)]
    if attention:
        out += ["", "## 需要注意的运行", ""]
        for r in attention:
            why = "测试补丁没打上，需要重评" if r["test_not_run"] else (r["exception"] or f"status={r['status']}")
            out.append(f"- {r['task']}  {r['run_id']}#{r['repeat']}（{LABELS.get(r['agent'], r['agent'])}）：{why}")

    out += ["", "## 逐次明细", "", "| 题目 | run_id | # | 组 | 用时 min | 得分 | 备注 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        note = "; ".join(filter(None, [
            f"重评自 {r['source_run']}" if r["regrade"] else "",
            f"{r['source']}（{r['source_run']}）" if r["agent"] == "patch" else "",
            r["exception"] or ""]))
        out.append(f"| {r['task'].split('/')[1]} | {r['run_id']} | {r['repeat']} | {LABELS.get(r['agent'], r['agent'])} | "
                   f"{r['agent_min'] if r['agent_min'] is not None else '—'} | {value(r)} | {note} |")

    text = "\n".join(out) + "\n"
    print(text)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    if a.csv:
        cols = ["task", "run_id", "repeat", "agent", "regrade", "source_run", "source", "agent_min", "score",
                "f2p", "f2p_total", "p2p_reg", "test_not_run", "exception", "status"]
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

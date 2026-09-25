"""SWE-EVO 题目一览：规模、评分覆盖、预计耗时与成本，以及是否符合选题规则。

  python -m eval.tools.sweevo_catalog                       # 读取 tasks.yaml 中配置的 SWE-EVO 数据
  python -m eval.tools.sweevo_catalog --data <jsonl 或 arrow> --out docs/sweevo_catalog.md

预计耗时与成本沿用 task_selection.md 4.2 节由调试集实测得到的比例：
  轮数 ≈ 8.22 × 参考解改动的代码文件数；每轮 7.55 秒；每轮 0.0163 元（高峰价）
评分耗时按测试数量粗估：2 + (F2P + P2P) / 200 分钟（conan 实测约 4 分钟 / 325 个测试）。
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

TURNS_PER_FILE, SEC_PER_TURN, CNY_PER_TURN, CAP_MIN = 8.22, 7.55, 0.0163, 180
TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")
NON_CODE = re.compile(r"(^|/)(docs?|\.github)/|\.(md|rst|txt|svg|png|jpe?g|cfg|toml|ini|yml|yaml|json)$|"
                      r"(^|/)(README|CHANGELOG|HISTORY|AUTHORS|LICENSE)[^/]*$", re.I)
ITEM = re.compile(r"^\s*(\d+\)|[-*])\s+\S", re.M)


def load(path: Path) -> list[dict]:
    if path.suffix == ".arrow":
        import pyarrow as pa
        with pa.memory_map(str(path)) as src:
            try:
                return pa.ipc.open_stream(src).read_all().to_pylist()
            except pa.ArrowInvalid:
                return pa.ipc.open_file(src).read_all().to_pylist()
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def patch_stats(patch: str) -> tuple[int, int, int]:
    files = re.findall(r"^diff --git a/(\S+) b/", patch, re.M)
    code = [f for f in files if not TEST_PATH.search(f) and not NON_CODE.search(f)]
    loc = sum(1 for l in patch.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
    return len(files), len(code), loc


def row(r: dict, status: dict[str, str]) -> dict:
    files, code, loc = patch_stats(r["patch"])
    f2p, p2p = r["FAIL_TO_PASS"], r["PASS_TO_PASS"]
    by_file = collections.Counter(t.split("::")[0] for t in f2p)
    top = by_file.most_common(1)[0][1] / len(f2p) if f2p else 0
    turns = TURNS_PER_FILE * code
    agent_min = min(CAP_MIN, turns * SEC_PER_TURN / 60)
    grade_min = 2 + (len(f2p) + len(p2p)) / 200
    flags = []
    if len(f2p) < 10:
        flags.append("F2P<10")
    if code and len(f2p) / code > 30:
        flags.append("F2P 与规模不相称")
    if top > 0.7 and len(f2p) >= 10:
        flags.append(f"F2P 集中（{top:.0%}）")
    if len(p2p) > 2000:
        flags.append("P2P 很多")
    if files < 14:
        flags.append("改动文件<14")
    if turns * SEC_PER_TURN / 60 > CAP_MIN:
        flags.append("可能超时")
    return {
        "id": r["instance_id"], "repo": r["repo"], "ver": f"{r['start_version']} → {r['end_version']}",
        "items": len(ITEM.findall(r["problem_statement"])), "prs": len(r.get("PRs") or []),
        "files": files, "code": code, "loc": loc,
        "f2p": len(f2p), "f2p_files": len(by_file), "p2p": len(p2p),
        "turns": round(turns), "agent_min": round(agent_min), "grade_min": round(grade_min),
        "total_min": round(agent_min + grade_min), "cost": round(turns * CNY_PER_TURN, 1),
        "status": status.get(r["instance_id"], ""), "flags": flags,
        "eligible": not any(f in flags for f in ("F2P<10", "F2P 与规模不相称", "改动文件<14")),
    }


def selection_status(tasks_yaml: Path) -> dict[str, str]:
    import yaml
    ty = yaml.safe_load(tasks_yaml.read_text())
    st = {}
    for split, name in (("test", "评测集"), ("dev", "调试集")):
        for e in (ty.get(split) or {}).get("swe_evo") or []:
            st[e["id"]] = name
    for e in ((ty.get("backups") or {}).get("swe_evo") or {}).get("any") or []:
        st[e["id"] if isinstance(e, dict) else e] = "备选"
    return st


def markdown(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda x: (not x["eligible"], x["repo"], -x["code"]))
    head = ("| 题目 | 版本 | 改动条目 | PR | 参考解文件（代码） | 改动行数 | F2P（测试文件数） | P2P | "
            "预计轮数 | 预计 agent / 评分 / 合计 (min) | 预计成本（元） | 选用 | 提示 |")
    L = ["# SWE-EVO 题目一览", "",
         "由 `python -m eval.tools.sweevo_catalog` 生成。预计值沿用 `task_selection.md` 4.2 节的比例："
         "轮数 ≈ 8.2 × 代码文件数，每轮约 7.6 秒、0.016 元（高峰价）；评分耗时按测试数量粗估。"
         "调试题 conan_2.0.2_2.0.3 的实测 agent 用时为 52.8 min，本表预估 49 min。", "",
         "**符合选题规则**指：参考解改动文件 ≥ 14、F2P ≥ 10、F2P 与代码文件数之比 ≤ 30。排在前面。", "",
         head, "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---|---|"]
    for x in rows:
        L.append(f"| `{x['id']}` | {x['ver']} | {x['items']} | {x['prs'] or ''} | {x['files']}（{x['code']}） | {x['loc']} | "
                 f"{x['f2p']}（{x['f2p_files']}） | {x['p2p']} | {x['turns']} | "
                 f"{x['agent_min']} / {x['grade_min']} / {x['total_min']} | {x['cost']} | {x['status']} | {'；'.join(x['flags'])} |")
    by_repo = collections.defaultdict(list)
    for x in rows:
        by_repo[x["repo"]].append(x)
    L += ["", "## 按仓库汇总", "", "| 仓库 | 题数 | 其中符合规则 | 代码文件数范围 | F2P 范围 |", "|---|---:|---:|---|---|"]
    for repo, xs in sorted(by_repo.items(), key=lambda kv: -len(kv[1])):
        L.append(f"| {repo} | {len(xs)} | {sum(x['eligible'] for x in xs)} | "
                 f"{min(x['code'] for x in xs)}–{max(x['code'] for x in xs)} | {min(x['f2p'] for x in xs)}–{max(x['f2p'] for x in xs)} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    from eval.config import DEFAULT_RUNS, load_tasks_yaml, load_yaml, resolve_path
    ap = argparse.ArgumentParser(prog="python -m eval.tools.sweevo_catalog")
    ap.add_argument("--data", help="SWE-EVO 数据（jsonl 或 arrow）；默认取 tasks.yaml 中的配置")
    ap.add_argument("--out", help="写入 markdown 文件")
    a = ap.parse_args(argv)
    runs = load_yaml(DEFAULT_RUNS)
    tasks_file = resolve_path(DEFAULT_RUNS.parent, runs["tasks_file"])
    data = Path(a.data) if a.data else Path(load_tasks_yaml(tasks_file)["benchmarks"]["swe_evo"]["data"])
    rows = [row(r, selection_status(tasks_file)) for r in load(data)]
    md = markdown(rows)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

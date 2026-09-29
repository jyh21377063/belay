"""SWE-EVO 逐题对照：以参考解为准，列出各组的 F2P 完成度与每个 P2P 回归的成因。

  python -m eval.tools.regressions --oracle gold-check-oracle-test-20260928 \
      --runs diag-cc-swe-notests-grade diag-gate-swe-notests-grade --gate-run diag-gate-swe

对每道题：
  F2P   agent 通过数 / 参考解通过数（参考解都过不了的测试不计）
  P2P   每个失败的 P2P 测试一行，归类为：
          env           参考解也失败：环境问题，不算 agent 的错
          gate-visible  在 A-gate 的基线里（原始代码上两次都通过），所在文件没被测试补丁改动：门禁本该看到
          patch-edited  所在文件被测试补丁改动：门禁跑的是旧版本，看不到新断言
          patch-new     测试补丁新建的文件：门禁看不到
          uncovered     其余：不在门禁基线中（原始代码上就没通过，或没被收集）
  另外给出 A-gate 的拦截记录（checks.jsonl）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from eval.tools.oracle_failures import END, START, parse

ROOT = Path(__file__).resolve().parents[2].parent
OK = ("PASSED", "XFAIL")


def statuses(trial: Path) -> dict[str, str] | None:
    logs = sorted(trial.glob("pier/grade/**/verifier/test_output.txt")) or sorted(trial.glob("pier/**/verifier/test_output.txt"))
    if not logs:
        return None
    log = logs[0].read_text(errors="replace")
    if START not in log or END not in log:
        return {}
    return parse(log.split(START)[1].split(END)[0])[0]


def test_patch_files(tp: Path) -> tuple[set[str], set[str]]:
    edited, new = set(), set()
    for blk in re.split(r"(?m)^(?=diff --git )", tp.read_text(errors="replace") if tp.exists() else ""):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", blk)
        if m:
            (new if re.search(r"(?m)^new file mode", blk[:500]) else edited).add(m.group(2))
    return edited, new


def gate_info(run: Path, tid: str) -> tuple[set[str] | None, list[dict]]:
    base = sorted((run / "swe_evo" / tid).glob("1/pier/**/gate/baseline.json"))
    checks = sorted((run / "swe_evo" / tid).glob("1/pier/**/gate/checks.jsonl"))
    stable = set(json.loads(base[0].read_text()).get("stable_pass", [])) if base else None
    rows = [json.loads(l) for l in checks[0].read_text().splitlines() if l.strip()] if checks else []
    return stable, rows


def fail(s: str | None) -> bool:
    return s is None or s in ("FAILED", "ERROR")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.tools.regressions")
    p.add_argument("--oracle", required=True)
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--gate-run", help="A-gate 的原始 run（读取基线与拦截记录）")
    p.add_argument("--results", type=Path, default=ROOT / "results")
    p.add_argument("--task-dirs", type=Path, default=ROOT / "tasks")
    a = p.parse_args(argv)

    oracle_root = a.results / a.oracle
    tids = sorted({d.name for r in a.runs for d in (a.results / r / "swe_evo").glob("*") if d.is_dir()})
    for tid in tids:
        tests = json.loads((a.task_dirs / "swe_evo" / tid / "tests" / "tests.json").read_text())
        edited, new = test_patch_files(a.task_dirs / "swe_evo" / tid / "tests" / "test.patch")
        orc = statuses(oracle_root / "swe_evo" / tid / "1")
        if orc is None:
            print(f"\n!! {tid}: 找不到参考解的评分日志")
            continue
        stable, checks = gate_info(a.results / a.gate_run, tid) if a.gate_run else (None, [])
        runs = {r: statuses(a.results / r / "swe_evo" / tid / "1") for r in a.runs}
        f2p_ok = [t for t in tests["FAIL_TO_PASS"] if orc.get(t) in OK]
        print(f"\n==== {tid}")
        for r, st in runs.items():
            if st is None:
                print(f"  {r}: 没有评分日志")
                continue
            n = sum(1 for t in f2p_ok if st.get(t) in OK)
            print(f"  F2P {n:>4}/{len(f2p_ok):<4} {r}")
        rows = []
        for t in tests["PASS_TO_PASS"]:
            per = {r: (st or {}).get(t) for r, st in runs.items()}
            if not any(fail(s) for s in per.values()):
                continue
            f = t.split("::")[0]
            if fail(orc.get(t)):
                cls = "env"
            elif f in new:
                cls = "patch-new"
            elif f in edited:
                cls = "patch-edited"
            elif stable is not None and t in stable:
                cls = "gate-visible"
            elif stable is None:
                cls = "?"
            else:
                cls = "uncovered"
            rows.append((cls, t, per))
        real = [x for x in rows if x[0] != "env"]
        print(f"  P2P 失败：共 {len(rows)} 个，其中环境问题 {len(rows) - len(real)} 个；以下只列非环境的")
        for cls, t, per in sorted(real):
            marks = "  ".join(f"{r.split('-')[1] if '-' in r else r}={'✗' if fail(s) else '✓'}" for r, s in per.items())
            print(f"   {cls:<13} {marks}  {t}")
        if checks:
            blocks = [c for c in checks if c.get("decision") == "block"]
            last = checks[-1]
            print(f"  A-gate：检查 {len(checks)} 次，拦截 {len(blocks)} 次；最后一次 {last.get('decision')}"
                  f"（{last.get('why', '')}）" + (f"，基线被篡改" if any(c.get("baseline_tampered") for c in checks) else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

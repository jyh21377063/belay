"""列出 SWE-EVO 某次运行中没有通过的 F2P / P2P 测试，并归类为"日志里没有"或"失败 / 出错"。

  python -m eval.tools.oracle_failures /data/results/gold-check-oracle-test-20260928
  python -m eval.tools.oracle_failures <run 目录> --tasks conan-io__conan_2.0.14_2.0.15 --repeat 1

归类：
  MISSING    日志里找不到这个测试名。若日志中有一个测试名是它的前缀，标 TRUNCATED?（多半是参数里带空格，
             被判分脚本按空格切断，属于判分问题而不是测试失败）；否则多半是收集失败或测试不存在
  FAILED / ERROR  测试确实运行了但失败，附上 pytest 简短摘要中的原因
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

START, END = ">>>>> Start Test Output", ">>>>> End Test Output"
STATUSES = ("FAILED", "PASSED", "SKIPPED", "ERROR", "XFAIL")


def parse(log: str) -> tuple[dict[str, str], dict[str, str]]:
    """与 grade.py 的 parse_log_pytest 相同的解析，另外记下 FAILED / ERROR 行里的原因。"""
    status, reason = {}, {}
    for line in log.split("\n"):
        line = re.sub(r"\x1b\[[0-9;]*m", "", line)
        if not any(line.startswith(s) for s in STATUSES):
            continue
        parts = line.split(" - ", 1)
        head = parts[0].split()
        if len(head) <= 1:
            continue
        status[head[1]] = head[0]
        if len(parts) > 1:
            reason[head[1]] = parts[1].strip()[:160]
    return status, reason


def find_trials(run: Path, tasks: list[str] | None, repeat: str | None):
    for d in sorted(run.glob("swe_evo/*/*")):
        if tasks and d.parent.name not in tasks:
            continue
        if repeat and d.name != repeat:
            continue
        logs = list(d.glob("pier/**/verifier/test_output.txt"))
        if logs:
            yield d, logs[0]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.tools.oracle_failures")
    p.add_argument("run", type=Path)
    p.add_argument("--tasks", help="逗号分隔的短 id")
    p.add_argument("--repeat", default="1", help="只看第几次（默认 1；传空字符串看全部）")
    p.add_argument("--task-dirs", type=Path, default=Path(__file__).resolve().parents[2].parent / "tasks",
                   help="eval.prepare 生成的任务目录（读取 tests/tests.json）")
    p.add_argument("--show", type=int, default=40, help="每类最多列出的测试数")
    a = p.parse_args(argv)
    tasks = [t.strip() for t in a.tasks.split(",")] if a.tasks else None

    found = False
    for d, log_path in find_trials(a.run, tasks, a.repeat or None):
        found = True
        tid = d.parent.name
        tests_json = a.task_dirs / "swe_evo" / tid / "tests" / "tests.json"
        if not tests_json.exists():
            print(f"!! {tid}: 找不到 {tests_json}")
            continue
        tests = json.loads(tests_json.read_text())
        log = log_path.read_text(errors="replace")
        if START not in log or END not in log:
            print(f"!! {tid}: 日志里没有测试输出（测试补丁可能没应用上）：{log_path}")
            continue
        status, reason = parse(log.split(START)[1].split(END)[0])
        keys = list(status)
        print(f"\n==== {tid} #{d.name}   日志中共 {len(status)} 个测试结果")
        for kind in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            rows = []
            for t in tests[kind]:
                s = status.get(t)
                if s in ("PASSED", "XFAIL", "SKIPPED"):
                    continue
                if s is None:
                    pref = [k for k in keys if t.startswith(k) and k != t]
                    rows.append(("TRUNCATED?" if pref else "MISSING", t, f"日志中为 {pref[0]}" if pref else ""))
                else:
                    rows.append((s, t, reason.get(t, "")))
            c = Counter(r[0] for r in rows)
            print(f"-- {kind}: {len(rows)} 个未通过  " + "  ".join(f"{k}={v}" for k, v in c.most_common()))
            for k in ("TRUNCATED?", "MISSING", "FAILED", "ERROR"):
                sel = [r for r in rows if r[0] == k]
                for r in sel[:a.show]:
                    print(f"   {r[0]:<10} {r[1]}" + (f"\n              {r[2]}" if r[2] else ""))
                if len(sel) > a.show:
                    print(f"   ……另有 {len(sel) - a.show} 个 {k}")
        errs = Counter(re.sub(r"\d+", "N", v.split(":")[0]) for v in reason.values())
        if errs:
            print("-- 失败原因的类型（全部测试）：" + "；".join(f"{k} ×{n}" for k, n in errs.most_common(6)))
    if not found:
        print(f"{a.run} 下没有找到 swe_evo/*/*/pier/**/verifier/test_output.txt")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

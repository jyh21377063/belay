"""从一次运行的补丁中剔除测试改动，生成 <run>-notests，供 regrade 重新评分。

  python -m eval.tools.strip_test_changes diag-cc-swe diag-gate-swe
  python -m eval.run --profile regrade --split test --benchmarks swe_evo --tasks $SWE \
      --source-run diag-cc-swe-notests --run-id diag-cc-swe-notests-grade -y

剔除两类文件的全部改动（含新增、删除）：
  test-path    路径符合测试文件约定（与 eval/report.py 的规则相同）
  test-patch   官方测试补丁也改动的文件（即使不在测试路径下，也会让测试补丁无法应用）
每个 trial 写一份 stripped.json，列出剔除的文件；不在测试路径下却被剔除的文件单独标出，
因为它们可能是 agent 的真实代码改动。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from eval.report import _TEST_PATH

RESULTS = Path(__file__).resolve().parents[2].parent / "results"
TASKS = Path(__file__).resolve().parents[2].parent / "tasks"


def blocks(diff: str) -> list[tuple[str, str]]:
    """把 git diff 按文件切块，返回 (路径, 块文本)。路径取 b/ 侧，删除文件取 a/ 侧。"""
    out = []
    for blk in re.split(r"(?m)^(?=diff --git )", diff):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", blk)
        if m:
            out.append((m.group(2) if m.group(2) != "/dev/null" else m.group(1), blk))
        elif blk.strip():
            out.append(("", blk))
    return out


def patch_files(diff: str) -> set[str]:
    files = set()
    for a, b in re.findall(r"(?m)^diff --git a/(\S+) b/(\S+)", diff):
        files.update((a, b))
    return files


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.tools.strip_test_changes")
    p.add_argument("runs", nargs="+", help="run_id（位于 results/ 下）")
    p.add_argument("--results", type=Path, default=RESULTS)
    p.add_argument("--task-dirs", type=Path, default=TASKS)
    a = p.parse_args(argv)

    for run in a.runs:
        src_root, dst_root = a.results / run, a.results / f"{run}-notests"
        trials = sorted(src_root.glob("*/*/*/patch.diff"))
        if not trials:
            print(f"!! {src_root} 下没有 patch.diff")
            continue
        print(f"== {run} → {dst_root.name}")
        for patch in trials:
            d = patch.parent
            bm, tid, rep = d.parent.parent.name, d.parent.name, d.name
            tp = a.task_dirs / bm / tid / "tests" / "test.patch"
            official = patch_files(tp.read_text(errors="replace")) if tp.exists() else set()
            kept, dropped = [], []
            for path, blk in blocks(patch.read_text(errors="replace")):
                why = [w for w, hit in (("test-path", bool(_TEST_PATH.search(path))),
                                        ("test-patch", path in official)) if hit]
                (dropped if why else kept).append((path, blk, why))
            out = dst_root / bm / tid / rep
            out.mkdir(parents=True, exist_ok=True)
            (out / "patch.diff").write_text("".join(b for _, b, _ in kept))
            for extra in ("run.json",):
                if (d / extra).exists():
                    shutil.copy(d / extra, out / extra)
            non_test = [x for x, _, w in dropped if "test-path" not in w]
            (out / "stripped.json").write_text(json.dumps(
                {"source": str(patch), "dropped": [{"path": x, "why": w} for x, _, w in dropped],
                 "dropped_non_test_path": non_test}, indent=1, ensure_ascii=False))
            flag = f"  ⚠️ 其中不在测试路径下：{', '.join(non_test)}" if non_test else ""
            print(f"  {bm}/{tid}#{rep}: 保留 {len(kept)} 个文件，剔除 {len(dropped)} 个{flag}")
        print(f"   重新评分：python -m eval.run --profile regrade --split test "
              f"--benchmarks swe_evo --tasks $SWE --source-run {run}-notests --run-id {run}-notests-grade -y")
    return 0


if __name__ == "__main__":
    sys.exit(main())

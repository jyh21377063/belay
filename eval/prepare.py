"""把 tasks.yaml 中选中的题目准备成 Pier 任务目录：<task_dirs>/<benchmark>/<id>/

  python -m eval.prepare --split dev                 # 准备调试集
  python -m eval.prepare --split dev --benchmarks deepswe
  python -m eval.prepare --split test --force        # 覆盖已存在的目录

DeepSWE、LHTB 是原生 Harbor 格式，直接复制；ProMax / SWE-EVO 调用各自的转换器。
所有任务统一强制 [environment] allow_internet = false：Pier 只会放行 agent 声明的模型 API。
LHTB 另外关闭 continue_until_timeout（它相当于用隐藏评分器当裁判，只允许在上界对照组中开启）。
"""
from __future__ import annotations

import argparse
import importlib
import re
import shutil
import sys
import tomllib
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_tasks_yaml, load_yaml, resolve_path, select_tasks

CONVERTERS = {  # benchmark -> 模块，需实现 convert(task_id: str, bench_cfg: dict, task_entry: dict, dst: Path)
    "promax": "eval.convert.promax_to_harbor",
    "swe_evo": "eval.convert.sweevo_to_harbor",
}


def force_no_internet(toml_path: Path) -> None:
    """在 [environment] / [agent] 中去掉联网设置，并写入 allow_internet = false。"""
    out, section = [], None
    for line in toml_path.read_text().splitlines():
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            section = m.group(1).strip()
            out.append(line)
            if section == "environment":
                out.append("allow_internet = false")
            continue
        if section in ("environment", "agent") and re.match(r"^\s*(allow_internet|network_mode)\s*=", line):
            continue
        out.append(line)
    if not any(re.match(r"^\s*\[environment\]\s*$", l) for l in out):
        out += ["", "[environment]", "allow_internet = false"]
    toml_path.write_text("\n".join(out) + "\n")
    env = tomllib.loads(toml_path.read_text()).get("environment", {})
    assert env.get("allow_internet") is False, toml_path


def disable_continue_until_timeout(toml_path: Path) -> None:
    text = toml_path.read_text()
    new = re.sub(r"(?m)^(\s*continue_until_timeout\s*=\s*)true\b", r"\1false", text)
    if new != text:
        toml_path.write_text(new)
    agent = tomllib.loads(new).get("agent", {})
    assert agent.get("continue_until_timeout", False) is False, toml_path


def prepare_one(bm: str, tid: str, bench_cfg: dict, entry: dict, dst: Path) -> str:
    if bench_cfg.get("task_format") == "native":
        src = Path(bench_cfg["data"]) / tid
        if not (src / "task.toml").exists():
            raise FileNotFoundError(f"找不到 {src}/task.toml")
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
    else:
        try:
            mod = importlib.import_module(CONVERTERS[bm])
        except ModuleNotFoundError:
            return f"跳过：转换器 {CONVERTERS[bm]} 尚未实现"
        dst.mkdir(parents=True)
        mod.convert(tid, bench_cfg, entry, dst)
    force_no_internet(dst / "task.toml")
    disable_continue_until_timeout(dst / "task.toml")
    return "ok"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.prepare")
    p.add_argument("--runs", default=str(DEFAULT_RUNS))
    p.add_argument("--split", required=True, choices=["test", "dev"])
    p.add_argument("--benchmarks", default="all")
    p.add_argument("--tasks", default="all")
    p.add_argument("--force", action="store_true")
    a = p.parse_args(argv)

    runs_path = Path(a.runs).resolve()
    runs = load_yaml(runs_path)
    tasks_yaml = load_tasks_yaml(resolve_path(runs_path.parent, runs["tasks_file"]))
    root = resolve_path(runs_path.parent, runs["task_dirs"])
    entries = {(bm, e["id"]): e for bm, lst in (tasks_yaml.get(a.split) or {}).items() for e in (lst or [])}

    for t in select_tasks(tasks_yaml, a.split, a.benchmarks, a.tasks):
        dst = root / t.benchmark / t.id
        if dst.exists() and not a.force:
            print(f"  = {t.key}（已存在）")
            continue
        if dst.exists():
            shutil.rmtree(dst)
        try:
            status = prepare_one(t.benchmark, t.id, tasks_yaml["benchmarks"][t.benchmark],
                                 entries[(t.benchmark, t.id)], dst)
        except Exception as e:
            shutil.rmtree(dst, ignore_errors=True)
            status = f"失败：{e}"
        print(f"  {'+' if status == 'ok' else '!'} {t.key}  {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

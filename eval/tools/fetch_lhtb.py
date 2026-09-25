"""下载 tasks.yaml 中选用的 LHTB 题目（稀疏克隆，固定到 tasks.yaml 记录的 commit）。

  python -m eval.tools.fetch_lhtb              # 正式集 + 调试集 + 备选
  python -m eval.tools.fetch_lhtb --all        # 全部 46 题
  python -m eval.tools.fetch_lhtb --commit <sha>

下载位置：tasks.yaml 中 benchmarks.lhtb.data 的上一级（默认 /data/benchmarks/LHTB）。
只取需要的题目目录，不需要 Git LFS（所选题目不含 LFS 文件；如检测到会给出提示）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_tasks_yaml, load_yaml, resolve_path


def git(*args, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True)


def selected_ids(ty: dict) -> list[str]:
    ids = []
    for split in ("test", "dev"):
        ids += [e["id"] for e in ((ty.get(split) or {}).get("lhtb") or [])]
    for e in ((ty.get("backups") or {}).get("lhtb") or {}).get("any") or []:
        ids.append(e["id"] if isinstance(e, dict) else e)
    return list(dict.fromkeys(ids))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.tools.fetch_lhtb")
    ap.add_argument("--all", action="store_true", help="下载全部题目（含 LFS 大文件的题目需另行 git lfs pull）")
    ap.add_argument("--commit", help="覆盖 tasks.yaml 中记录的 commit")
    a = ap.parse_args(argv)

    runs = load_yaml(DEFAULT_RUNS)
    ty = load_tasks_yaml(resolve_path(DEFAULT_RUNS.parent, runs["tasks_file"]))
    cfg = ty["benchmarks"]["lhtb"]
    root = Path(cfg["data"]).parent
    commit = a.commit or cfg.get("commit")
    ids = selected_ids(ty)
    print(f"目标目录：{root}\ncommit：{commit}\n题目：{'全部' if a.all else ', '.join(ids)}\n")

    if not (root / ".git").exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--filter=blob:none", "--no-checkout", cfg["source"], str(root))
    git("sparse-checkout", "init", "--no-cone", cwd=root)
    paths = ["/README.md", "/LICENSE"] + (["/tasks/"] if a.all else [f"/tasks/{t}/" for t in ids])
    git("sparse-checkout", "set", "--no-cone", *paths, cwd=root)
    if commit:
        if git("cat-file", "-e", f"{commit}^{{commit}}", cwd=root, check=False).returncode != 0:
            git("fetch", "--filter=blob:none", "origin", commit, cwd=root)
        git("-c", "advice.detachedHead=false", "checkout", "--quiet", commit, cwd=root)
    else:
        git("checkout", "--quiet", "origin/HEAD", cwd=root)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    print("\n检查题目目录：")
    problems = 0
    for t in (sorted(p.name for p in (root / "tasks").iterdir() if p.is_dir()) if a.all else ids):
        d = root / "tasks" / t
        missing = [x for x in ("task.toml", "instruction.md", "environment", "tests", "solution") if not (d / x).exists()]
        lfs = [str(f.relative_to(d)) for f in d.rglob("*") if f.is_file() and f.stat().st_size < 300
               and f.read_bytes()[:40].startswith(b"version https://git-lfs")]
        state = "ok" if not missing and not lfs else f"缺少 {missing}" if missing else f"含 {len(lfs)} 个 LFS 占位文件，需 git lfs pull"
        problems += state != "ok"
        print(f"  {'+' if state == 'ok' else '!'} {t:36s} {state}")
    print(f"\n完成：HEAD = {head}" + ("" if not commit or head.startswith(commit) else "（与 tasks.yaml 记录的 commit 不一致！）"))
    print("下一步：python -m eval.prepare --split dev --benchmarks lhtb")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

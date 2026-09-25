"""预先拉取任务所需的 Docker 镜像（在 eval.prepare 之后运行）。

  python -m eval.tools.prefetch_images --split test
  python -m eval.tools.prefetch_images --split dev --benchmarks lhtb
  python -m eval.tools.prefetch_images --split test --dry-run      # 只列出镜像

镜像来源：task.toml 中的 environment.docker_image、verifier.environment.docker_image，
以及 environment/Dockerfile 的 FROM。已存在的镜像跳过。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_tasks_yaml, load_yaml, resolve_path, select_tasks


def images_for(task_dir: Path) -> list[str]:
    imgs = []
    toml = task_dir / "task.toml"
    if toml.exists():
        c = tomllib.loads(toml.read_text())
        imgs += [(c.get("environment") or {}).get("docker_image"),
                 ((c.get("verifier") or {}).get("environment") or {}).get("docker_image")]
    df = task_dir / "environment" / "Dockerfile"
    if df.exists() and not (imgs and imgs[0]):
        imgs += re.findall(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", df.read_text(), re.M)
    return [i for i in imgs if i and "$" not in i]


def exists(img: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.tools.prefetch_images")
    ap.add_argument("--split", required=True, choices=["test", "dev"])
    ap.add_argument("--benchmarks", default="all")
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    runs = load_yaml(DEFAULT_RUNS)
    ty = load_tasks_yaml(resolve_path(DEFAULT_RUNS.parent, runs["tasks_file"]))
    root = resolve_path(DEFAULT_RUNS.parent, runs["task_dirs"])
    need: dict[str, list[str]] = {}
    for t in select_tasks(ty, a.split, a.benchmarks, a.tasks):
        d = root / t.benchmark / t.id
        if not d.exists():
            print(f"  ! {t.key}：任务目录不存在，先运行 python -m eval.prepare")
            continue
        for img in dict.fromkeys(images_for(d)):
            need.setdefault(img, []).append(t.key)
    print(f"共 {len(need)} 个镜像：")
    for img, users in need.items():
        print(f"  {img}  ← {', '.join(users)}")
    if a.dry_run:
        return 0

    failed = []
    for i, img in enumerate(need, 1):
        if exists(img):
            print(f"[{i}/{len(need)}] 已存在 {img}")
            continue
        print(f"[{i}/{len(need)}] 拉取 {img} …", flush=True)
        t0 = time.time()
        rc = subprocess.run(["docker", "pull", img]).returncode
        print(f"      {'完成' if rc == 0 else '失败'}，用时 {time.time() - t0:.0f}s", flush=True)
        if rc != 0:
            failed.append(img)
    if failed:
        print("\n拉取失败：\n  " + "\n  ".join(failed))
    subprocess.run(["docker", "system", "df"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

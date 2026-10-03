"""把 tasks.yaml 中选中的题目准备成 Pier 任务目录：<task_dirs>/<benchmark>/<id>/

  python -m eval.prepare --split dev                 # 准备调试集
  python -m eval.prepare --split dev --benchmarks deepswe
  python -m eval.prepare --split test --force        # 覆盖已存在的目录

DeepSWE、LHTB 是原生 Harbor 格式，直接复制；ProMax / SWE-EVO 调用各自的转换器。
所有任务统一强制 [environment] allow_internet = false：Pier 只会放行 agent 声明的模型 API。
LHTB 另外关闭 continue_until_timeout（它相当于用隐藏评分器当裁判，只允许在上界对照组中开启）；
独立评分容器（verifier.environment_mode = "separate"）Pier 不支持，改为在 agent 容器中评分。
原生格式（LHTB、DeepSWE）的预构建镜像里没有 git 时（例如 python:3.11-slim 上构建的 LHTB 镜像），在本机构建一个只多装了
git 的派生镜像 belay-local/<镜像>:<tag>-git，并把 task.toml 的 docker_image 指向它：Belay 的影子仓库在容器里运行 git。
所有组都用同一个任务目录，所以环境对各组一致；评分不受影响。--no-git 跳过这一步。需要本机有 docker、能联网安装 git。
"""
from __future__ import annotations

import argparse
import importlib
import re
import shutil
import subprocess
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


def use_shared_verifier(toml_path: Path) -> bool:
    """把 verifier.environment_mode = "separate"（独立评分容器）改为在 agent 容器中评分。

    Pier 不启动独立评分容器（LHTB 的 langchain-version-migration 因此报 RewardFileNotFoundError）。
    只在评分镜像与 agent 镜像相同时改写：此时评分脚本依赖的东西 agent 容器里都有；隐藏测试仍只在评分时上传。
    镜像不同则报错，需要单独处理。返回是否做了改写。
    """
    text = toml_path.read_text()
    cfg = tomllib.loads(text)
    ver = cfg.get("verifier", {})
    if ver.get("environment_mode") != "separate":
        return False
    v_img = (ver.get("environment") or {}).get("docker_image")
    a_img = (cfg.get("environment") or {}).get("docker_image")
    if v_img and v_img != a_img:
        raise ValueError(f"独立评分镜像 {v_img} 与 agent 镜像 {a_img} 不同，不能直接改为共用容器评分")
    out, section = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            section = m.group(1).strip()
            if section == "verifier.environment" or section.startswith("verifier.environment."):
                continue
            out.append(line)
            continue
        if section is not None and (section == "verifier.environment" or section.startswith("verifier.environment.")):
            continue
        if section == "verifier" and re.match(r"^\s*environment_mode\s*=", line):
            continue
        out.append(line)
    toml_path.write_text("\n".join(out) + "\n")
    ver = tomllib.loads(toml_path.read_text()).get("verifier", {})
    assert "environment_mode" not in ver and "environment" not in ver, toml_path
    return True


GIT_IMAGE_PREFIX = "belay-local/"
GIT_DOCKERFILE = """FROM {base}
USER root
RUN if command -v git >/dev/null 2>&1; then exit 0; fi; \\
    if command -v apt-get >/dev/null 2>&1; then \\
      apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*; \\
    elif command -v apk >/dev/null 2>&1; then apk add --no-cache git; \\
    elif command -v dnf >/dev/null 2>&1; then dnf install -y git && dnf clean all; \\
    elif command -v yum >/dev/null 2>&1; then yum install -y git && yum clean all; \\
    else echo "no package manager to install git" >&2; exit 1; fi
{user}"""


def _docker(*args: str, input: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    res = subprocess.run(["docker", *args], input=input, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} 失败：{(res.stderr or res.stdout)[-800:]}")
    return res


def git_image_name(base: str) -> str:
    """zli12321/lhtb-x:20260615 → belay-local/zli12321_lhtb-x:20260615-git（没有 tag 时用 latest）。"""
    name, tag = base, "latest"
    last = base.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = base.rsplit(":", 1)
    name = re.sub(r"[^a-z0-9._-]+", "_", name.lower()).strip("_")
    return f"{GIT_IMAGE_PREFIX}{name}:{tag}-git"


def ensure_git_image(toml_path: Path) -> str | None:
    """task.toml 的 docker_image 里没有 git 时，构建只多装 git 的派生镜像并改写 docker_image。返回新镜像名；
    已有 git、已经是派生镜像、或没有 docker_image（从 Dockerfile 构建）时返回 None。"""
    cfg = tomllib.loads(toml_path.read_text())
    base = (cfg.get("environment") or {}).get("docker_image")
    if not base or base.startswith(GIT_IMAGE_PREFIX):
        return None
    if _docker("image", "inspect", base).returncode != 0:
        _docker("pull", base, check=True)
    if _docker("run", "--rm", "--entrypoint", "sh", base, "-c", "command -v git").returncode == 0:
        return None
    image = git_image_name(base)
    if _docker("image", "inspect", image).returncode != 0:
        user = _docker("image", "inspect", "-f", "{{.Config.User}}", base, check=True).stdout.strip()
        dockerfile = GIT_DOCKERFILE.format(base=base, user=f"USER {user}" if user else "")
        _docker("build", "-t", image, "-", input=dockerfile, check=True)
    text = toml_path.read_text()
    new = re.sub(r'(?m)^(\s*docker_image\s*=\s*)["\']' + re.escape(base) + r'["\']', rf'\g<1>"{image}"', text)
    if new == text:
        raise ValueError(f"没能在 {toml_path} 里改写 docker_image = {base}")
    toml_path.write_text(new)
    (toml_path.parent / "belay_git_image.txt").write_text(f"base: {base}\nimage: {image}\n"
                                                          "（eval.prepare 只在其上安装了 git）\n")
    return image


def prepare_one(bm: str, tid: str, bench_cfg: dict, entry: dict, dst: Path, with_git: bool = True) -> str:
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
    if use_shared_verifier(dst / "task.toml"):
        print(f"    {bm}/{tid}: 独立评分容器改为在 agent 容器中评分")
    if with_git and bench_cfg.get("task_format") == "native":
        if shutil.which("docker") is None:
            print(f"    {bm}/{tid}: 本机没有 docker，跳过 git 检查（Belay 需要容器里有 git）")
        else:
            image = ensure_git_image(dst / "task.toml")
            if image:
                print(f"    {bm}/{tid}: 镜像里没有 git，改用派生镜像 {image}")
    return "ok"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.prepare")
    p.add_argument("--runs", default=str(DEFAULT_RUNS))
    p.add_argument("--split", required=True, choices=["test", "dev"])
    p.add_argument("--benchmarks", default="all")
    p.add_argument("--tasks", default="all")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-git", action="store_true", help="不检查、不补装镜像里的 git")
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
                                 entries[(t.benchmark, t.id)], dst, with_git=not a.no_git)
        except Exception as e:
            shutil.rmtree(dst, ignore_errors=True)
            status = f"失败：{e}"
        print(f"  {'+' if status == 'ok' else '!'} {t.key}  {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

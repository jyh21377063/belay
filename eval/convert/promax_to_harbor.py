"""SWE-Bench ProMax → Pier / Harbor 任务目录。

由 `python -m eval.prepare` 调用。生成：

  <dst>/
    task.toml              元数据、超时、不联网、workdir=/testbed
    instruction.md         只含 problem_statement（不含 hints_text）
    environment/Dockerfile FROM 官方镜像；去掉 base commit 之后的 git 历史
    solution/solve.sh      oracle 用：应用参考解（只在 oracle 运行时上传）
    solution/gold.patch
    tests/eval.sh          eval.json 中该题的官方评测脚本，原样保存
    tests/test.sh          调用 eval.sh，按其输出写 /logs/verifier/reward.json

tests/ 只在评分阶段才被 Pier 上传进容器，agent 看不到测试补丁。

官方 eval_script 的判分方式：只运行与测试补丁相关的测试文件 / 包，
以打印出的 OMNIGRIL_EXIT_CODE 为准，0 即通过（不区分 F2P / P2P）。
注意：测试补丁重试后仍无法应用时，官方脚本会打印 OMNIGRIL_TEST_PATCH_APPLY_FAILED=1，
却仍以 OMNIGRIL_EXIT_CODE=0、exit 0 结束——测试根本没跑。test.sh 必须把这种情况判为失败。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

REPO_DIR = "/testbed"


# ---------------------------------------------------------------- 读取数据
@lru_cache(maxsize=4)
def _load_eval_scripts(path: str) -> dict:
    return json.loads(Path(path).read_text())


@lru_cache(maxsize=4)
def _load_instances(path: str) -> dict:
    """兼容 list / {id: 记录} / jsonl 三种形式，统一成 {instance_id: 记录}。"""
    text = Path(path).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = [json.loads(l) for l in text.splitlines() if l.strip()]
    if isinstance(data, dict):
        if all(isinstance(v, dict) for v in data.values()):
            return {v.get("instance_id", k): v for k, v in data.items()}
        data = data.get("data") or data.get("instances") or []
    return {r["instance_id"]: r for r in data}


def _field(rec: dict, *names: str) -> str:
    for n in names:
        if rec.get(n):
            return rec[n]
    raise KeyError(f"数据集记录中没有 {names} 中的任何字段；实际字段：{sorted(rec)}")


def base_commit(eval_script: str) -> str | None:
    """评测脚本在应用测试补丁前 checkout 的 commit，即题目的 base commit。"""
    m = re.search(r"git checkout ([0-9a-f]{40})|BASE=([0-9a-f]{40})", eval_script)
    return (m.group(1) or m.group(2)) if m else None


# ---------------------------------------------------------------- 生成文件
TEST_SH = r"""#!/bin/bash
# 由 eval/convert/promax_to_harbor.py 生成：运行官方评测脚本并写出 reward.json
mkdir -p /logs/verifier
LOG=/logs/verifier/eval.log

bash /tests/eval.sh > "$LOG" 2>&1
script_rc=$?

# 只认行首的标记（set -x 的回显以 "+ " 开头，不会误匹配）
code=$(grep -aoE '^OMNIGRIL_EXIT_CODE=[0-9]+' "$LOG" | tail -n1 | cut -d= -f2)
apply_failed=0
grep -aq '^OMNIGRIL_TEST_PATCH_APPLY_FAILED=1' "$LOG" && apply_failed=1

if [ "$apply_failed" = "0" ] && [ "$code" = "0" ]; then resolved=1; else resolved=0; fi

cat > /logs/verifier/reward.json <<EOF
{"resolved": $resolved, "reward": $resolved, "exit_code": ${code:--1}, "script_rc": $script_rc, "test_patch_apply_failed": $apply_failed}
EOF
cat /logs/verifier/reward.json
exit 0
"""

SOLVE_SH = f"""#!/bin/bash
set -euo pipefail
cd {REPO_DIR}
git -c safe.directory='*' apply --binary --whitespace=nowarn /solution/gold.patch
"""


def dockerfile(image: str, sha: str | None) -> str:
    keep = sha or "HEAD"
    return f"""FROM {image}
WORKDIR {REPO_DIR}

# 去掉 base commit 之后的 git 历史，防止 agent 通过 git log / git show 看到后续提交。
# base commit 及其祖先保留：评测脚本需要 `git checkout <base>` 来恢复测试文件。
RUN set -eux; cd {REPO_DIR}; git config --global --add safe.directory '*'; \\
    git checkout -q --detach; \\
    git tag -f _base {keep}; \\
    git for-each-ref --format='%(refname)' | grep -vx 'refs/tags/_base' | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git gc -q --prune=now; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse _base)" || echo "WARNING: HEAD 与评测脚本的 base commit 不一致"; \\
    rm -f /root/setup_*.sh
"""


def task_toml(tid: str, entry: dict, sha: str | None, image: str) -> str:
    stats = entry.get("stats") or {}
    return f"""schema_version = "1.2"
source = "promax"

[metadata]
benchmark = "promax"
instance_id = "{tid}"
repo = "{entry.get('repo', '')}"
language = "{entry.get('lang', '')}"
image = "{image}"
base_commit = "{sha or ''}"
ref_src_files = {stats.get('src_files', 0)}
ref_loc = {stats.get('loc', 0)}

[agent]
timeout_sec = 10800

[verifier]
timeout_sec = 5400          # burn / fprime 等需要完整编译，给足时间

[environment]
build_timeout_sec = 3600    # 首次拉取镜像可能较慢
allow_internet = false
workdir = "{REPO_DIR}"
"""


def convert(task_id: str, bench_cfg: dict, entry: dict, dst: Path) -> None:
    scripts = _load_eval_scripts(bench_cfg["eval_scripts"])
    if task_id not in scripts:
        raise KeyError(f"eval.json 中没有 {task_id}")
    eval_script = scripts[task_id]["eval_script"]
    if "OMNIGRIL_EXIT_CODE=" not in eval_script:
        raise ValueError(f"{task_id} 的评测脚本不输出 OMNIGRIL_EXIT_CODE，无法判分")

    rec = _load_instances(bench_cfg["data"]).get(task_id)
    if rec is None:
        raise KeyError(f"{bench_cfg['data']} 中没有 {task_id}")
    problem = _field(rec, *bench_cfg.get("instruction_fields", ["problem_statement"]))
    gold = _field(rec, "patch", "gold_patch", "model_patch")

    sha = base_commit(eval_script)
    image = bench_cfg["image_template"].format(id=task_id)

    (dst / "environment").mkdir(parents=True, exist_ok=True)
    (dst / "solution").mkdir(exist_ok=True)
    (dst / "tests").mkdir(exist_ok=True)

    (dst / "instruction.md").write_text(problem.rstrip() + "\n")
    (dst / "task.toml").write_text(task_toml(task_id, entry, sha, image))
    (dst / "environment" / "Dockerfile").write_text(dockerfile(image, sha))
    (dst / "solution" / "gold.patch").write_text(gold if gold.endswith("\n") else gold + "\n")
    (dst / "solution" / "solve.sh").write_text(SOLVE_SH)
    (dst / "tests" / "eval.sh").write_text(eval_script)
    (dst / "tests" / "test.sh").write_text(TEST_SH)
    for p in ("solution/solve.sh", "tests/test.sh", "tests/eval.sh"):
        (dst / p).chmod(0o755)

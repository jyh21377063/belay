"""SWE-EVO → Pier / Harbor 任务目录。

由 `python -m eval.prepare` 调用。评测逻辑复刻官方 SWE-bench 分支（SWE-EVO/SWE-bench）：

  官方流程                                         本转换器
  ─────────────────────────────────────────────   ─────────────────────────────────────────────
  拉取 SWE-Gym 镜像（属于另一道题）                 Dockerfile: FROM 同一镜像
  git reset --hard <base_commit>                   Dockerfile: 构建时 reset，再重建为单一提交
  评测时 pip install（需联网）                      Dockerfile: 构建时执行（评分容器不联网）
  合并 test_patch + 模型补丁，一次 git apply        replay 应用模型补丁 → test.sh 应用 test_patch
  pytest --continue-on-collection-errors ...       test.sh: 同一命令（来自 sweevo_specs.json）
  parse_log_pytest + F2P/P2P + Fix Rate            tests/grade.py: 同样的解析与判分规则

tests/ 与 solution/ 只在评分 / oracle 时进入容器；数据集中的 PRs、end_version_commit
等字段不会写入任何文件。仓库重建为单一提交后，目标版本的提交对象也被删除。

生成：
  task.toml  instruction.md
  environment/Dockerfile  environment/setup_env.sh
  solution/solve.sh  solution/gold.patch
  tests/test.sh  tests/grade.py  tests/test.patch  tests/tests.json
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

REPO_DIR = "/testbed"
SPECS_FILE = Path(__file__).with_name("sweevo_specs.json")
SUPPORTED_PARSERS = {"parse_log_pytest"}


# ---------------------------------------------------------------- 读取数据
@lru_cache(maxsize=2)
def _load_instances(path: str) -> dict:
    p = Path(path)
    if p.suffix == ".arrow":
        import pyarrow as pa
        with pa.memory_map(str(p)) as src:
            try:
                rows = pa.ipc.open_stream(src).read_all().to_pylist()
            except pa.ArrowInvalid:
                rows = pa.ipc.open_file(src).read_all().to_pylist()
    else:
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return {r["instance_id"]: r for r in rows}


@lru_cache(maxsize=1)
def _load_specs() -> dict:
    return json.loads(SPECS_FILE.read_text())


# ---------------------------------------------------------------- 任务描述
def strip_urls(text: str) -> str:
    """删除 release notes 中的链接（PR / issue / 文档），保留链接文字。"""
    text = re.sub(r"\[([^\]]*)\]\(\s*https?://[^)\s]*\s*\)", r"\1", text)   # [文字](url) → 文字
    text = re.sub(r"\(\s*https?://[^)\s]*\s*\)", "", text)                   # (url) → 空
    text = re.sub(r"<?https?://[^\s>)\]]+>?", "", text)                      # 裸 url
    text = re.sub(r"[ \t]+([.,;])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def instruction(rec: dict) -> str:
    notes = strip_urls(rec["problem_statement"]).strip()
    return (f"The repository at {REPO_DIR} contains {rec['repo']} at version {rec['start_version']}.\n"
            f"Below are the release notes for version {rec['end_version']}. "
            f"Implement all changes they describe in the codebase.\n\n"
            f"<release_notes>\n{notes}\n</release_notes>\n")


# ---------------------------------------------------------------- 环境
def setup_env_sh(rec: dict, spec: dict) -> str:
    install = "\n".join(spec["install"] + spec["hot_fix"])
    base = rec["base_commit"]
    return f"""#!/bin/bash
# 由 eval/convert/sweevo_to_harbor.py 生成；在 docker build 时执行（此时可以联网）
set -uxo pipefail
source /opt/miniconda3/bin/activate && conda activate testbed
cd {REPO_DIR}
git config --global --add safe.directory '*'

# 1. 切换到 SWE-EVO 的起始版本（镜像原本属于另一道 SWE-Gym 题）
git reset --hard {base} || {{ echo "reset 到 base_commit 失败"; exit 1; }}
BASE_TREE=$(git rev-parse '{base}^{{tree}}')

# 2. 官方评测脚本中的安装步骤（spec {spec['spec_version']}），允许部分失败，与官方一致
{install}

# 3. 重建为单一提交：删除全部历史（包括目标版本 {rec['end_version']} 的提交）
set -e
git checkout -q --orphan belay-base
git -c user.name=belay -c user.email=belay@localhost commit -q --allow-empty -m "{rec['repo']} {rec['start_version']}"
git for-each-ref --format='%(refname)' | grep -vx 'refs/heads/belay-base' | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git gc -q --prune=now
test "$(git rev-parse 'HEAD^{{tree}}')" = "$BASE_TREE"
test "$(git rev-list --all | wc -l)" = "1"
"""


DOCKERFILE = """FROM {image}
WORKDIR {repo}
# 让 agent 默认使用评测所用的 conda 环境
ENV PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:$PATH
COPY setup_env.sh /tmp/belay_setup_env.sh
RUN bash /tmp/belay_setup_env.sh && rm -f /tmp/belay_setup_env.sh
"""


# ---------------------------------------------------------------- 评分
def test_sh(spec: dict) -> str:
    eval_cmds = "\n  ".join(spec["eval_commands"]) or ":"
    return f"""#!/bin/bash
# 由 eval/convert/sweevo_to_harbor.py 生成：应用测试补丁 → 运行测试 → 按官方规则判分
set -uo pipefail
mkdir -p /logs/verifier
LOG=/logs/verifier/test_output.txt
: > "$LOG"
source /opt/miniconda3/bin/activate && conda activate testbed
cd {REPO_DIR}

applied=0
if git apply -v /tests/test.patch >> "$LOG" 2>&1; then
  applied=1
elif patch --batch --fuzz=5 -p1 -i /tests/test.patch >> "$LOG" 2>&1; then
  applied=1
fi

if [ "$applied" = 1 ]; then
  {eval_cmds}
  echo ">>>>> Start Test Output" >> "$LOG"
  {spec['test_cmd']} >> "$LOG" 2>&1
  echo ">>>>> End Test Output" >> "$LOG"
fi

python /tests/grade.py "$LOG" /tests/tests.json "$applied" /logs/verifier/reward.json
exit 0
"""


GRADE_PY = r'''"""判分：逐项复刻 SWE-EVO 官方实现（兼容 Python 3.6+）。

- 解析：swebench/harness/log_parsers/python_swegym.py::parse_log_pytest
- 单个测试：PASSED / XFAIL 为成功；缺失或 FAILED / ERROR 为失败；其余（如 SKIPPED）两边都不计
- resolved：F2P 与 P2P 的成功率都为 1（空列表视为 1）
- fix_rate：P2P 无失败时为 F2P 成功率，否则为 0（SWE-EVO/SWE-bench/evaluate_instance.py）
"""
import json
import sys

STATUSES = ("FAILED", "PASSED", "SKIPPED", "ERROR", "XFAIL")
START, END = ">>>>> Start Test Output", ">>>>> End Test Output"


def parse_log_pytest(log):
    status = {}
    for line in log.split("\n"):
        if any(line.startswith(s) for s in STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) <= 1:
                continue
            status[parts[1]] = parts[0]
    return status


def split(tests, sm):
    ok, bad = 0, 0
    for t in tests:
        if t in sm and sm[t] in ("PASSED", "XFAIL"):
            ok += 1
        elif t not in sm or sm[t] in ("FAILED", "ERROR"):
            bad += 1
    return ok, bad


def rate(ok, bad):
    return 1.0 if ok + bad == 0 else ok / (ok + bad)


def main(log_path, tests_path, applied, out_path):
    tests = json.load(open(tests_path))
    log = open(log_path, errors="replace").read()
    reward = {"resolved": 0, "reward": 0, "fix_rate": 0.0, "test_patch_applied": int(applied),
              "f2p_success": 0, "f2p_failure": len(tests["FAIL_TO_PASS"]),
              "p2p_success": 0, "p2p_failure": len(tests["PASS_TO_PASS"])}
    if applied == "1" and START in log and END in log:
        sm = parse_log_pytest(log.split(START)[1].split(END)[0])
        f_ok, f_bad = split(tests["FAIL_TO_PASS"], sm)
        p_ok, p_bad = split(tests["PASS_TO_PASS"], sm)
        resolved = int(rate(f_ok, f_bad) == 1 and rate(p_ok, p_bad) == 1)
        reward.update(resolved=resolved, reward=resolved,
                      fix_rate=round(rate(f_ok, f_bad) if p_bad == 0 else 0.0, 6),
                      f2p_success=f_ok, f2p_failure=f_bad, p2p_success=p_ok, p2p_failure=p_bad)
    json.dump(reward, open(out_path, "w"))
    print(json.dumps(reward))


if __name__ == "__main__":
    main(*sys.argv[1:5])
'''

SOLVE_SH = f"""#!/bin/bash
set -euo pipefail
cd {REPO_DIR}
git apply --binary --whitespace=nowarn /solution/gold.patch || patch --batch --fuzz=5 -p1 -i /solution/gold.patch
"""


def task_toml(tid: str, rec: dict, spec: dict, image: str) -> str:
    return f"""schema_version = "1.2"
source = "swe_evo"

[metadata]
benchmark = "swe_evo"
instance_id = "{tid}"
repo = "{rec['repo']}"
start_version = "{rec['start_version']}"
end_version = "{rec['end_version']}"
image = "{image}"
spec_version = "{spec['spec_version']}"
f2p = {len(rec['FAIL_TO_PASS'])}
p2p = {len(rec['PASS_TO_PASS'])}

[agent]
timeout_sec = 10800

[verifier]
timeout_sec = 5400          # dask 约 3500 个测试，给足时间

[environment]
build_timeout_sec = 3600    # 构建时要执行 pip install 与 git gc
allow_internet = false
workdir = "{REPO_DIR}"
"""


def convert(task_id: str, bench_cfg: dict, entry: dict, dst: Path) -> None:
    rec = _load_instances(bench_cfg["data"]).get(task_id)
    if rec is None:
        raise KeyError(f"{bench_cfg['data']} 中没有 {task_id}")
    spec = _load_specs().get(task_id)
    if not spec or "error" in spec:
        raise KeyError(f"sweevo_specs.json 中没有 {task_id} 的可用配置：{spec}")
    if spec["log_parser"] not in SUPPORTED_PARSERS:
        raise NotImplementedError(f"{task_id} 使用 {spec['log_parser']}，grade.py 尚未移植该解析器")
    image = entry.get("image") or rec["image"]

    for sub in ("environment", "solution", "tests"):
        (dst / sub).mkdir(parents=True, exist_ok=True)
    files = {
        "task.toml": task_toml(task_id, rec, spec, image),
        "instruction.md": instruction(rec),
        "environment/Dockerfile": DOCKERFILE.format(image=image, repo=REPO_DIR),
        "environment/setup_env.sh": setup_env_sh(rec, spec),
        "solution/gold.patch": rec["patch"] if rec["patch"].endswith("\n") else rec["patch"] + "\n",
        "solution/solve.sh": SOLVE_SH,
        "tests/test.patch": rec["test_patch"] if rec["test_patch"].endswith("\n") else rec["test_patch"] + "\n",
        "tests/tests.json": json.dumps({"FAIL_TO_PASS": rec["FAIL_TO_PASS"],
                                        "PASS_TO_PASS": rec["PASS_TO_PASS"]}, indent=1),
        "tests/test.sh": test_sh(spec),
        "tests/grade.py": GRADE_PY,
    }
    for rel, text in files.items():
        (dst / rel).write_text(text)
    for rel in ("environment/setup_env.sh", "solution/solve.sh", "tests/test.sh"):
        (dst / rel).chmod(0o755)

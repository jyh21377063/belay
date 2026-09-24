# Belay

长程编码任务的外部任务图 runtime，以及配套的评测框架。设计见 `docs/long_horizon_runtime_plan.md`，选题见 `task_selection.md`。

## 目录

```
<父目录>/
├── benchmarks/        原始数据集（只读，不进 git）
└── belay/             本仓库
    ├── tasks.yaml     题目清单（冻结）
    ├── runs.yaml      运行编排：agent 定义 + profile
    ├── belay/         runtime 本体（开发中）
    ├── eval/          评测框架，入口 python -m eval.run
    ├── build/tasks/   eval.prepare 生成的 Pier 任务目录（gitignore）
    └── results/       运行结果（gitignore）
```

`runs.yaml` 和 `tasks.yaml` 中的相对路径都相对于文件自身所在目录解析，整个父目录放在服务器任意位置都可以。

## 环境准备

服务器上只需要 Docker 和 Python，**不需要安装 Claude Code**：Pier 会在每道题的容器里自动安装，
并通过 `runs.yaml` 中的环境变量把模型指向 DeepSeek。

```bash
cd belay
python -m venv .venv && source .venv/bin/activate
pip install datacurve-pier pyyaml      # Pier 必须装在本 venv 里，自定义 agent 通过 import_path 加载
export DEEPSEEK_API_KEY=...
```

然后填写 `runs.yaml` 中 claude-code 的 `model`（DeepSeek 模型名）和 `kwargs.version`（Claude Code 版本号，可先删掉这一行）。

## 运行

以下命令都在 `belay/` 下执行。

```bash
python -m eval.prepare --split dev                        # 生成任务目录（DeepSWE 可直接用）
python -m eval.run --profile gold-check -y                # 环境验证：oracle 两次通过、nop 失败
python -m eval.run --profile cc-pilot --tasks koota-query-predicates   # 先跑一道
python -m eval.run --profile cc-pilot -y                  # 调试集全部
python -m eval.run --profile cc-test -y                   # 评测集，A 组
python -m eval.run --profile gold-check --split test      # 验证评测集题目环境
python -m eval.run --profile regrade --source-run <run_id>  # 对已有补丁重新评分
python -m eval.run --profile cc-pilot --dry-run           # 只打印计划与 Pier 配置
python -m eval.report results/<run_id>                    # 重新生成汇总
```

- `--profile` 决定 agent、split、重复次数；`--tasks`、`--benchmarks`、`--agent`、`--model`、`--repeats` 可临时覆盖。
- 结果写到 `results/<run_id>/<benchmark>/<id>/<repeat>/`，汇总在 `results/<run_id>/summary.md` 和 `summary.csv`。
- 同一命令重跑即断点续跑，已完成的 trial 自动跳过。

## 评测流程

每个 trial 分两个阶段，以 `patch.diff` 为边界：

1. **agent 阶段**：Pier 启动题目容器，运行 agent；结束时 `eval/agents/patch_capture.py` 导出补丁（超时也会导出）。
2. **评分阶段**：在全新容器中用 `eval/agents/replay.py` 应用补丁，运行题目自带的 verifier。

所有对比组走同一条评分路径。`inline_verify` 开启时，agent 结束后也会在原容器评分一次；两次结果不一致会在汇总表中标为 `inline≠replay`。

防泄漏：`eval.prepare` 把所有任务设为 `allow_internet = false`，Pier 只放行 agent 声明的模型 API 域名；运行前检查会拒绝允许联网的任务。

## eval 模块

| 文件 | 职责 |
|---|---|
| `eval/run.py` | 命令行入口 |
| `eval/config.py` | defaults ← profile ← 命令行 合并；选题与校验（纯函数） |
| `eval/runner.py` | 运行前检查、trial 循环、断点续跑、agent 阶段与重放评分 |
| `eval/pier_backend.py` | 唯一调用 Pier 的模块：生成 JobConfig、调用 `pier run -c`、解析 result.json |
| `eval/prepare.py` | 生成 Pier 任务目录，强制不联网 |
| `eval/report.py` | 生成 summary.csv / summary.md |
| `eval/convert/` | ProMax、SWE-EVO 转 Pier 格式（待实现） |
| `eval/agents/patch_capture.py` | 快照 + 导出 patch.diff |
| `eval/agents/claude_code.py` | A 组：Pier 的 ClaudeCode + 补丁导出 |
| `eval/agents/flat_agent.py` | B 组：自研执行器（骨架） |
| `eval/agents/replay.py` | 评分用：在全新容器中应用补丁 |

## 待办

- [ ] `eval/convert/promax_to_harbor.py`、`sweevo_to_harbor.py`：instruction.md 只写 problem_statement；SWE-EVO 的单提交重建写进 Dockerfile；`tests/test.sh` 写 `/logs/verifier/reward.json`（`{"resolved": 0|1, "fix_rate": x}`）
- [ ] `eval/agents/belay_agent.py`：C/D 组接入
- [ ] 填写 `task_selection.md` 4.3 节的实测难度

# Belay

长程编码任务的任务状态图 runtime，以及配套的评测框架。runtime 的实现设计见 `docs/design.md`，选题见 `task_selection.md`。

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
pip install datacurve-pier pyyaml anthropic pytest   # Pier 必须装在本 venv 里，自定义 agent 通过 import_path 加载
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

## belay 模块（v6：长程单 worker 的可靠性底座）

设计见 [docs/design.md](docs/design.md)，目录与依赖规则见 [docs/architecture.md](docs/architecture.md)。
一条只追加的事件日志是唯一真相；任务图、执行状态、存档链三个视图由纯函数从日志推出；runtime 是唯一写者。
循环跑在宿主机进程里，工具经由 `Env` 在任务容器中执行；容器里只需要 bash、coreutils、git 与 python3。

agent 只管写代码：runtime 在工具边界自动拍快照，在验证槽位里用原始测试验证并推进一条两级存档链（related 通过是暂存点，
全量通过是确认点，交付最新的确认点）；被拒或降级时先用快照二分定位，再让诊断者解释；需求账本与复查者防止漏做和提前结束；
交接落在步骤边界，开场上下文分层且有界；全部状态以事件日志和 git bundle 保存在宿主机上，会话、runtime 进程、容器都可以
随时被替换（内存重试、读盘重放、`resume --rebuild`）。

| 路径 | 职责 |
| --- | --- |
| `belay/core/` | **纯函数核心**：事件（`events`）、三个视图（`model`）、推导函数（`reduce`）、状态转换规则（`rules`）、验证规则（`verify`）、不变量（`invariants`）、调度建议（`suggest`）、上下文构建（`context`）、压缩规则 L0–L2（`compact`）、规划校验（`plan`）、副作用计划（`effects`）、文字渲染（`render`）、配置（`config`） |
| `belay/runtime/` | **命令式外壳**：事件存储（`store`）、唯一写者（`runtime`）、影子仓库与 CAS（`gitops`）、作业与验证器（`verifier`）、会话循环与 L0–L4（`session`）、工具接口（`port`）、规划器（`planner`）、运行驱动（`driver`）、重启对账（`recovery`）、提示词（`prompts`） |
| `belay/tools/` | 通用工具（文件、bash、todo、explore）与 Belay 工具（`belay.py`：board / task / claim / release / add_task / note / step_done / checkpoint / ready_for_review / report_blocked / run_check / wait / failure_log / revert_change / history / rollback） |
| `belay/worker/` | B 组的 worker（同一套工具，不用图），也用来跑只读探索子 agent |
| `belay/env.py`、`belay/llm.py` | 执行环境（Local / Docker / Pier）；模型客户端（重试、thinking、录制与回放、ScriptedLLM） |
| `belay/container/runner.py` | 容器内的检查运行器（标准库）：把候选树增量导出到验证槽位（或降级时临时切换工作区）跑测试与命令检查、解析结果、导入隔离探针 |
| `belay/cli.py` | 本地调试入口：`run` / `resume` / `ledger` / `flat` |

```bash
python -m pytest -q                                                    # 全部测试，不需要容器和模型
python -m belay.cli run --workdir /path/to/repo --task-file task.md --gate gate.json --run-dir runs/x
python -m belay.cli run --docker <容器> --workdir /testbed --task-file task.md --gate gate.json --run-dir runs/x
python -m belay.cli run ... --replay runs/x/llm_record.jsonl            # 回放录制的模型回复，不调用模型
python -m belay.cli resume --run-dir runs/x                             # runtime 崩溃后：重放 → 对账 → 继续
python -m belay.cli resume --run-dir runs/x --rebuild [--docker <新容器>] # 容器 / 工作区丢了：从 git bundle 重建
python -m belay.cli ledger --run-dir runs/x                             # 从事件库重放出账本
python -m belay.cli flat --workdir /path/to/repo --task-file task.md    # B 组
```

运行目录：`events.sqlite`（事件与视图快照，唯一真相）、`events.jsonl`（同内容，便于阅读）、`sessions/S*.jsonl`
（每个会话的完整对话与消息轨迹，用于审计与读盘重放）、`git/<m>.bundle`（影子仓库的增量镜像，用于重建）、`blobs/`
（大工具输出、diff、压缩后的消息）、`checkpoints/<k>.diff`（每个存档的补丁镜像，给人看）、`deliverable.diff`
（交付物 = 最新的确认点）、`worktree.diff`（结束时工作区的完整改动）、`ledger.json` / `ledger.md`（六类口径）。

> eval 适配（`eval/agents/belay_agent.py`）仍指向 v4 的旧 runtime，接评测时按 `belay.runtime.driver.BelayRun` 重写。

## 待办

- [ ] `eval/convert/promax_to_harbor.py`、`sweevo_to_harbor.py`：instruction.md 只写 problem_statement；SWE-EVO 的单提交重建写进 Dockerfile；`tests/test.sh` 写 `/logs/verifier/reward.json`（`{"resolved": 0|1, "fix_rate": x}`）
- [ ] `eval/agents/belay_agent.py`：按 v5 的 `BelayRun` 重新接入
- [ ] 多 worker（可选）：每个 worker 一个 worktree、存档 = 三方合并再验证
- [ ] 填写 `task_selection.md` 4.3 节的实测难度

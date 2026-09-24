# 评测任务集说明

> 冻结日期：2026-09-24 · 对应文件：`tasks.yaml`（题目清单）、`runs.yaml`（运行编排）

## 1. 总览

本项目关注**长程编码任务**：需要在大型仓库中大量阅读代码、跨多个文件协调修改、持续较长时间。为此从三个 2025–2026 年发布的 benchmark 中选取了 27 道题，全部基于 Docker 镜像运行，评估为确定性的测试执行，不消耗 LLM token。

| 数据集 | 任务类型 | 题库规模 | 评测集 | 调试集 | 判分方式 | 公开最好成绩 |
|---|---|---|---|---|---|---|
| SWE-Bench ProMax | 大规模多文件重构 | 170 题 · 7 种语言 | 8 | 2 | 测试套件全部通过 | 41.2%（GPT-5.2 + OpenHands） |
| DeepSWE | 在已有仓库上实现原创大功能 | 113 题 · 5 种语言 | 12 | 2 | 手写功能测试 + 回归测试 | 70.0%（GPT-5.5） |
| SWE-EVO | 按 release notes 完成一次版本升级 | 48 题 · Python | 2 | 1 | F2P + P2P 全过；另报 Fix Rate | 25.0%（GPT-5.4） |
| **合计** | | | **22** | **5** | | |

- **评测集**：最终对比结果只来自这些题。机制设计阶段不查看其运行轨迹。
- **调试集**：开发 runtime 时反复运行、查看失败轨迹、调整机制使用。结果不进入最终报告。

### 三个数据集各自的作用

- **ProMax**：重构任务的主要失败模式是"改不全"，即 agent 修改的文件数少于实际需要。这直接对应本项目要解决的"提前宣布完成"问题。改动分散在多个顶层目录的题，也能用来检验任务图的并行能力。
- **DeepSWE**：题目为人工原创、从未合并回上游仓库，不存在训练数据污染。prompt 简短，agent 必须自行探索仓库、确定修改位置。评分使用手写功能测试，只要行为正确就通过，不要求特定的实现方式。
- **SWE-EVO**：一道题对应一个完整的版本升级，release notes 里通常包含多条相互独立的改动。评分只看最终仓库状态，如何拆分子任务完全由 agent 决定，最能体现外部任务图的价值。

## 2. 选题原则

所有规则在运行任何实验前固定，选题只依据题目本身的属性，不依据任何 agent 的运行结果。

1. **长程**：参考解需修改较多文件。ProMax 要求非测试源文件 ≥ 10；DeepSWE 在同语言内优先选改动文件多的题；SWE-EVO 要求改动文件 ≥ 14。
2. **评分覆盖充分**（SWE-EVO）：F2P 测试 ≥ 10 且与改动规模相称，确保评分覆盖多个子改动，Fix Rate 有区分度。
3. **多样性**：评测集中同一仓库至多 1 道（SWE-EVO 因仓库数量有限，至多 2 道同仓库题，最终未出现同仓库题）；尽量覆盖所有语言。
4. **可运行性**：排除构建或测试极重的超大仓库（kubernetes、bazel、hbase、iceberg、pinot、angular、transformers 等），以适配 8 vCPU / 32 GiB 的实验机器。
5. **调试集与评测集同分布**：调试题规模与评测题相当，且来自评测集中未出现的仓库。
6. **参考解验证**：每道题须满足"参考解连续两次通过、空补丁失败"；不满足的按备选顺序替换，并在 `tasks.yaml` 中记录原因。

## 3. 题目明细

### 3.1 SWE-Bench ProMax（8 + 2）

| 集合 | 题目 | 语言 | 仓库 | 源文件 | 改动行数 | 顶层目录 | 测试文件 |
|---|---|---|---|---:|---:|---:|---:|
| 评测 | `google__langextract-239` | Python | google/langextract | 19 | 1728 | 10 | 12 |
| 评测 | `stanfordnlp__dspy-9193` | Python | stanfordnlp/dspy | 13 | 1829 | 6 | 4 |
| 评测 | `OpenListTeam__OpenList-1001` | Go | OpenListTeam/OpenList | 46 | 850 | 37 | 2 |
| 评测 | `go-gitea__gitea-35775` | Go | go-gitea/gitea | 24 | 1356 | 5 | 10 |
| 评测 | `ant-design__ant-design-53739` | TypeScript | ant-design/ant-design | 22 | 234 | 13 | 6 |
| 评测 | `nacos__alibaba-13142` | Java | alibaba/nacos | 46 | 889 | 7 | 9 |
| 评测 | `nasa__fprime-3642` | C++ | nasa/fprime | 32 | 1575 | 24 | 14 |
| 评测 | `tracel-ai__burn-4337` | Rust | tracel-ai/burn | 49 | 1954 | 12 | 2 |
| 调试 | `TecharoHQ__anubis-1308` | Go | TecharoHQ/anubis | 16 | 1451 | 9 | 5 |
| 调试 | `roboflow__supervision-1943` | Python | roboflow/supervision | 11 | 861 | 7 | 2 |

- **源文件**：参考解修改的非测试文件数；**改动行数**：参考解的增删行数合计；**顶层目录**：改动分布在多少个不同的顶层目录，越大越有利于并行拆分；**测试文件**：评测用测试补丁涉及的文件数。
- 评测集覆盖全部 7 种语言中的 6 种（C 语言候选题集中在少数仓库，未选入）。
- 两种典型形态：**宽而深**（如 burn-4337、langextract-239，读得多、改得多）与**宽而浅**（如 ant-design-53739，把同一改动铺到十几个组件，考验是否漏改）。

### 3.2 DeepSWE（12 + 2）

| 集合 | 题目 | 语言 | 上游仓库 | 改动文件 | 改动行数 |
|---|---|---|---|---:|---:|
| 评测 | `scriggo-method-declarations` | Go | open2b/scriggo | 18 | 872 |
| 评测 | `arcane-drift-detection-baselines` | Go | getarcaneapp/arcane | 14 | 1086 |
| 评测 | `participle-grammar-conflict-analysis` | Go | alecthomas/participle | 14 | 767 |
| 评测 | `testem-bail-on-test-failure` | JavaScript | testem/testem | 18 | 616 |
| 评测 | `yjs-map-conflict-detection` | JavaScript | yjs/yjs | 8 | 656 |
| 评测 | `python-statemachine-state-data-scoping` | Python | fgmacedo/python-statemachine | 17 | 554 |
| 评测 | `narwhals-rolling-window-suite` | Python | narwhals-dev/narwhals | 11 | 816 |
| 评测 | `sqlfmt-create-table-ddl-formatting` | Python | tconbeer/sqlfmt | 10 | 757 |
| 评测 | `wasmi-trap-coredumps` | Rust | wasmi-labs/wasmi | 11 | 645 |
| 评测 | `boa-hierarchical-evaluation-cancellation` | Rust | boa-dev/boa | 7 | 575 |
| 评测 | `dynamodb-toolbox-conditional-attribute-requirements` | TypeScript | dynamodb-toolbox/dynamodb-toolbox | 36 | 669 |
| 评测 | `valibot-recursive-schema-composition` | TypeScript | open-circle/valibot | 22 | 837 |
| 调试 | `wazero-multi-module-snapshots` | Go | wazero/wazero | 14 | 555 |
| 调试 | `koota-query-predicates` | TypeScript | pmndrs/koota | 17 | 877 |

- 5 种语言各至少 2 道，12 道题来自 12 个不同的上游仓库。
- DeepSWE 各题参考解的改动行数集中在 450–1650 行，差异主要体现在文件数上。其"长程"更多来自 prompt 简短、需要自行探索。
- Rust 题目整体规模偏小，所选两道是该语言中改动文件最多的。

### 3.3 SWE-EVO（2 + 1）

| 集合 | 题目 | 仓库 | 版本升级 | 改动文件 | F2P 测试 |
|---|---|---|---|---:|---:|
| 评测 | `iterative__dvc_0.52.1_0.53.1` | iterative/dvc | 0.52.1 → 0.53.1 | 42 | 132 |
| 评测 | `dask__dask_2023.6.0_2023.6.1` | dask/dask | 2023.6.0 → 2023.6.1 | 33 | 105 |
| 调试 | `conan-io__conan_2.0.2_2.0.3` | conan-io/conan | 2.0.2 → 2.0.3 | 48 | 8 |

- 使用 **release-note only** 设置：agent 只看到官方 release notes，不附带关联 PR / issue 的原文。这是论文中难度更高的设置。
- 两道评测题的 F2P 均超过 100 个。预计两组都难以完全解决，因此以 **Fix Rate**（通过的 F2P 比例；任一 P2P 回归则记 0）作为主要指标。
- 未选入的高规模题及原因：`dvc_0.33.1_0.34.0`（29 个文件，但 F2P 仅 1 个，评分覆盖不足）、两道 scikit-learn（F2P 均为 1）、`dask_2024.1.0_2024.1.1`（F2P 达 2774 个，与改动规模不相称）、`pydantic_v2.7.0_v2.7.1`（关联 PR 20 个，所在仓库的平均解决率接近 0）。

## 4. 难度参考

### 4.1 数据集整体难度（来自各论文 / 排行榜）

| 数据集 | 单题平均规模 | 公开结果 |
|---|---|---|
| ProMax | 平均修改 11.4 个源文件、261.6 行 | 最好 41.2%；Claude Sonnet 4.6 为 38.8%（OpenHands，每题平均 117.9 步、$4.77） |
| DeepSWE | 参考解代码量约为 SWE-Bench Pro 的 5.5 倍 | GPT-5.5 为 70.0%，Claude Opus 4.7 为 54.2% |
| SWE-EVO | 平均修改 20.9 个文件、610.5 行，每题平均 874 个测试 | 最好 25.0%；约 64% 的题没有任何模型解出；dvc、dask 仓库的平均解决率分别约为 4%–9% 和 2.6% |

### 4.2 本任务集的规模分布

| 数据集 | 评测题改动文件数 | 评测题改动行数 |
|---|---|---|
| ProMax | 13 – 49（中位数 28） | 234 – 1954 |
| DeepSWE | 7 – 36（中位数 14） | 554 – 1086 |
| SWE-EVO | 33 – 42 | — |

### 4.3 实测难度（待填）

以下数据在环境验证完成后，由 A 组（Claude Code）实际运行得到，用于标定每道题的真实耗时与难度。

| 数据集 | 题目 | 墙钟时间 | Token 用量 | 结果 / Fix Rate |
|---|---|---|---|---|
| … | … | 待测 | 待测 | 待测 |

## 5. 防泄漏措施

| 风险 | 措施 |
|---|---|
| ProMax 的 `hints_text` 由参考答案生成，会直接点出需要修改的函数 | 只向 agent 提供 `problem_statement` |
| SWE-EVO 的 release notes 中每条改动都附带 GitHub PR 链接，打开即可看到修复 diff | 转换时删除问题描述中的全部 URL |
| 复用的 SWE-bench 系列镜像可能保留目标版本之后的 git 历史 | 容器启动后将仓库重建为只含一个提交的干净状态 |
| agent 联网后可能检索上游仓库的后续提交 | 使用 Pier 的网络白名单，仅放行模型 API |
| agent 修改测试文件或在容器中残留状态影响评分 | 只保存 agent 的最终补丁，在全新容器中重放评分 |
| 数据集文件、参考解、测试补丁进入容器 | 这些文件只在宿主机读取，不挂载、不复制进 agent 容器 |

## 6. 备选题与替换规则

某道题参考解验证不通过、或单次评估耗时过长时，按下列顺序替换，替换原因记录在 `tasks.yaml` 的 `replacements` 中。

| 数据集 | 被替换题的语言 / 仓库 | 备选顺序 |
|---|---|---|
| ProMax | C++ | `LMMS__lmms-7454` |
| ProMax | Rust | `astral-sh__ruff-20221` |
| ProMax | Go | `gitleaks__gitleaks-1831` |
| ProMax | Python | `confident-ai__deepeval-c_08844c8` |
| ProMax | Java | `plantuml__plantuml-c_15fa06c` |
| ProMax | TypeScript | `ant-design__ant-design-52470` |
| DeepSWE | Go | `expr-try-catch-errors` → `helm-unified-manifest-stream` |
| DeepSWE | Python | `bandit-interprocedural-taint-checks` |
| DeepSWE | TypeScript / JavaScript | `drizzle-orm-window-function-builders` |
| SWE-EVO | 任一 | `iterative__dvc_3.43.1_3.44.0` |

## 7. 数据来源

- SWE-Bench ProMax：<https://huggingface.co/datasets/swe-bench-promax/SWE-Bench-ProMax> · 论文 arXiv:2608.09802
- DeepSWE：<https://github.com/datacurve-ai/deep-swe> · 论文 arXiv:2607.07946
- SWE-EVO：<https://huggingface.co/datasets/Fsoft-AIC/SWE-EVO> · 论文 arXiv:2512.18470

题目规模数据（文件数、行数、F2P 数）由本项目脚本从各数据集的参考解和测试列表统计得到。

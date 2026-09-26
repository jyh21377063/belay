# 评测任务集说明（第 2 版）

> 修订日期：2026-09-25 · 对应文件：`tasks.yaml`（题目清单，version 2）、`runs.yaml`（运行编排）
> 第 1 版（ProMax 8 + DeepSWE 12 + SWE-EVO 2）的修订原因见第 1 节与 `tasks.yaml` 的 `revisions`。

## 1. 总览

pilot 表明，Claude Code + DeepSeek V4 Flash 在 11–17 分钟内就能解出第 1 版中的中等题（supervision、koota），这类题无法体现长程任务的问题。第 2 版按 Belay v3 方案重选：正式集只保留预计需要数十分钟以上、包含多条需求或多个步骤的题目，统一 90 分钟预算。

| 集合 | SWE-EVO | ProMax | LHTB | 负对照 | 合计 |
|---|---:|---:|---:|---:|---:|
| 正式集 | 7 | 3 | 4 | 2（ProMax 1、DeepSWE 1） | **16** |
| 调试集 | 2 | — | 1 | — | **3** |

- **正式集**：最终结论只来自这里。机制设计阶段不查看其运行轨迹（`eval.analyze` 默认拒绝分析）。
- **调试集**：开发机制、调参时反复运行。
- **负对照**：pilot 中 Claude Code 已解出的两道题，用来确认 runtime 在简单任务上没有收益、开销可见。
- **预算**：所有组、所有题每次运行的 agent 阶段不超过 90 分钟（`runs.yaml` 的 `timeout_min`），覆盖各题 `task.toml` 中原有的超时（LHTB 原为 1–6 小时）。

### 各数据集的作用

- **SWE-EVO**（主体）：一道题对应一次版本升级，release notes 列出数十条相互独立的改动。直接考验错误完成、回归、误解需求，以及可分解任务的并发。
- **ProMax**：大规模多文件重构，主要失败模式是改不全与回归。选改动最多、构建最重的三道。
- **LHTB**：真实终端环境中的长程任务，隐藏评分器按产物重建后评分，得分是 0–1 的连续值。覆盖三种计分方式（按比例、先要求正确再按比例、二元），并包含长时间编译等 SWE-EVO 没有的情形。
- **DeepSWE**：pilot 中最大的题也只需 17 分钟，正式集不再选用，只保留 koota 作为负对照。

## 2. 选题原则

所有规则只依据题目本身的属性与 pilot 的实测耗时，不依据任何 runtime 的运行结果。

1. **足够长**：按 pilot 实测的比例（每个改动文件约 8.2 轮、每轮约 7.6 秒）估算，Claude Code 需要约 30 分钟以上；LHTB 以专家估时（1.5–8 小时）为准。不再设改动文件数的上限：超出 90 分钟预算的题正好用来检验超时时的交付。
2. **评分覆盖充分**（SWE-EVO）：F2P ≥ 5，且分布在多个测试文件中。modin、requests、scikit-learn 的所有题都不满足（F2P 分别不超过 4、4、1），因此不选用。
3. **是写代码的任务**（LHTB）：只从软件工程与系统类中选；游戏谜题、多模态、领域科学审计、论文复现（需联网）、APEX 专业工作流均不选。
4. **离线可评**：评分器隐藏且确定，不需要 GPU 或联网。所有任务在 prepare 时强制禁止联网。
5. **同仓库限制**：正式集中同一仓库至多 3 道（SWE-EVO 的 48 题里 dvc 占 26 道，所选三道的版本相差很远：0.52、1.0 alpha、2.8）。
6. **参考解验证**：每道题须满足"参考解连续两次通过、空补丁失败"（gold-check）；不满足的按第 6 节的备选顺序替换，并在 `tasks.yaml` 的 `replacements` 中记录原因。

## 3. 题目明细

### 3.1 SWE-EVO（正式 7 道）

| 题目 | 改动文件（代码） | release notes 条目 | F2P（测试文件数） | P2P | 预计 agent 用时 |
|---|---:|---:|---|---:|---:|
| `iterative__dvc_1.0.0a1_1.0.0a2` | 107（105） | 34 | 68（18） | 242 | 超出 90 min |
| `conan-io__conan_2.0.14_2.0.15` | 78（77） | 43 | 72（5） | 649 | 80 min |
| `iterative__dvc_2.8.1_2.8.2` | 79（69） | 30 | 133（28） | 662 | 71 min |
| `iterative__dvc_0.52.1_0.53.1` | 42（41） | 12 | 132（13） | 0 | 42 min |
| `dask__dask_2023.3.2_2023.4.0` | 69（36） | 39 | 61（8） | 6246 | 37 min |
| `dask__dask_2022.9.2_2022.10.0` | 50（28） | 25 | 44（8） | 2861 | 29 min |
| `dask__dask_2023.6.0_2023.6.1` | 33（22） | 21 | 105（8） | 3415 | 23 min |

- 使用 **release-note only** 设置，删除 release notes 中的全部链接。
- 以 **Fix Rate**（P2P 无失败时为 F2P 通过比例，否则为 0）为主要指标，同时报告 F2P 通过数与 P2P 回归数：dask 的 P2P 数以千计，一个回归即可清零 Fix Rate。
- conan 同时出现在调试集（2.0.2）与正式集（2.0.14）。两者同属 2.0 系列，代码有重叠；若要严格区分，用备选 `dvc_1.10.2_1.11.0` 替换 2.0.14。
- 全部 48 题的指标见 `docs/sweevo_catalog.md`（由 `python -m eval.tools.sweevo_catalog` 生成）。

### 3.2 ProMax（正式 3 道）

| 题目 | 语言 | 源文件 | 改动行数 | 顶层目录 | 预计 agent 用时 | 特点 |
|---|---|---:|---:|---:|---:|---|
| `tracel-ai__burn-4337` | Rust | 49 | 1954 | 12 | 51 min | 完整编译耗时长 |
| `OpenListTeam__OpenList-1001` | Go | 46 | 850 | 37 | 48 min | 改动分散在 37 个目录，适合并发 |
| `nacos__alibaba-13142` | Java | 46 | 889 | 7 | 48 min | Maven 构建 |

### 3.3 LHTB（正式 4 道）

| 题目 | 计分方式 | 资源 | 专家 / 新手估时 | 评分环境 |
|---|---|---|---|---|
| `commit0-multilib-tdd` | 按比例（784 个隐藏测试） | 2 核 / 4 GB | 480 / 1200 min | 同容器 |
| `langchain-version-migration` | 二元（部分完成不计分） | 2 核 / 4 GB | 120 / 360 min | 独立容器，只传递声明的产物 |
| `duckdb-optimizer-closure` | 先要求全部查询正确，再按性能计分 | 4 核 / 8 GB | 300 / 720 min | 同容器 |
| `riscv-core-debug` | 按比例（数量未知的注入 bug） | 4 核 / 8 GB | 360 / 720 min | 同容器 |

- 数据来源固定在 commit `d78f5eb`（2026-09-15），用 `python -m eval.tools.fetch_lhtb` 下载，只取所选题目（约 5 MB，不需要 Git LFS）。镜像为作者预构建的 `zli12321/lhtb-*`。
- **评分**：由 Pier 按题目自身配置评分（`grading_mode: official`），不做补丁重放，因为部分题的工作目录不是 git 仓库、评分依赖产物文件。`reward ≥ 0.95` 视为解出，与 LHTB 排行榜口径一致；同时报告连续得分。
- **continue-until-timeout**：LHTB 在 30 道题上默认开启（agent 宣布完成后，用隐藏评分器检查并让它继续），相当于完美裁判。prepare 时统一关闭；Pier 本身也不实现这一行为。
- 专家估时远超 90 分钟预算，预期大多数运行会用满预算，按超时时的交付计分。

### 3.4 负对照（2 道）

| 题目 | 来源 | pilot 结果 |
|---|---|---|
| `roboflow__supervision-1943` | ProMax | ✅，5.5 / 10.9 min |
| `koota-query-predicates` | DeepSWE | ✅，16.9 min |

在 `tasks.yaml` 中标注 `role: negative_control`。A 组可直接复用 pilot 的结果。

### 3.5 调试集（3 道）

| 题目 | 来源 | 预计用时 | 用途 |
|---|---|---:|---|
| `conan-io__conan_2.0.2_2.0.3` | SWE-EVO | 实测 53 min | 失败模式最完整（看到失败仍宣布完成、引入回归、误解需求） |
| `pydantic__pydantic_v2.7.0_v2.7.1` | SWE-EVO | 约 11 min | 23 条改动但运行快，适合频繁迭代；该仓库不出现在正式集中 |
| `spot-scheduler-traces` | LHTB | 专家 200 min | 无限速、2 核 4 GB、评分 5 分钟，用于打通 LHTB 流程。原选 `unknown-config-semantics` 因强制限速（每次探测等待 50 秒，完整解法至少约 88 分钟）改为备选 |

## 4. 难度参考

### 4.1 数据集整体难度（来自论文 / 排行榜）

| 数据集 | 公开结果 |
|---|---|
| SWE-EVO | 最好 25%；约 64% 的题没有任何模型解出；dvc、dask 仓库的平均解决率分别约为 4%–9% 和 2.6% |
| ProMax | 最好 41.2%；平均修改 11.4 个源文件、261.6 行 |
| LHTB | 90 分钟预算下最好的模型平均得分约 0.5，解出（R ≥ 0.95）13 / 46；29 道题没有任何模型解出 |

### 4.2 pilot 实测与估算方法

#### 4.2.1 模型与价格

- Agent：Claude Code 2.1.281（npm 安装，版本固定）；模型 `deepseek-flash[1m]`，haiku / subagent 使用 `deepseek-flash`，`--effort max`。
- 运行环境：8 vCPU / 32 GiB，agent 容器仅放行 `api.deepseek.com`。
- DeepSeek 价格（元 / 百万 tokens；高峰、空闲时段的划分以官网说明为准）：

| 项目 | 空闲时段 | 高峰时段 |
|---|---:|---:|
| 输入（缓存命中） | 0.02 | 0.04 |
| 输入（缓存未命中） | 1 | 2 |
| 输出 | 4 | 8 |

下文成本默认按**高峰价**计算（上限）；空闲时段所有单价减半，成本也恰好减半。

#### 4.2.2 调试集实测（run_id：`pilot-v2`）

| 数据集 | 题目 | 参考解改动文件 | 结果 | F2P 通过 / P2P 回归 | agent 用时 (min) | 单题总耗时 (min) | 轮数 | 输入 / 其中缓存命中 / 输出 tokens | 成本（高峰 / 空闲，元） |
|---|---|---:|:-:|---|---:|---:|---:|---|---:|
| ProMax | `roboflow__supervision-1943` | 11 | ✅ | — | 10.9 | 11.5 | 96 | 9,808,457 / 9,718,272 / 81,033 | 1.22 / 0.61 |
| DeepSWE | `koota-query-predicates` | 17 | ✅ | 43/43，0 | 16.9 | 18.3 | 141 | 26,213,629 / 26,078,592 / 169,728 | 2.67 / 1.34 |
| SWE-EVO | `conan-io__conan_2.0.2_2.0.3` | 48 | ❌（Fix Rate 0.0） | 1/8，1 | 52.8 | 56.8 | 366 | 95,102,038 / 94,873,984 / 262,477 | 6.35 / 3.18 |
| **合计** | | | 2 / 3 | | **80.6** | **86.6** | **603** | 缓存命中率 99.7% | **10.24 / 5.12** |

- 单题总耗时 = agent 阶段（含环境启动与原容器评分）+ 全新容器重放评分；不含首次拉取镜像。
- 缓存命中率均在 99% 以上。高峰价下成本构成：缓存命中 5.23 元（51%）、输出 4.11 元（40%）、未命中输入 0.91 元（9%）。
- **波动**：同一道 supervision，此前一次运行（`pilot-v1`）agent 用时 5.5 min，本次 10.9 min，单次运行耗时可相差一倍。
- **conan 失败原因**：22 条 release notes 实现了 20 条；未做的两条（服务端源码备份、下载认证）恰好对应 8 个 F2P 中的 7 个；另因 `conan cache clean` 改动引入 1 个 P2P 回归，Fix Rate 由 1/8 清零为 0。F2P 只覆盖约 3 条 release notes，评分覆盖不足（该题 F2P = 8，评分覆盖不足，仅作调试用）。
- 流程问题均已修复：koota 首次运行被误中断（`CancelledError`）后重跑；DeepSWE 只评已提交的改动，重放评分已改为应用补丁后 commit，修复后原容器与重放评分一致。

#### 4.2.3 估算方法

三道调试题呈现稳定的比例关系：

| 指标 | supervision | koota | conan | 采用值（区间） |
|---|---:|---:|---:|---|
| 轮数 ÷ 参考解改动文件数 | 8.7 | 8.3 | 7.6 | 8.2（7.6–8.7） |
| 每轮耗时（秒） | 6.8 | 7.2 | 8.7 | 7.5（6.8–8.7） |
| 每轮成本（元，高峰） | 0.013 | 0.019 | 0.017 | 0.016（0.013–0.019） |

据此：**预估轮数 = 8.2 × 参考解改动文件数；agent 用时 = 轮数 × 每轮耗时；成本 = 轮数 × 每轮成本**。区间取三道题比例的最小、最大值。附加假设：

1. **编译型语言**：调试题只覆盖 Python / TypeScript，每轮中的构建与测试较快。Rust、C++ 的用时上限按每轮耗时 × 2、Java 按 × 1.5 估计（点估计不加倍数）。
2. **评分等开销**（环境启动、重放评分）：调试题实测 0.6–4 min，默认取 3 min；`burn`、`fprime`、`dask` 取 20 min，`nacos` 取 15 min，`dvc` 取 10 min（完整编译或测试量大）。这些均为假设，应以评测集 gold-check 的实测评分耗时替换。
3. agent 用时以 180 min 超时为上限；不含首次拉取镜像的时间。
4. 样本仅 3 道题，且评测集题目整体比调试题大；估算用于安排时间与预算，不代表实际表现。

#### 4.2.4 正式集估算（每题 1 次，每组）

按 4.2.3 的比例估算，agent 用时以 90 分钟为上限；LHTB 按用满预算估计（上限）；负对照取实测值。评分时间：SWE-EVO 按测试数量粗估，ProMax 沿用第 1 版的假设，LHTB 按其评分器超时的一半估计。

| 集合 | agent 用时 | 评分 | 成本（高峰价） |
|---|---:|---:|---:|
| SWE-EVO 7 道 | 6.2 h | 1.5 h | 48 元 |
| ProMax 3 道 | 2.5 h | 0.6 h | 19 元 |
| LHTB 4 道 | 6.0 h | 1.3 h | 47 元 |
| 负对照 2 道 | 0.5 h | — | 4 元 |
| **合计** | **15.1 h** | **3.4 h** | **约 117 元** |

- 串行约 18.5 h；并发 2 约 9 h。duckdb、riscv 各需 4 核 8 GB，与其他题并发时会互相争用，实际偏长。
- 成本按每轮 0.016 元估算，这是在最长 366 轮的题上测得的。运行到 600–700 轮时上下文更长，每轮成本更高，最长的几道题实际成本可能是估算的 1.5–2 倍。
- 按 Belay v3 方案，每组每题运行 1 次；A-gate、PEE、C 三组合计约 3 倍。

#### 4.2.5 正式集实测（待回填）

正式集运行后，用 `python -m eval.report <results_root>/<run_id>` 生成汇总，将每题的实际用时、轮数、成本回填到这里，并据此修正 4.2.3 的比例。

## 5. 防泄漏与评分隔离

| 风险 | 措施 |
|---|---|
| ProMax 的 `hints_text` 由参考答案生成 | 只向 agent 提供 `problem_statement` |
| SWE-EVO 的 release notes 附带 PR 链接 | 转换时删除全部 URL |
| 镜像中保留目标版本之后的 git 历史 | SWE-EVO 重建为单一提交；ProMax 删除 base 之后的提交。pilot 中 supervision 用五种途径寻找上游答案，均被拦截 |
| agent 联网检索或下载新版本 | 网络白名单，仅放行模型 API；pilot 中 `curl`、`pip download`、`WebFetch` 均被拦截 |
| **服务端工具绕过网络白名单**：Claude Code 的 `WebSearch` / `WebFetch` 由模型 API 在服务端执行，容器隔离拦不住。dev-v1 中 cc、gate 两组在 conan 上用 WebSearch 共命中评测数据 51 次（HuggingFace 上 SWE-EVO 中其他 agent 在同一道题上的运行记录）以及新版本文档 | 所有基于 Claude Code 的 agent 强制禁用这两个工具：命令行 `--disallowedTools` 与用户配置 `permissions.deny` 两层（后者同样作用于子 agent）；`eval.tools.audit` 统计每次运行的 WebSearch 次数与是否命中评测数据 |
| 镜像自带的搭建脚本（`/root/setup_*.sh`，含上游仓库地址与 commit 哈希） | SWE-EVO、ProMax 构建镜像时删除 |
| A-gate 的门禁配置暴露隐藏测试的文件名（dev-v1 中 agent 读到后以文件名为关键词检索） | 转换器生成门禁配置时去掉测试补丁新建的文件；门禁的配置、基线与记录放在 `/opt/belay-gate`，运行结束才复制到 `/logs/agent/gate` |
| agent 手动运行门禁脚本，消耗拦截次数 | 门禁只响应 Stop hook（stdin 中 `hook_event_name == "Stop"`），手动运行不检查、不计数，只记录 |
| agent 修改测试让门禁失效（dev-v1 中 cc 改 27 个测试文件、删 7 条断言；PEE 改 17 个、删 6 条） | 门禁检查时把 agent 改过的测试文件临时恢复为原始版本，运行后放回；官方评分同样使用原始测试，修改测试会导致测试补丁无法应用、整题记 0 |
| 模型训练数据中含有目标版本之后的文档（PEE 未联网却使用了新版本的配置名） | 无法消除；属于用历史版本出题的基准固有的污染，在报告中说明 |
| 做题阶段可写评分目录 `/logs/verifier`，事先放入伪造结果 | Claude Code agent 结束时清空该目录并留证（`verifier_dir_before_cleanup.txt`）；replay 数据集在全新容器中评分 |
| LHTB 的 continue-until-timeout 相当于隐藏评分器当裁判 | prepare 时关闭，仅允许在上界对照组（O 组）中开启 |
| LHTB 读取评分器（其仓库记录过一轮测试中 17 个满分有 14 个来自读取评分器） | 隐藏测试只在评分时上传；langchain 使用独立评分容器；`eval.analyze` 统计访问 harness 目录等"寻找答案"的尝试 |
| 数据集文件、参考解、测试补丁进入容器 | 只在宿主机读取；`tests/`、`solution/` 仅在评分与 oracle 时上传 |

## 6. 备选题与替换规则

某道题 gold-check 不通过、或单次评分耗时过长时，按下列顺序替换，并在 `tasks.yaml` 的 `replacements` 中记录原因。

| 数据集 | 备选顺序 |
|---|---|
| SWE-EVO | `iterative__dvc_1.10.2_1.11.0` → `iterative__dvc_2.5.0_2.5.1` → `iterative__dvc_3.43.1_3.44.0` |
| ProMax | C++：`nasa__fprime-3642`、`LMMS__lmms-7454`；Go：`go-gitea__gitea-35775`、`gitleaks__gitleaks-1831`；Java：`plantuml__plantuml-c_15fa06c`；Rust：`astral-sh__ruff-20221` |
| LHTB | `vector-db-iterative-build` → `nbody-accel-iterative` → `great-expectations-audit` → `unknown-config-semantics` |

LHTB 备选中 `great-expectations-audit` 原本允许联网，关闭联网后能否评分需要先用 gold-check 确认。

## 7. 数据来源

- SWE-EVO：<https://huggingface.co/datasets/Fsoft-AIC/SWE-EVO> · 论文 arXiv:2512.18470；评测逻辑复刻自官方仓库的 SWE-bench 分支（见 `eval/convert/sweevo_to_harbor.py`）
- SWE-Bench ProMax：<https://huggingface.co/datasets/swe-bench-promax/SWE-Bench-ProMax> · 论文 arXiv:2608.09802
- LHTB：<https://github.com/zli12321/LHTB> · 论文 arXiv:2607.08964
- DeepSWE：<https://github.com/datacurve-ai/deep-swe> · 论文 arXiv:2607.07946

题目规模数据（文件数、行数、F2P 数、release notes 条目数）由本项目脚本从各数据集的参考解和测试列表统计得到。

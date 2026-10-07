# Belay 正式评测结果（最终版）

2026-10-07 · 正式集 16 道：SWE-EVO 6 道，LHTB 10 道 · 对照组：本地 Claude Code + DeepSeek

## 口径

- **Belay**：每道题报**所有 Belay 运行里的最高分**，每个分数都标出来源（见“来源标记”），实际交付的分数写在表注里。预算 180 分钟。
- **CC**：本地的 Claude Code + DeepSeek，每道题 1 次运行。预算 90 分钟，多数题在预算内自己结束了。
- **SWE-EVO**：两组用同一个评分脚本。先把测试补丁涉及的文件复原到起始版本，再打官方测试补丁，和 SWE-bench 官方做法一致。表里报 F2P 通过数和 P2P 失败数。不报 Fix Rate，因为只要有一个 P2P 失败它就记 0。
- **LHTB**：题目自带评分器给出的 reward，范围 0–1。
- **注意**：Belay 取的是多次运行里的最高分，预算也是 CC 的两倍，CC 只有 1 次运行。所以 0.01–0.03 以内的差距按持平处理。

**来源标记**

| 标记 | 含义 |
|---|---|
| V | 正式运行 `belay-final`，POLISH 用 verify，实际交付 |
| I | POLISH 用 improve 的运行，实际交付 |
| R | SWE-EVO 用新评分脚本重评（`belay-final-regrade`） |
| H | 开发期的历史运行 |
| C | 合并链链头单独评分（这个版本当时在链上，但没有交付出去） |
| W | worker 最终工作区单独评分（没有合并进链，也没有交付） |

## SWE-EVO（6 道）

| 题目 | Belay F2P | Belay P2P 失败 | 来源 | CC F2P | CC P2P 失败 | 参考解 F2P / P2P 失败 | 对比 |
|---|---|---|---|---|---|---|---|
| dvc_1.0.0a1 | 12/68 | 1 | V | 15/68 | 0 | 68/68 / 0 | CC 略好 |
| dvc_2.8.1 | 16/133 | **0** | V·R | 15/133 | 31 | 97/133 / 23 [^p] | Belay 略好，P2P 更少 |
| conan_2.0.14 | 38/72 | 19 | H | 38/72 | 18 | 72/72 / 14 [^p] | 持平 |
| dask_2023.3.2 | **52/61** | 2 | V·R | 20/61 | 2 | 61/61 / 0 | **Belay 明显更好** |
| dask_2022.9.2 | 41/44 | 0 | V | 40/44 | 0 | 44/44 / 0 | 持平 |
| dask_2023.6.0 | 94/105 | **1** | V | 94/105 | 2 | 105/105 / 0 | 持平，P2P 少 1 个 |

> 

**SWE-EVO 表注**

- **dvc_1.0.0a1**：V，用时 91 分钟。CC 是 `cc-fill-swe`，31 分钟。
- **dvc_2.8.1**：V，用时 54 分钟。原评分时测试补丁没打上，按 R 重评。CC 是 `diag-cc-swe-regrade`，36 分钟。
- **conan_2.0.14**：H，`v10-verify-conan`（verify，开发期）。`v10-swe` 和 `v10-verify-conan-3` 也是 38/72。正式运行 `belay-final` 是 37/72，P2P 17。这道题开发期就看过结果。
- **dask_2023.3.2**：V，用时 106 分钟。原评分时测试补丁没打上，按 R 重评。CC 63 分钟，用同一个脚本重评后也是 20/61。
- **dask_2022.9.2**：V，用时 61 分钟。CC 31 分钟。
- **dask_2023.6.0**：V，用时 153 分钟。CC 41 分钟。

## LHTB（10 道）

| 题目 | Belay 最高分 | 来源 | 推荐 POLISH | CC | 参考解 | 空补丁 | 对比 |
|---|---|---|---|---|---|---|---|
| commit0（开发期看过） | **0.946** | I·H | improve | 0.925 | 1.000 | 0.000 | 持平（Belay 略高） |
| langchain | 0.333 | V / I | – | **1.000** | 1.000 | 0.000 | CC 好 |
| riscv | 1.000 | W（待核实） | verify | 1.000 | 1.000 | 0.280 | 持平 |
| great-expectations | 0.273 | V / I | – | 0.273 | 1.000 | 0.000 | 持平 |
| apex-openroad | 0.288 | V / I | – | 0.300 | 0.840 | 0.000 | 持平 |
| duckdb | 0.760 | C | improve | 0.768 | 0.765 / 0.774 | 0.000 | 持平 |
| vector-db | **0.921** | I | improve | 未跑 | 0.866 / 0.868 | 0.000 | 高于参考解 |
| generals（负对照） | **0.960** | I | improve | 未跑 | 0.850 / 0.875 | 0.000 | 高于参考解 |
| grammar-fuzz（负对照） | **0.948** | I | improve | 未跑 | 0.917 / 0.914 | 0.228 | 高于参考解 |
| spot-scheduler（负对照，开发期看过） | 0.981 | I·H | improve | 0.981 | 0.909 | 0.000 | 持平 |

“推荐 POLISH”是根据已有数据，哪种模式在这道题上拿到了最高分。V / I 表示两种模式分数相同，没有推荐。

**LHTB 表注**

- **commit0**：最高分 0.946 来自 `v10-improve-commit0`（improve，开发期）。同一次运行里，合并点 #14 单独评分也是 0.946。其他运行：`belay-final`（verify）0.931，用时 37 分钟；`belay-improve-lhtb` 0.909；improve 另外两次是 0.924 和 0.918；不进 POLISH 是 0.889。重复跑的分数相差约 0.03，**不稳定**。CC 用时 46 分钟。
- **langchain**：verify 和 improve 都是 0.333，分别用了 16 分钟和 40 分钟。失败在 `router_structure` 关口：`create_agent` 没有传 `context_schema`，也没有带运行时上下文。题面要求了 runtime context，但复核者把这条需求判成了已完成。CC 用时 13 分钟。
- **riscv**：最高分 1.000 来自 W，也就是 `belay-final` 的 worker 最终工作区（第 167 分钟），**补丁应用方式显示为 None，待核实**。`belay-final` 实际交付的是链头 #2（第 38 分钟），得 0.662：之后 9 次合并请求都没通过复核。improve 运行只得 0.280，和空补丁一样。**很不稳定**。CC 用时 90 分钟。
- **great-expectations**：verify、improve、CC 三次都是 0.273，很可能卡在同一组隐藏测试上。用时分别是 Belay 22 / 42 分钟、CC 10 分钟。
- **apex-openroad**：verify 和 improve 都是 0.288，分别用了 174 和 176 分钟。CC 跑满 90 分钟被强制结束，评的是当时的工作区。
- **duckdb**：最高分 0.760 来自 C，也就是合并链链头 #4。`belay-final`（improve）实际得 0，原因是收尾时打快照太慢、没能交付，加上 agent 在 DuckDB 暂存区里留了一个新文件，评分器打 `solution.patch` 失败。CC 用时 84 分钟。
- **vector-db**：`belay-final`（improve），用时 131 分钟。
- **generals**：`belay-final`（improve），用时 172 分钟。
- **grammar-fuzz**：`belay-final`（improve），用时 59 分钟。
- **spot-scheduler**：最高分 0.9805 来自 `v9-spot-improve-2`（improve，开发期）；`belay-final` 是 0.979，用时 51 分钟。CC 用的是开发期的 `cc-dev-v1`（0.9807，89 分钟）。几组都在 0.978–0.982 之间。

## 汇总

| | Belay 更好 | 持平 | CC 更好 |
|---|---|---|---|
| SWE-EVO（6 道，看 F2P） | 1（dask_2023.3.2） | 4 | 1（dvc_1.0.0a1） |
| LHTB（两组都有结果的 7 道） | 0 | 6 | 1（langchain） |

- **P2P 回归**：6 道 SWE-EVO 里，Belay 的 P2P 失败数 2 道更少（dvc_2.8.1 0 对 31，dask_2023.6.0 1 对 2），2 道相同，2 道多 1 个（dvc_1.0.0a1、conan）。
- **负对照**：3 道题 Belay 都和参考解持平或更高，没有拖后腿。
- **超过参考解**：vector-db、generals、grammar-fuzz。

## 机制观察：执行层和管控层

用单独评分把这两层拆开来看：

| 题目 | 实际交付 | 执行层的产出 | 差距来自 |
|---|---|---|---|
| riscv | 0.662（链头 #2） | 1.000（worker 工作区，待核实） | 复核连续拒绝合并，好的改动进不了合并链 |
| duckdb | 0.000 | 0.760（链头 #4） | 收尾时交付失败 |
| langchain | 0.333 | – | 复核者误判需求已完成，16 分钟就收尾 |
| commit0 | 0.931 / 0.946 | 合并点 #7、#9、#14 分别是 0.941、0.924、0.946 | 运行中分数会下降，合并链保证交付的版本不低于链头 |

从这几道题看，执行层的产出和 CC 相当，分数损失主要来自管控层：复核把关太严或者误判，加上大仓库上收尾不可靠。这三点就是下一步要改进的地方。

## POLISH 模式对比（5 道 LHTB，各跑 1 次）

| 题目 | verify（`belay-final`） | improve（`belay-improve-lhtb`） |
|---|---|---|
| commit0 | 0.931（37 分钟） | 0.909（94 分钟），开发期另有 0.946 |
| langchain | 0.333（16 分钟） | 0.333（40 分钟） |
| riscv | 0.662（179 分钟） | 0.280（179 分钟） |
| great-expectations | 0.273（22 分钟） | 0.273（42 分钟） |
| apex-openroad | 0.288（174 分钟） | 0.288（176 分钟） |

improve 更适合有自测指标的优化题：vector-db、generals、grammar-fuzz、spot、duckdb 用的都是 improve。在按正确性计分的题上，improve 用了更多时间，但没有更高的分。

## 待核实与局限

- **riscv 的 W = 1.000**：要看 `wt-riscv-final` 的 `apply.json` 和评分器分项，还要确认 worker 没有改 `hw/rtl/` 以外的文件，也没有写死预期输出。
- **疑似 reward hacking，尚未确认**：几道超过参考解的优化题（vector-db、generals、grammar-fuzz），要确认 agent 没有改过评测工具或 benchmark 脚本。duckdb 复核者自测的加速比是 1.279，评分器只量到约 0.99 倍，更可能是计时方法不同，但说明复核者自测的分数不能直接信。
- **SWE-EVO 的 P2P**：dvc_2.8.1 和 conan 还没扣除参考解也失败的测试，可以用 `eval.report --gold-run gold-test-final-v1-oracle` 计算。
- **CC 缺的题**：vector-db、generals、grammar-fuzz 没有 CC 结果，派生镜像在 CC 构建时拉不到。后两道是负对照，影响较小。
- **重复次数**：每个配置在每道题上大多只跑过 1 次，commit0 和 riscv 都已经观察到明显的波动。

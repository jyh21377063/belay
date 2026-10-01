# Belay 实现设计（v7：worker 只做自然的事，状态由图从观察推出）

本文是 Belay 落到代码时的设计说明：计划里没写死、但实现必须定下来的东西，以及为什么这样定。目标场景：一个任务连续
运行几十分钟到一天，中间经历多次会话交接，runtime 进程与容器都可能丢失。

## v7 为什么大改

v6 里图上所有有用的东西都挂在 worker 的主动声明上：认领 → 当前焦点 → todo 变成步骤 → `step_done` 锚点 → 后台验证
→ `ready_for_review` → 任务完成。模型（DeepSeek）不认领，整条链就全断：todo 只记成笔记、后台没有语义节点可验证、
任务永远是 open、复查者从来不跑、开场让它先去认领。一次 dask 2023.6.1 的试跑里，几百次工具调用之后链头仍是 0。
它看了 `board` / `task` 十几次——图被当成参考资料，而不是要走的流程；认领在训练分布里没有对应的习惯，循环里也没有
任何东西把它拉回来。同一次试跑的规划里 22 个任务没有一个带检查项（SWE-EVO 的评分测试都是新加的），任务层与需求
一一对应，只多了一层编号。

v7 的四条原则：

1. **只依赖模型的自然行为**：读、改、跑测试、（可选的）todo、结束时说“做完了”。认领、步骤声明、逐个任务申请验收这些
   要模型主动想起来的流程全部删掉。
2. **需要模型提供的信息，由 harness 在自己控制的时机去要**：交接摘要（L3/L4）、提交时的说明、模型停下时的追问。
3. **反应式工具的用法写在触发它们的消息里**：`revert_change`、`waive_check` 的说明不放进系统提示。
4. 不变：事件日志是唯一事实来源；纯函数核心；LLM 只能收紧不能放行；存档链、验证、定位、诊断、恢复的机制都保留。

## 0. 一句话结构

```
          worker 工具请求 / 快照 / 验证器结果 / git 结果 / LLM 结果 / 时钟
                           │  (输入)
                           ▼
   rules.*(graph, 输入, now, cfg) ──► [事件草稿]      纯函数：决定
                           │
                  store.append(事件)                  先写日志
                           │
           graph' = reduce(graph, 事件)               纯函数：推导视图
                           │
           effects_for(事件, graph') ──► [副作用]      纯函数：计划副作用
                           │
      执行副作用（作业、git CAS、bundle、定位 diff、诊断、复查……）   命令式外壳，结果再作为输入回来
```

一次运行内部有两条并行的线：worker 在前台写代码；runtime 在后台拍快照、持续验证最新快照、推进存档、提升、定位。两者
只通过事件日志与通知交汇，后台从不碰 worker 的工作区（验证在验证槽位里跑；隔离无效时降级，见 §4.1）。

- `belay/core/` 全是纯函数：不做 IO、不调模型、不读时钟（`now` 作为参数传入）。
- `belay/runtime/` 是薄的命令式外壳：事件存储、git、验证槽位、会话循环、LLM 调用、镜像与恢复。
- **唯一写者**：`Runtime.submit()` 在一把锁里执行“规则 → 追加事件 → 更新视图”，所以不需要其他并发控制。

## 1. worker 与图分别持有什么

| | worker | 图 |
|---|---|---|
| 计划 | todo（自己的，可以不列） | 镜像为运行级的 todo；勾掉一条时拍锚点快照，被链上存档包含即 anchored |
| 正在做什么 | 只在它的上下文里 | 不持有“当前焦点”；需要时从 todo（in_progress 的那条）和链头以来的 diff 推出 |
| 完成 | `submit`（唯一要它做的声明） | 每条需求的状态：verified / submitted / blocked / open |
| 存档 | 不用管 | 快照 → 后台持续验证最新快照 → 暂存点 → 空闲时提升为确认点 |
| 思路、走不通的路 | 交接时由 harness 要求它写 | 存为摘要（compacted），下个会话开场交还 |
| 回归 | 收到消息后调 `revert_change` / `waive_check` | 定位、诊断 |
| 何时结束 | 不用管 | 提交被接受 + 预算 + 停滞 |

## 2. 事件

每条事件：`seq`（从 1 开始连续递增，同时是图的版本号）、`t`（墙钟秒）、`type`、`actor`、`source`、`payload`。

**来源纪律**（`invariants.check_log` / `llm_effects`）：`requirement_verified`、`checkpoint_created`、
`checkpoint_advancing`、`checkpoint_confirmed`、`todo_anchored`、`delivered` 只能来自 `rule` / `observed`；
`requirement_submitted`、`requirement_blocked`、`todos_updated`、`todo_completed` 是 worker 的自述（`self_report`），账本如实
区分；`llm` 的事件只能记录、重开（`diagnosis_recorded`、`review_recorded`、`checkpoint_labeled`），永远不引起完成、
存档、提升。

| 类别 | 事件 | 来源 | 视图变化 |
| --- | --- | --- | --- |
| 运行 | `run_started` / `runtime_recovered` / `clock_started` / `run_suspended` | rule / observed | 预算、恢复与重建次数、丢失的快照、隔离状态 |
| | `deadline_reserve` / `finalize_started` / `delivered` | rule | 截止预留；收尾开始（不再开后台验证）；交付点、级别、为什么不是 DONE |
| 需求 | `plan_proposed` / `requirement_frozen` | llm / rule | 需求清单冻结：引文、摘要、`kind`（actionable / context）、`checks` |
| | `requirement_verified` | rule | 证据检查在链上存档里全部通过 |
| | `requirement_submitted` / `requirement_blocked` | self_report | 提交时记下（在提交的存档上）；受阻带种类、理由、引文 |
| | `requirement_reopened` | rule | 复查者说没做完 / 给出了合理读法 / 回退 |
| todo | `todos_updated` / `todo_completed` | self_report | todo_write 的镜像（按标题匹配保持 id 稳定，条目文字里的 R 编号顺带关联）；勾掉时带锚点快照 |
| | `todo_anchored` / `todo_invalidated` | rule | 锚点被链上同段存档包含；回退使锚点不在链上 |
| 提交 | `submit_requested` | rule | 一次提交：快照、摘要、受阻清单、是否隐式；带存档尝试，或直接落在链头 |
| | `submit_updated` | rule | pending → checkpointed → reviewing → accepted / returned；被拒（rejected）由 `checkpoint_rejected` 推出 |
| 执行 | `session_started` / `session_resumed` / `session_ended` / `compacted` | rule / observed / llm | `session_resumed.mode` = memory / replay |
| | `snapshot_taken` | observed | 快照时间线（序号、段号、原因、可测、拍下时进行中的 todo、快照提交）；同时更新 WIP |
| | `stall_detected` | rule | no_progress / repeated_failure（提示）、sessions_no_progress（停止） |
| 验证 | `job_started`（`where`：slot / workspace / live）/ `job_preempted` / `job_finished`（带 `reasons`）/ `baseline_recorded` | rule / observed | 作业与失败原因、守护集合、导入隔离 |
| 存档 | `checkpoint_attempted`（`snapshot`、`lane`、`kind`）/ `attempt_superseded` | rule | 前台（submit、收尾）/ 后台（最新快照）尝试；更旧的尝试被新存档取代 |
| | `checkpoint_advancing` / `checkpoint_created` / `checkpoint_rejected` | rule / observed | 父节点在推进时确定；新存档为暂存（related）或确认（full） |
| | `checkpoint_confirmed` / `checkpoint_demoted` / `checkpoint_marked` | observed / rule | 全量通过 → 确认点前移；确认过的回归 → 降级；勾掉的 todo、提交落在已有存档上 → 升级 kind 并带标签 |
| | `check_waived` / `rollback` | rule | 豁免回归门里的测试；回退（只由恢复流程使用）：段号 +1 |
| 定位 | `persistent_regression` / `locate_started` / `locate_concluded` / `regression_located` / `relation_learned` | rule / observed | 见 §4.4 |
| LLM | `diagnosis_requested` / `diagnosis_recorded` | rule / llm | 诊断者（只解释） |
| | `review_started` / `review_recorded` | rule / llm | 复查者（只收紧），一批若干条需求 |
| | `checkpoint_labeled` | llm | 没有标签的存档补一行说明 |

v6 的 `task_*`、`steps_planned` / `step_*`、`review_requested`、`note` 都删掉了。旧日志不能在 v7 上重放。

## 3. 三个视图

全部定义在 `belay/core/model.py`，是不可变 dataclass；`reduce` 返回新图（结构共享，不修改旧图）。

- 需求账本：`Requirement`（`kind`、`checks`、`status`、`checkpoint`、`submit`、受阻信息、`reopen*`、`last_failure`、
  `passed_checks`、`review*`、`history`）、`Todo`、`Submit`、`Review`。没有任务层、持有关系与当前焦点。
- 执行状态：`Session`、`Wip`、`Snapshot`（`todo` 取代了 v6 的 `held` / `step`）、`Job`、`Persistent`、`Locate`、
  `Diagnosis`；`graph.epoch` / `graph.epoch_base`；`graph.isolation`。
- 存档链：`Attempt`（`submit` 取代了 `tasks`）、`Checkpoint`（`kind` ∈ auto / todo / submit / handoff / final / baseline、
  `level` ∈ provisional / confirmed、`demoted`、`label`）；`graph.head` 与 `graph.confirmed`。

### 需求的状态

| 状态 | 怎么进入 | 来源 |
|---|---|---|
| `open` | 初始；被重开也回到这里，带上原因与 `last_failure` | rule |
| `verified` | 证据检查在链上的某个存档里全部通过（`auto_verify`：每次存档创建、作业完成后检查） | rule |
| `submitted` | worker 提交，提交的存档通过回归门；这条需求没有证据检查 | self_report |
| `blocked` | worker 在提交的 `blocked` 清单里声明做不了（种类、理由；check_conflict 要逐字引文） | self_report |

- **证据检查** = 需求的 `checks` 中在原始代码上**不通过**的那些（`queries.evidence_checks`）。基线上本来就通过的检查
  已经在回归门里，证明不了任何事；这一条在验证时判断，规划器与基线谁先跑完都没关系。
- `context` 需求（标题、日期、版本横幅、“代码在 /testbed”、“以下是发布说明”）只为覆盖原文，不进清单、不参与提交与
  DONE；`requirement_frozen` 要求至少一条 actionable。
- 复查者对 submitted 与 blocked 只能做两件事：yes 只记录；no / partial（或受阻给出了合理读法）重开为 open。

### 三个基准

| 用途 | 基准 | 实现 |
| --- | --- | --- |
| 交付 | 最新的确认点 | `queries.delivery_checkpoint`；除基线外没有确认点时按 `deliver_unconfirmed` |
| 恢复的起点 | 链头（后台持续验证，链头紧跟工作区） | `queries.resume_point` |
| `rollback` 的默认目标（只由恢复流程使用） | 最近的里程碑（todo / submit / handoff / final / baseline） | `queries.latest_milestone` |
| 容器重建 | 整条链、全部快照与最新快照 | `recovery.rebuild_container` |

## 4. 关键规则

### 4.1 独立目录验证（模块 A）

- 非 live 的作业默认 `where=slot`：runner 先把候选树导出到验证槽位（`read-tree --reset -u` 按槽位索引只改动不同的
  文件；`git clean -fd` 清掉未被忽略的未跟踪文件，被忽略的构建产物留作缓存），第一次导出后从工作区复制被忽略的文件与
  项目自己的 `.git` 作为构建缓存种子（`cp -a --reflink=auto`）。
- `sys.path` 映射：基线阶段在工作区跑 `runner.py probe`，把位于工作区之下的条目按原顺序映射到槽位（再补根目录与
  `src/`），作为 `PYTHONPATH` 放在最前（`RunnerVerifier.map_sys_path`）。
- 基线双跑：一次 `where=workspace`（此时还没有 worker），一次 `where=slot`；工作区通过而槽位没通过的测试先在槽位里确认
  重跑，剩下的数量超过 `isolation_max_diff` 就判定隔离无效。守护集合只取两边都通过的测试。
- 破坏探针（`runner.py isolation-probe`）：在槽位里找一个被测试导入的源文件，先确认 `import` 解析到槽位，再在文件开头插
  `raise ImportError` 跑那个测试，必须失败。命令里写死了工作区绝对路径（`VerifierSpec.mentions`）同样判为无效。
- 降级模式（`graph.degraded`）：作业回到 `where=workspace`（`TreeOverlay` 切换工作区），与 worker 的写类工具在
  `workspace_lock` 上互斥；不做任何后台验证（不验证步骤锚点、不提升、不定位），只在 worker 本来就在等待时
  验证（submit、会话结束与交接——这时驱动等验证结束再开新会话、收尾）。基线用工作区上的两次。
- 调度：`RunnerVerifier` 的槽位池 + 优先级队列（`verify.job_priority`：1 收尾与基线 / 2 worker 在等的（submit、
  证据、定位）/ 3 后台：最新快照的验证与提升 / 4 已被取代还在跑的后台作业）。第 1、2 档到达而槽位被第 3、4 档占着时取消低档作业（TERM）并重新排队，写 `job_preempted`，不算丢失。
  第 3、4 档以 `nice` 运行，可选限制并发（`background_cpu_limit`）。
- 作业进程：`setsid` 起会话，`timeout --foreground` 保证取消信号能到达 runner；外层 shell 用 trap 挡住 TERM，保证写完成
  标记；进程组 id 由作业自己写。启动命令先把自己的输出换成 `/dev/null`，launch 立即返回（v5 里 launch 实际会等作业跑完）。

### 4.2 快照与后台验证（模块 B）

三层：**存**（快照，只存不打扰）→ **验**（后台空闲时验证最新的可测快照；submit 与收尾在前台验证）→ **查**（只有
worker 的提交被拒、或提交的存档被降级时，才二分定位）。没有任何基于时间的存档或提醒。

- 快照时机（`_Hooks.after_tools` → `driver.take_snapshot`，没有限流）：`edit_file` / `write_file` 之后一定拍；bash 不一定
  写文件，累计 `snapshot_bash_every` 次再拍；模型跑测试 / 构建的 bash 命令执行前拍（`model_test`）；勾掉 todo、submit、
  会话结束、交接、截止、恢复开始、撤销之后都拍。原样树与上一张相同就直接沿用，不记新快照（交接 / 会话结束例外）。
- 每张快照是一个确定的提交（树 = `{raw: 原样树, cand: 候选树}`，父提交是上一张快照），ref 为 `refs/belay/snap/<n>`。
- 候选树 = 原样树剔除测试路径下的改动（恢复为基线版本），交付也只交付候选树。测试路径按基线实际收集到的测试判断
  （`verify.suite_layout`），所以 `django/test/`、`numpy/testing/` 这类源码包不会被当成测试剔除。
- 预检：改动的 `.py` 文件用 `compile()` 检查语法（不写 `.pyc`）；可配 `precheck_cmd`。失败的快照标为不可测。
- **后台线**（`rules.schedule_background` / `_background_candidate`）：同一时刻每个 worker 至多一个后台尝试；空闲时取这个
  worker 同一段里**最新**的可测快照——比链头新、它的树在这一段还没尝试过（被拒过的树不再重试，等新的改动）；最新的
  快照预检不过就往前找最近的可测快照。为前台意图拍的快照（submit、收尾）后台不取，由发起者自己验证。
  `cfg.background`：`latest`（默认）/ `handoff`（只验交接快照，v6 的做法，用于消融）/ `off`。降级模式等同 `handoff`。
- 为什么能这样做：回归门只判断“原来能过的有没有被弄坏”，所以任何过门的快照都是不比基线差的合法交付物；交付最新的
  过门快照，正好是评分上最好的选择。后台被拒是常态（中间态），只是链头不动：不通知、不定位、不诊断、不计入“同一
  回归反复被拒”的停滞检测。代价只有 CPU（第 3 档作业，`nice`、`background_cpu_limit`）。
- **新快照胜出**：前台、后台尝试各至多一个；链头的快照序号不小于尝试的快照序号（同段）时，尝试被 `attempt_superseded`
  取代（它带着的提交转到链头上判定）；正在跑的尝试不会被更新的快照打断，跑完再去拿最新的。父节点在
  `checkpoint_advancing` 时才确定，CAS 用它校验。
- 每次存档尝试的选择都带上还没完成的需求的证据检查（`_evidence_of_open`），需求验证通过不需要任何人声明。
- **进展**（`reduce._progress`）只来自：需求验证通过、某条证据检查第一次在存档上通过、需求被提交或受阻、todo 被锚定。
  存档本身（包括后台存档、提升为确认点）不算进展：否则一个不断写出能过门的半成品的 worker 会永远“有进展”，
  停滞检测与“连续几个会话没有进展就停”都失效。
- 标签：todo 存档的标签是条目文字，提交存档的标签是摘要第一行；勾掉的 todo 或提交落在已有的自动 / 交接存档上时
  `checkpoint_marked` 把它升级为对应的 kind（回退的默认目标不会越过它）。其余没有标签的里程碑（以及每 `label_every`
  个其他存档）用 `aux_llm` 补一行。

### 4.3 两级存档链（模块 C）

- related 档位通过 → 暂存点；full 档位通过（例如 related 升级为 full、收尾）→ 直接是确认点。
- 提升（`schedule_promotion`）：验证队列有空闲时，只看最新确认点与最近一次降级之后的暂存点，取最新的跑全量（更老的
  跳过），同一时刻只提升一个。全量通过（回归先确认重跑）→ `checkpoint_confirmed`；确认过的回归 → `checkpoint_demoted`，
  它之后的暂存点是 suspect，直到它们自己跑完全量。
- 降级后：提交的存档（kind = submit）对最新的可测快照只跑这几个失败的测试（`recheck`）：仍失败 →
  `persistent_regression(trigger=demoted)` → 通知 worker + 定位 + 诊断；已通过 → 只记录。后台、todo、交接存档被降级
  只是不再交付，不追查、不通知。
- 收尾（`driver._finalize`）：`finalize_started` 取消后台尝试 → 对当前 WIP 做一次 full 前台尝试 → 链头仍是暂存点且还有
  时间就提升它 → 取消剩下的作业 → 交付最新的确认点（`delivered` 带 `level`、`lag`、`not_delivered`、`status_reasons`）。
- 豁免（`rules.waive_checks`，工具 `waive_check`）：任务原文明确要求的行为与某个现有测试冲突时，worker 可以把这个测试
  从回归门里去掉。规则校验：引文逐字出现在任务原文里（至少三个词）；每个检查都在守护集合里、不是公开检查，并且确实
  在 worker 的某个候选树上失败过（不能预先豁免）；一次运行最多 `waive_max_tests` 个。可以注明是哪条需求。豁免不改变
  需求的检查项；账本逐条列出。整个需求做不了时用 `submit(blocked=[{kind: "check_conflict", quote}])`。
- DONE 的条件（`queries.status_reasons`，空列表 = DONE）：
  1. 每条 actionable 需求都 verified，或 submitted 且复查者没有认定没做完；没有 open、没有 blocked；
  2. 每条需求所在的存档都在交付点的祖先链上（否则是“完成但未交付”）；
  3. 交付的是确认点（没有被降级）。
  受阻是诚实、正确的结束方式（收尾照常进行），只是不计为 DONE。

### 4.4 被拒信息与规则定位（模块 D）

- D1：runner 的 `reasons` 经 `JobOutcome` → `job_finished.reasons` → 拒绝消息（每个回归下一行原因）；`failure_log(test)`
  从最近一次包含它的作业日志里截出 traceback 段落（`verifier.extract_failure`）。
- D2：回归所在的测试文件在快照的 `dropped` 里时提示“以原始版本运行”（worker 改了测试文件不算数）。
- D3：`locate_started` 记下测试、坏端（快照）、段号与段起点。区间内的点 = 段起点存档 + 同段内坏端之前的可测快照（连续
  相同的树只取一张）+ 坏端；每个点上某个测试的状态由作业结果推出（`verify.point_status`：pass / fail / unknown /
  running / untested），所以二分的中间状态不单独记事件。每个测试取“最后一次已知通过”为好端（自然处理了非单调的一过性
  失败），与之后第一次失败之间取中点跑那个测试单元；跑不出结果的点记为 unknown 跳过；达到 `locate_max_steps` /
  `locate_max_sec` 就给出已缩小的区间。多个测试共享作业，按（好端, 坏端）分组写 `locate_concluded`；外壳算出组内
  “好 → 坏”的改动与 diff 后写 `regression_located`（带当时进行中的 todo 与会话）。
  触发（只针对 worker 的提交）：提交因回归被拒、提交的存档被降级后问题仍在。后台验证的快照结果直接复用为二分的点。二分时才按需
  测中间快照；预检不过的快照不可测，直接跳过（相当于 `git bisect skip`）。
- D4：`revert_change(located)` 逐文件三方合并（ours = 工作区，base = 坏端，theirs = 好端），全部干净才写回，有冲突就什么
  都不改（`gitops.revert_files`）。
- 学到的相关性：降级触发、且定位精确时，把“改动的源文件 → 失败测试所在文件”记为 `relation_learned`；`related_units`
  之后把它们加入选择。

### 4.5 诊断者（模块 E）

- 不从后台快照里推断“持续性回归”：那些都是中间态。`persistent_regression` 只剩降级后
  问题仍在（`trigger=demoted`）一种；之后没有任何同段快照上它通过就算“仍未解决”。
- 诊断：规则写 `diagnosis_requested`（同一（回归签名, 定位区间）只一次；同一签名第二次被拒时带上前一次的结论再诊断），
  外壳从图里组装输入（失败原因、测试源码、定位 diff、当时进行中的 todo 与它提到的需求原文、压缩摘要，限
  `diagnose_input_tokens`），用 `aux_llm` 调用，结果经 `record_diagnosis` 校验：`intentional=true` 的引文不在任务原文里就
  丢弃这一项。诊断不改变门的判定。

### 4.6 提交（唯一的完成声明）与复查者（模块 F）

`submit(summary, blocked=[{requirement, kind, reason, quote?}])`（`rules.request_submit`）：

1. 外壳强制拍一张快照；规则先校验受阻清单（需求在清单上、种类合法、有理由、check_conflict 的引文逐字在原文里），
   不合法就整个拒绝，什么都不记。
2. 快照的树就是链头：直接在链头上判定（`checkpoint_marked` 把链头标成 submit）。否则发起前台存档尝试（related 档位，
   带上还没完成的需求的证据检查），`submit_requested` 在尝试有结果之前写入。预检不过、回归、被取消 → 提交被拒
   （`render_submit` 给出失败的测试与原因，定位与诊断随之开始），worker 继续干活。
3. 存档有了（`checkpointed`）：先等证据检查的结果（缺就起 `evidence` 作业），再逐条判定 open 的 actionable 需求：
   证据检查全部通过 → verified；在受阻清单里 → blocked；有证据检查但没过 → 仍是 open，没过的检查记进
   `submit.failing` 与 `last_failure`；其余 → submitted（锚在这个存档上）。
4. 复查（`_start_reviews`）：还没复查过、被复查者重开的次数没到 `review_max_reopens` 的 submitted 需求，以及以
   insufficient_info 受阻的需求，按 `review_batch` 条一批发起 `review_started`；提交进入 `reviewing`。
5. 所有批次都有结果后（`finish_submit`）：还有 open 的 actionable 需求 → `returned`（清单交还 worker，带原因）；没有 →
   `accepted`，会话结束，运行收尾。

复查者（`driver._eff_review` + `runtime/review.py`）：输入是需求原文 + 按每条需求原文里的名字（反引号里的代码、带下划线
/ 驼峰 / 带点的标识符、文件路径、PR 号）筛出的相关 hunk + 全部改动文件的列表 + 剩余预算内的完整 diff，再加上 worker
的提交摘要、todo 与交接摘要（都标为自述）。它只能收紧：`no` / `partial` → `requirement_reopened(review_missing)`；受阻的
需求给出合理读法 → `requirement_reopened(review_reading)`；`yes` 什么都不做；调用失败记为 `failed`，也什么都不做。
截止收尾时复查结果只进账本。账本口径（`render.requirement_category`）：verified / reviewed（自报，复查通过）/
self-reported（复查没跑完或没有复查）/ done-not-delivered / blocked / open。在 SWE-EVO 这类题上，大部分需求会落在
reviewed：图能提供硬保证的只有“不回归”和“每条需求都被过问过”，需求是不是真做对了，靠的是复查。

**模型停下不调用工具时**（`BelaySession._on_stop`）：第一次在同一个会话里追问一句（“If every requirement is done, call
submit; otherwise continue working.”）；再次停下就当作提交（`implicit=true`，最后的回复作为摘要），结果作为一条
system-reminder 交还给它，会话继续；被接受时会话结束。一个会话最多 `max_implicit_submits` 次隐式提交，之后会话结束。
图的正确性不依赖模型调不调 submit。

### 4.7 todo 与恢复点（模块 H）

- todo_write 的列表每次立即镜像到图上（`rules.update_todos`）：按标题匹配保持 id 稳定；从列表里删掉的没完成的条目
  删除，完成的保留；条目文字里的 `R12` 关联到需求，不写也没关系。
- 新标为 completed 的条目：外壳先强制拍一张锚点快照，写 `todo_completed`；锚点被链上同段、快照序号不小于它的存档
  包含时写 `todo_anchored`，那个存档标成 todo 存档；回退使锚点不再在链上时写 `todo_invalidated`。
- todo 提醒（学 Claude Code，`BelaySession._todo_notes`）：第一次改文件时还没有 todo，提醒一次（“todo 是你在上下文被
  重置后能拿回的进度；简单任务可以不列”）；之后每 `todo_reminder_turns` 轮没更新再提醒，一个会话最多
  `todo_reminder_max` 次。
- `queries.resume_point`：基底 = 链头，部分快照 = 最新一张，当前 todo = in_progress 的那条。部分改动默认保留在工作区，
  开场上下文展示链头 → 最新快照的 diff，并预读链头以来改过的文件和当前 todo 提到的文件。
- 交接时机（`session._manage_context`）：到软阈值（`handoff_soft_tokens`，默认等于 `l2_tokens`）且有进行中的 todo 时暂缓
  L2，等下一个自然停顿点再交接：勾掉一条 todo、模型要跑测试（`model_test` 快照）、拿到提交结果；没有进行中的 todo
  时照旧 L2/L3；硬阈值（`l4_tokens`）照旧强制交接。

## 5. 分层开场上下文（模块 I）

`build_context`，按顺序：任务原文 → 需求清单索引（只列 actionable 的 id + 摘要，冻结后逐字不变，前缀缓存整次运行都能
命中）→ 待处理的问题（仍未解决的持续回归与降级、定位结果、诊断、被拒的提交）→ 需求状态（计数；verified 与
submitted 折叠成区间；open 与 blocked 逐条列出，重开过的带原因）→ 你的 todo → 你的交接摘要（模型写的）与中断前
最后几个动作 → 工作区（链头、确认点、链头以来的 diff）→ 离开期间（只在恢复时）→ 回归门 → 一句“做完了就调 submit”。
受保护段：任务原文、需求索引、待处理的问题、todo、摘要、工作区；每段各有上限（`opening_caps`），被折叠的段都留下
查询入口（`board(...)`、`failure_log`）。v6 的“当前焦点”和“建议顺序”删掉了（`core/suggest.py` 一并删除）。
`resume_reminder`：原样接上对话时，把待处理的问题、需求状态与离开期间作为 system-reminder 追加。

worker 能看到的工具（13 个）：read_file、edit_file、write_file、list_files、grep_search、bash、todo_write、explore、
`submit`、`board`（只读：清单与状态、需求详情、存档链、基线失败列表）、`failure_log`、`revert_change`、`waive_check`。
系统提示里讲 harness 的部分只有四句：后台在存档和测试；有一份需求清单、上下文会被恢复；多步工作列 todo；做完了调
submit。

## 6. 会话、压缩与交接

| 层 | 触发 | 做什么 |
| --- | --- | --- |
| L0 | 单个工具结果超过 `l0_chars` | 全文存附件，上下文保留开头、报错行、结尾和路径 |
| L1 | 上下文超过 `l1_trigger_tokens`（默认 50 万） | 先只清命令 / 测试输出，保留 `read_file`；仍超过再一起清 |
| L2 | 上下文超过 `l2_tokens`（默认 70 万），且没有进行中的 todo | 旧对话 → `build_context(mode=compaction)` + 最近一段原文 + 重读最近改过的文件 |
| L3 | L2 之后仍超过目标 | 模型只写图里没有的东西，摘要入图 |
| L4 | 软阈值后的下一个自然停顿点，或超过 `l4_tokens`（默认 76 万），或压缩次数达到上限 | 交接摘要；结束会话；新会话从恢复点开始 |

会话的结束原因：submitted（提交被接受）| done（不调用工具，隐式提交用完或追问关闭）| handoff | deadline | max_turns |
crash | stuck | runtime_crash。`next_step`：提交被接受 → 收尾；截止预留 → 收尾；连续 `max_idle_sessions` 个会话没有进展
→ 收尾；连续崩溃 → 收尾；有提交或前台存档在判定 → 等；会话结束了但没有提交 → 开新会话。

每条追加进对话的消息都写进轨迹（`message` 记录），整体替换（开场、L1–L3、提醒）时写 `messages_checkpoint`（blob）：
读盘重放 = 最后一个 checkpoint + 之后的 message 记录，没有结果的 `tool_use` 补一条“中断，效果未知”。

## 7. 恢复（模块 G）

| 中断 | 做法 |
| --- | --- |
| 会话崩溃（模型接口多次重试仍失败），runtime 仍在 | 内存重试：`ModelCallFailed` → 退避 → 原样再调用，附上离开期间的变化（`session_resumed(memory)`）；上下文本身的问题（400 / 413 / “too long”）或连续失败达到 `resume_max_failures` 改开新会话 |
| runtime 进程崩溃，容器仍在 | `reconcile`：CAS 对账；有完成标记的作业补收结果，进程组还活着的重新接上（G5），其余记 unknown 重跑；停机不超过 `resume_max_downtime_sec` 且轨迹读得出来就读盘重放（`session_resumed(replay)`），否则开新会话 |
| 容器 / 工作区 / 影子仓库丢失 | `resume(rebuild=True)`：从原始代码重建 0 号存档（树与提交必须一致）→ 按顺序 unbundle → 最后一次导出之后的快照记为 lost；丢了提交的存档按确定的提交重做，连树都没有就截链 → 恢复引用 → 工作区检出为最新一张已导出快照的原样树 → 重跑破坏探针 → `runtime_recovered(rebuilt=true)` |

- 镜像（G3）：每张快照、每个存档都有 ref；`driver.mirror` 按节奏（每 `mirror_every` 张快照、每个里程碑存档、会话结束、
  交付、挂起）导出增量 bundle（`git bundle create <新 ref> ^<上次已导出的提交>`，以 0 号基线提交为前提），经 base64 分段
  传回宿主机 `run_dir/git/<n>.bundle`；已导出的 ref 记在事件库的 meta 里。增量 bundle 累积到 `mirror_consolidate` 份时
  合并成一份完整的 `<n>-full.bundle`，旧文件删除。非里程碑存档（后台自动存档）不单独触发导出：它的树就是某张快照的候选树，提交是确定的，
  重建时按原来的父提交与日期原样重做。补丁镜像 `checkpoints/<k>.diff` 也只写里程碑。
- 恢复的第一步是补拍一张 `recover` 快照（G7）；离开期间的变化（G1）进入开场。
- 外层调度：`BelayRun.suspend()` = 强制快照 → 导出 bundle → 会话以 suspended 结束 → `run_suspended`。

## 8. 不变量（`core/invariants.py`）

1. 事件序号连续；快照序号从 1 连续。
2. 冻结后至少有一条 actionable 需求；context 需求永远是 open；verified 的需求，其证据检查在它的存档那棵树上全部
   PASSED；verified / submitted 的需求所在的存档在链上；submitted 的需求来自一次存在的提交。
3. 每个 worker 至多一个在判定中的提交。
4. 存档链从链头沿 parent 能走回 0 号；链上存档的快照序号严格递增；每个非基线存档来自一个没有回归的尝试；
   `confirmed` 等于链头最近的确认祖先；没有既确认又降级的存档。
5. anchored 的 todo，其锚点快照被链上某个同段存档包含。
6. 同一时刻最多一个尝试处于 advancing；每个 worker 前台、后台尝试各至多一个；同一个作业键最多一个非 unknown 作业。
7. 来源纪律（§2）；交付后封口。

## 9. 与计划的差异和补充决定（需要时可以改回）

- 回归门豁免：允许在有引文和失败证据时豁免具体的测试，代价是门不再完全由基线决定，所以每条豁免都进账本。
- 提交被接受后已经复查通过的需求，之后的提交不再复查（只复查新提交的、还没复查过的）。
- `rollback` 不再是工具；规则保留给恢复流程（容器重建后链上的存档丢了）。worker 用 `revert_change` 撤销定位出的改动。
- 规划器只产出需求清单（没有任务、依赖、优先级、拆分）；停滞时只给提示，不再重新规划。
- 降级模式下不做规则定位（二分作业会切换工作区）；后台只验证交接快照。
- 评测接入：`eval/agents/belay_agent.py`。准备（`prepare`）放在评测框架的 setup 阶段，`clock_started` 让预算从 run
  阶段开始计时；被取消时 `emergency_deliver` 不经过规则，直接按图把工作区检出为交付点。

## 10. 测试（`python -m pytest -q`，不需要容器和模型）

| 要求 | 测试 |
| --- | --- |
| 事件、推导、非法转换 | `tests/unit/test_reduce.py` |
| 规则：后台验证最新快照、新快照胜出、需求随检查项验证、提交（判定、证据失败、受阻、复查批次与上限、截止）、todo 与锚点、提升 / 降级 / 交付一致性、二分、只追查提交、回归门豁免、停滞、DONE 的条件 | `tests/unit/test_rules.py` |
| 重放一致性（提交、复查、todo、后台验证、定位、诊断、回退、抢占……随机驱动 40 个种子） | `tests/unit/test_replay.py` |
| 分层开场：段顺序、前缀稳定、重开原因、300 需求 / 100 会话仍在预算内、board 过滤 | `tests/unit/test_context.py` |
| 外壳辅助：traceback 截取、sys.path 映射、L1 保留读取、读盘重放、离开期间上限 | `tests/unit/test_runtime_helpers.py` |
| 端到端：提交被接受、复查交还清单、追问后隐式提交、回归被拒、交接、内存重试 / 读盘重放、截止交付 | `tests/integration/test_belay_run.py` |
| 长程：导入隔离、降级、验证与编辑并发、抢占、被拒信息与撤销、重新接上作业、从 bundle 重建、todo 停顿点交接、中途被杀、诊断者与复查者 | `tests/integration/test_long_run.py` |

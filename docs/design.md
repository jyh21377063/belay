# Belay 实现设计（v6：长程单 worker 的可靠性底座）

本文是《Belay 改进计划 v2（修订稿）》落到代码时的设计说明：计划里没写死、但实现必须定下来的东西，以及为什么这样定。
计划讲“要解决什么、机制要做对什么”，这里讲“这张图具体长什么样、怎么动”。目标场景：一个任务连续运行十几小时到一天，
中间经历几十次会话交接，runtime 进程与容器都可能丢失。

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

一次运行内部有两条并行的线：worker 在前台写代码；runtime 在后台拍快照、验证、推进存档、提升、定位。两者只通过事件
日志与通知交汇，后台从不碰 worker 的工作区（验证在验证槽位里跑；隔离无效时降级，见 §3.1）。

- `belay/core/` 全是纯函数：不做 IO、不调模型、不读时钟（`now` 作为参数传入）。
- `belay/runtime/` 是薄的命令式外壳：事件存储、git、验证槽位、会话循环、LLM 调用、镜像与恢复。
- **唯一写者**：`Runtime.submit()` 在一把锁里执行“规则 → 追加事件 → 更新视图”，所以不需要其他并发控制。

## 1. 事件

每条事件：`seq`（从 1 开始连续递增，同时是图的版本号）、`t`（墙钟秒）、`type`、`actor`、`source`、`payload`。

**来源纪律**（`invariants.check_log` / `llm_effects`）：`task_done`、`checkpoint_created`、`checkpoint_advancing`、
`checkpoint_confirmed`、`step_anchored`、`delivered` 只能来自 `rule` / `observed`；`llm` 的事件只能记录、重开、新增
（`diagnosis_recorded`、`review_recorded`、`checkpoint_labeled`、`progress_summary`），永远不引起完成、存档、提升。
由观察推断出的结论（学到的相关性、持续性回归）记为 `rule`。

| 类别 | 事件 | 来源 | 视图变化 |
| --- | --- | --- | --- |
| 运行 | `run_started` / `runtime_recovered` / `run_suspended` | rule / observed / rule | 预算、恢复与重建次数、丢失的快照、隔离状态 |
| | `deadline_reserve` / `finalize_started` / `delivered` | rule | 截止预留；收尾开始（不再开后台验证）；交付点、级别、落后多少 |
| 任务 | `plan_proposed` `requirement_frozen` `task_added` `task_split` | llm / rule / self | 同 v5 |
| | `task_claimed`（带 `head`）/ `task_released` / `review_requested` / `task_done` / `task_blocked` / `task_reopened` | rule / self | 当前焦点（没有时效）；认领时的链头作为恢复点的基底 |
| 步骤 | `steps_planned` / `step_started` / `step_done` | self_report | todo 列表即焦点任务的步骤计划；`step_done` 带锚点快照 |
| | `step_anchored` / `step_invalidated` | rule | 锚点被链上同段存档包含；回退使锚点不在链上 |
| 执行 | `session_started` / `session_resumed` / `session_ended` / `compacted` / `note` | rule / observed / … | `session_resumed.mode` = memory（内存重试）/ replay（读盘重放） |
| | `snapshot_taken` | observed | 快照时间线（序号、段号、原因、可测、持有的任务与步骤、快照提交）；同时更新 WIP |
| | `stall_detected` | rule | 同 v5 |
| 验证 | `job_started`（带 `where`：slot / workspace / live）/ `job_preempted` / `job_finished`（带 `reasons`） | rule / observed | 作业与失败原因 |
| | `baseline_recorded`（带 `isolation`） | observed | 守护集合、导入隔离是否有效 |
| 存档 | `checkpoint_attempted`（带 `snapshot`、`lane`、`kind`）/ `attempt_superseded` | rule | 前台 / 后台尝试；更旧的尝试被新存档取代 |
| | `checkpoint_advancing` / `checkpoint_created` / `checkpoint_rejected` | rule / observed | 父节点在推进时确定；新存档为暂存（related）或确认（full） |
| | `checkpoint_confirmed` / `checkpoint_demoted` | observed / rule | 全量通过 → 确认点前移；确认过的回归 → 降级 |
| | `rollback` | rule | 段号 +1；确认点退回链上最近的确认祖先 |
| 定位 | `persistent_regression` / `locate_started` / `locate_concluded` / `regression_located` / `relation_learned` | rule / rule / rule / observed / rule | 见 §3.4 |
| LLM | `diagnosis_requested` / `diagnosis_recorded` | rule / llm | 诊断者（只解释） |
| | `review_started` / `review_recorded` | rule / llm | 复查者（只收紧：只能重开） |
| | `checkpoint_labeled` / `progress_summary` | llm | 没有步骤时的兜底 |

去掉的事件：`lease_renewed`、`lease_expired`（单 worker 下持有没有时效）、`wip_recorded`（并入 `snapshot_taken`）。

## 2. 三个视图

全部定义在 `belay/core/model.py`，是不可变 dataclass；`reduce` 返回新图（结构共享，不修改旧图）。

- 任务图：`Requirement`、`Task`（新增 `claimed_head`、`passed_checks`、`review*`、`history`）、`Step`。
- 执行状态：`Session`（`ended_seq`、`resumes`）、`Lease`（当前焦点，没有 TTL）、`Wip`、`Snapshot`、`Job`、`Persistent`、
  `Locate`、`Diagnosis`；`graph.epoch` / `graph.epoch_base`（段号 → 段起点存档）；`graph.isolation`。
- 存档链：`Attempt`（`snapshot`、`epoch`、`lane`、`kind`、状态新增 `superseded`）、`Checkpoint`（`snapshot`、`epoch`、
  `kind` ∈ auto/step/milestone/review/handoff/final/baseline、`level` ∈ provisional/confirmed、`demoted`、`label`）；
  `graph.head` 与 `graph.confirmed`（最新确认点，永远是链头的祖先或就是链头）。

### 三个基准

| 用途 | 基准 | 实现 |
| --- | --- | --- |
| 交付 | 最新的确认点 | `queries.delivery_checkpoint`；除基线外没有确认点时按 `deliver_unconfirmed` |
| `rollback` 的默认目标 | 最近的里程碑（暂存或确认） | `queries.latest_milestone`（kind ∈ step/milestone/review/handoff/final/baseline） |
| 容器重建 | 整条链、全部快照与最新快照 | `recovery.rebuild_container` |

## 3. 关键规则

### 3.1 独立目录验证（模块 A）

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
  `workspace_lock` 上互斥；不做任何后台验证（不自动存档、不提升、不定位、不做持续性检测），只在 worker 本来就在等待时
  验证（手动存档、ready_for_review、会话结束与交接——这时驱动等验证结束再开新会话、收尾）。基线用工作区上的两次。
- 调度：`RunnerVerifier` 的槽位池 + 优先级队列（`verify.job_priority`：1 收尾与基线 / 2 worker 在等的 / 3 提升 /
  4 自动存档）。第 1、2 档到达而槽位被第 3、4 档占着时取消低档作业（TERM）并重新排队，写 `job_preempted`，不算丢失。
  第 3、4 档以 `nice` 运行，可选限制并发（`background_cpu_limit`）。
- 作业进程：`setsid` 起会话，`timeout --foreground` 保证取消信号能到达 runner；外层 shell 用 trap 挡住 TERM，保证写完成
  标记；进程组 id 由作业自己写。启动命令先把自己的输出换成 `/dev/null`，launch 立即返回（v5 里 launch 实际会等作业跑完）。

### 3.2 自动快照与后台存档（模块 B）

- 快照时机（`driver.take_snapshot`）：写类工具之后按限流（`snapshot_min_interval_sec` 或 `snapshot_min_writes`）；模型跑
  测试 / 构建的 bash 命令执行前一定拍（`model_test`）；会话结束、交接、截止、`step_done`、手动存档、review、按门自查、
  回退与撤销之后、恢复开始时跳过限流。树与上一张相同就不记。
- 每张快照是一个确定的提交（树 = `{raw: 原样树, cand: 候选树}`，父提交是上一张快照），ref 为 `refs/belay/snap/<n>`：
  防 gc，也让 git bundle 能增量导出。
- 预检：改动的 `.py` 文件用 `compile()` 检查语法（不写 `.pyc`）；可配 `precheck_cmd`。失败的快照标为不可测：不进验证
  队列，但留在时间线上。
- 后台线（`rules.schedule_background`）：同一时刻最多一个后台尝试。优先验证还没被包含的步骤锚点与比链头新的交接快照
  （最早的先，到来时取代正在等结果的自动尝试）；否则取最新的可测快照（原因 ∈ `AUTO_REASONS`，为前台意图拍的快照由
  发起者自己验证）。
- 新快照胜出：前台、后台尝试各至多一个；链头的快照序号不小于尝试的快照序号（同段）时，尝试被 `attempt_superseded`
  取代（它带着的 review 任务转到链头上判定）；父节点在 `checkpoint_advancing` 时才确定，CAS 用它校验。同一批级联里两个
  尝试同时完成时，第二个等第一个落地后再推进（多半随即被取代）。
- 进展（`reduce._progress`）只来自：任务完成、确认点前移、某个任务的检查项第一次在存档上通过、步骤锚定、手动 / 步骤 /
  review 存档。自动存档与交接存档不算。

### 3.3 两级存档链（模块 C）

- related 档位通过 → 暂存点；full 档位通过（例如 related 升级为 full、收尾）→ 直接是确认点。
- 提升（`schedule_promotion`）：验证队列有空闲时，只看最新确认点与最近一次降级之后的暂存点，取最新的跑全量（更老的
  跳过），同一时刻只提升一个。全量通过（回归先确认重跑）→ `checkpoint_confirmed`；确认过的回归 → `checkpoint_demoted`，
  它之后的暂存点是 suspect，直到它们自己跑完全量。
- 降级后对最新的可测快照只跑这几个失败的测试（`recheck`）：仍失败 → `persistent_regression(trigger=demoted)` → 通知
  worker + 定位 + 诊断；已通过 → 只记录。
- 收尾（`driver._finalize`）：`finalize_started` 取消后台尝试 → 对当前 WIP 做一次 full 前台尝试 → 链头仍是暂存点且还有
  时间就提升它 → 取消剩下的作业 → 交付最新的确认点（`delivered` 带 `level`、`lag`、`not_delivered`）。
- 交付一致性：`done_checkpoint` 不在交付点祖先链上的任务记为“完成但未交付”，DONE 要求没有这类任务、没有未解决的任务，
  并且交付的是确认点（`rules.final_status`）。

### 3.4 被拒信息与规则定位（模块 D）

- D1：runner 的 `reasons` 经 `JobOutcome` → `job_finished.reasons` → 拒绝消息（每个回归下一行原因）；`failure_log(test)`
  从最近一次包含它的作业日志里截出 traceback 段落（`verifier.extract_failure`）。
- D2：回归所在的测试文件在快照的 `dropped` 里时提示“以原始版本运行”；`run_check(as_gate=true)` 对当前工作区拍快照、
  剔除测试改动，在槽位里以第 2 档运行。
- D3：`locate_started` 记下测试、坏端（快照）、段号与段起点。区间内的点 = 段起点存档 + 同段内坏端之前的可测快照（连续
  相同的树只取一张）+ 坏端；每个点上某个测试的状态由作业结果推出（`verify.point_status`：pass / fail / unknown /
  running / untested），所以二分的中间状态不单独记事件。每个测试取“最后一次已知通过”为好端（自然处理了非单调的一过性
  失败），与之后第一次失败之间取中点跑那个测试单元；跑不出结果的点记为 unknown 跳过；达到 `locate_max_steps` /
  `locate_max_sec` 就给出已缩小的区间。多个测试共享作业，按（好端, 坏端）分组写 `locate_concluded`；外壳算出组内
  “好 → 坏”的改动与 diff 后写 `regression_located`（带当时持有的任务、步骤与会话）。
  触发：前台 / 步骤 / 交接尝试因回归被拒、降级后问题仍在、持续性回归。
- D4：`revert_change(located)` 逐文件三方合并（ours = 工作区，base = 坏端，theirs = 好端），全部干净才写回，有冲突就什么
  都不改（`gitops.revert_files`）。降级与持续性回归的通知把它列为首选、`rollback` 为备选。
- 学到的相关性：降级触发、且定位精确时，把“改动的源文件 → 失败测试所在文件”记为 `relation_learned`；`related_units`
  之后把它们加入选择。

### 3.5 持续性回归、诊断者（模块 E）

- 持续性回归（`rules.detect_persistent`）：同一守护测试在当前段最近 `persist_k` 个可测快照上都失败；或 worker 自己的
  `run_check` 也看到它失败而最新快照上它也失败。一过性失败不触发。之后没有任何同段快照上它通过就算“仍未解决”。
- 诊断：规则写 `diagnosis_requested`（同一（回归签名, 定位区间）只一次；同一签名第二次被拒时带上前一次的结论再诊断），
  外壳从图里组装输入（失败原因、测试源码、定位 diff、当时的任务 / 步骤 / 需求原文 / 笔记 / 压缩摘要，限
  `diagnose_input_tokens`），用 `aux_llm` 调用，结果经 `record_diagnosis` 校验：`intentional=true` 的引文不在任务原文里就
  丢弃这一项。诊断不改变门的判定。

### 3.6 复查者（模块 F）

- 任务进入 `done_unverified` 时异步复查一次；收尾前 `next_step` 返回 `review`，对还没复查过的 `done_unverified` 与以
  `insufficient_info` 受阻的任务发起复查，进行中时返回 `wait: review in progress`。
- `no` / `partial` → `task_reopened(review_missing)`；受阻任务给出合理读法 → `task_reopened(review_reading)`；`yes` 什么都
  不做。每个任务最多被复查者重开 `review_max_reopens` 次，之后不再复查。截止收尾时只进账本。
- 账本口径（`render.task_category`）：verified / reviewed / self-reported / done-not-delivered / blocked / open。

### 3.7 步骤与恢复点（模块 H）

- 认领后 todo 列表即步骤计划：每次 `todo_write` 立即写 `steps_planned`（按标题匹配保持步骤 id 稳定）；未持有任务时写成
  普通笔记。新标为 completed 的条目按 `step_done` 处理（先强制拍锚点快照）。
- `step_done`：锚点快照以第 2 档在后台验证（被拒要通知并定位）；锚点被链上同段、快照序号不小于它的存档包含时写
  `step_anchored`；回退使锚点不再在链上时写 `step_invalidated` 并清空锚点（否则新段的存档会把它重新判成 anchored）。
- `queries.resume_point`：基底 = 当前步骤之前最后一个锚定步骤所在的存档，没有就是认领时的链头；部分快照 = 最新一张。
  部分改动默认保留在工作区，开场上下文展示基底 → 最新快照的 diff，并预读涉及的文件。
- 交接时机（`session._manage_context`）：到软阈值（`handoff_soft_tokens`，默认等于 `l2_tokens`）且有进行中的步骤时暂缓
  L2，下一次 `step_done` 后交接；没有步骤时照旧 L2/L3；硬阈值（`l4_tokens`）照旧强制交接。
- 没有步骤时的兜底：里程碑存档（以及每 `label_every` 个自动存档）用 `aux_llm` 生成一行标签（手动存档的 summary 直接作
  标签）；长时间中断或重建后对对话尾部生成进度摘要。

## 4. 调度建议与分层开场上下文（模块 I）

- `suggest`：依赖改为排序提示（依赖未完成的排后，但仍可认领）；一次建议只建一次反向依赖索引。
- `build_context`：九段，按顺序：任务原文、需求索引（id + 一行摘要，不带状态，冻结后逐字不变）、当前焦点（恢复点：
  任务、需求引文、检查结果、步骤、部分改动 diff、本任务全部会话的笔记、交接摘要、崩溃时中断前最后几个工具调用）、
  待处理的问题（仍未解决的持续回归与降级、定位结果、诊断、最近一次被拒）、离开期间（上个会话结束后的事件按重要性取前
  `away_top` 条，其余计数；工作区变化的文件）、工作区、进度总览（需求按状态计数，未完成与受阻的展开，已完成的压成
  id 区间，太多时只展开与当前任务相关的）、下一步、回归门。前四段受保护，每段各有上限（`opening_caps`），被折叠的段都
  留下查询入口（`board(status=…)`、`task(id=…)`、`history`、`failure_log`）。
- `resume_reminder`：原样接上对话时，把当前焦点、待处理的问题与离开期间作为 system-reminder 追加。

## 5. 会话、压缩与交接

| 层 | 触发 | 做什么 |
| --- | --- | --- |
| L0 | 单个工具结果超过 `l0_chars` | 全文存附件，上下文保留开头、报错行、结尾和路径 |
| L1 | 上下文超过 `l1_trigger_tokens`（默认 50 万；按数量触发默认关闭） | 先只清命令 / 测试输出，保留 `read_file`；仍超过再一起清 |
| L2 | 上下文超过 `l2_tokens`（默认 70 万），且没有进行中的步骤 | 旧对话 → `build_context(mode=compaction)` + 最近一段原文 + 重读最近改过的文件 |
| L3 | L2 之后仍超过目标 | 模型只写图里没有的东西，摘要入图 |
| L4 | 软阈值后的下一次 `step_done`，或超过 `l4_tokens`（默认 76 万），或压缩次数达到上限 | 可选交接摘要；结束会话；新会话从恢复点开始 |

每条追加进对话的消息都写进轨迹（`message` 记录），整体替换（开场、L1–L3、提醒）时写 `messages_checkpoint`（blob）：读盘
重放 = 最后一个 checkpoint + 之后的 message 记录，没有结果的 `tool_use` 补一条“中断，效果未知”。

## 6. 恢复（模块 G）

| 中断 | 做法 |
| --- | --- |
| 会话崩溃（模型接口多次重试仍失败），runtime 仍在 | 内存重试：`ModelCallFailed` → 退避 → 原样再调用，附上离开期间的变化（`session_resumed(memory)`）；上下文本身的问题（400 / 413 / “too long”）或连续失败达到 `resume_max_failures` 改开新会话 |
| runtime 进程崩溃，容器仍在 | `reconcile`：CAS 对账；有完成标记的作业补收结果，进程组还活着的重新接上（G5），其余记 unknown 重跑；停机不超过 `resume_max_downtime_sec` 且轨迹读得出来就读盘重放（`session_resumed(replay)`），否则开新会话 |
| 容器 / 工作区 / 影子仓库丢失 | `resume(rebuild=True)`：从原始代码重建 0 号存档（树与提交必须一致）→ 按顺序 unbundle → 最后一次导出之后的快照记为 lost；丢了提交的存档按确定的提交重做，连树都没有就截链 → 恢复引用 → 工作区检出为最新一张已导出快照的原样树 → 重跑破坏探针 → `runtime_recovered(rebuilt=true)` |

- 镜像（G3）：每张快照、每个存档都有 ref；`driver.mirror` 按节奏（每 `mirror_every` 张快照、每个存档、会话结束、交付、
  挂起）导出增量 bundle（`git bundle create <新 ref> ^<上次已导出的提交>`，第一份以 0 号基线提交为前提），经 base64 分段
  传回宿主机 `run_dir/git/<m>.bundle`；已导出的 ref 记在事件库的 meta 里。
- 恢复的第一步是补拍一张 `recover` 快照（G7）；离开期间的变化（G1）进入开场。
- 外层调度：`BelayRun.suspend()` = 强制快照 → 导出 bundle → 会话以 suspended 结束 → `run_suspended`。

## 7. 不变量（`core/invariants.py`）

1. 事件序号连续；快照序号从 1 连续。
2. active / review 的任务有且只有一个持有者；active 的任务一定有持有者。
3. 需求冻结后每条需求至少被一个未拆分的任务链接；`blocked_by` 无环（排序需要）。
4. 存档链从链头沿 parent 能走回 0 号；链上存档的快照序号严格递增；每个非基线存档来自一个没有回归的尝试；
   `confirmed` 等于链头最近的确认祖先；没有既确认又降级的存档。
5. `done` 的检查在存档那棵树上全部 PASSED；`done_unverified` 没有检查且有存档。
6. anchored 的步骤，其锚点快照被链上某个同段存档包含。
7. 同一时刻最多一个尝试处于 advancing；每个 worker 前台、后台尝试各至多一个；同一个作业键最多一个非 unknown 作业。
8. 拆分出的子任务合起来覆盖父任务的链接。
9. 来源纪律（§1）；交付后封口。

## 8. 与计划的差异和补充决定（需要时可以改回）

- 手动 `checkpoint(summary)` 的 summary 直接作为里程碑标签；标签模型只给没有标签、没有步骤的存档补一行。
- todo 列表里新标为 completed 的条目等同于 `step_done`（计划只写了 `step_done` 工具）；两者都保留。
- 每个存档创建时都会导出 bundle（计划写的是“每个里程碑”）：链上存档的提交丢失会导致链断，bundle 很小，所以不区分。
- DONE 的条件按计划 F2：没有 open、没有 done-not-delivered、交付的是确认点；全部任务受阻时仍可能是 DONE（账本会写明
  blocked 的数量；以 insufficient_info 受阻的任务会先被复查）。
- 降级模式下也不做规则定位与持续性检测（二分作业会切换工作区）；诊断仍会以“相对最新确认点的 diff”为输入进行。
- 构建缓存种子额外复制了项目自己的 `.git`（有些测试会调用 git，例如 setuptools_scm）。
- 分片轮转的全量、竞争式分支、运行级 supervisor 都没有做（计划里是扩展点）。
- `eval/` 本轮没有改：`eval/agents/belay_agent.py` 仍需按 `BelayRun` 重写（0-1）。

## 9. 测试（`python -m pytest -q`，不需要容器和模型）

| 要求 | 测试 |
| --- | --- |
| 事件、推导、非法转换 | `tests/unit/test_reduce.py` |
| 规则：两条线、新快照胜出、提升 / 降级 / 交付一致性、二分（精确、跳过、非单调、跨回退、上限）、持续性回归、诊断与复查、步骤与恢复点、DONE 的条件 | `tests/unit/test_rules.py` |
| 重放一致性（快照、后台线、步骤、定位、诊断、复查、抢占……随机驱动 40 个种子） | `tests/unit/test_replay.py` |
| 分层开场：段顺序、前缀稳定、跨会话笔记、300 需求 / 1000 任务 / 100 会话仍在预算内、board 过滤 | `tests/unit/test_context_suggest.py` |
| 外壳辅助：traceback 截取、sys.path 映射、L1 保留读取、读盘重放、离开期间上限 | `tests/unit/test_runtime_helpers.py` |
| 端到端（v5 场景 + 内存重试 / 读盘重放） | `tests/integration/test_belay_run.py` |
| 长程：导入隔离三种夹具、降级、验证与编辑并发、抢占、D1–D4、重新接上作业、从 bundle 重建、步骤边界交接、步骤中间被杀、诊断者与复查者 | `tests/integration/test_long_run.py` |

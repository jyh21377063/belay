# Belay v5 实现设计

本文是 `Belay v5：用任务状态图管理长程 agent 的 runtime` 方案落到代码时的设计说明：方案里没写死、但实现必须
定下来的东西，以及为什么这样定。方案本身讲“为什么要有这张图”，这里讲“这张图具体长什么样、怎么动”。

## 0. 一句话结构

```
          worker 工具请求 / 验证器结果 / git 结果 / 时钟
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
      执行副作用（跑作业、git CAS、还原工作区……）      命令式外壳，结果再作为输入回来
```

- `belay/core/` 全是纯函数：不做 IO、不调模型、不读时钟（`now` 作为参数传入）。决定、推导、调度建议、上下文构建、
  压缩的规则部分、不变量都在这里，全部可以用普通单元测试覆盖。
- `belay/runtime/` 是薄的命令式外壳：事件存储、git、作业、会话循环、LLM 调用、恢复对账。
- **唯一写者**：`Runtime.submit()` 在一把锁里执行“规则 → 追加事件 → 更新视图”，所以不需要其他并发控制。

## 1. 事件

每条事件：`seq`（从 1 开始连续递增，同时是图的版本号）、`t`（墙钟秒）、`type`、`actor`、`source`、`payload`。

- `actor`：`runtime` / `worker:<id>` / `verifier` / `planner` / `compactor`
- `source`：`observed`（观察）/ `rule`（规则）/ `llm`（LLM 提议，已被规则校验）/ `self_report`（自述）

**来源纪律**（由不变量检查）：`task_done`、`checkpoint_created` 的来源只能是 `rule` 或 `observed`；
`self_report` 与 `llm` 的事件永远不能让任务进入完成、不能推进存档链。worker 发起的认领、放弃、声明做完、
报告受阻是被允许的转换（方案原文），它们由规则接受后写成 `source=rule, actor=worker:w1`，
其中的文字（笔记、原因）作为自述字段保留。

### 事件表

与方案一致，只增加了一种：`checkpoint_advancing`（三步推进存档的第一步，恢复对账必须有它）。

| 类别 | 事件 | 谁写 | 视图变化 |
| --- | --- | --- | --- |
| 运行 | `run_started` | runtime | 任务原文、预算、截止时间、验证器能力 |
| | `runtime_recovered` | runtime | 恢复次数、停机时长 |
| | `deadline_reserve` | rule | 进入截止预留：停下 worker，由 runtime 做全量存档并交付 |
| | `delivered` | rule | 运行结束（DONE / INCOMPLETE），交付的存档 |
| 任务 | `plan_proposed` | planner(llm) | 只记录提议与校验报告，不改变图 |
| | `requirement_frozen` | rule | 一次性写入全部需求并冻结 |
| | `task_added` | llm / self_report | 新任务 |
| | `task_split` | llm / self_report | 父任务 → `split`，子任务继承链接 |
| | `task_claimed` / `task_released` | rule | 租约与任务状态 |
| | `review_requested` | rule | `active → review` |
| | `task_done` | rule | `review → done / done_unverified` |
| | `task_blocked` | self_report | `→ blocked`，原因与引文 |
| | `task_reopened` | rule | `→ active`（租约还在）或 `open` |
| 执行 | `session_started` / `session_ended` | runtime | 会话、开场上下文摘要、结束原因 |
| | `compacted` | compactor | 压缩层级、前后 token、L3 摘要 |
| | `lease_renewed` / `lease_expired` | rule | 租约 |
| | `note` | self_report | 笔记 / 待办 |
| | `wip_recorded` | observed | 未验证进度：基于哪个存档、改了哪些文件、diff 附件 |
| | `stall_detected` | rule | 停滞类型与处理（提示 / 重新规划） |
| 验证 | `job_started` / `job_finished` | rule / observed | 作业；同一 (树, 检查集合) 只跑一次 |
| | `baseline_recorded` | observed | 原始代码上两次运行的归类：pass / fail / flaky / skip |
| | `checkpoint_attempted` | rule | 一次存档尝试：候选树、档位、要跑的检查 |
| | `checkpoint_advancing` | rule | 验证通过，准备 CAS（先记意图） |
| | `checkpoint_created` | observed | CAS 成功，存档链前进 |
| | `checkpoint_rejected` | rule | 回归列表；链不动；原因写入 WIP |
| | `rollback` | rule | 还原工作区；必要时把链头退回，之后的存档作废 |

## 2. 三个视图

全部定义在 `belay/core/model.py`，是不可变 dataclass；`reduce` 返回新图（结构共享，不修改旧图）。

### A 任务图

- `Requirement(id, quote, summary, origin)`：`requirement_frozen` 之后不可改（reducer 拒绝任何修改）。
- `Task(id, title, description, links, blocked_by, parent, discovered_from, priority, checks, status, origin, ...)`
- 检查（Check）不单独建表：检查 id 就是测试 node id（`tests/test_x.py::test_y`）或公开检查 `cmd:<name>`；
  基线归类在 `graph.baseline`，公开检查列表在 `run.public_checks`。任务的 `checks` 就是 `verified_by` 边。
  worker 自己写的测试（基线里不存在）只是开发信号，不能作为 `verified_by`。

任务状态机：

```
            claim                ready_for_review          证据（规则）
  open ──────────────► active ─────────────────► review ─────────────► done / done_unverified
   ▲  ◄────────────── │  ▲                          │
   │     release      │  └──── reopened ────────────┘  （存档被拒 / 证据失败；租约还在）
   │                  │
   └─── reopened ─────┴──► blocked （report_blocked，任意未完成状态可进入；再次 claim 会先 reopen）
  open/active/blocked ──split──► split（子任务继承链接）
  done* ──reopened（rollback 使其存档作废）──► open
```

“可做”（ready）是推导的：`open` 且 `blocked_by` 全部完成（done 或 done_unverified）。

### B 执行状态

- `Worker(id, status, session, last_heartbeat)`
- `Session(id, worker, n, started_t, ended_t, end_reason, opening, peak_context, compactions, transcript, progress)`
  —— `progress` 记录这个会话里（包括它结束后 runtime 替它做的存档）有没有新证据，用于“连续两个会话没有进展”。
- `Lease(task, worker, acquired_t, expires_t)`
- `Wip(worker, base, tree, files, dropped, diff, last_rejection)`；worker 的笔记与待办在 `notes`（自述，单独标注）。
- `Job(id, key, tree, selection, purpose, state, results, sec, error)`；`purpose` ∈ baseline / verify / confirm / evidence / dev。

### C 存档链

- `Checkpoint(id, commit, tree, parent, attempt, tier, trigger, created_seq, files, tasks, abandoned)`；0 号是基线。
- `Attempt(id, worker, trigger, tree, base, tier, selection, tasks, jobs, phase, status, regressions, flaky, ...)`
- `graph.head`：链头。**存档 = (事件序号, git 提交)** 的一致切面。

一个树上的检查结果 = 所有在这棵树上完成的作业结果的合并（`results_for_tree`）。任务的“证据”就是它的检查在
存档那棵树上的结果，所以不需要单独存证据：作业按树哈希去重，证据按树哈希连接到存档。

## 3. 关键规则

### 3.1 存档：验证后比较并交换

1. 请求（worker 的 `checkpoint` / `ready_for_review`，或 runtime 在会话结束、交接、截止、收尾时发起）先由外壳
   观察工作区：快照成树 → **剔除测试路径下的改动**（测试文件恢复为原始版本）→ 候选树。
2. 规则写 `wip_recorded` 和 `checkpoint_attempted`：
   - 档位：平时 `related`（按文件路径规则挑选相关测试文件，宁多勿少；改了非 Python 文件、配置、conftest，
     或者一个相关测试都找不到，就升级为 `full`）；截止与收尾时 `full`。
   - 要跑的检查 = 相关守护测试所在的文件 ∪ 待验证任务的检查。
   - 同一 (树, 检查集合) 已有作业就复用，不再跑。
3. 作业结束后判定：守护集合（基线上两次都通过）中被选中的测试，候选上**失败、出错、被跳过、漏跑**都算回归。
   有回归时先对这些测试重跑一次确认（`confirm`），重跑通过的记为 `flaky`，不算回归（这仍是观察到的事实）。
4. 没有回归 → `checkpoint_advancing`（意图入日志）→ 外壳 `commit-tree`（提交日期取事件时间，所以重做得到同一个
   提交）+ `update-ref <新> <旧>` → `checkpoint_created`。有回归 → `checkpoint_rejected`，存档链不动，工作区
   原样保留，回归写入 WIP 的 `last_rejection`。
5. 候选树与链头相同：不产生尝试（没有东西可存）。`ready_for_review` 此时直接在链头上判定证据；收尾时链头的全量验证
   由 `verify_head` 在链头那棵树上起作业完成（有回归同样先确认），全量结果里出现的回归如实写进账本
   （`head_regressions`）—— 这就是分档验证可能漏掉的回归。
6. 收尾时被取消的验证作业不再重跑，依赖它的尝试直接拒绝；正在 CAS 的尝试不能中止，交付前等它做完。

### 3.2 任务完成

- `ready_for_review` → `review_requested` + 一次存档尝试（带上该任务的检查）。
- 尝试的结果决定任务：被拒 → `task_reopened(checkpoint_rejected)`；通过（新存档或未变）→ 在链头那棵树上看任务的
  检查：全部 PASSED → `task_done(verified)`；有失败 → `task_reopened(evidence_failed)`；缺结果 → 起 `evidence` 作业。
- 没有检查的任务在**进入存档后**成为 `done_unverified`。与方案的差别：方案写“声明做完即进入完成（未验证）”，这里
  要求它的工作已经在某个存档里。否则账本说“做完了”，交付物里却没有这部分改动。

### 3.3 租约

一个任务最多一个租约；worker 的任何工具调用都是心跳，租约剩余不到一半时写 `lease_renewed`（不是每次调用都写，
避免日志膨胀）。时钟 tick 时过期的租约写 `lease_expired`，`active` 任务回到 `open`（`review` 的任务保持 review）。
worker 交接给自己的新会话时，`session_started` 顺带续期它的所有租约。

### 3.4 运行的结束

`next_step(graph, worker, now)` 是纯函数：

1. 已交付 → 停止。
2. 剩余时间低于截止预留（全量验证实测耗时 × 系数，有上下限）→ `deadline_reserve` → 停 worker → 全量存档尝试 → 交付。
3. 没有可做的工作（没有 ready / active / review 的任务），且每条需求都至少有一个已完成或受阻的任务 → 收尾：
   全量存档尝试 → `delivered(DONE)`（要求链头那棵树有全量结果且无回归）否则 `INCOMPLETE`。
4. 没有可做的工作但需求没覆盖（例如只剩被受阻任务卡住的任务）→ `INCOMPLETE`。
5. 连续 2 个会话没有新证据 → `stall_detected(sessions_no_progress)` → `INCOMPLETE`。
6. 否则 → 用 `build_context` 开新会话。**会话结束不等于运行结束。**

交付物永远是链头的存档：`delivered` 的副作用把链头导出为补丁（测试路径的改动在进入存档前就剔除了），并可选地把
工作区检出为链头。

### 3.5 停滞

满足任一条件写 `stall_detected`（同一种停滞在出现新进展之前只记一次）：超过阈值时间没有新存档也没有任务完成；同一个
回归签名连续被拒 3 次；连续 2 个会话没有进展。处理按成本递增：第一次只记录（同一回归连续被拒时在上下文里如实说明被拒的原因；
按时长判定的停滞不告诉模型，避免变相按时间催促），再次发生时调规划器提议拆分，规则校验子任务仍覆盖原来的需求链接。

时间只在 runtime 内部使用（何时进入截止预留、何时收尾、停滞与存档提醒的触发）。给模型的任何文字——系统提示、开场上下文、
board、通知——都不包含剩余时间、已用时间或截止预留。

## 4. 调度建议与上下文构建

两者都只读图（`belay/core/suggest.py`、`belay/core/context.py`）。

- `suggest(graph, worker, now)`：候选 = 可做的任务 + 自己持有的任务；排序键依次为
  被重开的优先 → 解锁的下游越多越靠前 → 所链接需求还没有任何进展的靠前 → 规划器优先级 → id。
  截止预留期间不给建议：此时 worker 已被停下，由 runtime 收尾。`claim` 记录该任务在建议里的名次，用于评估采纳率。
- `build_context(graph, worker, budget, blobs)`：按方案的 9 段顺序生成，超预算时从下往上裁，前三段永远不裁。
  每段带来源标签；第 7 段（笔记）标为自述，第 8 段（压缩摘要）标为 LLM。`blobs` 是调用方按附件路径读出的内容
  （例如 WIP diff），函数本身仍是纯的。

## 5. 会话与压缩

| 层 | 触发 | 做什么 | 实现 |
| --- | --- | --- | --- |
| L0 | 单个工具结果超过阈值 | 全文存附件，上下文保留开头、报错行、结尾和路径 | `core/compact.py: l0_shrink` |
| L1 | 工具结果数或估算 token 达到阈值 | 最近 N 个结果保留，更早的换成一行占位（工具、参数、退出码、全文路径） | `l1_clear` |
| L2 | 上下文达到压缩阈值 | 旧对话 → `build_context` + 最近一段原文（在安全边界切，tool_use/tool_result 不拆开）+ 重读最近改过的文件 | `l2_rebuild` |
| L3 | L2 之后仍超过目标 | 模型只写图里没有的东西：关键判断及理由、放弃的做法、当前思路；摘要作为 `compacted` 事件入图，所以下一次 `build_context` 第 8 段就包含它 | `runtime/session.py` |
| L4 | 需要再次压缩但本会话压缩次数已达上限，或上下文超过重开阈值 | 可选地让模型写一份 L3 格式的交接摘要（记为 `compacted(level=4)`）；结束会话；runtime 尝试存档、记录 WIP，用 `build_context` 开新会话，租约保留 | `session` + `driver` |

每次压缩写 `compacted(level, before, after)`。开场与 L2 的前缀永远是任务原文 + 需求列表，让前缀缓存尽量命中。
worker 崩溃（模型接口多次重试仍失败、工具实现异常逃逸）→ `session_ended(crash)` → 新会话，租约保留；连续崩溃
`max_crash_restarts` 次则收尾。worker 卡死（超过 `idle_timeout_sec` 没有模型回复也没有工具调用；进行中的工具调用
不算）→ 取消会话，`session_ended(stuck)` → 新会话。

## 6. 恢复

启动时 `Runtime.open(store)`：加载最近的视图快照，重放之后的事件（`replay(快照, 尾部) == replay(全部)` 由测试保证）。
然后对账：

1. `advancing` 状态的尝试：查 git 引用。已经指向新提交 → 补记 `checkpoint_created`；还指向旧提交 → 重做 CAS
   （提交是确定的，重做安全）；都不是 → `checkpoint_rejected(cas_conflict)`。
2. `running` 的作业：有完成标记就补收结果；否则记为 `unknown`，由规则按同样的 (树, 检查集合) 重跑。
3. 未结束的会话 → `session_ended(runtime_crash)`；单 worker 下租约属于持久的 worker 身份，照常续期。
4. `runtime_recovered(downtime)`，截止时间按墙钟计算，然后回到 `next_step`。

三条不变量：先写事件再做副作用；副作用幂等（作业按键去重、存档用 CAS、提交确定）；状态只在宿主机（事件库、快照、
每个存档的补丁镜像都在运行目录里）。

## 7. 不变量（`core/invariants.py`）

在事务边界上成立（一个请求由规则产生的一批事件整体提交，例如冻结需求后逐个加入初始任务）；测试里每个事务之后都检查，
运行时由 `check_invariants` 开关控制（违反即抛出，不写日志）。

1. 事件序号连续，图的版本号等于最后一条事件的序号。
2. 每个任务最多一个租约；有租约的任务状态是 active 或 review；active 的任务一定有租约。
3. 需求冻结后每条需求至少被一个未拆分的任务链接；任务链接的需求都存在；`blocked_by` 无环。
4. 存档链从链头沿 parent 能走回 0 号；每个非基线存档都来自一个经过 advancing 的尝试，且该尝试没有回归。
5. `done` 的任务：存档在链上（未作废），它的每个检查在那棵树上都是 PASSED；`done_unverified` 没有检查且有存档。
6. 同一时刻最多一个尝试处于 advancing；同一个作业键最多一个非 unknown 作业。
7. 拆分出的子任务合起来覆盖父任务的链接。
8. 来源纪律：`task_done` / `checkpoint_created` 不来自自述或 LLM。
9. 交付后不再有任务、存档类事件。

## 8. 机制开关（`core/config.py: BelayConfig`）

`suggest`、`graph_context`（关掉即“Belay − 图上下文”：开场与 L2 不用 `build_context`，改用模型写的完整摘要）、
`confirm_regressions`、`protect_tests`、`stall`、`checkpoint_tier`（related / full）以及各层压缩的阈值。

## 9. 与 B 组的关系

B 组（`belay/worker/` + `eval/agents/flat_agent.py`）保持不变：它是“同一个 worker、同样的工具、不用图”的对照。
Belay 的会话循环（`belay/runtime/session.py`）复用同一套工具实现（`belay/tools/`）、同一个模型客户端与相同的
系统提示主体，只多出 Belay 工具、分层压缩和 runtime 管理的会话生命周期。

## 10. 测试（`python -m pytest -q`，不需要容器和模型）

| 要求 | 测试 |
| --- | --- |
| 事件类型、三个视图的推导函数 | `tests/unit/test_reduce.py`：事件格式与来源、`apply` 不修改输入、快照往返、十类不合法转换被拒、交付后封口 |
| 状态转换与不变量 | `tests/unit/test_rules.py`：认领 / 租约续期与过期、验证后完成、回归拒绝与重开、漏跑与跳过算回归、不稳定确认、证据失败、链头判定、作业去重与丢失重跑、追加与拆分、受阻与引文、回退、截止预留、停滞升级、会话结束 ≠ 运行结束、DONE 的条件、崩溃上限、来源纪律 |
| 纯函数重放一致性 | `tests/unit/test_replay.py`：40 个随机种子，作业乱序完成 / 丢失 / 取消、CAS 失败、回退、拆分、会话起止；每个事务后检查不变量；`replay(日志) == 实时的图`、任意前缀一致、快照 + 尾部 == 全部、事件经 JSON 往返后重放不变；另有覆盖率断言 |
| Belay 工具、规划器、首次开场上下文 | `test_planner.py`（校验反馈重试、机械兜底）、`test_context_suggest.py`（9 段顺序、从下往上裁、前三段不裁、来源标注）、集成测试里的开场断言 |
| 验证后 CAS 推进存档、交付物取自最近的存档 | 集成：`test_happy_path_done`、`test_regression_rejected_and_test_changes_are_not_delivered`、`test_deadline_delivers_the_latest_checkpoint`、`test_run_check_wait_and_rollback` |
| L0–L4 压缩与交接、每次开新会话都用 build_context、会话结束 ≠ 运行结束 | 单元：`test_verify_plan_compact.py`（L0/L1/L2 的配对与纯度）；集成：`test_l2_compaction_…`、`test_l3_summary_…`、`test_l4_handoff_…`、`test_session_end_is_not_run_end`、`test_repeated_idle_sessions_…`、`test_worker_crash_…`、`test_stuck_worker_…` |
| 恢复 | 集成：`test_runtime_crash_around_cas_is_reconciled`（CAS 前 / CAS 后崩溃，只创建一次存档，引用正确）、`test_runtime_crash_with_a_lost_job`（丢失的作业按同一个键重跑）、`test_stall_escalates_to_replan_and_split` |
| 分层规则 | `tests/unit/test_layering.py`：core 不做 IO、不读时钟；tools / worker 不认识 runtime；容器脚本只用标准库；两处测试路径规则一致 |

## 11. 没有做的

- 多 worker（数据模型按多 worker 设计，但只实现单 worker 的调度与工作区）。
- 容器整个丢失后的自动重建：机制（每个存档的补丁镜像 + `restore_workspace`）已实现并有测试，重建容器需要评测
  框架配合，留到接 eval 时做。
- 可插拔的检查作者（为无检查的任务补测试）：接口位置在 `task_added.checks`，暂未实现。
- eval 适配：`eval/agents/belay_agent.py` 依赖旧 runtime，需要按新接口（`belay.runtime.driver.BelayRun`）重写。

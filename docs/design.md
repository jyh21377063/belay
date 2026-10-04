# Belay 实现设计（v8：以合并为关口、复核者为裁判）

本文是 Belay 落到代码时的设计说明：计划里没写死、但实现必须定下来的东西，以及为什么这样定。目标场景：一个任务连续
运行几十分钟到一天，中间经历多次会话交接，runtime 进程与容器都可能丢失；有的任务有测试（SWE-EVO、ProMax），有的
没有公开测试、交付物是产物（LHTB）。

## v8 为什么改

v7 的验证实际只回答“有没有变差”，没有可靠地回答“需求做没做完”：没有检查项的需求在提交时一律记为 submitted，
worker 的一次笼统自述就等于完成；复查只在提交时触发，而且是一次只读 diff 的 LLM 调用；测试冲突由 worker 自己豁免；
暂存点 / 确认点、提交被拒、后台验证、提升各有一套触发。换到没有测试的 LHTB，整套验证几乎空转。

v8 的一句话：**worker 只管干活，runtime 只管存档，复核者是唯一的裁判，合并点是唯一的交付单位，账本是唯一的进度来源。**

1. **不绑定数据集。** 测试是复核者手里最强的一种证据，而不是“验证”本身。没有测试时流程不变，只是证据等级变低。
2. **合并与完成分开。** 合并的标准是“不比上一个合并点差”；需求是否完成记在账本里。合并是增量的，任何时刻超时都有
   可交付的版本。
3. **不依赖模型主动走流程。** worker 不认领、不必写需求编号；todo 勾选和 submit 只是“请现在看一眼”的信号，
   不直接改变任何需求状态。
4. **复核者可以放行，但必须带证据。** 每条判定附证据等级和合并点；规则校验证据等级是否站得住，worker 的自述只作线索。
5. **合并链是单调的。** 已完成的需求不会在后面的合并点上退回、分数不会下降，所以链头就是最好的结果，交付的永远是链头。
6. **事件日志仍是唯一事实来源。** 复核结论也是事件，账本由纯函数推导，崩溃后可重放。v7 的日志不能在 v8 上重放
   （`run_started` 没有 `version` 或类型不认识时直接报错），v7 的结果用它们的 `ledger.json`。

保留不动的部分：事件日志、影子 git 快照、独立验证目录、回归门、二分定位、崩溃与容器丢失的恢复、由账本生成会话开场。

## 0. 一句话结构

```
          worker 工具请求 / 快照 / 验证器结果 / git 结果 / 复核结论 / 时钟
                           │  (输入)
                           ▼
   rules.*(graph, 输入, now, cfg) ──► [事件草稿]      纯函数：决定（复核结论在这里校验）
                           │
                  store.append(事件)                  先写日志
                           │
           graph' = reduce(graph, 事件)               纯函数：推导视图
                           │
           effects_for(事件, graph') ──► [副作用]      纯函数：计划副作用
                           │
      执行副作用（作业、git CAS、bundle、定位 diff、诊断、复核会话……）   命令式外壳，结果再作为输入回来
```

## 1. 角色

| 角色 | 做什么 | 不做什么 | 代码 |
| --- | --- | --- | --- |
| worker | 读、改、跑测试；可选 todo；可选 submit（请求立即复核，可附受阻声明与豁免提议） | 不改需求状态；不豁免测试 | `runtime/session.py`、`tools/belay.py` |
| runtime | 拍快照；发起与节流合并请求；跑回归门；管理会话、预算、交接与收尾；交付链头 | 不判断需求是否完成 | `runtime/driver.py`、`core/rules.py` |
| 规划器 | 开工时拆需求清单，每条 actionable 需求写一句验收方法 | 运行中不改清单 | `runtime/planner.py`、`core/plan.py` |
| 复核者 | 处理合并请求：读代码、跑命令和测试、二分定位；判定是否合并、逐条判定需求、裁决豁免、测分数、写反馈 | 不改 worker 工作区；它的结论要经规则校验 | `runtime/reviewer.py`、`rules.decide_review` |
| 诊断者 | 回归被拒时解释原因（只是建议） | 不改变任何状态 | `driver._eff_diagnose` |
| 账本 | 由事件推导：需求的状态、证据等级、合并点；合并链 | 不存事件之外的状态 | `core/reduce.py`、`core/model.py` |

复核者与诊断者用 `aux_llm`（评测里默认与 worker 同一个模型、单独的客户端与录制文件）。v7 的“存档标签”LLM 角色
取消：合并点的标签就是复核者 verdict 里的一行 `summary`，它同时是影子仓库里合并提交的说明；交付时
`refs/belay/delivered` 指向交付的合并点。

## 2. 事件

每条事件：`seq`、`t`、`type`、`actor`、`source`、`payload`。来源纪律（`invariants.check_log` / `llm_effects`）：
`merged`、`merge_advancing`、`review_decided`、`waiver_granted`、`todo_anchored`、`delivered` 只能来自 `rule` /
`observed`；`requirement_judged` 来自 `rule`，只有复核者不可用时的 E0 完成与自述受阻来自 `self_report`；
复核者的原始结论 `merge_reviewed`（llm）之后必须紧跟同一次复核的 `review_decided`（rule）。

| 类别 | 事件 | 来源 | 视图变化 |
| --- | --- | --- | --- |
| 运行 | `run_started`（带 `version: 8`）/ `runtime_recovered` / `clock_started` / `run_suspended` | rule / observed | |
| | `deadline_reserve` / `finalize_started` / `delivered` | rule | 交付点 = 链头 |
| 需求 | `plan_proposed` / `requirement_frozen` | llm / rule | 清单冻结：引文、摘要、kind、checks、acceptance |
| | `requirement_judged` | rule / self_report | 状态 open / done / blocked，证据等级、证据、缺失项、所在合并点、来源（review / checks / self_report / rollback） |
| todo | `todos_updated` / `todo_completed` / `todo_anchored` / `todo_invalidated` | self_report / rule | 锚点被链上合并点包含即 anchored |
| 改进 | `improve_started` / `improvement_proposed` / `improvement_judged` / `improve_closed` | rule | 改进阶段与改进项（after_accept=improve，见 4.5） |
| 提交 | `submit_requested` / `submit_updated` | rule | pending → accepted / returned；被拒由 `merge_rejected` 推出 |
| 执行 | `session_*` / `compacted` / `snapshot_taken` / `stall_detected` | | |
| 验证 | `job_started` / `job_preempted` / `job_finished` / `baseline_recorded` | rule / observed | |
| 合并 | `merge_requested`（`lane`、`trigger`、`selection`）/ `merge_superseded` | rule | 合并请求 |
| | `merge_advancing` / `merged` / `merge_rejected`（`reason`：regression / requirement_regression / review / precheck / cancelled / cas_conflict） | rule / observed | 合并点 |
| | `rollback` | rule | 只由恢复流程使用 |
| 复核 | `review_started`（请求或只判定、focus、回归门结果）/ `merge_reviewed`（llm 原文、执行过的命令） | rule / llm | |
| | `review_decided`（合并与否、原因、被忽略的判断、判定、分数、标签、反馈）/ `review_cancelled` / `waiver_granted` | rule | |
| 定位 | `persistent_regression` / `locate_started` / `locate_concluded` / `regression_located` | rule / observed | |
| 诊断 | `diagnosis_requested` / `diagnosis_recorded` | rule / llm | |

## 3. 视图

全部定义在 `belay/core/model.py`（不可变 dataclass）。

- 需求账本：`Requirement`（`status` open / done / blocked，`level` E0–E3，`judgement` done / partial / not_done / blocked，
  `by`、`evidence`、`tests`（E3 依据）、`runs`（E2 依据）、`missing`、`checkpoint`、`review`、受阻信息、`misses`）、
  `Todo`、`Submit`、`Review`。
- 执行状态：`Session`、`Wip`、`Snapshot`、`Job`、`Persistent`、`Locate`、`Diagnosis`、`Waiver`（带裁决它的复核）。
- 合并链：`Attempt`（合并请求：`trigger`、`lane`、`selection`、`review`、`reviews`）、`Checkpoint`（合并点：`review`、
  `score`、`score_note`、`label`）；`graph.head`。没有暂存点 / 确认点、提升与降级。

### 证据等级

| 等级 | 含义 | 规则怎么校验 | 是否计为完成 |
| --- | --- | --- | --- |
| E3 测试 | 引用的测试在这个快照上通过，且至少一个在原始代码上不通过 | 测试必须在基线里、在这棵树上有结果（回归门全量结果或复核者的 `run_tests`）；不够就降为 E2 | 是 |
| E2 运行验证 | 复核者执行命令，观察到预期行为或产物 | 引用的命令编号（X1…）必须在这次复核里执行过；不够就降为 E1 | 是 |
| E1 代码审读 | 复核者读改动，判断已实现 | — | 是，报告单独统计 |
| E0 自述 | 只有 worker 的说法 | 复核者给的 E0 完成改记为 partial；只有复核者不可用时的自述才是 E0 | 否 |

规划器关联的检查（原始代码上不通过的已有测试）在合并点上全部通过时，规则直接记 E3（`by=checks`）。

### DONE 的条件（`queries.status_reasons`，空列表 = DONE）

交付的合并点上，每条 actionable 需求都完成（E1 及以上），或受阻且复核者认可。只有自述的完成（E0）、复核者没有认可
（或复核者不可用时自述）的受阻、没做完的需求都如实列为原因。

## 4. 合并

### 4.1 合并请求的触发（`rules.schedule_background`、`request_submit`、`driver._finalize`）

| 触发 | 车道 | 节流 |
| --- | --- | --- |
| 勾掉 todo：最新的锚点快照（哪怕 worker 之后又改了别的） | bg | 距上一次后台复核开始至少 `merge_todo_interval_sec`（默认 60 s，只防连续勾掉琐碎条目） |
| 交接 / 会话结束的快照 | bg | 不节流 |
| 跑通过（stable）：没有 todo / 交接边界时，最新的跑通过快照——worker 改过代码之后自己跑测试或普通运行命令（`merge_stable_generic`）且退出码为 0、之后这一批没再编辑时 driver 拍的（哪怕 worker 之后又改了别的；自上一张跑通过之后树没变的只当普通快照） | bg | 距上一次后台复核开始至少 `merge_stable_interval_sec`（默认 300 s，只是复核成本的上限） |
| 兜底（auto）：上面都没有时的最新可测快照 | bg | 距上一次后台复核开始或上一次合并（含 submit）至少 `merge_min_interval_sec`（默认 1200 s） |
| submit | fg | 不节流；取代正在进行的后台请求（作业按树复用，复核取消） |
| 收尾（最新快照还没合并） | fg | 在截止预留里做 |

只按回归门被拒的请求没有复核，不计入间隔。没有复核者时（`reviewer=False`）不节流：回归门只花 CPU。时钟（`tick`）
会在间隔到了时补发后台请求。同一时刻每个 worker 每条车道至多一个合并请求，整个运行同一时刻只有一个复核；前台需要
复核者时取代正在复核的后台请求。

后台怎么挑快照（`rules._background_candidate`）：候选是这个 worker 同一段里、比链头新、比它最近一次合并请求的快照新、
可测、树没请求过的快照。

- **边界快照优先**：勾掉 todo 的锚点快照与交接 / 会话结束的快照是 worker 自己停下来的完整节点，取其中最新的一张；
  没有边界快照才取最新的可测快照（auto），而且只取最新的那一张，它不能合并时不退回更早的中间状态。
- **合并期间攒下的 todo 合成一次**：后台请求进行中时，新勾掉的 todo 不抢占、不排队；请求结束后重新挑候选，直接取最新
  的边界。同一个 worker 的快照是累积的，较新的锚点包含较早的，所以合并它会一并锚定前面几个 todo（请求的标签列出它们）。
  不抢占是为了不饥饿：worker 勾 todo 比复核快时，抢占会让链头永远不前进。submit 仍然取代后台请求。
- **不回退**：不回到最近一次请求之前的快照（被拒的由 worker 按反馈接着改，下一个 todo 再来），也不越过 worker 撤回
  定位到的坏改动的快照（revert），因为更早的快照里还带着那段改动。
- `background=handoff` 与降级模式只取交接快照。

勾掉 todo 决定了后台什么时候合并、合并哪一张，所以会话循环（`session._todo_notes`）在工具结果后附 system-reminder
提醒 worker 勾选，但只按事件提醒、不强推（Claude Code 按空闲轮数重复提醒、每次重贴整份列表，被反馈过于频繁、诱导
“表演式”地改 todo）：

| 提醒 | 什么时候 | 上限 |
| --- | --- | --- |
| 做完了就勾掉（`todo_done_nudge`） | 这一轮跑了测试命令、有进行中的条目、上次更新 todo 之后成功改过文件、最近 `todo_done_nudge_quiet_turns`（3）轮没碰过 todo；一项进行中时带上它的标题，几项时按“这一组”说（最多列 3 个标题） | 同一组进行中的条目 `todo_done_nudge_max`（2）次（组变了重新计数），两次之间至少 `todo_done_nudge_gap_turns`（8）轮 |
| 第一次改文件、还没有 todo | 一次 | 1 |
| 很久没更新（`todo_reminder_turns`，30 轮） | 从上次更新 todo（或上次这类提醒）之后**第一次成功改文件**起计轮数：纯探索期（读代码、跑测试复现，没改文件）不计时；有进行中的条目时带上标题（几项时按组说），并说明勾选会触发复核；没有列表时换成“还没写列表”的说法 | 不限次数（`todo_reminder_max`=0）；提醒之后模型没更新 todo，下一次间隔 ×`todo_reminder_backoff`（2）直到 `todo_reminder_turns_max`（240），更新了就恢复 30 |

几项可以同时 in_progress：Belay 的 `todo_write` 描述（`tools.BELAY_OVERRIDES`）写“通常一次一项，一起做的几项可以同时
进行；每项做完就勾、一起做完的可以一起勾”。B 组 flat worker 共用的定义（`tools/shell.py`）与系统提示保持原样，对比基线
不变。快照记下拍摄时全部进行中的条目（`snapshot_taken.todos`），定位到的改动归因、诊断者的上下文、交接后预读的文件都按
这一组来。交接后的新会话里模型还没写过列表时，进行中的条目从图上取。提醒都写进会话轨迹（`notices`），以后可以统计“提醒 → 勾选”
的转化。worker 不勾时 auto 兜底仍会合并进度，提醒失灵的代价只是合并点不够干净。

### 4.2 一个合并请求怎么走（`rules.advance_attempt`）

1. 被链头超过（同一段、快照序号不大于链头）或就是链头的树 → `merge_superseded`。
2. 回归门：有测试配置时跑全量（`selection=None`；v7 的 related 档位与“相关测试先过、全量后验”的两级取消），
   回归先确认重跑，重跑通过的记为 flaky。没有测试配置时 `selection=()`，直接通过。
3. 单调检查：已完成（E3）的需求依据的测试在这棵树上不再通过 → `merge_rejected(requirement_regression)`。
4. 有回归：worker 在这次 submit 里提议了豁免 → 请复核者裁决；后台请求里有回归在上一个被拒的后台请求（另一棵树）
   上也挂、没被豁免、也还没记为持续性回归（`bg_waivers`）→ 请复核者判断要不要豁免（第一次出现的直接拒：多半是改到
   一半）；否则 `merge_rejected(regression)`（前台的立即定位与诊断，后台的同一回归连续两次才定位与诊断）。豁免一旦
   批准就写进 `g.waived`，之后所有回归门（后台与 submit）都不再检查它，豁免了也不提醒 worker；复核者拒绝豁免时才记为
   持续性回归、定位并提示 worker（只含没豁免的测试），测试重新通过之前同一组不再送审。
5. 没有回归 → 请复核者（复核者在忙就等；前台取代后台）。
6. 复核者的结论经规则校验后写 `review_decided`：批准 → `merge_advancing` → CAS → `merged`，随后写这次复核的需求判定
   与测试判定（`requirement_judged`）、锚定 todo、给提交下结论；不批准 → `merge_rejected(review)`，原因与反馈进账本
   并告诉 worker。
7. 复核者失败（没有给出结论）→ 重试 `review_retries` 次；仍失败就按复核者不可用处理：有回归门时只按回归门合并
   （合并点 `review=None`，提交的需求按自述 E0 记下）；没有回归门时只有 worker 自己 submit 的快照会合并，后台与
   截止时的快照（常常改到一半）不合并。

### 4.3 合并标准（`rules.decide_review`，纯函数）

- 回归门全过；复核者批准的豁免必须引用任务原文（至少三个词、逐字），测试必须在守护集合里并且确实在这棵树上失败，
  每次运行最多 `waive_max_tests` 个；没被豁免的回归 → 不合并。
- 复核者批准（`merge=true`）；它不批准时写明原因（破坏性改动、调试代码、伪造结果……）。
- 已完成的需求没有被这次改动弄坏：复核者说一条已完成的需求不再成立，必须有 E2 / E3 的证据；`regressed=true` →
  不合并；不是这次弄坏的（重新评估）→ 合并，需求退回 open（`reason=reassessed`）。只凭阅读（E1）的否定判断不改变
  已完成的需求（记为 note，防止复核者在同一棵树上来回改判）。证据等级只升不降。
- 分数：复核者执行过命令时报告的 `score` 不比链上最近一次测到的低超过 `score_tolerance`（相对，默认 2%）；没测分数
  的合并点不会把门槛清零。
- 合并不要求任何需求已经完成。

### 4.4 交付

交付点 = 链头（`queries.delivery_checkpoint`）。收尾（`driver._finalize`）：`begin_finalize` 取代后台请求 → 等 worker
最后的 submit → 最新快照还没合并就发起一次前台请求（`final` / `deadline`）→ 到点还没结束的作业与复核取消 →
`delivered`。最新快照的复核没做完时交付的仍是链头。截止预留 = max(下限, 全量回归门耗时 × 系数 + 余量 + 一次复核)，
最多占预算的 `reserve_max_frac`。

### 4.5 需求都做完之后：收尾还是继续改进（`after_accept`）

`after_accept=finalize`（默认）：submit 被接受即收尾，与之前完全相同。`after_accept=improve`（需要复核者）：需求都做完
不等于不能更好——LHTB 这类按比例计分、隐藏评分器看不到的任务，剩下的预算用来加强已交付的版本。合并链单调，交付的
永远是链头，所以继续做的风险只在复核者看不到的地方（SWE 的隐藏 P2P 测试、复核者自测的分数与隐藏评分器不一致），
SWE-EVO 不开。

- **复核者当 leader**：改进项（`Improvement`，I1…）只由复核者提出、只由复核者判定，worker 只做。每条必须挂到任务原文
  的逐字引文（至少 3 个词，规则校验）或可测的目标（链上测过分数，或这次测了）上；与已有的不重复；同时 open 的最多
  `improve_max_open`（5）条。只在需求都做完之后提（复核时估计：判完成 / 受阻，或证据检查全部通过；落地时按账本再确认），
  和需求的判定一样只在合并（或只判定）时落地，没被合并的复核里的改进项不记。
- **判定**：完成要有证据——链上测过分数时要 E2 / E3（复核者实际运行了），否则 E1 起；E0 只算 partial；放弃（dropped）
  要写原因。改进项不影响 DONE 的判定。
- **开始**：需求都做完、submit 走到 `finish_submit` 时写 `improve_started`。还没有 open 的改进项、复核者也没宣布结束时，
  先在链头上开一次只判定的复核（触发 `improve`）请复核者提改进方向，有了结论再接受 submit——接受的回复里就带着改进项。
  给不出挂得上的改进项，重试 `review_retries` 次后改进阶段结束（`improve_closed`，原因由规则写）。
- **改进阶段里**：submit 被接受只是“清单都做完了”，会话不结束（`port.submit`），回复带着改进项；后台复核照常（勾掉
  todo、交接、兜底），复核者每次都看到改进项并判定、可以补新的；后台复核更新了改进项时提醒 worker。开场与 board 有
  “Improvements”一节。合并标准不变（不比链头差，分数不降）。
- **进展**：改进项判完成，或合并点的分数比链上上一次测到的高出 `score_tolerance` 以上（复核决定里的 `improved`；只在
  improve 模式下算），记到会话上。
- **结束**（任一）：截止预留；复核者宣布没有值得做的改进了（`no_more_improvements` 写原因；open 的都要先判完成或放弃；
  不能同时提新的；链上测过分数时这次也要测）；改进阶段开始之后开的会话里连续 `improve_idle_sessions`（2）个没有进展
  （开始时正在进行的那个会话不算）。之后照常收尾、交付链头。收尾时正在进行的 `improve` 复核直接取消。

### 4.6 换新会话进入 POLISH（`after_accept=polish`）

`after_accept=improve` 在宣布完成的那个会话里继续：上下文里全是为现有实现辩护的推理。`polish` 把会话当作一个阶段的
工作单元：需求都做完、submit 走到 `finish_submit` 时写 `improve_started`（payload 带 `mode`），这次 submit 的回复之后
结束当前会话（结束原因 `phase`，`port.submit` 判断：这个会话开始于 `improve_seq` 之前），会话还开着时让模型按
`L3_PHASE` 写交接摘要（怎么验证的、哪里最没把握、怎么构建运行、放弃过的方案），新会话的开场理由是 `phase`。

- **时间门槛**：剩余时间扣掉截止预留后不足 `new_session_min_sec`（600 秒）时不写 `improve_started`，直接接受、收尾
  （判断在规则里，写 `improve_started` 之前）。
- **模式**（`polish_mode`）：`auto` 按链上有没有测过分数选——有分数选 `improve`，没有选 `verify`；也可以固定。
- **IMPROVE**：与 4.5 的改进项机制相同，只有一处不同：改进项的说明、verdict 的改进项字段、改进项的提议与判定都只在
  POLISH 开始之后才有（`queries.improvement_items`）。需求阶段的复核者输入与 `finalize` 完全相同，对比才干净。
- **VERIFY**：不新增数据结构，复用“已完成的需求在 E2 / E3 的反证下退回 open”（`reassessed`）这条规则。接受之前先在
  链头上开一次只判定的复审（触发 `verify`，焦点是判了完成、证据不到 E3 的需求，E1 在前）：复核者设法拿到 E2 / E3——
  跑通了就升级证据等级，跑出缺口就以 E2 / E3 判 not_done、在 missing 里写出复现命令，规则把它退回 open，这次 submit
  随之**交还**；只读代码的怀疑不退回（E1 的否定判断只记 note）。没有退回任何需求时 POLISH 结束（`improve_closed`），
  submit 被接受、收尾。修好之后再 submit 会再复审一轮，最多 `verify_rounds`（2）轮；复核者给不出复审结论（重试
  `review_retries` 次后）、没有可复审的需求时也结束。退回过的需求再判完成要 E2 / E3（只读代码判完成记为 partial）。
- **开场按图的状态**：“Finishing”一段由图决定（VERIFY / IMPROVE / 普通），POLISH 里交接、崩溃之后开的会话也拿到同样
  的说明；开场理由只换开头一段。POLISH 的开场另有“已交付的版本改了哪些文件”（基线 → 链头的 numstat），`phase` 开场
  预读其中改动最多的 `phase_preread_files`（4）个文件。

### 4.7 同一个问题反复失败：换新会话（`stuck_handoff`）

只在 submit 的结果返回时判断（`rules.check_stuck`，在 `finish_submit` 交还、前台请求被拒时调用），不需要“等自然停顿点”：
submit 的回复本身就是边界。

- **信号**（每个带签名）：同一需求在连续 `stuck_submit_misses`（2）次 submit 的复核里被明确判为 partial / not_done
  （没有新改动又 submit、它仍未完成也算一次；没被复核到的提交跳过）——`req:R5`；同一组回归连续 `stall_same_failure`（3）
  次拒掉 submit——`reg:<签名>`。
- **阶梯**：达到门槛时写一次 `stall_detected(action=hint)`（这个会话里每个签名一次），worker 收到提醒；这个会话里提醒过
  之后又失败一次、提醒之后没有任何进展、剩余时间够开新会话、这个签名从没换过人 → 写 `action=handoff`。port 在这次
  submit 的回复之后结束会话（`stuck_handoff`），模型按 `L3_STUCK` 只写事实（试过什么、怎么失败的、确认过的事实，不写
  当前假设和下一步），新会话开场理由 `fresh`：“Why a new session”一段（停滞信号、复核者能复现问题的命令、受阻声明的
  出路），链头以来的改动标明“上一个会话留下、没被批准，可以保留也可以撤掉”。
- **每个签名只换一次**。换出去的会话不计入 `max_idle_sessions` / `improve_idle_sessions`：合并链单调，多给新会话一次
  机会不会让交付变差。
- `review_rejections`（连续被复核者拒绝）只数有真正阻断原因的拒绝，仍然只提醒。

### 4.8 给 worker 的复核反馈

- 复核者每条命令（X1…）的输出尾部随 `merge_reviewed` 记下，但只留给 worker 复现用得上的：结论里引用过的、退出码非 0
  的，最多 8 条、每条 1500 字。submit 被拒 / 交还的回复、后台不批准的提醒、`fresh` 开场都附上这些命令与输出
  （`review_commands` 条）。
- verdict 新增 `blockers`（`merge=false` 时必填，枚举：regression / breaks_done / destructive / fake_result /
  debug_code / score_drop / other，other 要附 `blocking` 文字）与 `blocking`（为什么不能合并、怎么修）；`feedback`
  只写还缺什么。“需求还没做完”从来不是阻断原因。`merge=false` 却没有有效阻断原因时照常不批准，记 note
  `merge=false without a blocking reason`，决定里 `blocks=false`：不推送后台提醒，开场不当作待处理的问题，不计入
  `review_rejections`。后台提醒只发阻断原因、`blocking` 与复现命令；submit 的回复仍给全部信息。
- 复核者开场里，没做完的需求附上最近 `review_history`（3）次被判为没做完时的判定（复核、快照、缺失项），提示词要求
  沿用之前的判断标准，改变判断时写明理由。

## 5. 复核者（`runtime/reviewer.py`）

- 每个复核开一个带工具的短会话，复用 worker 的循环（`belay/worker/loop.py`），轮数（`review_max_turns`）与时间
  （`review_max_sec`）有上限；用完还没给结论就再给一轮只要求 verdict，仍没有就记为失败。
- 复核目录（默认 `<state>/review`）：`ShadowRepo.export_to` 把被复核的候选树增量导出（上次复核改过的已跟踪文件恢复、
  未跟踪的输出清掉、被忽略的构建缓存保留，第一次从工作区复制被忽略的文件作为种子）。复核结束杀掉工作目录在复核目录
  里的进程。
- 工具：`read_file` / `list_files` / `grep_search`（只读快照）；`run`（在复核目录执行命令，编号 X1…，退出码入日志）；
  `run_tests`（让验证器在独立目录用原始测试文件跑指定测试，结果是观察，E3 的依据）；`run_gate`（回归门结果）；
  `locate`（在快照之间二分）；`verdict`（唯一的写出口）。
- 开场（全部来自图）：任务原文；触发与上一个合并点；上次测到的分数与测法；回归门结果（含没通过的测试与原因、
  原始代码上失败现在通过的测试）；需求清单（账本状态、验收方法、关联测试的结果、上次的缺失项，focus 标星）；worker
  的自述（提交摘要、受阻声明、豁免提议、todo、交接摘要，统一标为未核实）；上一次复核的结论；相对上一个合并点的改动
  （按需求预排序的 diff）；被剔除的测试改动；怎么跑测试。
- 防偏差：不共享 worker 的上下文；工作区路径、影子仓库、作业与验证目录、`/logs` 都在保护名单里（读文件与命令都检查）；
  git 写、联网、全盘搜索拒绝。
- 反馈 worker（`port.py`）：submit 触发的复核结论总是随 submit 返回；后台请求没被批准时以 system-reminder 告诉 worker
  原因与反馈；同一需求连续 `notify_misses` 次被判为没做完时提醒；连续 `stall_same_failure` 个请求没被批准时给一次
  停滞提示（最新的合并点仍是交付物，撤掉有问题的改动不会丢东西）。

## 6. 需求状态

需求状态只在合并时由判定改变（`requirement_judged`）：复核者的判定（经校验）、关联检查全部通过（E3）、复核者不可用
时的自述（E0 / 自述受阻），以及恢复流程的回退（退回 open）。todo、submit、worker 的说法都不直接改状态。

```
open ──(复核者 done + 证据，或关联检查通过)──► done(E1/E2/E3)
open ──(worker 声明受阻且复核者认可)────────► blocked
done ──(E2/E3 证据表明不再成立，重新评估)────► open        （这次改动弄坏的：不合并，需求保持 done）
done ──(合并点在恢复时被回退)──────────────► open
blocked ──(复核者给出读法或做法)────────────► open
```

只判定、不合并的复核：worker 调 submit 时快照就是链头（没有新改动），而还有没在这棵树上判定过的需求、或新的受阻声明，
请复核者看一眼；都判定过就直接按账本回答（不会在同一棵树上反复复核同样的东西）。

## 7. 会话交接与导出

开场上下文继续由 `build_context` 从账本生成：任务原文、需求索引（冻结后逐字不变）、待处理的问题（持续回归、定位、
诊断、最近一次被拒或没被批准的原因与反馈）、需求状态（等级与缺失项）、todo、交接摘要、链头与最新合并点以来的 diff、
离开期间的事件（合并、判定、复核反馈……）、回归门。换会话、进程崩溃、容器重建都从账本接着做。
`python -m belay.cli handoff --run-dir <dir>` 随时把同样的上下文从事件库导出来，交给下一个会话。

## 8. 数据集适配

| 数据集 | 回归门 | 主要证据 | 复核者主要做什么 |
| --- | --- | --- | --- |
| SWE-EVO | 有 | E3 少量 + E1 为主 | 跑相关测试，逐条对照发布说明读改动；裁决测试冲突 |
| ProMax | 有 | E3 + 构建通过 | 构建、跑测试、检查跨文件重构是否一致 |
| LHTB | 无 | E2 | 运行程序、检查产物、按任务描述自测分数（分数不下降是合并标准的一部分） |

LHTB 的注意事项：工作目录不是 git 仓库也可以（影子仓库在工作区之外，超过 20 MB 的文件不进快照）；交付时工作区检出为
链头，工作目录之外的产物和被忽略的构建产物不会随之回退——最新快照没被合并时，这些产物可能来自更新的代码；复核者
运行程序写绝对路径时可能碰到工作区之外的东西（复核目录只隔离相对路径）。复核者的开场里说明了这一点：工作区下的
绝对路径（程序的默认路径、配置、环境变量）指向 worker 的活工作区，要改成指向复核目录里的副本，且不能往那里写
（例如 spot-scheduler-traces 的 `simulate` 默认读 `/app/workspace/policy.py`，可用 `POLICY_PATH` / `OUTPUT_DIR` 指过去）。
影子仓库在容器里运行 git：预构建镜像里没有 git 时（如基于 python:3.11-slim 的 LHTB 镜像），`eval.prepare` 在本机构建
只多装了 git 的派生镜像 `belay-local/<镜像>:<tag>-git` 并改写 task.toml（各组共用同一个任务目录，环境一致）。

## 9. 恢复

与 v7 相同（G1–G7），差别：合并提交的说明由图决定（复核者的标签），容器重建时可以原样重做；正在进行的复核会话在
runtime 重启后从头再开（`recovery.reconcile` 第 4 步）；复核目录随状态目录一起重建。

## 10. 配置（`core/config.py`，新增与变化）

`merge_min_interval_sec`、`merge_todo_interval_sec`、`merge_stable_interval_sec`、`merge_stable_generic`、`bg_waivers`、`review_max_turns`、`review_max_sec`、`review_run_timeout_sec`、
`review_retries`、`review_input_chars`、`review_locate_wait_sec`、`score_tolerance`、`notify_misses`、`reserve_review_sec`、
`todo_done_nudge`、`todo_done_nudge_max`、`todo_done_nudge_gap_turns`、`todo_done_nudge_quiet_turns`、
`todo_reminder_backoff`、`todo_reminder_turns_max`（`todo_reminder_max` 默认改为 0 = 不限）、`after_accept`、
`improve_idle_sessions`、`improve_max_open`。
取消：`checkpoint_tier`、`deliver_unconfirmed`、`review_batch`、`review_max_reopens`、`labeler`、`label_every`。

## 11. 测试（`python -m pytest -q`，不需要容器和模型）

| 要求 | 测试 |
| --- | --- |
| 事件、推导、非法转换、v7 日志被拒 | `tests/unit/test_reduce.py`、`test_rules.py::test_v7_logs_are_refused_with_a_clear_error` |
| 合并请求（节流、交接与 todo、回归门、复核）、证据等级校验、单调（已完成不退回、E3 测试、分数）、豁免由复核者裁决、复核失败的重试与降级、只判定的复核、受阻的裁决、没有回归门的路径、收尾、DONE 的条件 | `tests/unit/test_rules.py` |
| 后台挑快照：todo 锚点优先于之后的改动、合并期间攒下的 todo 合成一次、不抢占进行中的复核、submit 仍然取代、被拒不回退、revert 是屏障、auto 只是兜底 | `tests/unit/test_rules.py`（“边界快照”一节） |
| todo 提醒的触发与节流（跑完测试、有进行中的条目、改过文件；同一组上限与间隔、组变了重新计数；探索期不计时；不限次数与退避；多项并行按组说；交接后从图上取条目） | `tests/unit/test_todo_reminders.py` |
| 改进阶段：finalize 不变；接受后开始、请复核者提方向；提议的校验（引文、目标、去重、上限、需求没做完时不提）；证据等级；分数提高与改进项完成算进展；放弃与宣布结束；重试后结束；空闲会话结束；收尾取消；后台复核更新改进项；被拒的复核不记；回退重新打开 | `tests/unit/test_improve.py`、`tests/integration/test_belay_run.py`（改进阶段一节）、`test_replay.py`（improve 模式） |
| 重放一致性（随机复核结论：失败、格式坏、豁免、分数、各种等级）；随机交错下后台总是挑最新的边界快照（性质检查） | `tests/unit/test_replay.py` |
| 开场、等级与缺失项、board | `tests/unit/test_context.py` |
| 端到端：复核会话（真实的复核目录与工具）、E2、复核失败、回归被拒、后台不批准的提醒、跑完测试后提醒勾掉 todo、交接、恢复、截止 | `tests/integration/test_belay_run.py`、`test_long_run.py` |
| 评测接入：没有 gate 时复核者与分数 | `tests/integration/test_flat_agent.py`（需要 pier） |

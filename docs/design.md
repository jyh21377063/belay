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
| 后台：空闲时最新的可测快照（比链头新、这棵树在这一段还没请求过） | bg | 两次后台复核之间至少 `merge_min_interval_sec`（默认 600 s）；只按回归门被拒的请求没有复核，不计入 |
| 勾掉 todo（锚点快照） | bg | `merge_todo_interval_sec`（默认 180 s） |
| 交接 / 会话结束的快照 | bg | 不节流 |
| submit | fg | 不节流；取代正在进行的后台请求（作业按树复用，复核取消） |
| 收尾（最新快照还没合并） | fg | 在截止预留里做 |

没有复核者时（`reviewer=False`）不节流：回归门只花 CPU。时钟（`tick`）会在间隔到了时补发后台请求。同一时刻每个
worker 每条车道至多一个合并请求，整个运行同一时刻只有一个复核；前台需要复核者时取代正在复核的后台请求。

### 4.2 一个合并请求怎么走（`rules.advance_attempt`）

1. 被链头超过（同一段、快照序号不大于链头）或就是链头的树 → `merge_superseded`。
2. 回归门：有测试配置时跑全量（`selection=None`；v7 的 related 档位与“相关测试先过、全量后验”的两级取消），
   回归先确认重跑，重跑通过的记为 flaky。没有测试配置时 `selection=()`，直接通过。
3. 单调检查：已完成（E3）的需求依据的测试在这棵树上不再通过 → `merge_rejected(requirement_regression)`。
4. 有回归：worker 在这次 submit 里提议了豁免 → 请复核者裁决；否则 `merge_rejected(regression)`（前台的立即定位与诊断，
   后台的同一回归连续两次才定位与诊断）。
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
运行程序写绝对路径时可能碰到工作区之外的东西（复核目录只隔离相对路径）。

## 9. 恢复

与 v7 相同（G1–G7），差别：合并提交的说明由图决定（复核者的标签），容器重建时可以原样重做；正在进行的复核会话在
runtime 重启后从头再开（`recovery.reconcile` 第 4 步）；复核目录随状态目录一起重建。

## 10. 配置（`core/config.py`，新增与变化）

`merge_min_interval_sec`、`merge_todo_interval_sec`、`review_max_turns`、`review_max_sec`、`review_run_timeout_sec`、
`review_retries`、`review_input_chars`、`review_locate_wait_sec`、`score_tolerance`、`notify_misses`、`reserve_review_sec`。
取消：`checkpoint_tier`、`deliver_unconfirmed`、`review_batch`、`review_max_reopens`、`labeler`、`label_every`。

## 11. 测试（`python -m pytest -q`，不需要容器和模型）

| 要求 | 测试 |
| --- | --- |
| 事件、推导、非法转换、v7 日志被拒 | `tests/unit/test_reduce.py`、`test_rules.py::test_v7_logs_are_refused_with_a_clear_error` |
| 合并请求（节流、交接与 todo、回归门、复核）、证据等级校验、单调（已完成不退回、E3 测试、分数）、豁免由复核者裁决、复核失败的重试与降级、只判定的复核、受阻的裁决、没有回归门的路径、收尾、DONE 的条件 | `tests/unit/test_rules.py` |
| 重放一致性（随机复核结论：失败、格式坏、豁免、分数、各种等级） | `tests/unit/test_replay.py` |
| 开场、等级与缺失项、board | `tests/unit/test_context.py` |
| 端到端：复核会话（真实的复核目录与工具）、E2、复核失败、回归被拒、后台不批准的提醒、交接、恢复、截止 | `tests/integration/test_belay_run.py`、`test_long_run.py` |
| 评测接入：没有 gate 时复核者与分数 | `tests/integration/test_flat_agent.py`（需要 pier） |

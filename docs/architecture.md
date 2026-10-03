# Belay 架构与开发约定

实现设计（事件、视图、规则、上下文、压缩、恢复）见 [design.md](design.md)；本文只讲代码组织。
依赖规则由 `tests/unit/test_layering.py` 自动检查。

## 目录

```
belay/
├── core/            纯函数核心：不做 IO、不调模型、不读时钟（now 作为参数传入）
│   ├── events.py    事件类型、必需字段、允许的来源
│   ├── model.py     三个视图（需求账本 + todo + 提交 + 复核 / 执行状态 + 快照时间线 / 合并链）的不可变数据模型；快照序列化
│   ├── reduce.py    apply(graph, event) / replay(events)：视图的推导函数，也是状态机的最后一道防线
│   ├── rules.py     状态转换规则：输入（worker 请求 / 快照 / 观察 / 时钟 / 复核结论）→ 事件：合并请求（后台节流、submit、
│   │                交接、收尾）→ 回归门 → 复核 → 合并；复核结论的校验（证据等级、豁免引文、单调：完成不退回、分数不降）；
│   │                需求随检查项（E3）记下、todo、快照二分定位（提交被拒、后台连续两次同一回归）、诊断；
│   │                改进阶段（after_accept=improve：复核者提出、判定改进项，接受之后继续加强链头）；
│   │                next_step（会话结束 ≠ 运行结束，提交被接受才收尾）；交付链头
│   ├── verify.py    基线归类、守护集合、相关测试选择、回归判定、按树合并作业结果、定位点状态、优先级
│   ├── queries.py   只读查询（需求与证据检查、提交、复核、合并链、交付点、快照时间线、todo、恢复点、DONE 的条件……）
│   ├── context.py   build_context：分层开场上下文（受保护段 + 各段上限 + 折叠与查询入口）；resume_reminder
│   ├── compact.py   L0 落盘 / L1 清理 / L2 用图重建（消息列表的纯变换）
│   ├── plan.py      规划提议的校验（逐字引文、覆盖、actionable / context、检查项存在）与机械切分
│   ├── invariants.py 不变量
│   ├── effects.py   事件 → 副作用计划
│   └── render.py    board、需求详情、提交结果与复核结论、定位与诊断、账本
├── runtime/         命令式外壳
│   ├── store.py     SQLite 事件表 + 视图快照 + 附件
│   ├── runtime.py   Runtime.submit：锁内“规则 → 追加事件 → 更新视图 → 检查不变量”，之后执行副作用
│   ├── gitops.py    影子仓库：快照与快照提交、剔除测试改动、确定的提交、CAS、检出、导出到复核目录、增量 bundle、只撤销一段改动
│   ├── verifier.py  作业：验证槽位池 + 可抢占的优先级队列、setsid 进程组、完成标记、重新接上、导入隔离探针
│   ├── session.py   会话循环：工具执行与快照钩子、提交结束会话、停下时追问与隐式提交、todo 提醒、L0–L4（自然停顿点
│   │                交接）、消息轨迹（读盘重放）、ModelCallFailed
│   ├── port.py      WorkerPort：Belay 工具与 runtime 之间的接口，通知（只推能据此行动的：复核不通过、持续回归……）
│   ├── planner.py   规划器（LLM 提议需求清单与验收方法 + 校验 + 重试 + 机械兜底）
│   ├── reviewer.py  复核者：每个复核一个带工具的短会话（读文件、run、run_tests、run_gate、locate、verdict）
│   ├── review.py    复核者开场里的 diff：按需求原文里的名字预排序
│   ├── driver.py    BelayRun：准备（基线双跑）、快照、会话（内存重试 / 读盘重放）、收尾与交付、镜像、挂起、副作用
│   ├── recovery.py  重启对账：CAS、重新接上作业、会话接续、复核重开、容器重建
│   └── prompts.py   系统提示、L3 / 规划器 / 复核者 / 诊断者提示词
├── tools/           模型能调用的工具（通用工具 + belay.py：submit、board、failure_log、revert_change）
├── worker/          B 组的 worker 循环（也跑只读探索子 agent）
├── container/       上传到容器里执行的脚本（只用标准库）：runner.py（槽位导出、种子、sys.path、隔离探针、检查运行）
├── env.py  llm.py  cli.py
tests/
├── sim.py           纯核心的模拟器（假的作业、git 与复核者），单元测试与重放一致性测试共用
├── unit/            纯逻辑：推导、规则、重放一致性、上下文、压缩、规划、分层
└── integration/     LocalEnv + ScriptedLLM + 真实 git / pytest 的端到端场景（fakes.py：复核者、诊断者的替身）
```

## 依赖规则

1. **`belay/core` 是纯的。** 不依赖 runtime / worker / tools / llm / env，不 import asyncio、os、subprocess、sqlite3、
   time、random 等模块。所有判断逻辑都能用普通单元测试覆盖；重放一致性测试直接驱动它。
2. **`belay/tools` 与 `belay/worker` 不认识 runtime 与 core。** 工具只经由 `ToolContext.runtime.request(...)` 提请求；
   B 组的 `ctx.runtime` 为 None。worker 的代码在 B 组与 Belay 之间共用，对比才干净。
3. **`eval` 可以依赖 `belay`，反过来不行。**

## 并发模型

整个 Belay 跑在一个 asyncio 事件循环里。`Runtime.submit` 是唯一的写入口，在一把锁里完成决定与写日志，
所以不需要别的并发控制；会话循环、作业、时钟、副作用都是这个循环里的协程，只通过 submit 改变状态、通过
`wait_until(谓词)` 等待图的变化。这就是“函数式核心、命令式外壳”：难调试的并发问题集中在很薄的外壳里。

一次运行内部的并发来自 worker 与 runtime 自己的后台活动（最新快照的验证、提升、定位、复查、长作业、会话交替、
崩溃重启），不来自多个 agent：前台（submit、收尾）与后台存档尝试各至多一个，父节点在推进时确定（CAS）；后台不抢占
进行中的请求，空出来时挑最新的边界快照（勾掉 todo、交接），其次是 worker 自己的命令刚跑通过的快照，很久都没有才兜底取最新快照；
验证在验证槽位里跑，不碰 worker 的工作区（隔离无效时降级为切换工作区，后台只验证交接快照）。

# Belay 架构与开发约定

实现设计（事件、视图、规则、上下文、压缩、恢复）见 [design.md](design.md)；本文只讲代码组织。
依赖规则由 `tests/unit/test_layering.py` 自动检查。

## 目录

```
belay/
├── core/            纯函数核心：不做 IO、不调模型、不读时钟（now 作为参数传入）
│   ├── events.py    事件类型、必需字段、允许的来源
│   ├── model.py     三个视图（任务图 / 执行状态 / 存档链）的不可变数据模型；快照序列化
│   ├── reduce.py    apply(graph, event) / replay(events)：视图的推导函数，也是状态机的最后一道防线
│   ├── rules.py     状态转换规则：输入（worker 请求 / 观察 / 时钟）→ 事件；next_step（会话结束 ≠ 运行结束）
│   ├── verify.py    基线归类、守护集合、相关测试选择、回归判定、按树合并作业结果
│   ├── queries.py   只读查询（可做的任务、需求状态、存档链……）
│   ├── suggest.py   调度建议
│   ├── context.py   build_context：9 段开场上下文，超预算从下往上裁
│   ├── compact.py   L0 落盘 / L1 清理 / L2 用图重建（消息列表的纯变换）
│   ├── plan.py      规划提议的校验（逐字引文、覆盖、无环）与机械切分
│   ├── invariants.py 不变量
│   ├── effects.py   事件 → 副作用计划
│   └── render.py    board、存档结果、作业结果、账本
├── runtime/         命令式外壳
│   ├── store.py     SQLite 事件表 + 视图快照 + 附件
│   ├── runtime.py   Runtime.submit：锁内“规则 → 追加事件 → 更新视图 → 检查不变量”，之后执行副作用
│   ├── gitops.py    影子仓库：快照、剔除测试改动、确定的提交、CAS、检出
│   ├── verifier.py  作业：setsid 进程组、完成标记、读写锁（验证独占工作区）
│   ├── session.py   会话循环：工具执行、L0–L4
│   ├── port.py      WorkerPort：Belay 工具与 runtime 之间的接口，通知
│   ├── planner.py   规划器（LLM 提议 + 校验 + 重试 + 机械兜底）；停滞时的拆分提议
│   ├── driver.py    BelayRun：准备、会话、收尾、交付、副作用执行
│   ├── recovery.py  重启对账
│   └── prompts.py   系统提示、L3 / 规划器提示词
├── tools/           模型能调用的工具（通用工具 + belay.py）
├── worker/          B 组的 worker 循环（也跑只读探索子 agent）
├── container/       上传到容器里执行的脚本（只用标准库）
├── env.py  llm.py  cli.py
tests/
├── sim.py           纯核心的模拟器（假的作业与 git），单元测试与重放一致性测试共用
├── unit/            纯逻辑：推导、规则、重放一致性、上下文、建议、压缩、规划、分层
└── integration/     LocalEnv + ScriptedLLM + 真实 git / pytest 的端到端场景
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

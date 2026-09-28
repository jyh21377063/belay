# Belay 架构与开发约定

本文档是代码组织的依据；依赖规则由 `tests/unit/test_layering.py` 自动检查。

### 目录规划

```
belay/                      # 仓库根目录
├── belay/                  # runtime 本体（宿主机上运行）
│   ├── llm.py              # 模型客户端（已有）
│   ├── env.py              # 执行环境（已有）
│   ├── config.py           # RuntimeConfig（每个机制一个开关）与容器内路径，从 runs.yaml 的 kwargs.runtime 构造
│   ├── cli.py              # 本地调试入口（已有）
│   │
│   ├── worker/             # M1：单个 worker 的一切
│   │   ├── loop.py         #   主循环
│   │   ├── context.py      #   清理过期结果、交接说明
│   │   ├── prompts.py      #   系统提示与首条消息
│   │   └── transcript.py   #   只追加的 JSONL 轨迹
│   │
│   ├── tools/              # 模型能调用的工具
│   │   ├── base.py         #   Tool、ToolContext、行动边界策略、RuntimeClient 接口
│   │   ├── files.py  shell.py  #   文件工具（含读后被改检测）、bash、todo、submit
│   │   ├── agents.py       #   explore：只读探索子 agent，经 ToolContext.subagent 由 worker 注入
│   │   ├── output.py       #   工具输出截断（保留报错行）
│   │   └── runtime.py      #   run_check / wait / ledger / submit / request_test / report_conflict
│   │                       #   （M5 加 spawn_work），只负责把请求交给 ctx.runtime
│   │
│   ├── graph/              # 证据图：数据模型 + 存储，不含调度逻辑
│   │   ├── model.py        #   节点、状态常量、GraphState、变更（Put / Delete / Event）与事务 Tx
│   │   ├── evidence.py     #   证据规则：按基线归类、相关测试、失败签名、独立测试的收录（纯函数）
│   │   ├── ledger.py       #   需求状态的计算，账本与作业结果的文字（纯函数）
│   │   ├── requirements.py #   需求抽取（release notes 机械切分）与引文校验
│   │   ├── build.py        #   初始图
│   │   ├── invariants.py   #   五条不变量的断言
│   │   └── store.py        #   SQLite：状态表 + 只追加的事件表（唯一做 IO 的文件）
│   │
│   ├── runtime/            # Orchestrator 及其副作用
│   │   ├── messages.py     #   收件箱里的消息与 decide 产出的动作
│   │   ├── decide.py       #   纯函数 decide(state, msg, cfg) -> (changes, actions)
│   │   ├── orchestrator.py #   单写者循环：取消息 → decide → 落库 → 执行 actions；WorkerRuntime（工具的接口）
│   │   ├── effects.py      #   执行 actions：启动 / 取消作业、回复请求、推进集成分支、调用裁判、启停 worker
│   │   ├── jobs.py         #   Job Runner：setsid 进程组、完成标记、分段等待
│   │   ├── gitops.py       #   影子仓库、快照、剔除测试改动、比较并交换推进、检出交付物
│   │   ├── bootstrap.py    #   setup 阶段：上传 runner、影子仓库、基线、原始代码副本、低权限用户
│   │   ├── judges.py       #   Test Author、Reviewer（独立上下文的模型调用）
│   │   ├── prompts.py      #   worker 的附加规则与首条消息、Test Author 与 Reviewer 的提示词
│   │   └── recovery.py     #   M6：重启对账
│   │
│   ├── container/          # 上传到容器里执行的脚本：只用标准库，兼容 Python 3.6
│   │   └── runner.py       #   跑测试（临时切换候选树、放入独立测试、结束后恢复）、解析结果、剔除测试改动
│   │
│   └── observe/            # M6：读事件表生成回放页面
│
├── eval/                   # 评测框架（已有），所有对照组共用
│   └── agents/             #   A / A-gate / PEE / B / Belay 的 Pier 适配层
├── tests/
│   ├── unit/               # 纯逻辑：decide、不变量、截断、glob…… 不起进程
│   ├── integration/        # LocalEnv + ScriptedLLM：完整跑一遍，不需要容器和模型
│   ├── docker/             # 需要容器的测试，默认跳过（pytest -m docker）
│   └── fixtures/           # 小型示例仓库、录制的模型回复
└── docs/                   # 计划、设计说明、决策记录
```

### 三条依赖规则

目录只是形式，真正防止代码缠在一起的是依赖方向：

1. **`graph/`（除 `store.py`）和 `runtime/decide.py` 是纯的。** 它们不做 IO、不调模型、不执行命令；`decide.py` 只依赖 `graph/` 的纯函数部分与 `runtime/messages.py`。所以 Orchestrator 的所有判断逻辑都可以用普通单元测试覆盖，这也是面试时最能体现工程质量的部分。
2. **`worker/` 和 `tools/` 不认识 Orchestrator 的内部实现。** 工具只通过一个很窄的接口提请求，比如 `await ctx.runtime.request(msg)`。B 组的 `ctx.runtime` 为 None（不注册 runtime 工具），Belay 传入真实的收件箱。这样 worker 的代码在 B 组和 Belay 之间完全共用，对比才干净。
3. **`eval` 可以依赖 `belay`，反过来不行。** 所有对照组共用评测框架，Belay 特有的逻辑只能出现在 `eval/agents/belay_agent.py` 这一个适配文件里，不能渗进评分和报告。这也是公平性的保证。

### 先把复杂度拆掉：一个事件循环、一个收件箱

你担心的"worker 是异步协程，runtime 也是异步的，两层叠在一起很乱"，可以用一个很朴素的结构化解：**整个 Belay 跑在 `BelayAgent.run()` 里的同一个 asyncio 事件循环上，所有组件都是这个循环里的协程，彼此只通过一个队列通信。**

- **Orchestrator** 是唯一消费收件箱的协程，逐条处理消息。因为只有它写状态，所以不需要任何锁。
- **worker** 是普通的循环：调模型 → 执行工具 → 再调模型。遇到需要 runtime 的工具（`run_check`、`submit`、`spawn_work` 等），就往收件箱投一条消息，附一个 Future，然后 `await` 这个 Future 等回复。
- **作业**由 Orchestrator 启动为后台任务，完成后把结果作为一条新消息投回收件箱。

```python
async def orchestrator(inbox, state, effects):
    while True:
        msg = await inbox.get()
        changes, actions = decide(state, msg)   # 纯函数：不碰 IO
        state.apply(changes)                     # 写 SQLite：事件 + 状态
        for a in actions:
            effects.spawn(a)                     # 副作用：exec、git、回复 Future
```

这就是"函数式核心、命令式外壳"：`decide()` 是纯函数，可以直接写单元测试，不需要容器和模型。难调试的并发问题因此都集中在很薄的外壳里。worker 数量从 1 变成 3，只是多起几个 worker 协程，Orchestrator 的逻辑不变。

### 开发顺序：每一步都能通过 eval 端到端跑通

原则是"行走的骨架"：每个里程碑结束时，`python -m eval.run --profile belay-dev` 都能跑出一个带补丁、带评分的结果。这样任何时候停下来，手里都有能演示的东西。

1. **M0 骨架（半天）**：`eval/agents/belay_agent.py` 接入 Pier；搭好 `belay/` 的包结构（llm、worker、tools、runtime、graph）。再做一个 `FakeEnvironment`，在本地临时目录里用 subprocess 模拟 `exec`，后面所有单元测试都靠它。
2. **M1 单 worker 执行器（3 天）**：这就是 `FlatAgent`，也就是 B 组。建议按这个顺序加：模型客户端（直接用 DeepSeek 的 Anthropic 兼容接口，工具调用格式和 Claude Code 一致）和重试 → 只有 bash 的循环 → 文件工具 → 输出截断 → 消息逐轮写盘 → 超过阈值时重开上下文。**完成标准**：在 2–3 道开发题上与 A 组对跑，不明显更弱。这一步不过，后面都不用做。
3. **M2–M4：同一套 runtime，按开关分阶段验收（按 v4 计划）。** 三者共用一套节点、消息与 `decide()`，一次设计、一次实现；
   每个机制对应 `RuntimeConfig` 的一个开关，`runs.yaml` 里用三个 agent 定义逐步打开：

   | agent | 打开的机制 | 完成标准 |
   | --- | --- | --- |
   | `belay-m2` | 证据图、作业（`run_check` / `wait`）、基线（setup 阶段两次）、`ledger`、需求切分；`submit` 直接合并 | worker 用 `wait` 代替 `sleep`，`ledger` 能看到基线 |
   | `belay-m3` | + 门禁（检查点跑相关子集、最终跑全量）、集成分支比较并交换推进、剔除测试路径下的改动、截止保护、交付 HEAD、DONE / INCOMPLETE、OS 层隔离 | conan 上回归被拦下、补丁里没有测试改动 |
   | `belay` | + `request_test`（Test Author，原始代码上断言失败才收录）、`report_conflict`（Reviewer，引文逐字校验） | 一次上报从提交到裁决、再到账本更新完整跑通 |

   另有 `gate: advise`（只提示不拒绝）用于"约束力"的消融。设计上的取舍：
   - **单 worker 时门禁在规范工作区原地运行。** worker 在 `submit` 期间阻塞；runner 临时把工作区切换为候选树（测试文件恢复为原始版本、放入独立测试），跑完原样恢复。独立门禁工作区要靠 `PYTHONPATH` 指向副本，旧式 `easy-install.pth` 与就地编译的扩展会让测试 import 到错误的代码，放到 M5 与 worker 的 worktree 一起做（作业的 `workspace` 字段已经留好）。
   - **Test Author 在原始代码副本上验证。** setup 阶段检查副本里 `sys.path` 的顺序，import 会落到工作区时关闭独立测试并记录原因。
   - **每一步失败只关闭对应机制**（没有测试配置、基线跑不出结果、建不了低权限用户），原因写进 `setup.json` 与账本。
4. **M5 并发（2 天）**：`spawn_work`、每个 worker 一个 worktree、合并队列（`merge-tree` + CAS）、失败签名广播。因为 M2 的数据模型已经按多 worker 设计，这一步主要是加 worker 协程和合并逻辑。**完成标准**：一道可分解的开发题上 3 个 worker 的改动都合并成功。
5. **M6 恢复、回放（1.5 天）**：见下文第三点。

### 几个会踩的坑，最好提前定下来

**补丁导出改为导出集成分支（已实现）。** `BelayAgent` 在 `finally`（`asyncio.shield`）里先 `Orchestrator.shutdown(deliver=True)`：停止 worker 与作业，用 `read-tree -m -u` 把集成分支 HEAD 精确检出到工作目录，再沿用 `PatchCaptureMixin` 导出。候选在进入集成分支之前已经剔除了测试路径下的改动，所以补丁里没有测试改动；结束时工作区的完整改动另存为 `belay/worktree.diff` 供调试。

**LHTB 的部分题目工作目录不是 git 仓库。** 集成分支依赖 git，可以在工作目录之外建一个影子仓库（`GIT_DIR` 放在 `/opt/belay`，`GIT_WORK_TREE` 指向工作目录），只由 runtime 使用。这类题没有现成测试，检查就是任务自带的公开工具，基线也从这些工具跑出来。

**恢复演示不要放在 Pier 里做。** Pier 管理的宿主进程一旦崩溃，这次 trial 就结束了。更实际的做法是：用 `keep_containers` 保留容器，再提供一个 `python -m belay.run --resume <run_dir>` 的命令，对着保留的容器和 SQLite 状态做对账，然后继续跑。故障注入也在这个命令上做。

**并发调用 `exec` 之前先确认 Pier 支持。** 多个 worker 会同时对同一个容器调用 `environment.exec`。docker exec 本身是可以并发的，但最好先写一个小测试确认 Pier 的封装没有串行化，或者没有共享状态。

**调试时录制模型回复。** 把每次调用模型的请求和回复录下来，调试 runtime 时直接回放，不花钱、结果可复现。这对调 `decide()` 和合并逻辑特别有用。

另外，`task_selection.md` 已经是第 2 版（正式集 16 题：SWE-EVO 7、ProMax 3、LHTB 4、负对照 2），而计划文档的实验设计一节还是按 20 题写的；要的话我可以把文档里的题目、运行次数和排期同步成这版。

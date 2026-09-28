# Belay 架构与开发约定

本文档是代码组织的依据；依赖规则由 `tests/unit/test_layering.py` 自动检查。

### 目录规划

```
belay/                      # 仓库根目录
├── belay/                  # runtime 本体（宿主机上运行）
│   ├── llm.py              # 模型客户端（已有）
│   ├── env.py              # 执行环境（已有）
│   ├── config.py           # 统一的配置 dataclass，从 runs.yaml 的 kwargs 构造
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
│   │   ├── files.py  shell.py
│   │   ├── output.py       #   工具输出截断（保留报错行）
│   │   └── runtime.py      #   M2 起：run_check / wait / ledger / submit / spawn_work /
│   │                       #   request_test / report_conflict，只负责把请求投进收件箱
│   │
│   ├── graph/              # 证据图：数据模型 + 存储，不含调度逻辑
│   │   ├── model.py        #   节点、边、状态枚举（dataclass）
│   │   ├── store.py        #   SQLite：状态表 + 只追加的事件表
│   │   ├── invariants.py   #   五条不变量的断言
│   │   └── requirements.py #   需求抽取（release notes 切分、LHTB 阶段）
│   │
│   ├── runtime/            # Orchestrator 及其副作用
│   │   ├── messages.py     #   收件箱里的消息类型
│   │   ├── decide.py       #   纯函数 decide(state, msg) -> (changes, actions)
│   │   ├── orchestrator.py #   单写者循环：取消息 → decide → 落库 → 执行 actions
│   │   ├── effects.py      #   执行 actions：派发 worker、启动作业、回复 Future
│   │   ├── jobs.py         #   Job Runner：进程组、完成标记、按树哈希去重
│   │   ├── checks.py       #   基线、测试结果解析、失败签名归一化与归类
│   │   ├── gitops.py       #   worktree、影子仓库、merge-tree、比较并交换推进
│   │   ├── judges.py       #   M4：Test Author、Reviewer（独立上下文的模型调用）
│   │   └── recovery.py     #   M6：重启对账
│   │
│   ├── container/          # 上传到容器里执行的脚本：只用标准库，兼容 Python 3.6
│   │   └── runner.py       #   跑检查、写完成标记、输出结构化结果
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

1. **`graph/` 和 `runtime/decide.py` 是纯的。** 它们不做 IO、不调模型、不执行命令，只依赖 `graph/model.py`。所以 Orchestrator 的所有判断逻辑都可以用普通单元测试覆盖，这也是面试时最能体现工程质量的部分。
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
3. **M2 Orchestrator + 证据图 + 作业（2 天）**：SQLite 表结构和事件表；收件箱与 `decide()`；Job Runner（`setsid` 起进程组、写完成标记文件）；`run_check`、`wait`、`ledger` 三个工具。基线可以直接复用 `gate_script.py` 的逻辑，放在 setup 阶段跑，和 A-gate 一样不计入 90 分钟。需求抽取先只做 SWE-EVO 的 release notes 切分。**完成标准**：worker 用 `wait` 代替 `sleep`，`ledger` 能看到基线。
4. **M3 完成权 + 门禁 + 集成分支（1.5 天）**：git 写操作收归 runtime；`submit` → 候选 → 门禁 → 比较并交换推进；受保护文件的哈希校验；bash 拒绝 git 写命令；DONE / INCOMPLETE 与截止保护。**完成标准**：在 conan 开发题上，回归被拦下、补丁里没有测试改动。**到这一步就是最小可演示版本**，时间不够时它本身已经是一个比 A-gate 更完整的对照。
5. **M4 独立证据与上报（1.5 天）**：`request_test` + Test Author，收录前在原始代码上验证"断言级失败"；`report_conflict` + Reviewer，校验引文逐字存在；按证据计算需求状态。**完成标准**：一次上报从提交到裁决、再到账本更新能完整跑通。
6. **M5 并发（2 天）**：`spawn_work`、每个 worker 一个 worktree、合并队列（`merge-tree` + CAS）、失败签名广播。因为 M2 的数据模型已经按多 worker 设计，这一步主要是加 worker 协程和合并逻辑。**完成标准**：一道可分解的开发题上 3 个 worker 的改动都合并成功。
7. **M6 恢复、隔离、回放（1.5 天）**：见下文第三点。

### 几个会踩的坑，最好提前定下来

**补丁导出要改成导出集成分支。** 现在的 `PatchCaptureMixin` 导出的是工作目录相对基线的 diff。Belay 的交付物应该是集成分支 HEAD，所以在 `finally` 里要先把 HEAD 检出到仓库工作目录（或者直接对 HEAD 的 tree 做 diff），再剔除测试路径下的改动，然后导出。Pier 超时取消 `run()` 时，这一步同样要放在 `asyncio.shield` 里。

**LHTB 的部分题目工作目录不是 git 仓库。** 集成分支依赖 git，可以在工作目录之外建一个影子仓库（`GIT_DIR` 放在 `/opt/belay`，`GIT_WORK_TREE` 指向工作目录），只由 runtime 使用。这类题没有现成测试，检查就是任务自带的公开工具，基线也从这些工具跑出来。

**恢复演示不要放在 Pier 里做。** Pier 管理的宿主进程一旦崩溃，这次 trial 就结束了。更实际的做法是：用 `keep_containers` 保留容器，再提供一个 `python -m belay.run --resume <run_dir>` 的命令，对着保留的容器和 SQLite 状态做对账，然后继续跑。故障注入也在这个命令上做。

**并发调用 `exec` 之前先确认 Pier 支持。** 多个 worker 会同时对同一个容器调用 `environment.exec`。docker exec 本身是可以并发的，但最好先写一个小测试确认 Pier 的封装没有串行化，或者没有共享状态。

**调试时录制模型回复。** 把每次调用模型的请求和回复录下来，调试 runtime 时直接回放，不花钱、结果可复现。这对调 `decide()` 和合并逻辑特别有用。

另外，`task_selection.md` 已经是第 2 版（正式集 16 题：SWE-EVO 7、ProMax 3、LHTB 4、负对照 2），而计划文档的实验设计一节还是按 20 题写的；要的话我可以把文档里的题目、运行次数和排期同步成这版。

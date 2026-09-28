"""Orchestrator 及其副作用。

  messages.py      收件箱里的消息与 decide 产出的动作
  decide.py        纯函数 decide(state, msg, cfg) -> (changes, actions)，只依赖 graph 的纯函数部分与 messages
  orchestrator.py  单写者循环：取消息 → decide → 落库 → 执行 actions；WorkerRuntime 是工具看到的接口
  effects.py       执行 actions：启动 / 取消作业、回复请求、推进集成分支、调用裁判、启停 worker
  jobs.py          Job Runner：进程组、完成标记、分段等待
  gitops.py        影子仓库、快照、剔除测试改动、比较并交换推进、检出交付物
  bootstrap.py     setup 阶段：上传 runner、影子仓库、基线、原始代码副本、低权限用户
  judges.py        Test Author、Reviewer（独立上下文的模型调用）
  prompts.py       worker 的附加规则与首条消息、Test Author 与 Reviewer 的提示词
  recovery.py      M6：重启对账
"""

"""Orchestrator 及其副作用（M2 起）。

  messages.py      收件箱里的消息类型
  decide.py        纯函数 decide(state, msg) -> (changes, actions)，只依赖 graph.model 与 messages
  orchestrator.py  单写者循环：取消息 → decide → 落库 → 执行 actions
  effects.py       执行 actions：派发 worker、启动作业、回复 Future
  jobs.py          Job Runner：进程组、完成标记、按树哈希去重
  checks.py        基线、测试结果解析、失败签名归一化与归类
  gitops.py        worktree、影子仓库、merge-tree、比较并交换推进
  judges.py        M4：Test Author、Reviewer（独立上下文的模型调用）
  recovery.py      M6：重启对账
"""

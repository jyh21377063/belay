"""证据图（M2 起）：数据模型与存储，不含调度逻辑。

  model.py         节点、边、状态枚举（dataclass）
  store.py         SQLite：状态表 + 只追加的事件表
  invariants.py    五条不变量的断言
  requirements.py  需求抽取（release notes 切分、LHTB 阶段）

依赖规则：只依赖标准库与本目录，不依赖 runtime / worker / tools / llm / env。
"""

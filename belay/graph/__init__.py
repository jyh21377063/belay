"""证据图：数据模型、证据规则与存储，不含调度逻辑。

  model.py         节点、状态常量、GraphState、变更与事务 Tx
  evidence.py      证据规则：按基线归类、相关测试、失败签名、独立测试的收录
  ledger.py        需求状态的计算；账本与作业结果的文字
  requirements.py  需求抽取（release notes 机械切分）与引文校验
  build.py         初始图
  invariants.py    五条不变量的断言
  store.py         SQLite：状态表 + 只追加的事件表（本目录唯一做 IO 的文件）

依赖规则：只依赖标准库与本目录，不依赖 runtime / worker / tools / llm / env；除 store.py 外都是纯函数。
"""

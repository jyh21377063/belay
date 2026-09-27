"""C / D 组：Belay（证据图 + 自研 worker）。

M0/M1 阶段：与 B 组共用同一个 worker，只把行动边界换成严格模式、打开剩余时间提示，
用来先把接入、补丁导出、评分这条链路跑通。
M2 起，这里的 run() 改为：启动 Orchestrator（收件箱 + decide()），由它派发 worker、调度作业、推进集成分支；
导出的补丁改为集成分支 HEAD 相对基线的 diff（剔除测试路径）。
"""
from __future__ import annotations

from belay.tools import Policy
from eval.agents.flat_agent import FlatAgent


class BelayAgent(FlatAgent):
    default_policy = Policy.strict()
    time_reminders = True

    def __init__(self, *args, workers: int = 1, **kwargs):
        self.max_workers = int(workers)            # M5 起生效
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "belay"

"""Belay 的纯函数核心：事件、三个视图的推导、状态转换规则、不变量、调度建议、上下文构建、压缩规则。

这里的模块不做 IO、不调模型、不读时钟；tests/unit/test_layering.py 自动检查。
"""
from belay.core.config import BelayConfig
from belay.core.events import Event
from belay.core.model import Graph
from belay.core.reduce import IllegalEvent, apply, replay
from belay.core.rules import Rejected, SnapObs, Tx

__all__ = ["BelayConfig", "Event", "Graph", "IllegalEvent", "Rejected", "SnapObs", "Tx", "apply", "replay"]

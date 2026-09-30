"""Runtime：唯一的写者。

submit(rule, *args) 在一把锁里做完“规则 → 追加事件 → 更新视图 → 检查不变量”，然后按事件计划副作用并交给
effect handler 异步执行；副作用的结果再通过 submit 回来。所有组件（worker 的工具、作业、时钟、会话驱动）只通过
submit 改变状态，所以不需要别的并发控制。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from belay.core.config import BelayConfig
from belay.core.effects import Effect, effects_for
from belay.core.events import Event
from belay.core.invariants import check
from belay.core.model import Graph
from belay.core.reduce import replay
from belay.core.rules import Tx
from belay.runtime.store import EventStore


class InvariantViolation(RuntimeError):
    pass


class Runtime:
    def __init__(self, store: EventStore, cfg: BelayConfig, graph: Optional[Graph] = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = lambda m: None):
        self.store = store
        self.cfg = cfg
        self.graph = graph or Graph()
        self.clock = clock
        self.log = log
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition()
        self.effect_handler: Optional[Callable[[Effect], Awaitable[None]]] = None
        self.listeners: list[Callable[[list[Event], Graph], None]] = []
        self._tasks: set[asyncio.Task] = set()
        self._since_snapshot = 0
        self.closed = False

    # ---- 打开：快照 + 重放尾部
    @classmethod
    def open(cls, store: EventStore, cfg: BelayConfig, **kw) -> "Runtime":
        snap = store.latest_snapshot()
        tail = store.events(after=snap.seq if snap else 0)
        g = replay(tail, start=snap)
        return cls(store, cfg, graph=g, **kw)

    def now(self) -> float:
        return self.clock()

    # ---- 唯一的写入口
    async def submit(self, rule: Callable[..., Any], *args, **kw) -> Any:
        if self.closed:
            raise RuntimeError("runtime is closed")
        async with self._lock:
            tx = Tx(self.graph, self.now(), self.cfg)
            result = rule(tx, *args, **kw)                    # Rejected 在这里抛出，什么都不写
            if tx.events:
                if self.cfg.check_invariants:
                    bad = check(tx.g)
                    if bad:
                        raise InvariantViolation(f"{[e.type for e in tx.events]}: {bad}")
                self.store.append(tx.events)                  # 先写日志
                self.graph = tx.g
                self._since_snapshot += len(tx.events)
                if self._since_snapshot >= self.cfg.snapshot_every:
                    self.store.save_snapshot(self.graph)
                    self._since_snapshot = 0
            events, graph = list(tx.events), self.graph
        if events:
            for fn in self.listeners:
                try:
                    fn(events, graph)
                except Exception as e:                        # 监听者的错误不影响状态
                    self.log(f"listener error: {type(e).__name__}: {e}")
            async with self._changed:
                self._changed.notify_all()
            for eff in effects_for(events, graph):            # 再做副作用
                self.spawn(eff)
        return result

    def spawn(self, eff: Effect) -> None:
        if self.effect_handler is None or self.closed:
            return
        task = asyncio.create_task(self._run_effect(eff))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_effect(self, eff: Effect) -> None:
        try:
            await self.effect_handler(eff)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(f"effect {eff.kind} failed: {type(e).__name__}: {e}")

    # ---- 等待图满足某个条件
    async def wait_until(self, pred: Callable[[Graph], bool], timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        async with self._changed:
            while not pred(self.graph):
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=left)
                except asyncio.TimeoutError:
                    return pred(self.graph)
        return True

    async def changed(self, timeout: float) -> None:
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def close(self, cancel_effects: bool = True) -> None:
        self.closed = True
        if cancel_effects:
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self.store.save_snapshot(self.graph)

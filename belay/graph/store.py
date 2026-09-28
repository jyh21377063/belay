"""证据图的持久化：SQLite（WAL），当前状态表 + 只追加的事件表。

不做完整的事件溯源：entities 保存每个实体的最新版本，events 记录每次状态转换，用于回放与对账。
每条消息的全部变更在一个事务里写入。transient 实体（wait 的挂起请求）不落库。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from belay.graph.model import TRANSIENT_KINDS, Delete, Event, GraphState, Put, from_dict, kind_of, to_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                                     PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, type TEXT NOT NULL,
                                   data TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def commit(self, changes: list) -> None:
        with self.db:
            for c in changes:
                if isinstance(c, Put):
                    k = kind_of(c.obj)
                    if k in TRANSIENT_KINDS:
                        continue
                    self.db.execute("INSERT OR REPLACE INTO entities (kind, id, data) VALUES (?, ?, ?)",
                                    (k, c.obj.id, json.dumps(to_dict(c.obj), ensure_ascii=False)))
                elif isinstance(c, Delete):
                    if c.kind not in TRANSIENT_KINDS:
                        self.db.execute("DELETE FROM entities WHERE kind = ? AND id = ?", (c.kind, c.id))
                elif isinstance(c, Event):
                    self.db.execute("INSERT INTO events (t, type, data) VALUES (?, ?, ?)",
                                    (c.t, c.type, json.dumps(c.data, ensure_ascii=False, default=str)))

    def load(self) -> GraphState | None:
        rows = self.db.execute("SELECT kind, id, data FROM entities").fetchall()
        run = next((from_dict("run", json.loads(d)) for k, _, d in rows if k == "run"), None)
        if run is None:
            return None
        state = GraphState(run=run)
        for k, i, d in rows:
            if k != "run":
                state.table(k)[i] = from_dict(k, json.loads(d))
        return state

    def events(self) -> list[dict]:
        rows = self.db.execute("SELECT seq, t, type, data FROM events ORDER BY seq").fetchall()
        return [{"seq": s, "t": t, "type": ty, **json.loads(d)} for s, t, ty, d in rows]

    def export_events(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for e in self.events():
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def close(self) -> None:
        self.db.close()


def init_state(store: Store, state: GraphState) -> None:
    """把初始图整体写入（首次启动）。"""
    changes: list = [Put(state.run)]
    for kind in ("requirement", "check", "work"):
        changes += [Put(o) for o in state.table(kind).values()]
    changes.append(Event("init", state.run.started_t, {"requirements": len(state.requirement),
                                                        "checks": len(state.check)}))
    store.commit(changes)

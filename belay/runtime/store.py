"""事件存储（宿主机）：SQLite 里一张只追加的事件表 + 视图快照表；附件按内容寻址存成文件。

  append(events)     一个事务里追加一批事件；序号必须紧接着最后一条（单写者）
  events(after)      读出某个序号之后的事件
  save_snapshot(g)   保存视图快照（恢复时加载最近的快照，再重放之后的事件）
  put_blob(text)     附件（大工具输出、diff、补丁镜像）；返回路径
另写一份 events.jsonl 方便人看和回放页面读取；SQLite 是唯一真相。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from belay.core.events import Event
from belay.core.model import Graph, graph_from_json, to_json


class StoreError(RuntimeError):
    pass


class EventStore:
    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "blobs").mkdir(exist_ok=True)
        self.db = sqlite3.connect(str(self.dir / "events.sqlite"), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, t REAL, type TEXT, actor TEXT, "
                        "source TEXT, payload TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshots (seq INTEGER PRIMARY KEY, graph TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self._jsonl = self.dir / "events.jsonl"

    # ---- 事件
    def last_seq(self) -> int:
        row = self.db.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(row[0] or 0)

    def append(self, events: Iterable[Event]) -> None:
        events = list(events)
        if not events:
            return
        last = self.last_seq()
        cur = self.db.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for e in events:
                if e.seq != last + 1:
                    raise StoreError(f"event seq {e.seq} does not follow {last}")
                cur.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                            (e.seq, e.t, e.type, e.actor, e.source, json.dumps(e.payload, ensure_ascii=False)))
                last = e.seq
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        with self._jsonl.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

    def events(self, after: int = 0) -> list[Event]:
        rows = self.db.execute("SELECT seq, t, type, actor, source, payload FROM events WHERE seq > ? ORDER BY seq",
                               (after,)).fetchall()
        return [Event(r[0], r[1], r[2], r[3], r[4], json.loads(r[5])) for r in rows]

    # ---- 快照
    def save_snapshot(self, g: Graph) -> None:
        self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?)", (g.seq, json.dumps(to_json(g))))

    def latest_snapshot(self) -> Optional[Graph]:
        row = self.db.execute("SELECT graph FROM snapshots ORDER BY seq DESC LIMIT 1").fetchone()
        return graph_from_json(json.loads(row[0])) if row else None

    # ---- 元数据（例如最近一次心跳时间，用来计算停机时长）
    def set_meta(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, json.dumps(v)))

    def get_meta(self, k: str, default=None):
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---- 附件
    def put_blob(self, text: str, suffix: str = ".txt") -> str:
        data = text.encode("utf-8", errors="surrogateescape")
        name = hashlib.sha256(data).hexdigest()[:24] + suffix
        path = self.dir / "blobs" / name
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return str(path)

    def read_blob(self, path: str, max_chars: int | None = None) -> str:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text if max_chars is None else text[:max_chars]

    def close(self) -> None:
        self.db.close()

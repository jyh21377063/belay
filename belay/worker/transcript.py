"""运行轨迹：只追加的 JSONL，每条事件写完立即 flush，超时被取消时也不丢。

事件类型：
  start / assistant / tool_result / event（越界等）/ reset / subagent_start / subagent_end / end
M2 起证据图的事件表也从这里的格式扩展出来，回放页面读的就是它。
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class Transcript:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._t0 = time.time()

    def sibling(self, name: str) -> "Transcript":
        """同目录下的另一份轨迹，例如探索子 agent 的 transcript-explore-1.jsonl。"""
        return Transcript(self.path.with_name(f"{self.path.stem}-{name}.jsonl") if self.path else None)

    def write(self, type_: str, **data) -> None:
        if not self.path:
            return
        rec = {"t": round(time.time(), 3), "dt": round(time.time() - self._t0, 3), "type": type_, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

"""todo 提醒的触发与节流（BelaySession._todo_notes，不调模型）。

勾掉 todo 是后台合并的时机，所以跑完测试时提醒一句“做完了就勾掉”；但不能太强、太频繁：
只在有进行中的条目、上次更新 todo 之后改过文件、最近几轮没碰过 todo 时提醒，同一条目有上限，两次之间有间隔。
"""
from __future__ import annotations

from typing import Optional

from belay.core.config import BelayConfig
from belay.env import LocalEnv
from belay.runtime.session import TEST_CMD, TODO_DONE, BelaySession
from belay.tools import get_belay_tools


class Hooks:
    def __init__(self, title: Optional[str] = None, has: bool = False):
        self.title = title
        self.has = has

    def has_todos(self) -> bool:
        return self.has or self.title is not None

    def active_todo_title(self) -> Optional[str]:
        return self.title


def session(tmp_path, hooks: Optional[Hooks] = None, **cfg) -> BelaySession:
    return BelaySession(None, LocalEnv(str(tmp_path)), get_belay_tools(), "system", "opening", hooks or Hooks(),
                        BelayConfig(**cfg))


EDIT = {"name": "edit_file", "input": {"file_path": "pkg/mod.py"}}
TEST = {"name": "bash", "input": {"command": "python -m pytest -q tests/test_mod.py"}}
LS = {"name": "bash", "input": {"command": "ls"}}
OK, FAIL = ("ok", False), ("boom", True)


def todo(*items: tuple[str, str]) -> dict:
    return {"name": "todo_write", "input": {"todos": [{"content": c, "status": s} for c, s in items]}}


class Driver:
    """一轮一轮地喂工具调用（模拟模型），收集每一轮的提醒。"""

    def __init__(self, s: BelaySession):
        self.s = s

    def turn(self, *uses: dict, results=None) -> list[str]:
        self.s.turns += 1
        uses = [{"id": f"t{self.s.turns}_{i}", **u} for i, u in enumerate(uses)]
        res = results or [OK] * len(uses)
        for u in uses:
            if u["name"] == "todo_write":
                self.s.ctx.todos = list(u["input"]["todos"])
        return self.s._todo_notes(uses, res)

    def idle(self, n: int) -> None:
        for _ in range(n):
            assert not self.turn(LS)


def done_notes(notes: list[str]) -> list[str]:
    return [n for n in notes if "You just ran tests" in n]


def test_test_commands_are_recognised():
    for cmd in ("pytest -x", "python -m pytest tests", "cargo test", "go test ./...", "npm run test", "make check",
                "python -m unittest discover"):
        assert TEST_CMD.search(cmd), cmd
    assert not TEST_CMD.search("cat tests/test_mod.py") and not TEST_CMD.search("ls")


def test_nudges_after_tests_when_an_item_is_in_progress_and_files_changed(tmp_path):
    d = Driver(session(tmp_path))
    d.turn(todo(("parse the header", "in_progress"), ("parse the body", "pending")))
    d.idle(3)
    assert not d.turn(EDIT)
    notes = d.turn(TEST)
    assert notes == [TODO_DONE.format(title="parse the header")]
    assert "ticking an item is when the harness reviews and merges your work" in notes[0]


def test_no_nudge_without_changes_without_tests_or_without_an_item_in_progress(tmp_path):
    d = Driver(session(tmp_path))
    d.turn(todo(("a", "in_progress")))
    d.idle(4)
    assert not d.turn(TEST)                                    # 上次更新 todo 之后没改过文件
    assert not d.turn(EDIT)
    assert not d.turn(LS)                                      # 没跑测试
    assert not d.turn(TEST, results=[FAIL])                    # 测试命令本身出错（工具错误）
    d = Driver(session(tmp_path))
    d.turn(todo(("a", "completed"), ("b", "pending")))         # 没有进行中的条目
    d.idle(4)
    d.turn(EDIT)
    assert not d.turn(TEST)


def test_no_nudge_right_after_the_todo_list_was_updated(tmp_path):
    d = Driver(session(tmp_path))
    d.turn(todo(("a", "in_progress")))
    d.turn(EDIT)
    assert not d.turn(TEST)                                    # 刚写过 todo（最近 3 轮内）
    d.turn(todo(("a", "in_progress"), ("b", "pending")))
    d.turn(EDIT, TEST)
    assert not done_notes(d.turn(TEST))
    d.idle(1)
    assert done_notes(d.turn(TEST))                            # 安静够了才提醒


def test_the_same_item_is_nudged_at_most_twice_with_a_gap(tmp_path):
    d = Driver(session(tmp_path))
    d.turn(todo(("a", "in_progress")))
    d.idle(3)
    d.turn(EDIT)
    assert done_notes(d.turn(TEST))
    first = d.s.turns
    hits = []
    for _ in range(40):                                        # 每一轮都改文件并跑测试
        if done_notes(d.turn(EDIT, TEST)):
            hits.append(d.s.turns - first)
    assert hits == [8]                                         # 间隔 8 轮后第二次，之后同一条目不再提醒
    d.turn(todo(("a", "completed"), ("b", "in_progress")))     # 换了条目：重新计数
    d.idle(3)
    d.turn(EDIT)
    assert d.turn(TEST) == [TODO_DONE.format(title="b")]


def test_after_a_reset_the_item_in_progress_comes_from_the_graph(tmp_path):
    d = Driver(session(tmp_path, Hooks(title="fix add (R1)")))
    d.idle(4)                                                  # 新会话：模型还没写过列表
    d.turn(EDIT)
    assert d.turn(TEST) == [TODO_DONE.format(title="fix add (R1)")]
    d = Driver(session(tmp_path, Hooks(title=None)))
    d.idle(4)
    d.turn(EDIT)
    assert not done_notes(d.turn(TEST))


def test_the_nudge_can_be_turned_off_and_the_stale_reminder_names_the_item(tmp_path):
    d = Driver(session(tmp_path, todo_done_nudge=False, todo_reminder_turns=5))
    d.turn(todo(("a", "in_progress")))
    d.idle(3)
    d.turn(EDIT)
    notes = d.turn(TEST)
    assert notes and not done_notes(notes)                     # 关掉后只剩很久没更新的提醒
    assert "\"a\" (in progress)" in notes[0] and "reviews and merges" in notes[0]


def test_first_reminder_still_fires_once_when_there_is_no_todo_list(tmp_path):
    d = Driver(session(tmp_path))
    notes = d.turn(EDIT)
    assert len(notes) == 1 and "Keeping a todo list" in notes[0]
    assert not d.turn(EDIT) and not d.turn(TEST)               # 没有进行中的条目：不提醒勾掉

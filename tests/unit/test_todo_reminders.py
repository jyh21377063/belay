"""todo 提醒的触发与节流（BelaySession._todo_notes，不调模型）。

勾掉 todo 是后台合并的时机，所以跑完测试时提醒一句“做完了就勾掉”；但不能太强、太频繁：
只在有进行中的条目、上次更新 todo 之后改过文件、最近几轮没碰过 todo 时提醒，同一条目有上限，两次之间有间隔。
"""
from __future__ import annotations

from typing import Optional

from belay.core.config import BelayConfig
from belay.env import LocalEnv
from belay.runtime.shellcmd import is_test_command
from belay.runtime.session import (TODO_DONE, TODO_FIRST, TODO_STALE, TODO_STALE_ACTIVE,
                                   TODO_STALE_NONE, BelaySession, in_progress_phrase)
from belay.tools import get_belay_tools


class Hooks:
    def __init__(self, title: Optional[str] = None, has: bool = False, titles: Optional[list[str]] = None):
        self.titles = list(titles or ([title] if title else []))
        self.has = has

    def has_todos(self) -> bool:
        return self.has or bool(self.titles)

    def active_todo_titles(self) -> list[str]:
        return list(self.titles)


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
        assert is_test_command(cmd), cmd
    for cmd in ("cat tests/test_mod.py", "ls", "cat pytest.ini", 'grep -rn "pytest\\|unittest" .', "pip install pytest",
                "ps aux | grep pytest"):
        assert not is_test_command(cmd), cmd


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
    d.turn(EDIT)                                               # 从第一次改文件起计时
    d.idle(4)
    notes = d.turn(TEST)
    assert notes and not done_notes(notes)                     # 关掉后只剩很久没更新的提醒
    assert "\"a\" (in progress)" in notes[0] and "reviews and merges" in notes[0]


def test_first_reminder_still_fires_once_when_there_is_no_todo_list(tmp_path):
    d = Driver(session(tmp_path))
    notes = d.turn(EDIT)
    assert len(notes) == 1 and "Keeping a todo list" in notes[0]
    assert not d.turn(EDIT) and not d.turn(TEST)               # 没有进行中的条目：不提醒勾掉


# ======================================================================== 很久没更新：开始干活后计时、不限次数、退避

def stale_notes(notes: list[str]) -> list[str]:
    return [n for n in notes if n.startswith("The todo list has not")]


def test_exploration_does_not_start_the_stale_clock(tmp_path):
    """全是 pending、只读不写：不管多少轮都不提醒（v8 那次 3.1 / 5.5 / 8.1 分钟的三次提醒都落在这里）。"""
    d = Driver(session(tmp_path))
    d.turn(todo(*[(f"item {i}", "pending") for i in range(11)]))
    for _ in range(200):
        assert not d.turn(LS)
        assert not d.turn(TEST)                                # 跑测试（复现问题）也不算开始干活


def test_the_clock_starts_at_the_first_edit_after_the_todo_update(tmp_path):
    d = Driver(session(tmp_path, todo_done_nudge=False))
    d.turn(todo(("a", "in_progress")))
    d.idle(50)                                                 # 探索期：不计时
    d.turn(EDIT)
    start = d.s.turns
    hits = []
    for _ in range(40):
        if stale_notes(d.turn(LS)):
            hits.append(d.s.turns - start)
    assert hits == [30]
    assert d.s._stale_reminders == 1


def test_unlimited_reminders_back_off_while_ignored_and_reset_when_the_list_is_updated(tmp_path):
    d = Driver(session(tmp_path, todo_done_nudge=False))
    d.turn(todo(("a", "in_progress")))
    fired = []
    for _ in range(500):                                       # 一直在改文件，从不更新 todo
        if stale_notes(d.turn(EDIT)):
            fired.append(d.s.turns)
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    assert len(fired) >= 4                                     # 不限次数
    assert gaps[:3] == [61, 121, 241] and all(g == 241 for g in gaps[3:])   # 30 → 60 → 120 → 240 封顶（+1：之后的第一次改文件起算）
    d.turn(todo(("a", "completed"), ("b", "in_progress")))     # 模型理会了：间隔恢复
    d.turn(EDIT)
    start = d.s.turns
    while not stale_notes(d.turn(EDIT)):
        pass
    assert d.s.turns - start == 30


def test_a_stale_reminder_needs_new_edits_after_the_previous_one(tmp_path):
    d = Driver(session(tmp_path, todo_done_nudge=False, todo_reminder_turns=5))
    d.turn(todo(("a", "in_progress")))
    d.turn(EDIT)
    d.idle(4)
    assert stale_notes(d.turn(LS))
    d.idle(100)                                                # 之后没再改文件（排查、读代码）：不再提醒
    d.turn(EDIT)
    d.idle(9)
    assert stale_notes(d.turn(LS))                             # 退避后的间隔 10


def test_the_stale_reminder_can_still_be_capped(tmp_path):
    d = Driver(session(tmp_path, todo_done_nudge=False, todo_reminder_turns=5, todo_reminder_backoff=1,
                       todo_reminder_max=2))
    d.turn(todo(("a", "in_progress")))
    n = sum(1 for _ in range(200) if stale_notes(d.turn(EDIT)))
    assert n == 2


def test_the_first_reminder_does_not_use_up_the_stale_reminders(tmp_path):
    """第一次的“还没有 todo”和“很久没更新”分开计数；没有列表时的提醒文字不说“列表没更新”。"""
    d = Driver(session(tmp_path, todo_done_nudge=False, todo_reminder_turns=5, todo_reminder_max=1))
    assert d.turn(EDIT) == [TODO_FIRST]
    d.turn(EDIT)
    d.idle(4)
    assert d.turn(LS) == [TODO_STALE_NONE]
    d = Driver(session(tmp_path, todo_done_nudge=False, todo_reminder_turns=5))
    d.turn(todo(("a", "completed"), ("b", "pending")))
    d.turn(EDIT)
    d.idle(4)
    assert d.turn(LS) == [TODO_STALE]                          # 有列表、没有进行中的条目


# ======================================================================== 多项并行 in_progress

def test_several_items_in_progress_are_named_as_a_group(tmp_path):
    d = Driver(session(tmp_path))
    items = [(f"R{i} change", "in_progress") for i in range(1, 7)]
    d.turn(todo(*items))
    d.idle(3)
    d.turn(EDIT)
    notes = d.turn(TEST)
    assert len(notes) == 1 and notes[0].startswith("You just ran tests while 6 todo items are in progress")
    assert "\"R1 change\"; \"R2 change\"; \"R3 change\"; and 3 more" in notes[0]
    assert "R4 change" not in notes[0] and "Mark each one" in notes[0]
    assert in_progress_phrase(["a", "b"]) == '2 todo items are in progress ("a"; "b")'


def test_the_nudge_budget_is_per_group_and_restarts_when_the_group_changes(tmp_path):
    d = Driver(session(tmp_path))
    d.turn(todo(("a", "in_progress"), ("b", "in_progress"), ("c", "in_progress")))
    d.idle(3)
    hits = []
    for _ in range(60):
        if done_notes(d.turn(EDIT, TEST)):
            hits.append(d.s.turns)
    assert len(hits) == 2                                      # 同一组最多 2 次
    d.turn(todo(("a", "completed"), ("b", "in_progress"), ("c", "in_progress")))   # 勾掉一项：组变了
    d.idle(3)
    d.turn(EDIT)
    notes = d.turn(TEST)
    assert done_notes(notes) and "2 todo items are in progress (\"b\"; \"c\")" in notes[0]
    d.turn(todo(("b", "in_progress"), ("c", "in_progress")))   # 同一组（只是删了勾掉的那项）：不重新计数
    d.idle(10)
    d.turn(EDIT)
    hits = [1 for _ in range(30) if done_notes(d.turn(EDIT, TEST))]
    assert len(hits) == 1


def test_the_stale_reminder_names_the_group_and_the_graph_group_after_a_reset(tmp_path):
    d = Driver(session(tmp_path, Hooks(titles=["fix add (R1)", "fix mul (R2)"]), todo_done_nudge=False,
                       todo_reminder_turns=5))
    d.turn(EDIT)                                               # 新会话：模型还没写过列表，进行中的条目来自图
    d.idle(4)
    notes = d.turn(LS)
    assert notes and notes[0].startswith("The todo list has not been updated recently. 2 todo items are in progress")
    assert "\"fix add (R1)\"; \"fix mul (R2)\"" in notes[0]
    d = Driver(session(tmp_path, Hooks(titles=["only one"]), todo_reminder_turns=5))
    d.idle(4)
    d.turn(EDIT)
    assert d.turn(TEST) == [TODO_DONE.format(title="only one")]
    d.idle(10)
    d.turn(EDIT)
    d.idle(4)
    assert stale_notes(d.turn(LS)) == [TODO_STALE_ACTIVE.format(title="only one")]


def test_belay_todo_write_allows_parallel_items_and_flat_keeps_the_original():
    from belay.tools import get_belay_tools, get_tools
    belay = next(t for t in get_belay_tools() if t.name == "todo_write")
    flat = next(t for t in get_tools() if t.name == "todo_write")
    assert "Keep exactly one item in_progress" in flat.description
    assert "exactly one" not in belay.description and "several items can be in_progress together" in belay.description
    assert belay.handler is flat.handler and belay.input_schema == flat.input_schema

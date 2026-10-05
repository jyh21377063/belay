"""以参考解在本环境中的实际结果为基准，重新统计 SWE-EVO 的 F2P / P2P。

数据集给的 F2P / P2P 清单里，有些测试在我们的镜像里连参考解都过不了（依赖版本、离线环境、测试名被截断等），
例如 conan_2.0.14 有 14 个 P2P、dvc_2.8.1 有 36 个 F2P 和 23 个 P2P。它们对任何组都不可能通过，
直接计数会让每个组都显示同样的“回归”，F2P 也永远到不了满分。

做法：读 gold-check 的 oracle 运行（runs.yaml 的 gold_run），参考解任何一次没通过的测试记为“不可达”，
在所有组里都扣掉：
  f2p_passed_ref / f2p_total_ref    只数参考解能过的 F2P
  p2p_regressions_ref               不算参考解自己也失败的 P2P
判定规则与 tests/grade.py 相同（直接加载任务目录里的 grade.py）：PASSED / XFAIL 为通过，缺失或 FAILED / ERROR 为失败。
原始的 f2p_passed / f2p_total / p2p_regressions 保持不变，两者并列报告。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

START, END = ">>>>> Start Test Output", ">>>>> End Test Output"
OK = ("PASSED", "XFAIL")
BAD = ("FAILED", "ERROR")


def _load_grade(task_dir: Path):
    spec = importlib.util.spec_from_file_location(f"belay_grade_{task_dir.name}", task_dir / "tests" / "grade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parser_name(task_dir: Path) -> str:
    gate = task_dir / "gate.json"
    if gate.exists():
        return json.loads(gate.read_text()).get("parser") or "parse_log_pytest"
    return "parse_log_pytest"


def test_log(trial_dir: Path) -> Path | None:
    """重放评分的日志优先（pier/grade/...），没有时用原容器评分的日志（oracle / inline）。"""
    for pattern in ("pier/grade/**/verifier/test_output.txt", "pier/**/verifier/test_output.txt"):
        logs = sorted(trial_dir.glob(pattern))
        if logs:
            return logs[0]
    return None


class GoldRef:
    def __init__(self, gold_root: Path, task_dirs: Path):
        self.gold_root = Path(gold_root)
        self.task_dirs = Path(task_dirs)
        self._grade: dict[str, object] = {}
        self._excluded: dict[str, tuple[set, set] | None] = {}

    def _task_dir(self, tid: str) -> Path:
        return self.task_dirs / "swe_evo" / tid

    def _tests(self, tid: str) -> dict:
        return json.loads((self._task_dir(tid) / "tests" / "tests.json").read_text())

    def statuses(self, tid: str, trial_dir: Path) -> dict | None:
        """一个 trial 的逐测试结果；日志缺失或测试补丁没应用（没有测试输出）时返回 None。"""
        log_path = test_log(trial_dir)
        if log_path is None:
            return None
        log = log_path.read_text(errors="replace")
        if START not in log or END not in log:
            return None
        if tid not in self._grade:
            self._grade[tid] = _load_grade(self._task_dir(tid))
        parser = self._grade[tid].PARSERS[_parser_name(self._task_dir(tid))]
        return parser(log.split(START)[1].split(END)[0])

    def excluded(self, tid: str) -> tuple[set, set] | None:
        """参考解任何一次没通过的 F2P、P2P。gold 运行里没有这道题时返回 None（不做调整）。"""
        if tid in self._excluded:
            return self._excluded[tid]
        tests = self._tests(tid)
        f_ex, p_ex, seen = set(), set(), False
        for d in sorted((self.gold_root / "swe_evo" / tid).glob("*")):
            sm = self.statuses(tid, d) if d.is_dir() else None
            if sm is None:
                continue
            seen = True
            f_ex |= {t for t in tests["FAIL_TO_PASS"] if sm.get(t) not in OK}
            p_ex |= {t for t in tests["PASS_TO_PASS"] if sm.get(t) not in OK}
        self._excluded[tid] = (f_ex, p_ex) if seen else None
        return self._excluded[tid]

    def adjust(self, tid: str, trial_dir: Path) -> dict:
        empty = {"f2p_passed_ref": None, "f2p_total_ref": None, "p2p_regressions_ref": None,
                 "ref_excluded": None}
        if not (self._task_dir(tid) / "tests" / "tests.json").exists():
            return empty
        ref = self.excluded(tid)
        if ref is None:
            return empty
        f_ex, p_ex = ref
        tests = self._tests(tid)
        f2p = [t for t in tests["FAIL_TO_PASS"] if t not in f_ex]
        out = {**empty, "f2p_total_ref": len(f2p), "ref_excluded": f"F2P {len(f_ex)} / P2P {len(p_ex)}"}
        sm = self.statuses(tid, trial_dir)
        if sm is None:                    # 测试补丁没应用上：没有运行任何测试
            out["f2p_passed_ref"] = 0
            return out
        out["f2p_passed_ref"] = sum(1 for t in f2p if sm.get(t) in OK)
        out["p2p_regressions_ref"] = sum(1 for t in tests["PASS_TO_PASS"]
                                         if t not in p_ex and (t not in sm or sm[t] in BAD))
        return out

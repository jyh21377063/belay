"""eval.belay_report：从一次真实（脚本化）的 Belay 运行生成组件报告，并把评分失败项对照到 Belay 的视角。"""
from __future__ import annotations

import asyncio
import json
import shutil

from belay.llm import ScriptedLLM
from eval import belay_report
from tests.integration.test_belay_run import ADD, ADD_SUB, FIX_ADD, MUL, PLANNER, READ, SUBMIT, TASK, Harness, call


def test_component_report_with_grade(tmp_path, monkeypatch):
    h = Harness(tmp_path / "h")
    run = h.make(ScriptedLLM([PLANNER, call(READ), call(FIX_ADD), call(SUBMIT), call(ADD_SUB), call(SUBMIT)]))
    assert asyncio.run(run.start(TASK)).status == "DONE"

    results, tasks = tmp_path / "results", tmp_path / "tasks"
    trial = results / "r1" / "swe_evo" / "demo" / "1"
    shutil.copytree(h.run_dir(), trial / "pier" / "agent" / "t1" / "agent" / "belay")
    verifier = trial / "pier" / "grade" / "g1" / "verifier"
    verifier.mkdir(parents=True)
    (verifier / "test_output.txt").write_text(">>>>> Start Test Output\n"
                                              f"PASSED {ADD}\nFAILED {MUL} - assert 0\n"
                                              ">>>>> End Test Output\n")
    (trial / "grade.json").write_text(json.dumps({"fix_rate": 0.0, "rewards": {
        "f2p_success": 1, "f2p_failure": 1, "p2p_failure": 1}}))
    (tasks / "swe_evo" / "demo" / "tests").mkdir(parents=True)
    (tasks / "swe_evo" / "demo" / "tests" / "tests.json").write_text(json.dumps({
        "FAIL_TO_PASS": [ADD, "tests/test_new.py::test_sub"], "PASS_TO_PASS": [MUL]}))
    monkeypatch.setattr(belay_report, "_root", lambda key: {"results_root": results, "task_dirs": tasks}[key])

    assert belay_report.main(["r1"]) == 0
    md = (trial / "belay_report.md").read_text()
    for phrase in ["失败的 P2P（原本通过、现在失败）：1 个", f"| {MUL} | FAILED | 基线 pass，在回归门里；交付树上 pass",
                   "| tests/test_new.py::test_sub | 没跑出结果 | 基线里没有这个测试（所在文件不在基线里",
                   "合并点 2 个（链头 #2）", "| 触发 | 合并 | 被取代 | 合计 |", "| #2 ← 交付 |", "完成判定来源：checks 1，review 1",
                   "| V2 | submit | A3 | decided |", "建议合并 | 通过 | done/E1 1", "worker 提交 2 次：returned 1，accepted 1"]:
        assert phrase in md, phrase

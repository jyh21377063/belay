"""Belay runtime 的统一配置：从 runs.yaml 中 belay agent 的 kwargs.runtime 构造。

裁判由两道门和一个申诉通道组成，存档是集成分支：
  合并门   原来能过的测试必须还能过（gate）；决定候选能不能进集成分支
  完成门   每条需求一个验收测试，开工时由 Test Author 写好冻结（test_author）；最终提交要求它们全部通过
  申诉     report_conflict → reviewer 按任务原文裁决（reports）

每个开关对应一个机制，关掉之后 runtime 退化为更弱的裁判：
  gate: off                       只记录候选，不跑合并门（= M2 阶段）
  gate: advise                    合并门照跑，但只提示不拒绝
  test_author / reports: false    关掉完成门 / 申诉通道（= M3 阶段）
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, fields


@dataclass
class RuntimeConfig:
    # ---- 需求拆解（worker 开始之前）
    requirement_planner: str = "llm"    # llm：一个模型拆、一个模型审、runtime 检查引文与覆盖；rules：规则切分
    planner_rounds: int = 2             # 检查或审阅有意见时，交回拆解模型重做的最多轮数
    planner_review: bool = True         # 是否用第二个模型审阅拆解
    # ---- 门禁
    gate: str = "block"                 # block | advise | off
    checkpoint_gate: str = "related"    # 检查点：related（改动相关的测试文件）| full
    final_gate: str = "full"            # 最终提交：full | related；剩余时间不够跑全量时自动降为 related
    protect_tests: bool = True          # 候选与交付剔除测试路径下的改动（只在有测试配置时生效）
    # ---- 完成门（验收测试）与申诉
    test_author: bool = True            # 开工时为每条需求写一个验收测试（不设数量上限）
    test_author_parallel: int = 4       # 同时在写的验收测试数（只影响先后，不影响写不写）
    test_author_max_turns: int = 30
    test_author_retries: int = 1        # 测试本身无效（语法、fixture、导入）时退回重写的次数
    reports: bool = True                # report_conflict + reviewer
    # ---- 作业与预算
    max_parallel_jobs: int = 2          # 同时运行的开发检查数（门禁不受限）
    gate_reserve_factor: float = 1.2    # 截止保护的预留 = 全量门禁实测耗时 × 系数 + judge_reserve_sec
    gate_reserve_min_sec: float = 90
    gate_reserve_max_frac: float = 0.25 # 预留最多占预算的比例；不够跑全量时最终门禁自动降为 related
    judge_reserve_sec: float = 30
    wait_max_sec: int = 1800
    tick_sec: float = 10
    stop_grace_sec: float = 30          # 截止时先让 worker 自己停，超过这个时间再取消
    # ---- 隔离
    isolation: bool = True              # worker 以低权限用户运行；失败时自动退回并记录原因
    agent_user: str = "belay-agent"
    # ---- 其他
    check_invariants: bool = True
    workers: int = 1                    # M5 起生效

    def reserve_sec(self, full_gate_sec: float, budget_sec: float, gate_on: bool) -> float:
        """截止保护的预留：最终门禁的耗时 × 系数 + 余量，有下限，最多占预算的一定比例。"""
        reserve = full_gate_sec * self.gate_reserve_factor if gate_on and self.gate != "off" else 0.0
        reserve = max(self.gate_reserve_min_sec, reserve + self.judge_reserve_sec)
        return min(reserve, budget_sec * self.gate_reserve_max_frac)

    @classmethod
    def from_dict(cls, d: dict | None) -> "RuntimeConfig":
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"未知的 runtime 配置项：{unknown}")
        cfg = cls(**d)
        if cfg.gate not in ("block", "advise", "off"):
            raise ValueError(f"gate 只能是 block / advise / off，而不是 {cfg.gate!r}")
        if cfg.requirement_planner not in ("llm", "rules"):
            raise ValueError("requirement_planner 只能是 llm / rules")
        for name in ("checkpoint_gate", "final_gate"):
            if getattr(cfg, name) not in ("related", "full"):
                raise ValueError(f"{name} 只能是 related / full")
        return cfg


@dataclass
class RuntimePaths:
    """容器内的路径。state 只属于 root（0700）；bin 对 worker 可读；dev_jobs 对 worker 可写。"""
    state: str = "/opt/belay"
    bin: str = "/opt/belay-bin"
    dev_jobs: str = "/tmp/belay-jobs"

    @property
    def git_dir(self) -> str:
        return posixpath.join(self.state, "git")

    @property
    def gate_jobs(self) -> str:
        return posixpath.join(self.state, "jobs")

    @property
    def checks(self) -> str:
        return posixpath.join(self.state, "checks")

    @property
    def orig(self) -> str:
        return posixpath.join(self.state, "orig")

    @property
    def runner(self) -> str:
        return posixpath.join(self.bin, "runner.py")

    def index(self, name: str) -> str:
        return posixpath.join(self.state, "idx", name)

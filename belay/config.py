"""Belay runtime 的统一配置：从 runs.yaml 中 belay agent 的 kwargs.runtime 构造。

v4 的原则 6：每个机制都对应一个观察到的问题，并且可以关掉。这里的每个开关对应一个机制，
关掉之后 runtime 退化为更弱的裁判（用于逐步验收和消融）：

  gate: off                       只记录候选，不跑门禁（= M2 阶段）
  gate: advise                    门禁照跑，但只提示不拒绝（"只建议不阻止"的消融）
  test_author / reports: false    关掉独立测试 / 上报通道（= M3 阶段）
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
    # ---- 独立证据与上报
    test_author: bool = True
    test_author_quota: int = 8          # 每次运行最多请求的独立测试数
    test_author_parallel: int = 2
    test_author_max_turns: int = 30
    test_author_retries: int = 1        # 测试本身无效（语法、fixture、导入）时退回重写的次数
    reports: bool = True                # report_conflict + reviewer
    # ---- 结束
    final_info_bounce: bool = True      # 第一次 final 提交时若仍有需求没有独立证据，把账本作为信息返回一次
    final_info_min_left_min: float = 15
    max_final_bounces: int = 2          # 独立检查失败（FAILED）时最多退回几次
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

"""BelayConfig：runtime 的全部阈值与机制开关（纯数据，core 与 runtime 共用）。

原则 8：每个机制都能关，用消融实验看哪些在承重。
  suggest=False          不给调度建议（“Belay − 调度建议”）
  graph_context=False    开场与 L2 不用 build_context，改用模型写的完整摘要（“Belay − 图上下文”）
  confirm_regressions    回归先重跑一次确认，重跑通过的记为 flaky
  protect_tests          候选剔除测试路径下的改动
  stall=False            不做停滞检测
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True)
class BelayConfig:
    # ---- 租约
    lease_ttl_sec: float = 1800
    # ---- 存档
    checkpoint_tier: str = "related"        # related | full：平时的验证档位；截止与收尾永远是 full
    confirm_regressions: bool = True
    protect_tests: bool = True
    checkpoint_reminder_sec: float = 1200   # 距上次存档超过该时长时提醒（只提醒，不强制）
    # ---- 截止预留 = max(下限, 全量验证实测耗时 × 系数 + 余量)，最多占预算的一定比例
    reserve_factor: float = 1.3
    reserve_extra_sec: float = 30
    reserve_min_sec: float = 120
    reserve_max_frac: float = 0.25
    # ---- 运行结束与停滞
    max_idle_sessions: int = 2              # 连续这么多个会话没有新证据 → 停止并报告
    max_crash_restarts: int = 3             # 连续崩溃这么多次 → 停止
    stall: bool = True
    stall_no_progress_sec: float = 1800
    stall_same_failure: int = 3
    idle_timeout_sec: float = 1500          # 这么久没有任何工具调用 → 视为卡死，结束会话
    # ---- 调度建议与上下文
    suggest: bool = True
    suggest_top: int = 5
    graph_context: bool = True
    opening_budget_tokens: int = 24_000
    context_diff_chars: int = 12_000        # 开场上下文里附带的 WIP diff 长度上限
    # ---- 压缩（token 为估算值：字符数 / chars_per_token，或模型报告的输入长度）
    chars_per_token: float = 4.0
    l0_chars: int = 30_000                  # 单个工具结果超过该长度就落盘
    l0_head_lines: int = 40
    l0_tail_lines: int = 80
    l0_signal_lines: int = 80
    l1_trigger_results: int = 60            # 上下文中完整的工具结果超过该数量
    l1_trigger_tokens: int = 80_000         # 或上下文超过该值时清理过期结果
    l1_keep_recent: int = 12
    l2_tokens: int = 150_000                # 上下文达到该值时用图替换旧对话
    l2_target_frac: float = 0.6             # L2 之后仍超过 l2_tokens × 该比例 → L3
    l2_keep_recent_tokens: int = 20_000
    l2_reread_files: int = 3
    l2_reread_chars: int = 12_000
    l3_mode: str = "overflow"               # overflow（L2 之后仍超限才调模型）| always | off
    l3_keep_recent_tokens: int = 6_000
    l4_max_compactions: int = 4             # 一个会话内压缩（L2/L3）次数达到该值 → 交接
    l4_tokens: int = 200_000                # 上下文超过该值 → 交接
    handoff_summary: bool = True            # 交接前让模型写一份只含图里没有的内容的摘要（L3 格式）
    # ---- 其他
    max_turns_per_session: int = 2000
    check_invariants: bool = True
    snapshot_every: int = 200

    @classmethod
    def from_dict(cls, d: dict | None) -> "BelayConfig":
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"未知的配置项：{unknown}")
        cfg = cls(**d)
        if cfg.checkpoint_tier not in ("related", "full"):
            raise ValueError("checkpoint_tier 只能是 related / full")
        if cfg.l3_mode not in ("overflow", "always", "off"):
            raise ValueError("l3_mode 只能是 overflow / always / off")
        return cfg

    def with_(self, **kw) -> "BelayConfig":
        return replace(self, **kw)

"""BelayConfig：runtime 的全部阈值与机制开关（纯数据，core 与 runtime 共用）。

每个机制都能关，用消融实验看哪些在承重：
  graph_context=False    开场与 L2 不用 build_context，改用模型写的完整摘要（“Belay − 图上下文”）
  confirm_regressions    回归先重跑一次确认，重跑通过的记为 flaky
  protect_tests          候选剔除测试路径下的改动
  stall=False            不做停滞检测
  locate=False           合并被拒时不做快照二分定位
  diagnoser=False        不做 LLM 诊断，worker 只拿到规则定位的结果
  reviewer=False         没有复核者：合并只看回归门；需求只由测试（E3）或 worker 的自述（E0）记下
  background=handoff     后台只在交接时发起合并请求；off 只在 submit 与收尾时合并
  todo_done_nudge=False  跑完测试后不提醒“做完了就勾掉”（只留第一次与长时间没更新的 todo 提醒）

v8 的节奏：快照照常拍（不打扰 worker）→ 后台空闲时，对最新的边界快照（勾掉 todo 的锚点、交接）发起合并请求，
很久没有边界快照时才兜底合并最新的可测快照：回归门（全量）→ 复核者 → 合并点。submit、交接与收尾不受间隔限制。
一个复核者会话的轮数与时间都有上限。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

DEFAULT_CAPS = {"pending": 2000, "progress": 3000, "todos": 1500, "summary": 3000, "workspace": 5000,
                "away": 2000, "gate": 1000}


@dataclass(frozen=True)
class BelayConfig:
    # ---- 合并
    confirm_regressions: bool = True
    protect_tests: bool = True
    # 后台合并请求：latest = 空闲时优先合并最新的边界快照（勾掉 todo / 交接），很久没有时兜底合并最新快照；
    # handoff = 只在交接时；off = 只有 submit 与收尾
    background: str = "latest"
    merge_min_interval_sec: float = 900     # 兜底（auto）：距上一次后台复核这么久还没有边界快照，才合并最新快照
    merge_todo_interval_sec: float = 60     # 勾掉 todo：距上一次后台复核至少这么久（只防连续勾掉琐碎条目）
    # ---- 快照（模块 B）
    snapshot_bash_every: int = 5
    precheck_python: bool = True
    precheck_cmd: str = ""
    # ---- 验证槽位（模块 A）
    verify_slots: int = 1
    isolation_max_diff: int = 0
    background_nice: int = 10
    background_cpu_limit: int = 0
    # ---- 定位与诊断（模块 D、E）
    locate: bool = True
    locate_max_steps: int = 8
    locate_max_sec: float = 3600
    locate_wait_sec: float = 120            # submit 被拒时最多等这么久，让定位结果随拒绝消息一起返回
    diagnoser: bool = True
    diagnose_input_tokens: int = 30_000
    # ---- 复核者（模块 F）
    reviewer: bool = True
    review_max_turns: int = 40              # 一次复核会话的轮数上限
    review_max_sec: float = 900             # 一次复核会话的时间上限
    review_run_timeout_sec: float = 600     # 复核者的单条命令的超时
    review_retries: int = 1                 # 复核失败（没有给出结论）后重试的次数；仍失败就按复核者不可用处理
    review_input_chars: int = 60_000        # 复核者开场里 diff 的长度上限（按需求预排序）
    review_locate_wait_sec: float = 120     # 复核者的 locate 工具最多等这么久
    score_tolerance: float = 0.02           # 分数比上一个合并点低超过这个比例就不合并（测量噪声）
    notify_misses: int = 2                  # 同一需求连续这么多次被判为没做完时提醒 worker
    # ---- 提交（请求立即复核）
    submit_wait_sec: float = 2400
    nudge_on_stop: bool = True
    max_implicit_submits: int = 3
    # ---- 回归门豁免：只能由复核者裁决（引文逐字出现在任务原文里，测试确实在这次的候选上失败）
    waivers: bool = True
    waive_max_tests: int = 20
    # ---- 截止预留 = max(下限, 全量回归门实测耗时 × 系数 + 余量 + 一次复核)，最多占预算的一定比例
    reserve_factor: float = 1.3
    reserve_extra_sec: float = 30
    reserve_review_sec: float = 300
    reserve_min_sec: float = 120
    reserve_max_frac: float = 0.25
    # ---- 运行结束与停滞
    max_idle_sessions: int = 2
    max_crash_restarts: int = 3
    stall: bool = True
    stall_no_progress_sec: float = 1800
    stall_same_failure: int = 3
    idle_timeout_sec: float = 1500
    # ---- 交接与恢复（模块 G）
    resume_max_downtime_sec: float = 1800
    resume_max_failures: int = 2
    mirror_every: int = 10
    mirror_consolidate: int = 32
    # ---- todo 与交接时机（模块 H）
    handoff_soft_tokens: int = 0
    todo_reminder_turns: int = 30
    todo_reminder_max: int = 3
    # 跑完测试、有进行中的 todo、上次更新 todo 之后改过文件时，提醒一句“做完了就勾掉”（勾掉是后台合并的时机）
    todo_done_nudge: bool = True
    todo_done_nudge_max: int = 2            # 同一条目最多提醒几次
    todo_done_nudge_gap_turns: int = 8      # 两次提醒之间至少隔这么多轮
    todo_done_nudge_quiet_turns: int = 3    # 最近这么多轮刚更新过 todo 时不提醒
    # ---- 上下文（模块 I）
    graph_context: bool = True
    opening_budget_tokens: int = 24_000
    opening_caps: dict = field(default_factory=lambda: dict(DEFAULT_CAPS))
    context_diff_chars: int = 12_000
    away_top: int = 12
    # ---- 压缩
    chars_per_token: float = 4.0
    l0_chars: int = 30_000
    l0_head_lines: int = 40
    l0_tail_lines: int = 80
    l0_signal_lines: int = 80
    l1_trigger_results: int = 0
    l1_trigger_tokens: int = 500_000
    l1_keep_recent: int = 12
    l2_tokens: int = 700_000
    l2_target_frac: float = 0.6
    l2_keep_recent_tokens: int = 20_000
    l2_reread_files: int = 3
    l2_reread_chars: int = 12_000
    l3_mode: str = "overflow"
    l3_keep_recent_tokens: int = 6_000
    l4_max_compactions: int = 4
    l4_tokens: int = 760_000
    handoff_summary: bool = True
    # ---- 其他
    max_turns_per_session: int = 2000
    check_invariants: bool = True
    snapshot_every: int = 200

    def __hash__(self) -> int:
        return id(self)

    @property
    def soft_handoff_tokens(self) -> int:
        return self.handoff_soft_tokens or self.l2_tokens

    def cap(self, key: str) -> int:
        return int(self.opening_caps.get(key, DEFAULT_CAPS.get(key, 2000)))

    @classmethod
    def from_dict(cls, d: dict | None) -> "BelayConfig":
        d = dict(d or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"未知的配置项：{unknown}")
        if "opening_caps" in d:
            d["opening_caps"] = {**DEFAULT_CAPS, **dict(d["opening_caps"] or {})}
        cfg = cls(**d)
        if cfg.background not in ("latest", "handoff", "off"):
            raise ValueError("background 只能是 latest / handoff / off")
        if cfg.l3_mode not in ("overflow", "always", "off"):
            raise ValueError("l3_mode 只能是 overflow / always / off")
        if cfg.snapshot_bash_every < 1:
            raise ValueError("snapshot_bash_every 至少为 1")
        if cfg.verify_slots < 1:
            raise ValueError("verify_slots 至少为 1")
        if cfg.review_retries < 0 or cfg.review_max_turns < 2:
            raise ValueError("review_retries 不能为负，review_max_turns 至少为 2")
        return cfg

    def with_(self, **kw) -> "BelayConfig":
        return replace(self, **kw)

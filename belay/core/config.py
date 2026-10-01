"""BelayConfig：runtime 的全部阈值与机制开关（纯数据，core 与 runtime 共用）。

每个机制都能关，用消融实验看哪些在承重：
  graph_context=False    开场与 L2 不用 build_context，改用模型写的完整摘要（“Belay − 图上下文”）
  confirm_regressions    回归先重跑一次确认，重跑通过的记为 flaky
  protect_tests          候选剔除测试路径下的改动
  stall=False            不做停滞检测
  locate=False           前台存档被拒时不做快照二分定位
  diagnoser=False        不做 LLM 诊断，worker 只拿到规则定位的结果
  reviewer=False         不做收紧式复查（提交的需求直接按自述接受）
  background=handoff     后台只验证交接快照（v6）；off 不做后台验证
压缩阈值按 Claude Code 的量级设定（只在真正接近上下文上限时才压缩），跑起来再调。

三层：存（每次实际改动都拍快照，不打扰 worker）→ 验（后台空闲时验证最新的可测快照，新快照胜出；submit 与收尾在前台
验证；没有基于时间的存档）→ 查（只有 worker 的提交被拒、或提交的存档被降级时，才在快照上二分定位并告诉 worker）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

# 开场上下文各段的上限（token，初始值）。受保护段（task / requirements / pending / todos / summary / workspace）
# 不会被去掉，只按上限截短；
# 受保护段的上限之和应当小于 opening_budget_tokens。
DEFAULT_CAPS = {"pending": 2000, "progress": 3000, "todos": 1500, "summary": 3000, "workspace": 5000,
                "away": 2000, "gate": 1000}


@dataclass(frozen=True)
class BelayConfig:
    # ---- 存档
    checkpoint_tier: str = "related"        # related | full：平时的验证档位；截止与收尾永远是 full
    confirm_regressions: bool = True
    protect_tests: bool = True
    deliver_unconfirmed: bool = False       # 除基线外没有确认点时：False 交付基线，True 交付最新暂存点
    # ---- 后台验证：latest = 空闲时验证最新的可测快照（v7）；handoff = 只验证交接 / 会话结束的快照（v6 的做法，
    #      用于消融）；off = 不做后台验证（只有 submit 与收尾在前台验证）
    background: str = "latest"
    # ---- 快照（模块 B）：edit_file / write_file 之后都拍；只有 bash 时每这么多次拍一次（树没变就不记）
    snapshot_bash_every: int = 5
    precheck_python: bool = True            # 快照前对改动的 .py 文件做语法检查（不写 .pyc）
    precheck_cmd: str = ""                  # 非 Python 项目的廉价预检命令（在工作区执行，退出码 0 = 可测）
    # ---- 验证槽位（模块 A）
    verify_slots: int = 1
    isolation_max_diff: int = 0             # 基线双跑时工作区与槽位“通过”集合允许的差
    background_nice: int = 10               # 第 3、4 档作业的 nice 值
    background_cpu_limit: int = 0           # >0 时限制第 3、4 档作业的并发（PYTEST_XDIST_AUTO_NUM_WORKERS 等）
    # ---- 定位与诊断（模块 D、E）
    locate: bool = True
    locate_max_steps: int = 8
    locate_max_sec: float = 3600
    locate_wait_sec: float = 120            # 手动存档被拒时最多等这么久，让定位结果随拒绝消息一起返回
    diagnoser: bool = True
    diagnose_input_tokens: int = 30_000
    # ---- 复查（模块 F）
    reviewer: bool = True
    review_max_reopens: int = 1             # 每条需求最多被复查者重开这么多次，之后只记进账本
    review_batch: int = 5                   # 一次复查调用看几条需求
    review_input_chars: int = 60_000        # 一次复查的输入上限（相关改动按需求筛过）
    # ---- 提交（唯一的完成声明）
    submit_wait_sec: float = 1800           # submit 最多等这么久（存档验证 + 复查），超时就先把当前状态返回
    nudge_on_stop: bool = True              # 模型停下不调用工具：先追问一次，再次停下就当作提交
    max_implicit_submits: int = 3           # 一个会话里最多这么多次隐式提交，之后会话结束
    # ---- 回归门豁免：worker 引用任务原文声明某个现有测试与要求冲突，且它确实在候选上失败过 → 从门里去掉
    waivers: bool = True
    waive_max_tests: int = 20
    # ---- 截止预留 = max(下限, 全量验证实测耗时 × 系数 + 余量)，最多占预算的一定比例
    reserve_factor: float = 1.3
    reserve_extra_sec: float = 30
    reserve_min_sec: float = 120
    reserve_max_frac: float = 0.25
    # ---- 运行结束与停滞
    max_idle_sessions: int = 2              # 连续这么多个会话没有进展 → 停止并报告
    max_crash_restarts: int = 3             # 连续崩溃这么多次 → 停止
    stall: bool = True
    stall_no_progress_sec: float = 1800
    stall_same_failure: int = 3
    idle_timeout_sec: float = 1500          # 这么久没有任何工具调用 → 视为卡死，结束会话
    # ---- 交接与恢复（模块 G）
    resume_max_downtime_sec: float = 1800   # runtime 崩溃后停机超过该时长就不再原样接上对话
    resume_max_failures: int = 2            # 同一会话连续恢复失败这么多次 → 开新会话
    mirror_every: int = 10                  # 每这么多张快照导出一次 git bundle
    mirror_consolidate: int = 32            # 增量 bundle 累积到这么多份时合并成一份完整的
    # ---- todo 与交接时机（模块 H）
    handoff_soft_tokens: int = 0            # 0 = 等于 l2_tokens；有进行中的 todo 时等下一个自然停顿点再交接
    todo_reminder_turns: int = 30           # 这么多轮没更新 todo 就提醒一次（第一次改文件时还没有 todo 也提醒一次）
    todo_reminder_max: int = 3              # 一个会话里最多提醒这么多次
    labeler: bool = True                    # 没有标签的里程碑存档生成一行标签（LLM）
    label_every: int = 5
    # ---- 上下文（模块 I）
    graph_context: bool = True
    opening_budget_tokens: int = 24_000
    opening_caps: dict = field(default_factory=lambda: dict(DEFAULT_CAPS))
    context_diff_chars: int = 12_000        # 开场上下文里 diff 的长度上限（渲染时截断，附件保存全文）
    away_top: int = 12
    # ---- 压缩（token 为估算值：字符数 / chars_per_token，或模型报告的输入长度）
    chars_per_token: float = 4.0
    l0_chars: int = 30_000
    l0_head_lines: int = 40
    l0_tail_lines: int = 80
    l0_signal_lines: int = 80
    l1_trigger_results: int = 0             # 0 = 不按结果数量触发
    l1_trigger_tokens: int = 500_000
    l1_keep_recent: int = 12
    l2_tokens: int = 700_000
    l2_target_frac: float = 0.6
    l2_keep_recent_tokens: int = 20_000
    l2_reread_files: int = 3
    l2_reread_chars: int = 12_000
    l3_mode: str = "overflow"               # overflow | always | off
    l3_keep_recent_tokens: int = 6_000
    l4_max_compactions: int = 4
    l4_tokens: int = 760_000                # 硬阈值：强制交接
    handoff_summary: bool = True
    # ---- 其他
    max_turns_per_session: int = 2000
    check_invariants: bool = True
    snapshot_every: int = 200

    def __hash__(self) -> int:              # opening_caps 是 dict：按身份哈希即可
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
        if cfg.checkpoint_tier not in ("related", "full"):
            raise ValueError("checkpoint_tier 只能是 related / full")
        if cfg.background not in ("latest", "handoff", "off"):
            raise ValueError("background 只能是 latest / handoff / off")
        if cfg.l3_mode not in ("overflow", "always", "off"):
            raise ValueError("l3_mode 只能是 overflow / always / off")
        if cfg.snapshot_bash_every < 1:
            raise ValueError("snapshot_bash_every 至少为 1")
        if cfg.verify_slots < 1:
            raise ValueError("verify_slots 至少为 1")
        return cfg

    def with_(self, **kw) -> "BelayConfig":
        return replace(self, **kw)

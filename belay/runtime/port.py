"""WorkerPort：Belay 工具与 runtime 之间的接口（belay.tools.base.RuntimeClient 的实现）。

每个请求 = 外壳先观察（需要时给工作区拍快照）→ Runtime.submit(规则) → 需要等结果的请求（submit）等图满足条件
→ 渲染给模型看的文字。规则拒绝的请求以 {"error": True} 返回。

通知（只推 worker 能据此行动的信息）：提交的存档被降级且问题仍在、定位结果、诊断结论、同一回归反复被拒。
后台验证被拒只是链头不动，记在图里（board 可见），不通知：中间态测不过是常态。复查者重开的需求随 submit 的结果返回。
通知不含剩余时间或已用时间：时间只由 runtime 用来决定何时收尾；也没有按时间提醒存档。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from belay.core import rules as R
from belay.core.model import JOB_FINISHED, SUB_ACCEPTED, SUB_FINAL
from belay.core.render import render_board, render_diagnosis, render_located, render_submit
from belay.core.rules import Rejected
from belay.runtime.verifier import extract_failure

if TYPE_CHECKING:
    from belay.runtime.driver import BelayRun


class WorkerPort:
    def __init__(self, run: "BelayRun", worker: str):
        self.run = run
        self.w = worker
        self._notices: list[str] = []
        self._returned_locates: set[str] = set()      # 已经随拒绝消息返回过的定位
        self._accepted = False                         # 最近一次 submit 是否被接受（会话随之结束）
        run.rt.listeners.append(self._on_events)

    def close(self) -> None:
        if self._on_events in self.run.rt.listeners:
            self.run.rt.listeners.remove(self._on_events)

    # ---------------------------------------------------------------- 通知
    def _on_events(self, events, g) -> None:
        for e in events:
            t = e.type
            if (t == "stall_detected" and e.get("worker") == self.w and e.get("action") != "stop"
                    and e.get("kind") == "repeated_failure"):
                self._notices.append(f"Your recent submits were all rejected for the same reason "
                                     f"({e.get('detail')}). If your current approach is not converging, it may "
                                     "help to look at those regressions from a different angle, or undo the change "
                                     "that introduced them.")
            elif t == "persistent_regression" and e.get("trigger") == "demoted":
                tests = ", ".join(e.get("tests")[:5])
                cp = g.checkpoints.get(e.get("checkpoint"))
                self._notices.append(f"Checkpoint {e.get('checkpoint')} failed the full test suite on {tests} "
                                     "(the related tests did not select them). Your current changes still make "
                                     f"{tests} fail. The harness is locating where it started; "
                                     + ("the checkpoint is no longer part of what would be delivered."
                                        if cp is not None and cp.demoted else ""))
            elif t == "regression_located":
                loc = g.locates.get(e.get("locate"))
                if loc is None or loc.id in self._returned_locates or loc.epoch != g.epoch:
                    continue
                self._notices.append(render_located(g, loc.id, e.get("group")))
            elif t == "diagnosis_recorded" and not e.get("failed"):
                txt = render_diagnosis(g, e.get("diagnosis"))
                if txt:
                    self._notices.append(txt)

    def drain_notices(self) -> list[str]:
        out, self._notices = [n for n in self._notices if n], []
        return out

    # ---------------------------------------------------------------- 请求
    async def _submit(self, rule, *args, **kw):
        return await self.run.rt.submit(rule, *args, **kw)

    async def _r_board(self, status=None, requirement=None, view=None, page: int = 1) -> str:
        rt = self.run.rt
        return render_board(rt.graph, self.w, rt.now(), self.run.cfg, status=status, requirement=requirement,
                            view=view, page=page)

    async def _r_submit(self, summary: str = "", blocked: list = (), implicit: bool = False) -> str:
        """提交：强制拍快照 → 前台存档 → 判定需求 → 复查 → 接受或交还清单。等结果（最多 submit_wait_sec）。"""
        text, _ok = await self.submit(summary, blocked, implicit)
        return text

    async def submit(self, summary: str = "", blocked: list = (), implicit: bool = False) -> tuple[str, bool]:
        n = await self.run.take_snapshot("submit")
        if n is None:
            raise Rejected("The harness is still setting up; try again shortly.")
        sid = await self._submit(R.request_submit, self.w, n, summary, list(blocked or ()), implicit)
        await self.run.rt.wait_until(lambda g: g.submits[sid].status in SUB_FINAL,
                                     timeout=self.run.cfg.submit_wait_sec)
        g = self.run.rt.graph
        s = g.submits[sid]
        text = render_submit(g, sid)
        if s.status == "rejected" and s.attempt:
            text += await self._with_located(s.attempt)
        self._accepted = s.status == SUB_ACCEPTED
        return text, self._accepted

    async def request(self, name: str, /, **p: Any) -> dict:
        handler = getattr(self, f"_r_{name}", None)
        if handler is None:
            return {"text": f"Unknown request {name}", "error": True}
        self._accepted = False
        try:
            text = await handler(**p)
            return {"text": text, "error": False, "accepted": self._accepted}
        except Rejected as e:
            return {"text": str(e), "error": True}

    async def _r_waive_check(self, tests: list, quote: str, reason: str, requirement=None) -> str:
        waived = await self._submit(R.waive_checks, self.w, tests, quote, reason, requirement)
        return (f"Waived {len(waived)} check(s) from the regression gate: {', '.join(waived[:10])}. They are listed "
                "in the final report. Call submit again to record your change.")

    async def _with_located(self, aid) -> str:
        """被拒时最多等 locate_wait_sec，让定位结果随拒绝消息一起返回。"""
        if aid is None:
            return ""
        g = self.run.rt.graph
        locs = [l for l in g.locates.values() if l.ref == aid]
        if not locs:
            return ""
        loc = locs[0]

        def located(g) -> bool:
            L = g.locates[loc.id]
            return L.status == "concluded" and len(L.results) >= len(L.groups)
        await self.run.rt.wait_until(located, timeout=self.run.cfg.locate_wait_sec)
        g = self.run.rt.graph
        if g.locates[loc.id].results:
            self._returned_locates.add(loc.id)
            return "\n" + render_located(g, loc.id)
        return "\nThe harness is locating where this started; the result will be reported to you."

    async def _r_failure_log(self, test: str) -> str:
        g = self.run.rt.graph
        if self.run.verifier is None:
            raise Rejected("No checks are configured for this task.")
        jobs = sorted((j for j in g.jobs.values() if j.state == JOB_FINISHED and test in j.results),
                      key=lambda j: j.started_seq, reverse=True)
        if not jobs:
            raise Rejected(f"No harness run has a result for {test} yet; submit runs the regression gate on your "
                           "changes.")
        j = jobs[0]
        log = await self.run.verifier.read_log(j.id)
        seg = extract_failure(log, test) if log else ""
        head = f"{test} was {j.results[test]} in job {j.id} ({j.purpose}{', your working tree' if j.live else ''})."
        if not seg:
            why = j.reasons.get(test)
            return head + (f"\nReason: {why}" if why else "\n(no traceback found in the log)")
        return head + "\n" + seg

    async def _r_revert_change(self, located: str) -> str:
        lid, _, grp = located.partition("#")
        g = self.run.rt.graph
        loc = g.locates.get(lid.strip())
        if loc is None:
            raise Rejected(f"Unknown located regression {located}.")
        recs = [r for r in loc.results if str(r.get("group", 0)) == (grp.strip() or "0")]
        if not recs:
            raise Rejected(f"{located} has no located change (yet).")
        rec = recs[0]
        paths = [f[0] for f in rec.get("files") or []]
        if not paths:
            raise Rejected("The located change touches no file outside test paths; nothing to revert.")
        ok, detail = await self.run.repo.revert_files(rec["good"]["tree"], rec["bad"]["tree"], paths)
        if not ok:
            raise Rejected(f"Nothing was changed: {detail}. Undo it by hand.")
        await self.run.take_snapshot("revert")
        return (f"Reverted the change between {rec['good'].get('id')} and {rec['bad'].get('id')} in "
                f"{', '.join(paths[:10])} ({detail}). Read these files again before editing them.")

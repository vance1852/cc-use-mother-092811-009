"""养护资金决策与执行服务的单元与端到端测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from transport_coordination.funding_service import FundingService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


def build_service() -> tuple[DomainService, FundingService]:
    database = Database(":memory:")
    clock = FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
    service = DomainService(database, clock)
    funding = FundingService(database, clock)
    service.register_organization(request_id="org-req", actor_id="bootstrap",
                                  organization_id="org1", name="县交通运输局")
    service.register_actor(request_id="admin-req", actor_id="bootstrap", new_actor_id="admin1",
                           display_name="管理员", role="admin", organization_id="org1")
    service.register_actor(request_id="hw-req", actor_id="admin1", new_actor_id="hw1",
                           display_name="公路主管", role="highway", organization_id="org1")
    service.register_actor(request_id="fin-req", actor_id="admin1", new_actor_id="fin1",
                           display_name="财政主管", role="finance", organization_id="org1")
    service.register_organization(request_id="org2-req", actor_id="admin1",
                                  organization_id="org2", name="县审计复核中心")
    service.register_actor(request_id="rv-req", actor_id="admin1", new_actor_id="rv1",
                           display_name="独立复核员", role="reviewer", organization_id="org2")
    return service, funding


def seed_segment_and_evidence(funding: FundingService, segment_id: str, route_code: str,
                              *, pci: float, population: int, sole: bool, routes: int,
                              hazard: float, aadt: int, req_prefix: str) -> None:
    funding.register_segment(
        request_id=f"{req_prefix}-seg", actor_id="hw1", segment_id=segment_id,
        route_code=route_code, name=f"路段-{segment_id}", length_km=10,
        chainage_start=0, chainage_end=10)
    payloads = {
        "condition": {"pci": pci},
        "population": {"served_population": population, "sole_access": sole},
        "alternatives": {"alternative_routes": routes},
        "hazard": {"hazard_level": hazard},
        "traffic": {"aadt": aadt},
        "maintenance_history": {"repeated_repair": False},
    }
    for dimension, payload in payloads.items():
        funding.add_evidence(
            request_id=f"{req_prefix}-{dimension}", actor_id="hw1", segment_id=segment_id,
            dimension=dimension, payload=payload, effective_at="2026-09-01T00:00:00Z")


def milestones(*amounts: float) -> list[dict]:
    return [{"seq": index, "name": f"里程碑{index}", "amount": amount,
             "due_date": f"2026-12-{index:02d}"} for index, amount in enumerate(amounts, start=1)]


def open_round(funding: FundingService, round_id: str = "R1", *, county=1000, provincial=1000,
               central=0, emergency=100, deadline="2026-09-15T17:00:00Z") -> None:
    funding.open_period(request_id="period-open", actor_id="fin1", period_id="P1", label="2026 年度")
    funding.create_round(request_id="round-open", actor_id="fin1", round_id=round_id,
                         name="集中养护批次", deadline_at=deadline, emergency_quota=emergency)
    funding.set_envelope(request_id="env-county", actor_id="fin1", round_id=round_id,
                         level="county", amount=county)
    funding.set_envelope(request_id="env-prov", actor_id="fin1", round_id=round_id,
                         level="provincial", amount=provincial)
    if central:
        funding.set_envelope(request_id="env-central", actor_id="fin1", round_id=round_id,
                             level="central", amount=central)


class ScoringAndFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.funding = build_service()
        open_round(self.funding)

    def test_sole_access_village_road_outranks_high_volume_artery(self) -> None:
        # 高流量干线：交通量大但路况尚可、有替代路线、灾害暴露低
        seed_segment_and_evidence(self.funding, "ARTERY", "G高流量", pci=62, population=80000,
                                  sole=False, routes=2, hazard=20, aadt=30000, req_prefix="a")
        # 乡村唯一通达路：交通量小、路况差、无替代、灾害高
        seed_segment_and_evidence(self.funding, "VILLAGE", "X村道", pci=32, population=1500,
                                  sole=True, routes=0, hazard=88, aadt=320, req_prefix="v")
        self.funding.submit_application(
            request_id="app-artery", actor_id="hw1", round_id="R1", application_id="APP-ART",
            segment_id="ARTERY", title="干线中修", amount_requested=100, chainage_start=0,
            chainage_end=5, milestones=milestones(100))
        self.funding.submit_application(
            request_id="app-village", actor_id="hw1", round_id="R1", application_id="APP-VIL",
            segment_id="VILLAGE", title="村道水毁修复", amount_requested=100, chainage_start=0,
            chainage_end=5, milestones=milestones(100))
        self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        portfolio = self.funding.get_portfolio("fin1", "R1")
        ranking = {item["application_id"]: item["rank"] for item in portfolio["items"]}
        self.assertLess(ranking["APP-VIL"], ranking["APP-ART"])

    def test_evidence_after_deadline_does_not_change_frozen_scores(self) -> None:
        seed_segment_and_evidence(self.funding, "SEG", "G1", pci=50, population=1000,
                                  sole=False, routes=1, hazard=50, aadt=1000, req_prefix="s")
        self.funding.submit_application(
            request_id="app1", actor_id="hw1", round_id="R1", application_id="APP1",
            segment_id="SEG", title="项目", amount_requested=50, chainage_start=0,
            chainage_end=5, milestones=milestones(50))
        self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        before = self.funding.get_trace("hw1", "APP1")["scoring"]["total_score"]
        # 截止后新增完美证据（极端值），评分必须不变
        self.funding.add_evidence(
            request_id="late-evidence", actor_id="hw1", segment_id="SEG", dimension="condition",
            payload={"pci": 0}, effective_at="2026-09-20T00:00:00Z")
        after = self.funding.get_trace("hw1", "APP1")["scoring"]["total_score"]
        self.assertEqual(before, after)

    def test_freeze_is_idempotent_and_cannot_re_freeze(self) -> None:
        seed_segment_and_evidence(self.funding, "SEG", "G1", pci=50, population=1000,
                                  sole=False, routes=1, hazard=50, aadt=1000, req_prefix="s")
        self.funding.submit_application(
            request_id="app1", actor_id="hw1", round_id="R1", application_id="APP1",
            segment_id="SEG", title="项目", amount_requested=50, chainage_start=0,
            chainage_end=5, milestones=milestones(50))
        first = self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        replay = self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ConflictError):
            self.funding.freeze_round(request_id="freeze-2", actor_id="fin1", round_id="R1")


class ConflictDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.funding = build_service()
        open_round(self.funding)
        seed_segment_and_evidence(self.funding, "SEG1", "G1", pci=50, population=1000,
                                  sole=False, routes=1, hazard=50, aadt=1000, req_prefix="s1")

    def _submit(self, application_id: str, *, start: float, end: float,
                window_start="2026-10-01", window_end="2026-10-15", amount=100,
                depends_on=None, request_id=None) -> None:
        self.funding.submit_application(
            request_id=request_id or f"req-{application_id}", actor_id="hw1", round_id="R1",
            application_id=application_id, segment_id="SEG1", title=application_id,
            amount_requested=amount, chainage_start=start, chainage_end=end,
            work_type="routine", window_start=window_start, window_end=window_end,
            depends_on=depends_on, milestones=milestones(amount))

    def test_overlapping_chainage_rejected_at_submission_as_split(self) -> None:
        self._submit("A1", start=0, end=5, window_start="2026-10-01", window_end="2026-10-15")
        with self.assertRaises(ConflictError):
            self._submit("A2", start=3, end=8, window_start="2026-11-01", window_end="2026-11-15")

    def test_mutually_exclusive_windows_block_decision(self) -> None:
        # 桩号不重叠，但在同一路段上施工窗口重叠
        self._submit("A1", start=0, end=3, window_start="2026-10-01", window_end="2026-10-15")
        self._submit("A2", start=6, end=9, window_start="2026-10-10", window_end="2026-10-25")
        conflicts = self.funding.detect_conflicts("hw1", "R1")
        self.assertTrue(conflicts["has_conflict"])
        self.assertEqual(len(conflicts["window_conflicts"]), 1)
        self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        with self.assertRaises(ConflictError):
            self.funding.decide_portfolio(request_id="decide", actor_id="fin1", round_id="R1")

    def test_dependency_order_violation_flagged(self) -> None:
        # A2 依赖 A1，但 A2 计划开工早于 A1 完工
        self._submit("A1", start=0, end=3, window_start="2026-11-01", window_end="2026-11-20")
        self._submit("A2", start=6, end=9, window_start="2026-10-01", window_end="2026-10-20",
                     depends_on=["A1"], request_id="req-A2")
        conflicts = self.funding.detect_conflicts("hw1", "R1")
        self.assertEqual(len(conflicts["dependency_violations"]), 1)

    def test_dependency_cycle_rejected(self) -> None:
        self._submit("A1", start=0, end=3)
        self._submit("A2", start=6, end=9, depends_on=["A1"], request_id="req-A2")
        with self.assertRaises(ValidationError):
            self._submit("A3", start=3.5, end=5.5, depends_on=["A3"], request_id="req-A3")


class PortfolioConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.funding = build_service()
        open_round(self.funding, county=60, provincial=40, central=0)
        seed_segment_and_evidence(self.funding, "SEG1", "G1", pci=50, population=1000,
                                  sole=False, routes=1, hazard=50, aadt=1000, req_prefix="s1")
        seed_segment_and_evidence(self.funding, "SEG2", "G2", pci=40, population=2000,
                                  sole=True, routes=0, hazard=70, aadt=500, req_prefix="s2")

    def test_waterfall_funding_and_waitlist(self) -> None:
        # 三个项目共需 210，盘子只有 100：县先兜底 60，省补 40，高分项目先得
        for application_id, segment in (("A1", "SEG1"), ("A2", "SEG2"), ("A3", "SEG1")):
            chainage = {"A1": (0, 2), "A2": (0, 2), "A3": (3, 5)}[application_id]
            self.funding.submit_application(
                request_id=f"req-{application_id}", actor_id="hw1", round_id="R1",
                application_id=application_id, segment_id=segment, title=application_id,
                amount_requested=70, chainage_start=chainage[0], chainage_end=chainage[1],
                work_type="routine",
                window_start=f"2026-1{0 if application_id != 'A3' else 1}-01",
                window_end=f"2026-1{0 if application_id != 'A3' else 1}-20",
                milestones=milestones(70))
        self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        decision = self.funding.decide_portfolio(request_id="decide", actor_id="fin1", round_id="R1")
        approved = {d["application_id"] for d in decision["decisions"] if d["decision"] == "approved"}
        waitlisted = {d["application_id"] for d in decision["decisions"]
                      if d["decision"] == "waitlisted"}
        # 村道 A2 分数最高，应被批准且资金按县→省瀑布式分配
        self.assertIn("A2", approved)
        allocation = next(d["allocation"] for d in decision["decisions"]
                          if d["application_id"] == "A2")
        self.assertEqual(allocation.get("county"), 60.0)
        self.assertEqual(allocation.get("provincial"), 10.0)
        self.assertTrue(waitlisted)
        balances = self.funding.get_round("fin1", "R1")["balances"]
        self.assertEqual(balances["county"], 0.0)
        self.assertEqual(balances["provincial"], 30.0)


class WaitlistPromotionTests(unittest.TestCase):
    def test_cancelled_approval_releases_funds_for_next_ranked(self) -> None:
        service, funding = build_service()
        open_round(funding, county=100, provincial=0)
        seed_segment_and_evidence(funding, "S1", "G1", pci=30, population=2000,
                                  sole=True, routes=0, hazard=90, aadt=100, req_prefix="a")
        seed_segment_and_evidence(funding, "S2", "G2", pci=70, population=90000,
                                  sole=False, routes=3, hazard=10, aadt=40000, req_prefix="b")
        funding.submit_application(
            request_id="req-a1", actor_id="hw1", round_id="R1", application_id="APPA",
            segment_id="S1", title="村道", amount_requested=100, chainage_start=0,
            chainage_end=5, window_start="2026-10-01", window_end="2026-10-20",
            milestones=milestones(100))
        funding.submit_application(
            request_id="req-a2", actor_id="hw1", round_id="R1", application_id="APPB",
            segment_id="S2", title="干线", amount_requested=100, chainage_start=0,
            chainage_end=5, window_start="2026-11-01", window_end="2026-11-20",
            milestones=milestones(100))
        funding.freeze_round(request_id="req-f", actor_id="fin1", round_id="R1")
        first = funding.decide_portfolio(request_id="req-d1", actor_id="fin1", round_id="R1")
        statuses = {d["application_id"]: d["decision"] for d in first["decisions"]}
        self.assertEqual(statuses, {"APPA": "approved", "APPB": "waitlisted"})
        # 高分项目未支付即取消，额度释放；再次决策使候补项目递补
        funding.cancel_project(request_id="req-cancel", actor_id="fin1",
                               application_id="APPA", reason="另有资金渠道")
        second = funding.decide_portfolio(request_id="req-d2", actor_id="fin1", round_id="R1")
        promoted = next(d for d in second["decisions"] if d["application_id"] == "APPB")
        self.assertEqual(promoted["decision"], "approved")
        self.assertTrue(promoted["promoted"])
        trace = funding.get_trace("fin1", "APPB")
        self.assertEqual(trace["status"], "approved")
        self.assertEqual(trace["money_summary"]["committed"], 100.0)


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.funding = build_service()
        open_round(self.funding, county=200, provincial=0)
        seed_segment_and_evidence(self.funding, "SEG1", "G1", pci=45, population=5000,
                                  sole=False, routes=1, hazard=60, aadt=8000, req_prefix="s1")
        self.funding.submit_application(
            request_id="app1", actor_id="hw1", round_id="R1", application_id="APP1",
            segment_id="SEG1", title="养护项目", amount_requested=100, chainage_start=0,
            chainage_end=5, milestones=milestones(40, 60))
        self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        self.funding.decide_portfolio(request_id="decide", actor_id="fin1", round_id="R1")

    def test_milestone_reservation_and_payment_settles_project(self) -> None:
        trace = self.funding.get_trace("fin1", "APP1")
        self.assertEqual(trace["milestones"][0]["status"], "reserved")
        self.funding.pay_milestone(request_id="pay1", actor_id="fin1",
                                   application_id="APP1", seq=1, amount=40)
        with self.assertRaises(ConflictError):
            # 支付不可冲销重复提交
            self.funding.pay_milestone(request_id="pay1-different", actor_id="fin1",
                                       application_id="APP1", seq=1, amount=40)
        with self.assertRaises(ValidationError):
            self.funding.pay_milestone(request_id="pay-over", actor_id="fin1",
                                       application_id="APP1", seq=2, amount=61)
        self.funding.pay_milestone(request_id="pay2", actor_id="fin1",
                                   application_id="APP1", seq=2, amount=60)
        trace = self.funding.get_trace("fin1", "APP1")
        self.assertEqual(trace["status"], "settled")
        self.assertEqual(trace["money_summary"]["paid"], 100.0)

    def test_closed_period_cannot_be_rewritten(self) -> None:
        self.funding.pay_milestone(request_id="pay1", actor_id="fin1",
                                   application_id="APP1", seq=1, amount=40)
        self.funding.close_period(request_id="close", actor_id="fin1", period_id="P1")
        # 关账后变更、取消、支付都必须被拒绝
        with self.assertRaises(ConflictError):
            self.funding.pay_milestone(request_id="pay2", actor_id="fin1",
                                       application_id="APP1", seq=2, amount=60)
        with self.assertRaises(ConflictError):
            self.funding.cancel_project(request_id="cancel", actor_id="fin1",
                                        application_id="APP1", reason="测试")
        with self.assertRaises(ConflictError):
            self.funding.change_project(request_id="change", actor_id="fin1",
                                        application_id="APP1", new_amount=90, reason="测试")
        # 新开期间后可以继续执行，历史期间台账原样保留
        self.funding.open_period(request_id="open2", actor_id="fin1", period_id="P2", label="次期")
        self.funding.pay_milestone(request_id="pay2", actor_id="fin1",
                                   application_id="APP1", seq=2, amount=60)
        ledger_periods = {entry["period_id"] for entry in
                          self.funding.get_trace("fin1", "APP1")["ledger"]}
        self.assertEqual(ledger_periods, {"P1", "P2"})

    def test_change_reduce_and_cancel_release_committed_funds(self) -> None:
        # 缩减 100 -> 70（尚有 40 已付），释放 30 承诺
        self.funding.pay_milestone(request_id="pay1", actor_id="fin1",
                                   application_id="APP1", seq=1, amount=40)
        self.funding.change_project(
            request_id="change", actor_id="fin1", application_id="APP1", new_amount=70,
            reason="设计优化缩减",
            milestones=[{"seq": 1, "name": "里程碑1", "amount": 40, "due_date": "2026-12-01"},
                        {"seq": 2, "name": "里程碑2", "amount": 30, "due_date": "2026-12-02"}])
        self.assertEqual(self.funding.get_round("fin1", "R1")["balances"]["county"], 130.0)
        # 不能缩到已付金额以下
        with self.assertRaises(ValidationError):
            self.funding.change_project(
                request_id="change2", actor_id="fin1", application_id="APP1", new_amount=30,
                reason="过度缩减")
        self.funding.cancel_project(request_id="cancel", actor_id="fin1",
                                    application_id="APP1", reason="终止")
        # 取消只释放剩余承诺 30，已付 40 不动
        self.assertEqual(self.funding.get_round("fin1", "R1")["balances"]["county"], 160.0)

    def test_recovery_after_settlement(self) -> None:
        self.funding.pay_milestone(request_id="pay1", actor_id="fin1",
                                   application_id="APP1", seq=1, amount=40)
        self.funding.pay_milestone(request_id="pay2", actor_id="fin1",
                                   application_id="APP1", seq=2, amount=60)
        self.assertEqual(self.funding.get_trace("fin1", "APP1")["status"], "settled")
        self.funding.recover_funds(request_id="recover", actor_id="fin1",
                                   application_id="APP1", amount=15, reason="竣工审计结余")
        summary = self.funding.get_trace("fin1", "APP1")["money_summary"]
        self.assertEqual(summary["recovered"], 15.0)
        with self.assertRaises(ValidationError):
            self.funding.recover_funds(request_id="recover2", actor_id="fin1",
                                       application_id="APP1", amount=1000, reason="超额追回")


class EmergencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.funding = build_service()
        open_round(self.funding, county=0, provincial=0, emergency=50)
        seed_segment_and_evidence(self.funding, "EMSEG", "Y应急", pci=20, population=800,
                                  sole=True, routes=0, hazard=95, aadt=200, req_prefix="e")

    def _emergency(self, amount: float, tag: str) -> str:
        application_id = f"EM-{tag}"
        offset = {"a": 0.0, "b": 2.5, "c": 5.0}[tag]
        self.funding.submit_application(
            request_id=f"req-{application_id}", actor_id="hw1", round_id="R1",
            application_id=application_id, segment_id="EMSEG", title="紧急抢修",
            amount_requested=amount, chainage_start=offset, chainage_end=offset + 2,
            work_type="emergency", milestones=milestones(amount))
        return application_id

    def test_emergency_quota_requires_independent_external_review(self) -> None:
        application_id = self._emergency(40, "a")
        # 申报人本人不能复核
        with self.assertRaises(PermissionDenied):
            self.funding.submit_emergency_review(
                request_id="rv-self", actor_id="hw1", application_id=application_id,
                verdict="approved", notes="自行确认")
        # 同单位（org1）的财政角色也不能独立复核
        with self.assertRaises(PermissionDenied):
            self.funding.submit_emergency_review(
                request_id="rv-sameorg", actor_id="fin1", application_id=application_id,
                verdict="approved", notes="同单位")
        result = self.funding.submit_emergency_review(
            request_id="rv-ok", actor_id="rv1", application_id=application_id,
            verdict="approved", notes="现场核实滑坡断通")
        self.assertEqual(result["status"], "emergency_approved")
        trace = self.funding.get_trace("rv1", application_id)
        self.assertIsNotNone(trace["independent_review"])
        self.assertEqual(trace["money_summary"]["committed"], 40.0)
        # 复核只能完成一次（幂等重放除外）
        replay = self.funding.submit_emergency_review(
            request_id="rv-ok", actor_id="rv1", application_id=application_id,
            verdict="approved", notes="现场核实滑坡断通")
        self.assertTrue(replay["replayed"])

    def test_emergency_over_quota_rejected_at_review(self) -> None:
        application_id = self._emergency(40, "a")
        self.funding.submit_emergency_review(
            request_id="rv1", actor_id="rv1", application_id=application_id,
            verdict="approved", notes="批准")
        second = self._emergency(40, "b")
        # 限额只剩 10：第二笔现场复核虽通过，但因限额不足被拒绝且不产生台账
        result = self.funding.submit_emergency_review(
            request_id="rv2", actor_id="rv1", application_id=second,
            verdict="approved", notes="现场属实")
        self.assertEqual(result["status"], "rejected")
        ledger = self.funding.get_trace("rv1", second)["ledger"]
        self.assertEqual(ledger, [])
        self.assertEqual(self.funding.get_round("rv1", "R1")["balances"]["emergency"], 10.0)

    def test_emergency_can_be_submitted_after_freeze(self) -> None:
        self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        application_id = self._emergency(30, "c")
        result = self.funding.submit_emergency_review(
            request_id="rv", actor_id="rv1", application_id=application_id,
            verdict="approved", notes="汛期应急")
        self.assertEqual(result["status"], "emergency_approved")


class PolicySimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.funding = build_service()
        open_round(self.funding, county=50, provincial=0)
        seed_segment_and_evidence(self.funding, "SEG1", "G1", pci=60, population=80000,
                                  sole=False, routes=2, hazard=10, aadt=30000, req_prefix="s1")
        seed_segment_and_evidence(self.funding, "SEG2", "G2", pci=30, population=1000,
                                  sole=True, routes=0, hazard=90, aadt=100, req_prefix="s2")
        self.funding.submit_application(
            request_id="app1", actor_id="hw1", round_id="R1", application_id="APP1",
            segment_id="SEG1", title="干线", amount_requested=50, chainage_start=0,
            chainage_end=5, window_start="2026-10-01", window_end="2026-10-20",
            milestones=milestones(50))
        self.funding.submit_application(
            request_id="app2", actor_id="hw1", round_id="R1", application_id="APP2",
            segment_id="SEG2", title="村道", amount_requested=50, chainage_start=0,
            chainage_end=5, window_start="2026-11-01", window_end="2026-11-20",
            milestones=milestones(50))
        self.funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        self.funding.decide_portfolio(request_id="decide", actor_id="fin1", round_id="R1")

    def test_new_policy_re_ranks_unapproved_without_touching_history(self) -> None:
        # 新版政策极端偏重交通量
        traffic_heavy = {
            "version": "v-traffic",
            "weights": {"condition": 0.1, "population": 0.1, "alternatives": 0.1,
                        "hazard": 0.1, "maintenance_history": 0.1, "traffic": 0.5},
            "condition": {"pci_poor": 40.0, "pci_good": 90.0},
            "population": {"high": 20000, "sole_access_bonus": 15.0},
            "alternatives": {"none_score": 100.0, "per_route_drop": 35.0},
            "hazard": {"low": 10.0, "high": 90.0},
            "maintenance_history": {"repeated_repair_score": 80.0,
                                    "repaired_recent_score": 25.0, "aging_score": 60.0},
            "traffic": {"low": 500, "high": 20000},
            "blocking_bonus": 3.0,
        }
        self.funding.register_policy(request_id="policy2", actor_id="fin1",
                                     rules=traffic_heavy, activate=False)
        simulation = self.funding.simulate_policy(
            actor_id="fin1", round_id="R1", policy_version="v-traffic")
        # 已批准项目不参与模拟重排，历史决定不动
        simulated_ids = {item["application_id"] for item in simulation["items"]}
        self.assertNotIn("APP2", simulated_ids)
        # 历史状态、评分、政策版本保持原样
        trace = self.funding.get_trace("fin1", "APP2")
        self.assertEqual(trace["status"], "approved")
        self.assertEqual(trace["scoring"]["policy_version"], "v2026.1")

    def test_policy_immutable_new_version_required(self) -> None:
        with self.assertRaises(ConflictError):
            self.funding.register_policy(
                request_id="dup", actor_id="fin1", rules={
                    "version": "v2026.1",
                    "weights": {"condition": 0.25, "population": 0.15, "alternatives": 0.2,
                                "hazard": 0.25, "maintenance_history": 0.1, "traffic": 0.05},
                    "condition": {"pci_poor": 40.0, "pci_good": 90.0},
                    "population": {"high": 20000, "sole_access_bonus": 15.0},
                    "alternatives": {"none_score": 100.0, "per_route_drop": 35.0},
                    "hazard": {"low": 10.0, "high": 90.0},
                    "maintenance_history": {"repeated_repair_score": 80.0,
                                            "repaired_recent_score": 25.0, "aging_score": 60.0},
                    "traffic": {"low": 500, "high": 20000},
                    "blocking_bonus": 3.0}, activate=False)


class TraceAndAuditTests(unittest.TestCase):
    def test_full_lifecycle_trace_and_audit_chain(self) -> None:
        service, funding = build_service()
        open_round(funding, county=100, provincial=0)
        seed_segment_and_evidence(funding, "SEG1", "G1", pci=45, population=3000,
                                  sole=True, routes=0, hazard=70, aadt=900, req_prefix="s1")
        funding.submit_application(
            request_id="app1", actor_id="hw1", round_id="R1", application_id="APP1",
            segment_id="SEG1", title="村道修复", amount_requested=80, chainage_start=0,
            chainage_end=5, milestones=milestones(80))
        funding.freeze_round(request_id="freeze", actor_id="fin1", round_id="R1")
        funding.decide_portfolio(request_id="decide", actor_id="fin1", round_id="R1")
        funding.pay_milestone(request_id="pay1", actor_id="fin1",
                              application_id="APP1", seq=1, amount=80)
        trace = funding.get_trace("fin1", "APP1")
        # 全链路要素齐备
        self.assertTrue(trace["frozen_evidence"])
        self.assertIsNotNone(trace["scoring"])
        self.assertEqual(trace["scoring"]["rank"], 1)
        self.assertEqual(len(trace["ledger"]), 2)  # commit + payment
        self.assertEqual(trace["policy_version_at_freeze"], "v2026.1")
        self.assertIn("rationale", trace["scoring"])
        valid, count = service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()

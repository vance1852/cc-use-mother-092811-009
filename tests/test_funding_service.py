import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from transport_coordination.funding_service import FundingService
from transport_coordination.storage import Database


class SettableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


V1 = {"weights": {"condition": 2, "service": 1, "alternative": 2,
                  "disaster": 3, "maintenance": 1, "dependency": 1}}
V2_SERVICE = {"weights": {"condition": 1, "service": 4, "alternative": 1,
                          "disaster": 1, "maintenance": 1, "dependency": 1}}


def ev(**overrides):
    data = {"condition_index": 60, "service_population": 2000, "sole_access": False,
            "alternative_routes": 1, "disaster_exposure": 40,
            "maintenance_history_ratio": 0.8, "blocked_dependents": 0}
    data.update(overrides)
    return data


MS_ONE = [{"code": "m1", "title": "完工", "weight": 100, "retention_pct": 10}]
MS_TWO = [{"code": "m1", "title": "路基", "weight": 60, "retention_pct": 10},
          {"code": "m2", "title": "路面", "weight": 40, "retention_pct": 10}]


class FundingFixture(unittest.TestCase):
    clock: SettableClock
    service: FundingService
    database: Database

    def setUp(self):
        self.database = Database()
        self.clock = SettableClock(datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.service = FundingService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="某县")
        s.register_actor(request_id="a-ad", actor_id="bootstrap", new_actor_id="ad",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="a-fin", actor_id="ad", new_actor_id="fin",
                         display_name="财政", role="finance", organization_id="o1")
        s.register_actor(request_id="a-hw", actor_id="ad", new_actor_id="hw",
                         display_name="公路", role="highway", organization_id="o1")
        s.register_actor(request_id="a-rv", actor_id="ad", new_actor_id="rv",
                         display_name="复核", role="reviewer", organization_id="o1")
        s.create_scoring_policy(request_id="p-v1", actor_id="fin", version_tag="v1", spec=V1)
        s.create_scoring_policy(request_id="p-v2", actor_id="fin", version_tag="v2",
                                spec=V2_SERVICE)
        s.create_round(request_id="rd", actor_id="fin", round_id="rd1", name="2027年度",
                       deadline_at="2026-09-25T00:00:00Z")
        s.open_period(request_id="per1", actor_id="fin", period_id="per1", label="2026-Q4")

    def tearDown(self):
        self.database.close()

    def allocation(self, level="county", amount=1_000_000, reserve=100_000, req="al"):
        self.service.set_allocation(request_id=req, actor_id="fin", round_id="rd1",
                                    funding_level=level, amount=amount,
                                    emergency_reserve=reserve)

    def submit(self, pid, *, route="X101", start=0.0, end=10.0, amount=300_000,
               evidence=None, req=None, milestones=None, **kwargs):
        self.service.submit_project(
            request_id=req or f"sub-{pid}", actor_id="hw", project_id=pid, round_id="rd1",
            external_key=kwargs.pop("external_key", f"k-{pid}"), title=f"项目{pid}",
            funding_level=kwargs.pop("funding_level", "county"), requested_amount=amount,
            evidence=evidence or ev(), milestones=milestones or MS_ONE,
            route_code=route, start_km=start, end_km=end, **kwargs)

    def freeze(self, policy="v1", req="fz"):
        self.clock.advance(days=10)
        return self.service.freeze_round(request_id=req, actor_id="fin", round_id="rd1",
                                         policy_version_tag=policy)

    def approve(self, req="ap"):
        return self.service.approve_portfolio(request_id=req, actor_id="fin", round_id="rd1")

    def decisions(self):
        round_view = self.service.get_round("rd1")
        return {p["project_id"]: (p["score"]["decision"], p["score"]["rank"])
                for p in round_view["projects"]}


class FreezeAndPolicyTest(FundingFixture):
    def test_segment_based_submission_replays_with_defaulted_evidence(self):
        # 证据未显式给 sole_access 时由路段台账补默认值；补默认值发生在幂等
        # 哈希之前，重复请求必须回放成功而不是被误判为内容冲突。
        self.allocation()
        self.service.register_segment(
            request_id="seg", actor_id="hw", segment_id="seg1", route_code="X303",
            start_km=0, end_km=8, name="村道", sole_access=True)
        def call_submit():
            fresh_evidence = {k: v for k, v in ev().items() if k != "sole_access"}
            return self.service.submit_project(
                request_id="sub-seg", actor_id="hw", project_id="pseg", round_id="rd1",
                external_key="k-pseg", title="村道养护", funding_level="county",
                requested_amount=200_000, segment_id="seg1",
                evidence=fresh_evidence, milestones=MS_ONE)

        first = call_submit()
        replay = call_submit()
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        stored = self.service.trace_funds("pseg")["project"]
        self.assertEqual(stored["route_code"], "X303")

    def test_cannot_freeze_before_deadline(self):
        self.allocation()
        self.submit("p1")
        with self.assertRaises(ConflictError):
            self.service.freeze_round(request_id="fz", actor_id="fin", round_id="rd1",
                                      policy_version_tag="v1")

    def test_freeze_binds_policy_and_blocks_resubmission(self):
        self.allocation()
        self.submit("p1")
        self.freeze()
        with self.assertRaises(ConflictError):
            self.submit("p2", req="sub-p2-late")
        round_view = self.service.get_round("rd1")
        self.assertEqual(round_view["round"]["status"], "frozen")
        self.assertTrue(round_view["round"]["evidence_hash"])
        self.assertTrue(all(p["score"] for p in round_view["projects"]))

    def test_highway_cannot_create_round_or_policy(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_round(request_id="xx", actor_id="hw", round_id="rdX",
                                      name="x", deadline_at="2026-09-25T00:00:00Z")

    def test_freeze_is_idempotent_after_state_changed(self):
        self.allocation()
        self.submit("p1")
        self.freeze()
        replay = self.freeze()  # 重复冻结请求回放原回执，而不是报状态冲突
        self.assertTrue(replay.replayed)


class PortfolioSelectionTest(FundingFixture):
    def test_rural_disaster_sole_access_ranks_above_low_risk_arterial(self):
        self.allocation()
        self.submit("rural", route="X301", evidence=ev(
            condition_index=82, service_population=600, sole_access=True,
            alternative_routes=0, disaster_exposure=95, maintenance_history_ratio=0.1))
        self.submit("arterial", route="G202", start=0, end=20, amount=400_000,
                    evidence=ev(condition_index=55, service_population=150_000,
                                alternative_routes=2, disaster_exposure=15))
        self.freeze()
        decisions = self.decisions()
        self.assertEqual(decisions["rural"][1], 1)
        self.assertEqual(decisions["rural"][0], "funded")

    def test_budget_cap_defers_lowest_rank(self):
        self.allocation(amount=500_000, reserve=0)
        self.submit("pa", amount=200_000, route="X1")
        self.submit("pb", amount=200_000, route="X2",
                    evidence=ev(disaster_exposure=90, sole_access=True, alternative_routes=0))
        self.submit("pc", amount=200_000, route="X3",
                    evidence=ev(disaster_exposure=92, sole_access=True, alternative_routes=0))
        self.freeze()
        decisions = self.decisions()
        funded = [pid for pid, (d, _) in decisions.items() if d == "funded"]
        self.assertEqual(len(funded), 2)
        self.assertNotIn("pa", funded)

    def test_split_flag_blocks_lower_ranked_piece(self):
        self.allocation()
        self.submit("p1", route="X401", start=0, end=10,
                    evidence=ev(disaster_exposure=90, sole_access=True))
        # 同一路线相邻小段，疑似拆项。
        self.submit("p2", route="X401", start=10.3, end=14,
                    evidence=ev(disaster_exposure=90, sole_access=True))
        self.freeze()
        decisions = self.decisions()
        self.assertEqual(len({d for d, _ in decisions.values()}), 2)
        self.assertEqual(decisions["p2"][0], "deferred")

    def test_window_conflict_is_flagged(self):
        self.allocation()
        self.submit("w1", route="X501", start=0, end=10,
                    window_start="2027-05-01", window_end="2027-06-01")
        self.submit("w2", route="X501", start=5, end=15,
                    window_start="2027-05-20", window_end="2027-07-01")
        self.freeze()
        flags = {f["flag_type"] for f in self.service.get_round("rd1")["flags"]}
        self.assertIn("window", flags)

    def test_dependency_chain_funds_together_or_not_at_all(self):
        self.allocation(amount=300_000, reserve=0)
        # 前置项目必须先存在；parent 占用 280k，child 需要 50k。
        self.submit("parent", route="X602", amount=280_000,
                    evidence=ev(disaster_exposure=20))
        self.submit("child", route="X601", amount=50_000, depends_on="parent",
                    evidence=ev(disaster_exposure=30))
        self.freeze()
        decisions = self.decisions()
        # parent 入选后仅剩 20k，child 既受依赖顺序约束又超余额，连带落选。
        self.assertEqual(decisions["parent"][0], "funded")
        self.assertEqual(decisions["child"][0], "deferred")

    def test_dependency_on_missing_project_rejected(self):
        self.allocation()
        with self.assertRaises(NotFoundError):
            self.submit("pb", route="X702", amount=100_000, depends_on="ghost")


class ExecutionAndLedgerTest(FundingFixture):
    def _funded_project(self, pid="p1", amount=400_000, milestones=None):
        self.allocation()
        self.submit(pid, amount=amount, milestones=milestones or MS_TWO)
        self.freeze()
        self.approve()
        return pid

    def test_milestone_retention_held_then_released_at_settlement(self):
        pid = self._funded_project(amount=300_000)
        self.service.start_project(request_id="st", actor_id="hw", project_id=pid)
        self.service.verify_milestone(request_id="v1", actor_id="rv", project_id=pid,
                                      milestone_code="m1")
        pay = self.service.pay_milestone(request_id="pay1", actor_id="fin", project_id=pid,
                                         milestone_code="m1", period_id="per1")
        self.assertFalse(pay.replayed)
        first_payment = self.service.trace_funds(pid)["payments"][0]
        # 300000 * 60% = 180000，保留 10% = 18000，实付 162000。
        self.assertEqual(first_payment["gross"], 180_000)
        self.assertEqual(first_payment["retained"], 18_000)
        receipt = self.service.pay_milestone(request_id="pay1", actor_id="fin", project_id=pid,
                                             milestone_code="m1", period_id="per1")
        self.assertTrue(receipt.replayed)

        with self.assertRaises(ConflictError):
            self.service.pay_milestone(request_id="pay2", actor_id="fin", project_id=pid,
                                       milestone_code="m2", period_id="per1")
        self.service.verify_milestone(request_id="v2", actor_id="rv", project_id=pid,
                                      milestone_code="m2")
        self.service.pay_milestone(request_id="pay2b", actor_id="fin", project_id=pid,
                                   milestone_code="m2", period_id="per1")
        self.service.release_retention(request_id="rel", actor_id="fin", project_id=pid,
                                       period_id="per1")
        trace = self.service.trace_funds(pid)
        self.assertEqual(trace["totals"]["disbursed"], 300_000)
        self.assertEqual(trace["totals"]["retention_released"], 30_000)
        self.assertEqual(trace["totals"]["open_commitment"], 0)
        self.assertEqual(trace["project"]["status"], "settled")

    def test_payment_requires_milestone_verification(self):
        pid = self._funded_project()
        self.service.start_project(request_id="st", actor_id="hw", project_id=pid)
        with self.assertRaises(ConflictError):
            self.service.pay_milestone(request_id="xx", actor_id="fin", project_id=pid,
                                       milestone_code="m1", period_id="per1")

    def test_highway_cannot_pay(self):
        pid = self._funded_project()
        with self.assertRaises(PermissionDenied):
            self.service.pay_milestone(request_id="xx", actor_id="hw", project_id=pid,
                                       milestone_code="m1", period_id="per1")

    def test_closed_period_rejects_new_entries_and_is_not_rewritten(self):
        pid = self._funded_project()
        self.service.start_project(request_id="st", actor_id="hw", project_id=pid)
        self.service.verify_milestone(request_id="v1", actor_id="rv", project_id=pid,
                                      milestone_code="m1")
        self.service.pay_milestone(request_id="pay1", actor_id="fin", project_id=pid,
                                   milestone_code="m1", period_id="per1")
        before = self.service.trace_funds(pid)["ledger"]
        self.service.close_period(request_id="cl", actor_id="fin", period_id="per1")
        # 关账后任何在旧期间上的变更/取消/支付都必须被拒绝。
        with self.assertRaises(ConflictError):
            self.service.adjust_contract(request_id="adj", actor_id="fin", project_id=pid,
                                         new_amount=350_000, reason="变更", period_id="per1")
        with self.assertRaises(ConflictError):
            self.service.cancel_project(request_id="can", actor_id="fin", project_id=pid,
                                        reason="取消", period_id="per1")
        with self.assertRaises(ConflictError):
            self.service.pay_milestone(request_id="pay2", actor_id="fin", project_id=pid,
                                       milestone_code="m2", period_id="per1")
        after = self.service.trace_funds(pid)["ledger"]
        self.assertEqual(len(before), len(after))

    def test_change_in_open_period_must_not_exceed_allocation(self):
        pid = self._funded_project(amount=900_000)
        with self.assertRaises(ConflictError):
            self.service.adjust_contract(request_id="adj", actor_id="fin", project_id=pid,
                                         new_amount=1_000_001, reason="超额度", period_id="per1")

    def test_cancel_recovers_unspent_commitment(self):
        pid = self._funded_project(amount=400_000)
        self.service.cancel_project(request_id="can", actor_id="fin", project_id=pid,
                                    reason="取消", period_id="per1")
        trace = self.service.trace_funds(pid)
        self.assertEqual(trace["totals"]["recovered"], 400_000)
        self.assertEqual(trace["project"]["status"], "cancelled")

    def test_recover_savings_cannot_exceed_open_commitment(self):
        pid = self._funded_project(amount=400_000)
        with self.assertRaises(ValidationError):
            self.service.recover_savings(request_id="rec", actor_id="fin", project_id=pid,
                                         amount=400_001, reason="结余", period_id="per1")


class EmergencyTest(FundingFixture):
    def _approved_round(self):
        self.allocation()
        self.submit("p1", amount=300_000, evidence=ev(disaster_exposure=90))
        self.freeze()
        self.approve()

    def _emergency(self, pid="e1", amount=50_000):
        self.service.submit_project(
            request_id=f"em-{pid}", actor_id="hw", project_id=pid, round_id="rd1",
            external_key=f"ke-{pid}", title="水毁抢修", funding_level="county",
            requested_amount=amount, emergency=True,
            evidence=ev(condition_index=100, disaster_exposure=100, sole_access=True,
                        alternative_routes=0, service_population=300),
            milestones=[{"code": "m1", "title": "抢通", "weight": 100, "retention_pct": 0}],
            route_code="X901", start_km=0, end_km=3)

    def test_emergency_capped_by_reserve(self):
        self._approved_round()
        with self.assertRaises(ConflictError):
            self._emergency("e1", amount=100_001)

    def test_emergency_review_rechecks_remaining_reserve(self):
        self._approved_round()
        # 两笔抢修在批准前都已申报（此时均未占用承诺）。
        self._emergency("e1", amount=80_000)
        self._emergency("e2", amount=30_000)
        self.service.review_emergency(request_id="rv1", actor_id="rv", project_id="e1",
                                      result="approved")
        # e1 已占用 80k，备用额度仅剩 20k，e2 在复核环节被拦下。
        with self.assertRaises(ConflictError):
            self.service.review_emergency(request_id="rv2", actor_id="rv", project_id="e2",
                                          result="approved")

    def test_independent_review_required_and_blocks_self_review(self):
        self._approved_round()
        self._emergency()
        with self.assertRaises(PermissionDenied):
            self.service.review_emergency(request_id="self", actor_id="hw", project_id="e1",
                                          result="approved")
        receipt = self.service.review_emergency(request_id="rv1", actor_id="rv", project_id="e1",
                                                result="approved", note="现场核实")
        self.assertFalse(receipt.replayed)
        trace = self.service.trace_funds("e1")
        self.assertEqual(trace["reviews"][0]["reviewer_id"], "rv")
        self.assertEqual(trace["totals"]["committed"], 50_000)

    def test_rejected_emergency_does_not_commit(self):
        self._approved_round()
        self._emergency()
        self.service.review_emergency(request_id="rv1", actor_id="rv", project_id="e1",
                                      result="rejected", note="证据不足")
        trace = self.service.trace_funds("e1")
        self.assertEqual(trace["project"]["status"], "rejected")
        self.assertEqual(trace["totals"]["committed"], 0)

    def test_emergency_before_approval_rejected(self):
        self.allocation()
        with self.assertRaises(ConflictError):
            self._emergency()


class PolicySimulationTest(FundingFixture):
    def test_simulation_compares_without_touching_history(self):
        self.allocation()
        self.submit("rural", route="X301", amount=300_000, evidence=ev(
            service_population=400, sole_access=True, alternative_routes=0,
            disaster_exposure=90, maintenance_history_ratio=0.1))
        self.submit("arterial", route="G202", start=0, end=20, amount=300_000,
                    evidence=ev(service_population=200_000, alternative_routes=2,
                                disaster_exposure=20))
        self.freeze()
        self.approve()
        settled_decisions = self.decisions()

        simulation = self.service.simulate_policy(round_id="rd1", policy_version_tag="v2")
        self.assertEqual(simulation["historical_policy"], "v1")
        self.assertEqual(simulation["candidate_policy"], "v2")
        # 换版测算可能改变候选结果，但落库的历史排名/决定保持不变。
        self.assertEqual(self.decisions(), settled_decisions)
        self.assertTrue(any(p["old_decision"] != p["new_decision"]
                            or p["old_rank"] != p["new_rank"] for p in simulation["projects"]))
        # 已经落账的实际状态不受模拟影响。
        self.assertEqual(
            {p["project_id"]: p["actual_status"] for p in simulation["projects"]}["rural"],
            "obligated")

    def test_simulation_before_freeze_rejected(self):
        self.allocation()
        self.submit("p1")
        with self.assertRaises(ConflictError):
            self.service.simulate_policy(round_id="rd1", policy_version_tag="v2")


class FundTraceTest(FundingFixture):
    def test_trace_covers_ranking_obligation_and_settlement(self):
        self.allocation()
        self.submit("p1", route="X801", amount=300_000, milestones=MS_ONE,
                    evidence=ev(disaster_exposure=95, sole_access=True, alternative_routes=0))
        self.freeze()
        self.approve()
        self.service.start_project(request_id="st", actor_id="hw", project_id="p1")
        self.service.verify_milestone(request_id="v1", actor_id="rv", project_id="p1",
                                      milestone_code="m1")
        self.service.pay_milestone(request_id="pay", actor_id="fin", project_id="p1",
                                   milestone_code="m1", period_id="per1")
        self.service.release_retention(request_id="rel", actor_id="fin", project_id="p1",
                                       period_id="per1")
        trace = self.service.trace_funds("p1")
        self.assertEqual(trace["ranking"]["rank"], 1)
        self.assertEqual(trace["ranking"]["decision"], "funded")
        types = [entry["entry_type"] for entry in trace["ledger"]]
        self.assertEqual(types, ["commit", "pay", "release"])
        self.assertEqual(trace["totals"]["disbursed"], 300_000)


if __name__ == "__main__":
    unittest.main()

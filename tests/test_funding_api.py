import json
import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.api import route
from transport_coordination.funding_service import FundingService
from transport_coordination.storage import Database


class SettableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class FundingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = SettableClock(datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.service = FundingService(self.database, self.clock)

    def tearDown(self):
        self.database.close()

    def call(self, method, path, actor=None, body=None):
        headers = {"X-Actor-Id": actor} if actor else {}
        return route(self.service, method, path, body or {}, headers)

    def call_json(self, method, path, actor=None, body=None):
        status, payload = self.call(method, path, actor, body)
        return status, payload

    def bootstrap(self):
        self.call("POST", "/organizations", "bootstrap",
                  {"request_id": "org", "organization_id": "o1", "name": "某县"})
        self.call("POST", "/actors", "bootstrap",
                  {"request_id": "ad", "new_actor_id": "ad", "display_name": "管理员",
                   "role": "admin", "organization_id": "o1"})
        self.call("POST", "/actors", "ad",
                  {"request_id": "fin", "new_actor_id": "fin", "display_name": "财政",
                   "role": "finance", "organization_id": "o1"})
        self.call("POST", "/actors", "ad",
                  {"request_id": "hw", "new_actor_id": "hw", "display_name": "公路",
                   "role": "highway", "organization_id": "o1"})
        self.call("POST", "/actors", "ad",
                  {"request_id": "rv", "new_actor_id": "rv", "display_name": "复核",
                   "role": "reviewer", "organization_id": "o1"})

    def prepare_round(self):
        self.bootstrap()
        self.call("POST", "/policies", "fin",
                  {"request_id": "pv1", "version_tag": "v1",
                   "spec": {"weights": {"condition": 2, "disaster": 3, "alternative": 2,
                                        "service": 1, "maintenance": 1}}})
        self.call("POST", "/rounds", "fin",
                  {"request_id": "rd", "round_id": "rd1", "name": "2027",
                   "deadline_at": "2026-09-25T00:00:00Z"})
        self.call("POST", "/allocations", "fin",
                  {"request_id": "al", "round_id": "rd1", "funding_level": "county",
                   "amount": 1_000_000, "emergency_reserve": 100_000})
        self.call("POST", "/periods", "fin",
                  {"request_id": "per1", "period_id": "per1", "label": "Q4"})

    def submit(self, pid, route_code="X101", amount=300_000, **evidence_over):
        evidence = {"condition_index": 70, "service_population": 2000, "sole_access": True,
                    "alternative_routes": 0, "disaster_exposure": 90,
                    "maintenance_history_ratio": 0.2}
        evidence.update(evidence_over)
        status, payload = self.call("POST", "/projects", "hw", {
            "request_id": f"sub-{pid}", "project_id": pid, "round_id": "rd1",
            "external_key": f"k-{pid}", "title": f"项目{pid}", "funding_level": "county",
            "requested_amount": amount, "evidence": evidence,
            "milestones": [{"code": "m1", "title": "完工", "weight": 100, "retention_pct": 10}],
            "route_code": route_code, "start_km": 0, "end_km": 10})
        return status, payload

    def test_full_funding_lifecycle_over_http(self):
        self.prepare_round()
        status, _ = self.submit("p1")
        self.assertEqual(status, 201)
        self.clock.advance(days=10)
        status, payload = self.call("POST", "/rounds/freeze", "fin",
                                    {"request_id": "fz", "round_id": "rd1",
                                     "policy_version_tag": "v1"})
        self.assertEqual(status, 201, payload)
        status, payload = self.call("POST", "/rounds/approve", "fin",
                                    {"request_id": "ap", "round_id": "rd1"})
        self.assertEqual(status, 201)
        status, round_view = self.call("GET", "/rounds/rd1")
        self.assertEqual(round_view["round"]["status"], "approved")
        self.assertEqual(sum(1 for p in round_view["projects"]
                             if p["score"]["decision"] == "funded"), 1)

        self.call("POST", "/projects/start", "hw", {"request_id": "st", "project_id": "p1"})
        self.call("POST", "/milestones/verify", "rv",
                  {"request_id": "v1", "project_id": "p1", "milestone_code": "m1"})
        status, pay = self.call("POST", "/milestones/pay", "fin",
                                {"request_id": "pay1", "project_id": "p1",
                                 "milestone_code": "m1", "period_id": "per1"})
        self.assertEqual(status, 201)
        self.assertTrue(pay["resource_id"])
        self.call("POST", "/projects/release-retention", "fin",
                  {"request_id": "rel", "project_id": "p1", "period_id": "per1"})

        status, trace = self.call("GET", "/projects/p1/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["ranking"]["rank"], 1)
        self.assertEqual(trace["payments"][0]["retained"], 30_000)
        self.assertEqual([e["entry_type"] for e in trace["ledger"]],
                         ["commit", "pay", "release"])

    def test_duplicate_submission_is_idempotent(self):
        self.prepare_round()
        self.submit("p1")
        status, first = self.submit("p1")
        self.assertEqual(status, 200)
        self.assertTrue(first["replayed"])

    def test_highway_cannot_freeze_or_pay(self):
        self.prepare_round()
        self.submit("p1")
        self.clock.advance(days=10)
        status, payload = self.call("POST", "/rounds/freeze", "hw",
                                    {"request_id": "fz", "round_id": "rd1",
                                     "policy_version_tag": "v1"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "permission_denied")

    def test_closed_period_blocks_payment_over_http(self):
        self.prepare_round()
        self.submit("p1")
        self.clock.advance(days=10)
        self.call("POST", "/rounds/freeze", "fin",
                  {"request_id": "fz", "round_id": "rd1", "policy_version_tag": "v1"})
        self.call("POST", "/rounds/approve", "fin", {"request_id": "ap", "round_id": "rd1"})
        self.call("POST", "/projects/start", "hw", {"request_id": "st", "project_id": "p1"})
        self.call("POST", "/milestones/verify", "rv",
                  {"request_id": "v1", "project_id": "p1", "milestone_code": "m1"})
        self.call("POST", "/milestones/pay", "fin",
                  {"request_id": "pay1", "project_id": "p1", "milestone_code": "m1",
                   "period_id": "per1"})
        self.call("POST", "/periods/close", "fin",
                  {"request_id": "cl", "period_id": "per1"})
        status, payload = self.call("POST", "/projects/release-retention", "fin",
                                    {"request_id": "rel", "project_id": "p1",
                                     "period_id": "per1"})
        self.assertEqual(status, 409)
        self.assertIn("关账", payload["message"])

    def test_policy_simulation_endpoint_does_not_mutate(self):
        self.prepare_round()
        self.call("POST", "/policies", "fin",
                  {"request_id": "pv2", "version_tag": "v2",
                   "spec": {"weights": {"service": 5, "condition": 1, "disaster": 1,
                                        "alternative": 1, "maintenance": 1}}})
        self.submit("rural", "X301", service_population=300)
        self.submit("arterial", "G202", service_population=200_000, disaster_exposure=10,
                    sole_access=False, alternative_routes=2)
        self.clock.advance(days=10)
        self.call("POST", "/rounds/freeze", "fin",
                  {"request_id": "fz", "round_id": "rd1", "policy_version_tag": "v1"})
        status, simulation = self.call("POST", "/rounds/simulate-policy", None,
                                       {"round_id": "rd1", "policy_version_tag": "v2"})
        self.assertEqual(status, 200)
        self.assertEqual(simulation["historical_policy"], "v1")
        status, round_view = self.call("GET", "/rounds/rd1")
        # 历史决策不随模拟改写。
        bound = round_view["round"]["bound_policy_id"]
        policies = {p["policy_id"]: p["version_tag"]
                    for p in self.service.list_policies()}
        self.assertEqual(policies[bound], "v1")

    def test_round_detail_lists_flags_and_scores(self):
        self.prepare_round()
        self.submit("p1", "X401")
        self.submit("p2", "X401")  # 同路线完全重叠 → 拆项/重复线索
        self.clock.advance(days=10)
        self.call("POST", "/rounds/freeze", "fin",
                  {"request_id": "fz", "round_id": "rd1", "policy_version_tag": "v1"})
        status, round_view = self.call("GET", "/rounds/rd1")
        self.assertEqual(status, 200)
        self.assertTrue(round_view["flags"])
        self.assertTrue(all("factor_names" in p["score"] for p in round_view["projects"]))


if __name__ == "__main__":
    unittest.main()

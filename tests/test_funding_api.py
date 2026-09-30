"""养护资金服务的 HTTP 路由集成测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.funding_service import FundingService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class FundingApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "api.sqlite3")
        clock = FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.funding = FundingService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="org1", name="县交通局")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="管理员", role="admin", organization_id="org1")
        self.service.register_actor(request_id="hw", actor_id="admin1", new_actor_id="hw1",
                                    display_name="公路", role="highway", organization_id="org1")
        self.service.register_actor(request_id="fin", actor_id="admin1", new_actor_id="fin1",
                                    display_name="财政", role="finance", organization_id="org1")

    def tearDown(self) -> None:
        self.database.close()
        self.tempdir.cleanup()

    def call(self, method: str, path: str, body=None, actor: str = "fin1"):
        # actor_id 只通过 X-Actor-Id 请求头传递，不出现在 JSON 请求体中
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, funding=self.funding)

    def _full_flow(self) -> str:
        self.call("POST", "/funding/periods/open",
                  {"request_id": "p1", "period_id": "P1", "label": "年度"})
        self.call("POST", "/funding/rounds",
                  {"request_id": "r1", "round_id": "R1", "name": "批次",
                   "deadline_at": "2026-09-15T17:00:00Z", "emergency_quota": 50})
        self.call("POST", "/funding/envelopes",
                  {"request_id": "e1", "round_id": "R1", "level": "county", "amount": 200})
        self.call("POST", "/funding/segments",
                  {"request_id": "s1", "segment_id": "SEG1", "route_code": "G1", "name": "村道",
                   "length_km": 8, "chainage_start": 0, "chainage_end": 8}, actor="hw1")
        evidence = {
            "condition": {"pci": 30},
            "population": {"served_population": 1200, "sole_access": True},
            "alternatives": {"alternative_routes": 0},
            "hazard": {"hazard_level": 90},
            "traffic": {"aadt": 300},
            "maintenance_history": {"repeated_repair": False},
        }
        for index, (dimension, payload) in enumerate(evidence.items()):
            status, _ = self.call("POST", "/funding/evidence", {
                "request_id": f"ev{index}", "segment_id": "SEG1", "dimension": dimension,
                "payload": payload, "effective_at": "2026-09-01T00:00:00Z"}, actor="hw1")
            self.assertEqual(201, status)
        self.call("POST", "/funding/applications", {
            "request_id": "a1", "round_id": "R1", "application_id": "APP1",
            "segment_id": "SEG1", "title": "水毁修复", "amount_requested": 80,
            "chainage_start": 0, "chainage_end": 5,
            "milestones": [{"seq": 1, "name": "挡墙", "amount": 80, "due_date": "2026-12-01"}]},
            actor="hw1")
        self.call("POST", "/funding/freeze", {"request_id": "f1", "round_id": "R1"})
        status, decision = self.call("POST", "/funding/decide",
                                     {"request_id": "d1", "round_id": "R1"})
        self.assertEqual(201, status)
        self.assertEqual(1, decision["approved"])
        self.call("POST", "/funding/payments",
                  {"request_id": "pay1", "application_id": "APP1", "seq": 1, "amount": 80})
        return "APP1"

    def test_full_funding_flow_over_http(self) -> None:
        application_id = self._full_flow()
        status, trace = self.call("GET", f"/funding/trace?application_id={application_id}")
        self.assertEqual(200, status)
        self.assertEqual("settled", trace["status"])
        self.assertEqual(80.0, trace["money_summary"]["paid"])
        self.assertEqual(6, len(trace["frozen_evidence"]))
        self.assertEqual("v2026.1", trace["policy_version_at_freeze"])

    def test_round_and_portfolio_endpoints(self) -> None:
        self._full_flow()
        status, round_info = self.call("GET", "/funding/rounds?round_id=R1")
        self.assertEqual(200, status)
        self.assertEqual("finalized", round_info["status"])
        self.assertIn("balances", round_info)
        status, portfolio = self.call("GET", "/funding/portfolio?round_id=R1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(portfolio["items"]))

    def test_conflict_endpoint_reports_split(self) -> None:
        self.call("POST", "/funding/periods/open",
                  {"request_id": "p1", "period_id": "P1", "label": "年度"})
        self.call("POST", "/funding/rounds",
                  {"request_id": "r1", "round_id": "R1", "name": "批次",
                   "deadline_at": "2026-09-15T17:00:00Z"})
        self.call("POST", "/funding/segments",
                  {"request_id": "s1", "segment_id": "SEG1", "route_code": "G1", "name": "路",
                   "length_km": 10, "chainage_start": 0, "chainage_end": 10}, actor="hw1")
        milestone = [{"seq": 1, "name": "m", "amount": 10, "due_date": "2026-12-01"}]
        self.call("POST", "/funding/applications", {
            "request_id": "a1", "round_id": "R1", "application_id": "A1",
            "segment_id": "SEG1", "title": "甲", "amount_requested": 10,
            "chainage_start": 0, "chainage_end": 5,
            "window_start": "2026-10-01", "window_end": "2026-10-15",
            "milestones": milestone}, actor="hw1")
        status, payload = self.call("POST", "/funding/applications", {
            "request_id": "a2", "round_id": "R1", "application_id": "A2",
            "segment_id": "SEG1", "title": "乙拆项", "amount_requested": 10,
            "chainage_start": 3, "chainage_end": 8,
            "window_start": "2026-11-01", "window_end": "2026-11-15",
            "milestones": milestone}, actor="hw1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_permission_denied_via_http(self) -> None:
        status, payload = self.call("POST", "/funding/rounds",
                                    {"request_id": "r1", "round_id": "R1", "name": "批次",
                                     "deadline_at": "2026-09-15T17:00:00Z"}, actor="hw1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_state_persists_across_service_restart(self) -> None:
        application_id = self._full_flow()
        funding2 = FundingService(self.database, FixedClock(
            datetime(2026, 9, 2, tzinfo=timezone.utc)))
        status, trace = route(self.service, "GET",
                              f"/funding/trace?application_id={application_id}", {},
                              {"X-Actor-Id": "fin1"}, funding=funding2)
        self.assertEqual(200, status)
        self.assertEqual("settled", trace["status"])
        self.assertEqual(80.0, trace["money_summary"]["paid"])


if __name__ == "__main__":
    unittest.main()

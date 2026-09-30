"""运行养护资金决策与执行服务的离线端到端验收。

覆盖：评分政策冻结、灾害高/替代少的乡村路压过单纯高流量干线、
拆项与施工窗口识别、组合批准与承诺、里程碑保留与支付、
关账期间不可改写、紧急抢修限额例外与独立复核、政策换版模拟、
资金全链路追踪与审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .funding_service import FundingService
from .storage import Database


class AdvancingClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "funding_acceptance.sqlite3")
        clock = AdvancingClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        service = FundingService(database, clock)

        service.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="county-001", name="示范县交通运输局")
        service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="county-001")
        service.register_actor(request_id="finance", actor_id="admin-001", new_actor_id="finance-001",
                               display_name="财政经办人", role="finance", organization_id="county-001")
        service.register_actor(request_id="highway", actor_id="admin-001", new_actor_id="highway-001",
                               display_name="公路经办人", role="highway", organization_id="county-001")
        service.register_actor(request_id="reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="独立复核人", role="reviewer", organization_id="county-001")

        service.create_scoring_policy(request_id="policy-v1", actor_id="finance-001",
                                      version_tag="2027-v1",
                                      spec={"weights": {"condition": 2, "service": 1,
                                                        "alternative": 2, "disaster": 3,
                                                        "maintenance": 1, "dependency": 1}})
        service.create_round(request_id="round", actor_id="finance-001", round_id="round-2027",
                             name="2027 年度普通公路养护", deadline_at="2026-09-25T00:00:00Z")
        service.set_allocation(request_id="alloc", actor_id="finance-001", round_id="round-2027",
                               funding_level="county", amount=1_000_000, emergency_reserve=100_000)
        service.open_period(request_id="period", actor_id="finance-001",
                            period_id="2026-q4", label="2026 年第四季度")

        milestones = [{"code": "base", "title": "路基处置", "weight": 60, "retention_pct": 10},
                      {"code": "pavement", "title": "路面铺筑", "weight": 40, "retention_pct": 10}]

        def submit(request_id, project_id, route, amount, evidence, start=0.0, end=10.0,
                   window=None, depends_on=None):
            kwargs = {}
            if window:
                kwargs["window_start"], kwargs["window_end"] = window
            if depends_on:
                kwargs["depends_on"] = depends_on
            service.submit_project(
                request_id=request_id, actor_id="highway-001", project_id=project_id,
                round_id="round-2027", external_key=request_id, title=f"养护项目-{project_id}",
                funding_level="county", requested_amount=amount, evidence=evidence,
                milestones=milestones, route_code=route, start_km=start, end_km=end, **kwargs)

        # 乡村唯一通达、灾害暴露高、历史欠账多。
        submit("rural", "prj-rural", "X301", 400_000,
               {"condition_index": 82, "service_population": 620, "sole_access": True,
                "alternative_routes": 0, "disaster_exposure": 95,
                "maintenance_history_ratio": 0.1})
        # 高流量干线、灾害风险低、替代路线多。
        submit("arterial", "prj-arterial", "G202", 400_000,
               {"condition_index": 55, "service_population": 150_000, "sole_access": False,
                "alternative_routes": 2, "disaster_exposure": 15,
                "maintenance_history_ratio": 1.0}, start=0.0, end=20.0)
        # 同一路线相邻的拆项申报。
        submit("split-a", "prj-split-a", "X401", 200_000,
               {"condition_index": 70, "service_population": 900, "sole_access": True,
                "alternative_routes": 0, "disaster_exposure": 88,
                "maintenance_history_ratio": 0.2}, start=0.0, end=10.0,
               window=("2027-05-01", "2027-06-01"))
        submit("split-b", "prj-split-b", "X401", 200_000,
               {"condition_index": 70, "service_population": 900, "sole_access": True,
                "alternative_routes": 0, "disaster_exposure": 88,
                "maintenance_history_ratio": 0.2}, start=10.3, end=14.0,
               window=("2027-05-20", "2027-07-01"))

        clock.advance(days=10)  # 越过申报截止
        service.freeze_round(request_id="freeze", actor_id="finance-001",
                             round_id="round-2027", policy_version_tag="2027-v1")
        service.approve_portfolio(request_id="approve", actor_id="finance-001",
                                  round_id="round-2027")
        round_view = service.get_round("round-2027")
        ranking = {p["project_id"]: (p["score"]["rank"], p["score"]["decision"])
                   for p in round_view["projects"]}
        flags = sorted({f["flag_type"] for f in round_view["flags"]})

        # 执行乡村项目：开工、核定、两期支付、保留金结清。
        service.start_project(request_id="start", actor_id="highway-001", project_id="prj-rural")
        service.verify_milestone(request_id="verify-base", actor_id="reviewer-001",
                                 project_id="prj-rural", milestone_code="base")
        service.pay_milestone(request_id="pay-base", actor_id="finance-001",
                              project_id="prj-rural", milestone_code="base", period_id="2026-q4")
        service.verify_milestone(request_id="verify-pavement", actor_id="reviewer-001",
                                 project_id="prj-rural", milestone_code="pavement")
        service.pay_milestone(request_id="pay-pavement", actor_id="finance-001",
                              project_id="prj-rural", milestone_code="pavement",
                              period_id="2026-q4")
        service.release_retention(request_id="settle", actor_id="finance-001",
                                  project_id="prj-rural", period_id="2026-q4")
        trace = service.trace_funds("prj-rural")

        # 关账：旧期间不可再记任何账。
        service.close_period(request_id="close", actor_id="finance-001", period_id="2026-q4")
        closed_blocked = False
        try:
            service.cancel_project(request_id="cancel-after-close", actor_id="finance-001",
                                   project_id="prj-arterial", reason="尝试改写关账期间",
                                   period_id="2026-q4")
        except Exception:
            closed_blocked = True

        # 紧急抢修：限额例外 + 独立复核（复核人不能是申报人）。
        service.open_period(request_id="period-2027", actor_id="finance-001",
                            period_id="2027-q1", label="2027 年第一季度")
        service.submit_project(
            request_id="emergency", actor_id="highway-001", project_id="prj-emergency",
            round_id="round-2027", external_key="emergency-1", title="山洪冲毁路段抢通",
            funding_level="county", requested_amount=60_000, emergency=True,
            evidence={"condition_index": 100, "service_population": 400, "sole_access": True,
                      "alternative_routes": 0, "disaster_exposure": 100,
                      "maintenance_history_ratio": 0.0},
            milestones=[{"code": "open", "title": "抢通", "weight": 100, "retention_pct": 0}],
            route_code="X399", start_km=0.0, end_km=2.0)
        self_blocked = False
        try:
            service.review_emergency(request_id="self-review", actor_id="highway-001",
                                     project_id="prj-emergency", result="approved")
        except Exception:
            self_blocked = True
        service.review_emergency(request_id="emergency-review", actor_id="reviewer-001",
                                 project_id="prj-emergency", result="approved",
                                 note="现场水毁核实，符合限额例外")

        # 政策换版模拟：偏向服务人口的新版本，不重算历史决定。
        service.create_scoring_policy(request_id="policy-v2", actor_id="finance-001",
                                      version_tag="2027-v2-service",
                                      spec={"weights": {"condition": 1, "service": 5,
                                                        "alternative": 1, "disaster": 1,
                                                        "maintenance": 1, "dependency": 1}})
        simulation = service.simulate_policy(round_id="round-2027",
                                             policy_version_tag="2027-v2-service")

        audit_valid, audit_events = service.verify_audit()
        result = {
            "status": "ok",
            "rural_rank": ranking["prj-rural"][0],
            "rural_decision": ranking["prj-rural"][1],
            "arterial_rank": ranking["prj-arterial"][0],
            "arterial_decision": ranking["prj-arterial"][1],
            "split_piece_decision": ranking["prj-split-b"][1],
            "flags": flags,
            "rural_disbursed": trace["totals"]["disbursed"],
            "rural_open_commitment": trace["totals"]["open_commitment"],
            "rural_settled": trace["project"]["status"] == "settled",
            "closed_period_blocked": closed_blocked,
            "emergency_self_review_blocked": self_blocked,
            "emergency_committed": service.trace_funds("prj-emergency")["totals"]["committed"],
            "simulation_changed_decisions": simulation["decision_changes"],
            "historical_policy_unchanged": simulation["historical_policy"] == "2027-v1",
            "audit_events": audit_events,
            "audit_valid": audit_valid,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

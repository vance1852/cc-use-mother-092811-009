"""运行综合交通协同服务（基础登记 + 养护资金决策执行）的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .funding_service import FundingService
from .service import DomainService
from .storage import Database


def _milestones(*amounts: float) -> list[dict]:
    return [{"seq": index, "name": f"里程碑{index}", "amount": amount,
             "due_date": f"2026-12-{index:02d}"} for index, amount in enumerate(amounts, start=1)]


def run() -> dict[str, object]:
    """执行基础登记链与养护资金全流程并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        funding = FundingService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))

        # ---- 基础登记 ----
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范县交通运输局")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-highway", actor_id="admin-001", new_actor_id="highway-001",
                               display_name="公路主管", role="highway", organization_id="org-001")
        service.register_actor(request_id="req-finance", actor_id="admin-001", new_actor_id="finance-001",
                               display_name="财政主管", role="finance", organization_id="org-001")
        service.register_organization(request_id="req-org2", actor_id="admin-001",
                                      organization_id="org-002", name="示范县独立复核中心")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="独立复核员", role="reviewer", organization_id="org-002")
        service.register_site(request_id="req-site", actor_id="admin-001", site_id="site-001",
                              organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
        service.record_domain_data(request_id="req-data", actor_id="admin-001", site_id="site-001",
                                   category="network_registry", external_key="record-001",
                                   data={"name": "公路网登记", "enabled": True})

        # ---- 会计期间与资金轮次 ----
        funding.open_period(request_id="req-period", actor_id="finance-001",
                            period_id="period-2026", label="2026 养护年度")
        funding.create_round(request_id="req-round", actor_id="finance-001", round_id="round-2026",
                             name="秋季集中养护批次", deadline_at="2026-10-15T17:00:00+08:00",
                             emergency_quota=50)
        funding.set_envelope(request_id="req-env-county", actor_id="finance-001",
                             round_id="round-2026", level="county", amount=300)
        funding.set_envelope(request_id="req-env-prov", actor_id="finance-001",
                             round_id="round-2026", level="provincial", amount=300)

        # ---- 两条对比路段：高流量干线 vs 乡村唯一通达路 ----
        funding.register_segment(request_id="req-seg-a", actor_id="highway-001", segment_id="seg-artery",
                                 route_code="G205", name="国道干线段", length_km=12,
                                 chainage_start=100, chainage_end=112)
        funding.register_segment(request_id="req-seg-b", actor_id="highway-001", segment_id="seg-village",
                                 route_code="X302", name="山区村道段", length_km=6,
                                 chainage_start=0, chainage_end=6)
        evidence = {
            "seg-artery": {"condition": {"pci": 62},
                           "population": {"served_population": 60000, "sole_access": False},
                           "alternatives": {"alternative_routes": 2},
                           "hazard": {"hazard_level": 25},
                           "traffic": {"aadt": 28000},
                           "maintenance_history": {"repeated_repair": False}},
            "seg-village": {"condition": {"pci": 31},
                            "population": {"served_population": 1300, "sole_access": True},
                            "alternatives": {"alternative_routes": 0},
                            "hazard": {"hazard_level": 88},
                            "traffic": {"aadt": 260},
                            "maintenance_history": {"repeated_repair": True}},
        }
        req_index = 0
        for segment_id, dimensions in evidence.items():
            for dimension, payload in dimensions.items():
                req_index += 1
                funding.add_evidence(request_id=f"req-ev-{req_index}", actor_id="highway-001",
                                     segment_id=segment_id, dimension=dimension, payload=payload,
                                     effective_at="2026-09-20T00:00:00+08:00")

        funding.submit_application(
            request_id="req-app-a", actor_id="highway-001", round_id="round-2026",
            application_id="app-artery", segment_id="seg-artery", title="干线中修",
            amount_requested=350, chainage_start=100, chainage_end=108,
            work_type="routine", window_start="2026-11-01", window_end="2026-11-25",
            milestones=_milestones(200, 150))
        funding.submit_application(
            request_id="req-app-b", actor_id="highway-001", round_id="round-2026",
            application_id="app-village", segment_id="seg-village", title="村道水毁修复",
            amount_requested=240, chainage_start=0, chainage_end=6,
            work_type="routine", window_start="2026-12-01", window_end="2026-12-20",
            milestones=_milestones(120, 120))

        # ---- 截止冻结：证据快照 + 评分政策版本固定 ----
        frozen = funding.freeze_round(request_id="req-freeze", actor_id="finance-001",
                                      round_id="round-2026")
        conflicts_before = funding.detect_conflicts("finance-001", "round-2026")["has_conflict"]
        portfolio = funding.decide_portfolio(request_id="req-decide", actor_id="finance-001",
                                             round_id="round-2026")

        # ---- 里程碑承诺与支付 ----
        funding.pay_milestone(request_id="req-pay-1", actor_id="finance-001",
                              application_id="app-village", seq=1, amount=120)
        funding.pay_milestone(request_id="req-pay-2", actor_id="finance-001",
                              application_id="app-village", seq=2, amount=120)
        village_trace = funding.get_trace("finance-001", "app-village")

        # ---- 关账后历史不可改写 ----
        funding.close_period(request_id="req-close", actor_id="finance-001",
                             period_id="period-2026")
        closed_blocks = False
        try:
            funding.pay_milestone(request_id="req-pay-forbidden", actor_id="finance-001",
                                  application_id="app-artery", seq=1, amount=10)
        except Exception:
            closed_blocks = True

        # ---- 紧急抢修限额例外 + 独立复核（新会计期间，旧期间台账原样保留）----
        funding.open_period(request_id="req-period-2", actor_id="finance-001",
                            period_id="period-2027", label="2027 养护年度")
        funding.register_segment(request_id="req-seg-c", actor_id="highway-001",
                                 segment_id="seg-emergency", route_code="Y518", name="应急抢险段",
                                 length_km=4, chainage_start=0, chainage_end=4)
        funding.submit_application(
            request_id="req-app-em", actor_id="highway-001", round_id="round-2026",
            application_id="app-emergency", segment_id="seg-emergency", title="汛期塌方抢通",
            amount_requested=40, chainage_start=0, chainage_end=2, work_type="emergency",
            milestones=_milestones(40))
        emergency = funding.submit_emergency_review(
            request_id="req-review-em", actor_id="reviewer-001", application_id="app-emergency",
            verdict="approved", notes="现场核实塌方断通，限额内抢修")

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        ranking = {item["application_id"]: item["rank"]
                   for item in funding.get_portfolio("finance-001", "round-2026")["items"]}
        result = {
            "status": "ok",
            "records": len(records),
            "frozen_snapshots": frozen["snapshots"],
            "frozen_policy": frozen["policy_version"],
            "pre_approval_conflicts": conflicts_before,
            "approved": portfolio["approved"],
            "waitlisted": portfolio["waitlisted"],
            # 核心政策取向：唯一通达+高灾害村道必须排在高流量干线之前
            "village_ranks_first": ranking["app-village"] < ranking["app-artery"],
            "village_settled": village_trace["status"] == "settled",
            "village_paid": village_trace["money_summary"]["paid"],
            "closed_period_blocks_writes": closed_blocks,
            "emergency_status": emergency["status"],
            "emergency_has_independent_review": (
                funding.get_trace("reviewer-001", "app-emergency")["independent_review"] is not None),
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ["audit_valid", "village_ranks_first", "village_settled",
                "closed_period_blocks_writes", "emergency_has_independent_review"]
    ok = result["status"] == "ok" and all(result[key] for key in required)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

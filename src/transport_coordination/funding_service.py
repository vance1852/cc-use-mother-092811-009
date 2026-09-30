"""养护资金决策与执行的编排服务。

覆盖轮次冻结证据与评分政策、批准前冲突识别、组合批准与额度承诺、
里程碑保留与支付、会计期间关账、紧急抢修限额例外与独立复核、
资金全链路追踪以及政策换版模拟。历史决定一旦落账即不可改写：
账务只追加 INSERT，且关账期间拒绝任何新分录。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .funding import (
    FACTOR_NAMES,
    FUNDING_LEVELS,
    detect_flags,
    score_project,
    select_portfolio,
    validate_policy,
)
from .models import WriteReceipt
from .service import DomainService


class FundingService(DomainService):
    """在基础主体/权限/审计能力之上实现养护资金业务。"""

    # ---- 评分政策 -------------------------------------------------------

    def create_scoring_policy(self, *, request_id: str, actor_id: str,
                              version_tag: str, spec: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "version_tag": version_tag, "spec": spec}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            version_tag = self._identifier(version_tag, "version_tag")
            try:
                weights = validate_policy(spec)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            normalized_spec = {"weights": weights}
            policy_hash = digest(normalized_spec)

            def create() -> tuple[str, str, dict[str, Any]]:
                policy_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO scoring_policies(policy_id,version_tag,spec_json,policy_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (policy_id, version_tag, canonical_json(normalized_spec), policy_hash,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("政策版本标签或内容已经存在") from exc
                append_event(connection, actor_id=actor_id, action="policy.created",
                             resource_type="scoring_policy", resource_id=policy_id,
                             detail={"version_tag": version_tag, "policy_hash": policy_hash,
                                     "weights": weights}, occurred_at=self._now())
                return "scoring_policy", policy_id, {"policy_id": policy_id,
                                                     "version_tag": version_tag}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_scoring_policy", payload=payload, create=create)

    def list_policies(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM scoring_policies ORDER BY created_at"
        ).fetchall()
        return [{"policy_id": row["policy_id"], "version_tag": row["version_tag"],
                 "spec": json.loads(row["spec_json"]),
                 "policy_hash": row["policy_hash"], "created_at": row["created_at"]} for row in rows]

    def _policy_by_tag(self, connection, version_tag: str):
        row = connection.execute(
            "SELECT * FROM scoring_policies WHERE version_tag=?", (version_tag,)
        ).fetchone()
        if row is None:
            raise NotFoundError("评分政策版本不存在")
        return row

    # ---- 路段台账 -------------------------------------------------------

    def register_segment(self, *, request_id: str, actor_id: str, segment_id: str,
                         route_code: str, start_km: float, end_km: float, name: str,
                         sole_access: bool, organization_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "segment_id": segment_id, "route_code": route_code,
                   "start_km": start_km, "end_km": end_km, "name": name, "sole_access": sole_access}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "highway")
            segment_id = self._identifier(segment_id, "segment_id")
            route_code = self._identifier(route_code, "route_code")
            name = self._text(name, "name")
            start_km, end_km = self._span(start_km, end_km)
            organization_id = organization_id or actor.organization_id
            if actor.role != "admin" and organization_id != actor.organization_id:
                raise PermissionDenied("不能为其他组织登记路段")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO road_segments(segment_id,organization_id,route_code,start_km,end_km,"
                        "name,sole_access,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (segment_id, organization_id, route_code, start_km, end_km, name,
                         1 if sole_access else 0, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("路段编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="segment.registered",
                             resource_type="road_segment", resource_id=segment_id,
                             detail={"route_code": route_code, "start_km": start_km,
                                     "end_km": end_km, "sole_access": bool(sole_access)},
                             occurred_at=self._now())
                return "road_segment", segment_id, {"segment_id": segment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_segment", payload=payload, create=create)

    # ---- 申报轮次与资金额度 ----------------------------------------------

    def create_round(self, *, request_id: str, actor_id: str, round_id: str,
                     name: str, deadline_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "round_id": round_id, "name": name,
                   "deadline_at": deadline_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            round_id = self._identifier(round_id, "round_id")
            name = self._text(name, "name")
            deadline_at = self._text(deadline_at, "deadline_at", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO funding_rounds(round_id,name,deadline_at,status,created_by,created_at) "
                        "VALUES(?,?,?,'open',?,?)",
                        (round_id, name, deadline_at, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("轮次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="round.created",
                             resource_type="funding_round", resource_id=round_id,
                             detail={"name": name, "deadline_at": deadline_at},
                             occurred_at=self._now())
                return "funding_round", round_id, {"round_id": round_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_round", payload=payload, create=create)

    def set_allocation(self, *, request_id: str, actor_id: str, round_id: str,
                       funding_level: str, amount: int, emergency_reserve: int = 0) -> WriteReceipt:
        payload = {"actor_id": actor_id, "round_id": round_id, "funding_level": funding_level,
                   "amount": amount, "emergency_reserve": emergency_reserve}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            round_row = self._round(connection, round_id)
            if round_row["status"] != "open":
                raise ConflictError("轮次冻结后不能再下达额度")
            if funding_level not in FUNDING_LEVELS:
                raise ValidationError("funding_level 必须是 central/provincial/county")
            amount = self._money(amount, "amount")
            emergency_reserve = self._money(emergency_reserve, "emergency_reserve")
            if emergency_reserve > amount:
                raise ValidationError("紧急备用额度不能超过该级总额度")
            allocation_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO round_allocations(allocation_id,round_id,funding_level,amount,"
                    "emergency_reserve,created_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(round_id,funding_level) DO UPDATE SET "
                    "amount=excluded.amount, emergency_reserve=excluded.emergency_reserve",
                    (allocation_id, round_id, funding_level, amount, emergency_reserve, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="allocation.set",
                             resource_type="funding_round", resource_id=round_id,
                             detail={"funding_level": funding_level, "amount": amount,
                                     "emergency_reserve": emergency_reserve},
                             occurred_at=self._now())
                row = connection.execute(
                    "SELECT allocation_id FROM round_allocations WHERE round_id=? AND funding_level=?",
                    (round_id, funding_level),
                ).fetchone()
                return "allocation", row["allocation_id"], {"allocation_id": row["allocation_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_allocation", payload=payload, create=create)

    # ---- 项目申报 -------------------------------------------------------

    def submit_project(self, *, request_id: str, actor_id: str, project_id: str, round_id: str,
                       external_key: str, title: str, funding_level: str, requested_amount: int,
                       evidence: dict[str, Any], milestones: list[dict[str, Any]],
                       segment_id: str | None = None, route_code: str | None = None,
                       start_km: float | None = None, end_km: float | None = None,
                       window_start: str | None = None, window_end: str | None = None,
                       depends_on: str | None = None, emergency: bool = False) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "round_id": round_id,
                   "external_key": external_key, "title": title, "funding_level": funding_level,
                   "requested_amount": requested_amount, "evidence": evidence,
                   "milestones": milestones, "segment_id": segment_id, "route_code": route_code,
                   "start_km": start_km, "end_km": end_km, "window_start": window_start,
                   "window_end": window_end, "depends_on": depends_on, "emergency": emergency}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "highway")
            round_row = self._round(connection, round_id)
            project_id = self._identifier(project_id, "project_id")
            external_key = self._identifier(external_key, "external_key")
            title = self._text(title, "title")
            if funding_level not in FUNDING_LEVELS:
                raise ValidationError("funding_level 必须是 central/provincial/county")
            requested_amount = self._money(requested_amount, "requested_amount")
            if not isinstance(evidence, dict) or not evidence:
                raise ValidationError("evidence 必须是非空对象")
            milestone_rows = self._validate_milestones(milestones)

            now_dt = self.clock.now()
            now = self._now()

            organization_id = actor.organization_id
            sole_default = False
            if segment_id:
                segment = connection.execute(
                    "SELECT * FROM road_segments WHERE segment_id=?", (segment_id,)
                ).fetchone()
                if segment is None:
                    raise NotFoundError("路段不存在")
                route_code = segment["route_code"]
                start_km, end_km = segment["start_km"], segment["end_km"]
                organization_id = segment["organization_id"]
                sole_default = bool(segment["sole_access"])
            else:
                if not route_code or start_km is None or end_km is None:
                    raise ValidationError("必须关联路段或提供路线桩号")
                route_code = self._identifier(route_code, "route_code")
                start_km, end_km = self._span(start_km, end_km)
            if window_start and window_end and window_start > window_end:
                raise ValidationError("施工窗口开始不能晚于结束")

            if depends_on:
                parent = connection.execute(
                    "SELECT * FROM projects WHERE round_id=? AND project_id=?",
                    (round_id, depends_on),
                ).fetchone()
                if parent is None:
                    raise NotFoundError("前置依赖项目不存在于同一轮次")
                if parent["emergency"] != (1 if emergency else 0):
                    raise ValidationError("紧急项目不能依赖常规项目，反之亦然")
                if self._would_cycle(connection, round_id, depends_on, project_id):
                    raise ValidationError("项目依赖形成循环")

            # 证据在幂等判断前完成归一化，保证请求哈希在首次与重放间一致。
            evidence.setdefault("sole_access", evidence.get("sole_access", sole_default))
            evidence_hash = digest(evidence)

            prior = self._replay_if_seen(
                connection, request_id=request_id, action="submit_project", payload=payload)
            if prior is not None:
                return prior
            if emergency:
                # 限额例外：轮次批准进入执行年后，紧急抢修绕过排名直接申报。
                if round_row["status"] != "approved":
                    raise ConflictError("紧急抢修只能在轮次批准后的执行期申报")
                reserve = self._reserve_remaining(connection, round_id, funding_level)
                if requested_amount > reserve:
                    raise ConflictError("申报金额超过该级紧急备用额度剩余")
            else:
                if round_row["status"] != "open":
                    raise ConflictError("轮次已冻结，不能再提交常规申报")
                if now_dt > self._parse_ts(round_row["deadline_at"]):
                    raise ConflictError("已超过申报截止时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO projects(project_id,round_id,external_key,title,organization_id,"
                        "segment_id,route_code,start_km,end_km,window_start,window_end,funding_level,"
                        "requested_amount,depends_on,emergency,evidence_json,evidence_hash,"
                        "milestones_json,status,submitted_by,submitted_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (project_id, round_id, external_key, title, organization_id, segment_id,
                         route_code, start_km, end_km, window_start, window_end, funding_level,
                         requested_amount, depends_on, 1 if emergency else 0,
                         canonical_json(evidence), evidence_hash,
                         canonical_json(milestone_rows), "submitted", actor_id, now),
                    )
                except Exception as exc:
                    raise ConflictError("项目编号或轮次内业务键已经存在") from exc
                for position, milestone in enumerate(milestone_rows):
                    connection.execute(
                        "INSERT INTO project_milestones(project_id,code,position,title,weight,"
                        "retention_pct,planned_date) VALUES(?,?,?,?,?,?,?)",
                        (project_id, milestone["code"], position, milestone["title"],
                         milestone["weight"], milestone["retention_pct"], milestone.get("planned_date")),
                    )
                if not emergency:
                    self._insert_pair_flags(connection, round_id, project_id)
                append_event(connection, actor_id=actor_id,
                             action="project.emergency_submitted" if emergency else "project.submitted",
                             resource_type="project", resource_id=project_id,
                             detail={"round_id": round_id, "external_key": external_key,
                                     "funding_level": funding_level,
                                     "requested_amount": requested_amount,
                                     "evidence_hash": evidence_hash, "emergency": emergency},
                             occurred_at=now)
                return "project", project_id, {"project_id": project_id, "status": "submitted"}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_project", payload=payload, create=create)

    # ---- 截止冻结：证据 + 政策 + 排名 + 组合试算 ---------------------------

    def freeze_round(self, *, request_id: str, actor_id: str, round_id: str,
                     policy_version_tag: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "round_id": round_id,
                   "policy_version_tag": policy_version_tag}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="freeze_round", payload=payload)
            if prior is not None:
                return prior
            round_row = self._round(connection, round_id)
            if round_row["status"] != "open":
                raise ConflictError("轮次不是开放状态，不能冻结")
            if self.clock.now() <= self._parse_ts(round_row["deadline_at"]):
                raise ConflictError("申报截止时间尚未到达，不能提前冻结")
            policy = self._policy_by_tag(connection, policy_version_tag)
            weights = json.loads(policy["spec_json"])["weights"]

            project_rows = connection.execute(
                "SELECT * FROM projects WHERE round_id=? AND emergency=0 ORDER BY project_id",
                (round_id,),
            ).fetchall()
            if not project_rows:
                raise ConflictError("轮次内没有可评分的常规申报")

            projects_view = [self._project_view(row) for row in project_rows]
            flags = detect_flags(projects_view)
            self._persist_flags(connection, round_id, flags)

            scored: list[dict[str, Any]] = []
            for row in project_rows:
                evidence = json.loads(row["evidence_json"])
                try:
                    result = score_project(evidence, weights)
                except ValueError as exc:
                    raise ValidationError(f"项目 {row['project_id']} 证据不完整: {exc}") from exc
                scored.append((row, result))
            scored.sort(key=lambda pair: (-pair[1]["total_score"], pair[0]["project_id"]))

            budgets = self._discretionary_budgets(connection, round_id)
            conflicts = self._conflict_map(connection, round_id)
            ranked = [{
                "project_id": row["project_id"], "funding_level": row["funding_level"],
                "requested_amount": row["requested_amount"], "total_score": result["total_score"],
                "rank": rank, "depends_on": row["depends_on"],
                "conflicts": conflicts.get(row["project_id"], ()),
            } for rank, (row, result) in enumerate(scored, start=1)]
            portfolio = select_portfolio(ranked, budgets)
            decisions = {item["project_id"]: item for item in portfolio["decisions"]}

            for rank, (row, result) in enumerate(scored, start=1):
                decision = decisions[row["project_id"]]
                connection.execute(
                    "INSERT INTO project_scores(round_id,project_id,policy_id,factor_json,basis_json,"
                    "total_score,rank,decision,reasons_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (round_id, row["project_id"], policy["policy_id"],
                     canonical_json(result["factors"]), canonical_json(result["basis"]),
                     result["total_score"], rank, decision["decision"],
                     canonical_json(decision["reasons"])),
                )
                connection.execute(
                    "UPDATE projects SET score=?,rank=?,policy_id=?,status='scored' WHERE project_id=?",
                    (result["total_score"], rank, policy["policy_id"], row["project_id"]),
                )

            snapshot = digest({
                "policy_id": policy["policy_id"], "policy_hash": policy["policy_hash"],
                "projects": [{"project_id": row["project_id"], "evidence_hash": row["evidence_hash"],
                              "requested_amount": row["requested_amount"],
                              "funding_level": row["funding_level"]} for row in project_rows],
            })
            frozen_at = self._now()
            connection.execute(
                "UPDATE funding_rounds SET status='frozen',bound_policy_id=?,frozen_year=?,"
                "evidence_hash=?,frozen_at=? WHERE round_id=?",
                (policy["policy_id"], int(frozen_at[:4]), snapshot, frozen_at, round_id),
            )
            append_event(connection, actor_id=actor_id, action="round.frozen",
                         resource_type="funding_round", resource_id=round_id,
                         detail={"policy_id": policy["policy_id"],
                                 "policy_version_tag": policy_version_tag,
                                 "policy_hash": policy["policy_hash"], "evidence_hash": snapshot,
                                 "projects": len(project_rows), "flags": len(flags),
                                 "selected": portfolio["selected"], "spent": portfolio["spent"]},
                         occurred_at=frozen_at)
            result_payload = {"round_id": round_id, "status": "frozen",
                             "evidence_hash": snapshot,
                             "selected": portfolio["selected"], "spent": portfolio["spent"],
                             "remaining": portfolio["remaining"]}

            def create() -> tuple[str, str, dict[str, Any]]:
                return "funding_round", round_id, result_payload

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_round", payload=payload, create=create)

    def approve_portfolio(self, *, request_id: str, actor_id: str, round_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "round_id": round_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="approve_portfolio", payload=payload)
            if prior is not None:
                return prior
            round_row = self._round(connection, round_id)
            if round_row["status"] != "frozen":
                raise ConflictError("只有已冻结轮次才能批准投资组合")
            period = self._open_period(connection)

            score_rows = connection.execute(
                "SELECT * FROM project_scores WHERE round_id=?", (round_id,)
            ).fetchall()
            funded = [row for row in score_rows if row["decision"] == "funded"]
            deferred = [row for row in score_rows if row["decision"] != "funded"]
            for row in funded:
                project = connection.execute(
                    "SELECT * FROM projects WHERE project_id=?", (row["project_id"],)
                ).fetchone()
                allocation = self._allocation_row(connection, round_id, project["funding_level"])
                self._insert_ledger(connection, round_id=round_id, project_id=project["project_id"],
                                    period=period, allocation=allocation, entry_type="commit",
                                    amount=project["requested_amount"],
                                    detail={"rank": row["rank"], "score": row["total_score"]},
                                    actor_id=actor_id, occurred_at=self._now())
                connection.execute(
                    "UPDATE projects SET status='obligated',contract_amount=requested_amount,"
                    "decided_at=? WHERE project_id=?",
                    (self._now(), project["project_id"]),
                )
            for row in deferred:
                connection.execute(
                    "UPDATE projects SET status='deferred',decided_at=? WHERE project_id=?",
                    (self._now(), row["project_id"]),
                )
            connection.execute("UPDATE funding_rounds SET status='approved' WHERE round_id=?",
                               (round_id,))
            append_event(connection, actor_id=actor_id, action="portfolio.approved",
                         resource_type="funding_round", resource_id=round_id,
                         detail={"funded": [row["project_id"] for row in funded],
                                 "deferred": [row["project_id"] for row in deferred],
                                 "period_id": period["period_id"]},
                         occurred_at=self._now())

            def create() -> tuple[str, str, dict[str, Any]]:
                return "funding_round", round_id, {"round_id": round_id, "status": "approved",
                                                   "funded": len(funded),
                                                   "deferred": len(deferred)}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_portfolio", payload=payload, create=create)

    # ---- 紧急抢修限额例外与独立复核 ---------------------------------------

    def review_emergency(self, *, request_id: str, actor_id: str, project_id: str,
                         result: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "result": result, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="review_emergency", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            if not project["emergency"]:
                raise ValidationError("该项目不是紧急抢修申报")
            if project["status"] != "submitted":
                raise ConflictError("紧急项目已经完成复核")
            if result not in ("approved", "rejected"):
                raise ValidationError("result 必须是 approved/rejected")
            # 独立复核：复核人不能是申报人本人。
            if actor.actor_id == project["submitted_by"]:
                raise PermissionDenied("独立复核必须由申报人之外的人员完成")
            if note:
                note = self._text(note, "note", 500)
            else:
                note = ""
            review_id = uuid.uuid4().hex
            now = self._now()
            connection.execute(
                "INSERT INTO project_reviews(review_id,project_id,reviewer_id,result,note,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (review_id, project_id, actor_id, result, note, now),
            )
            if result == "approved":
                reserve = self._reserve_remaining(connection, project["round_id"],
                                                  project["funding_level"])
                if project["requested_amount"] > reserve:
                    raise ConflictError("紧急备用额度剩余不足，不能批准")
                period = self._open_period(connection)
                allocation = self._allocation_row(connection, project["round_id"],
                                                  project["funding_level"])
                obligated_total, _ = self._balances(connection, allocation["allocation_id"])
                if obligated_total + project["requested_amount"] > allocation["amount"]:
                    raise ConflictError("批准后承诺将超过该级资金总额度")
                self._insert_ledger(connection, round_id=project["round_id"], project_id=project_id,
                                    period=period, allocation=allocation, entry_type="commit",
                                    amount=project["requested_amount"],
                                    detail={"emergency": True, "review_id": review_id},
                                    actor_id=actor_id, occurred_at=now)
                connection.execute(
                    "UPDATE projects SET status='obligated',contract_amount=requested_amount,"
                    "decided_at=? WHERE project_id=?",
                    (now, project_id),
                )
            else:
                connection.execute("UPDATE projects SET status='rejected',decided_at=? WHERE project_id=?",
                                   (now, project_id))
            append_event(connection, actor_id=actor_id, action="emergency.reviewed",
                         resource_type="project", resource_id=project_id,
                         detail={"result": result, "review_id": review_id, "note": note},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "project", project_id, {"project_id": project_id, "status": result}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_emergency", payload=payload, create=create)

    # ---- 会计期间 -------------------------------------------------------

    def open_period(self, *, request_id: str, actor_id: str, period_id: str,
                    label: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "period_id": period_id, "label": label}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            period_id = self._identifier(period_id, "period_id")
            label = self._text(label, "label", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO accounting_periods(period_id,label,status,opened_by,opened_at) "
                        "VALUES(?,?, 'open',?,?)",
                        (period_id, label, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("会计期间编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="period.opened",
                             resource_type="accounting_period", resource_id=period_id,
                             detail={"label": label}, occurred_at=self._now())
                return "accounting_period", period_id, {"period_id": period_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="open_period", payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, period_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="close_period", payload=payload)
            if prior is not None:
                return prior
            period = connection.execute(
                "SELECT * FROM accounting_periods WHERE period_id=?", (period_id,)
            ).fetchone()
            if period is None:
                raise NotFoundError("会计期间不存在")
            if period["status"] == "closed":
                raise ConflictError("会计期间已经关账")
            now = self._now()
            connection.execute(
                "UPDATE accounting_periods SET status='closed',closed_by=?,closed_at=? WHERE period_id=?",
                (actor_id, now, period_id),
            )
            append_event(connection, actor_id=actor_id, action="period.closed",
                         resource_type="accounting_period", resource_id=period_id,
                         detail={"label": period["label"]}, occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "accounting_period", period_id, {"period_id": period_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="close_period", payload=payload, create=create)

    # ---- 执行：开工、里程碑核定、支付、保留金 -------------------------------

    def start_project(self, *, request_id: str, actor_id: str, project_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "highway")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="start_project", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            if project["status"] != "obligated":
                raise ConflictError("只有已承诺项目可以开工")
            now = self._now()
            connection.execute("UPDATE projects SET status='in_progress' WHERE project_id=?",
                               (project_id,))
            append_event(connection, actor_id=actor_id, action="project.started",
                         resource_type="project", resource_id=project_id,
                         detail={"round_id": project["round_id"]}, occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "project", project_id, {"project_id": project_id, "status": "in_progress"}

            return self._idempotent(connection, request_id=request_id,
                                    action="start_project", payload=payload, create=create)

    def verify_milestone(self, *, request_id: str, actor_id: str, project_id: str,
                         milestone_code: str, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id,
                   "milestone_code": milestone_code, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="verify_milestone", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            milestone = self._milestone(connection, project_id, milestone_code)
            if milestone["verified_by"]:
                raise ConflictError("里程碑已经核定")
            now = self._now()
            connection.execute(
                "UPDATE project_milestones SET verified_by=?,verified_at=? WHERE project_id=? AND code=?",
                (actor_id, now, project_id, milestone_code),
            )
            append_event(connection, actor_id=actor_id, action="milestone.verified",
                         resource_type="project", resource_id=project_id,
                         detail={"milestone_code": milestone_code, "note": note}, occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return ("milestone", f"{project_id}:{milestone_code}",
                        {"project_id": project_id, "milestone_code": milestone_code,
                         "verified": True})

            return self._idempotent(connection, request_id=request_id,
                                    action="verify_milestone", payload=payload, create=create)

    def pay_milestone(self, *, request_id: str, actor_id: str, project_id: str,
                      milestone_code: str, period_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id,
                   "milestone_code": milestone_code, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="pay_milestone", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            if project["status"] != "in_progress":
                raise ConflictError("项目未在执行中，不能支付里程碑")
            milestone = self._milestone(connection, project_id, milestone_code)
            if not milestone["verified_by"]:
                raise ConflictError("里程碑未经独立核定，不能支付")
            existing = connection.execute(
                "SELECT 1 FROM payments WHERE project_id=? AND milestone_code=?",
                (project_id, milestone_code),
            ).fetchone()
            if existing:
                raise ConflictError("该里程碑已经支付")
            period = self._period(connection, period_id)
            allocation = self._allocation_row(connection, project["round_id"],
                                              project["funding_level"])
            contract = project["contract_amount"]
            gross = contract * milestone["weight"] // 100
            retained = gross * milestone["retention_pct"] // 100
            net = gross - retained
            obligated, disbursed = self._balances(connection, allocation["allocation_id"])
            if disbursed + gross > obligated:
                raise ConflictError("支付超过已承诺额度")
            now = self._now()
            payment_id = uuid.uuid4().hex
            self._insert_ledger(connection, round_id=project["round_id"], project_id=project_id,
                                period=period, allocation=allocation, entry_type="pay", amount=net,
                                detail={"milestone_code": milestone_code, "gross": gross,
                                        "retained": retained, "payment_id": payment_id},
                                actor_id=actor_id, occurred_at=now)
            connection.execute(
                "INSERT INTO payments(payment_id,project_id,milestone_code,period_id,gross,"
                "retained,released,created_by,created_at) VALUES(?,?,?,?,?,?,0,?,?)",
                (payment_id, project_id, milestone_code, period_id, gross, retained, actor_id, now),
            )
            append_event(connection, actor_id=actor_id, action="milestone.paid",
                         resource_type="project", resource_id=project_id,
                         detail={"milestone_code": milestone_code, "gross": gross,
                                 "retained": retained, "paid": net, "period_id": period_id},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "payment", payment_id, {"payment_id": payment_id, "gross": gross,
                                               "retained": retained, "paid": net}

            return self._idempotent(connection, request_id=request_id,
                                    action="pay_milestone", payload=payload, create=create)

    def release_retention(self, *, request_id: str, actor_id: str, project_id: str,
                          period_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="release_retention", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            if project["status"] != "in_progress":
                raise ConflictError("只有执行中的项目可以结清保留金")
            milestones = connection.execute(
                "SELECT * FROM project_milestones WHERE project_id=? ORDER BY position",
                (project_id,),
            ).fetchall()
            unpaid = [row["code"] for row in milestones if not row["verified_by"]]
            if unpaid:
                raise ConflictError(f"尚有里程碑未核定: {unpaid}")
            payments = connection.execute(
                "SELECT * FROM payments WHERE project_id=?", (project_id,),
            ).fetchall()
            if len(payments) != len(milestones):
                raise ConflictError("尚有里程碑未支付，不能释放保留金")
            retained_total = sum(row["retained"] - row["released"] for row in payments)
            period = self._period(connection, period_id)
            allocation = self._allocation_row(connection, project["round_id"],
                                              project["funding_level"])
            now = self._now()
            if retained_total > 0:
                self._insert_ledger(connection, round_id=project["round_id"], project_id=project_id,
                                    period=period, allocation=allocation, entry_type="release",
                                    amount=retained_total, detail={"retention": True},
                                    actor_id=actor_id, occurred_at=now)
                connection.execute("UPDATE payments SET released=retained WHERE project_id=?",
                                   (project_id,))
            # 取整差额形成的承诺结余自动回收，结清后该项目承诺归零。
            project_obligated, project_disbursed = self._balances_for_project(
                connection, allocation["allocation_id"], project_id
            )
            residual = project_obligated - project_disbursed
            if residual > 0:
                self._insert_ledger(connection, round_id=project["round_id"], project_id=project_id,
                                    period=period, allocation=allocation, entry_type="recover",
                                    amount=residual, detail={"auto_settle": True},
                                    actor_id=actor_id, occurred_at=now)
            connection.execute("UPDATE projects SET status='settled' WHERE project_id=?",
                               (project_id,))
            append_event(connection, actor_id=actor_id, action="project.settled",
                         resource_type="project", resource_id=project_id,
                         detail={"retained_released": retained_total, "residual_recovered": residual,
                                 "period_id": period_id}, occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "project", project_id, {"project_id": project_id, "status": "settled",
                                               "retained_released": retained_total,
                                               "residual_recovered": residual}

            return self._idempotent(connection, request_id=request_id,
                                    action="release_retention", payload=payload, create=create)

    def adjust_contract(self, *, request_id: str, actor_id: str, project_id: str,
                        new_amount: int, reason: str, period_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "new_amount": new_amount,
                   "reason": reason, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="adjust_contract", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            if project["status"] not in ("obligated", "in_progress"):
                raise ConflictError("当前状态不允许变更合同")
            new_amount = self._money(new_amount, "new_amount")
            reason = self._text(reason, "reason", 300)
            period = self._period(connection, period_id)
            allocation = self._allocation_row(connection, project["round_id"],
                                              project["funding_level"])
            paid_gross = connection.execute(
                "SELECT COALESCE(SUM(gross),0) AS amount FROM payments WHERE project_id=?",
                (project_id,),
            ).fetchone()["amount"]
            if new_amount < paid_gross:
                raise ValidationError("变更后金额不能低于已支付金额")
            obligated, disbursed = self._balances(connection, allocation["allocation_id"])
            delta = new_amount - project["contract_amount"]
            if obligated + delta > allocation["amount"]:
                raise ConflictError("变更后承诺超过该级资金总额度")
            now = self._now()
            if delta:
                self._insert_ledger(connection, round_id=project["round_id"], project_id=project_id,
                                    period=period, allocation=allocation, entry_type="adjust",
                                    amount=delta, detail={"reason": reason},
                                    actor_id=actor_id, occurred_at=now)
            connection.execute("UPDATE projects SET contract_amount=? WHERE project_id=?",
                               (new_amount, project_id))
            append_event(connection, actor_id=actor_id, action="contract.adjusted",
                         resource_type="project", resource_id=project_id,
                         detail={"old_amount": project["contract_amount"], "new_amount": new_amount,
                                 "delta": delta, "reason": reason, "period_id": period_id},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "project", project_id, {"project_id": project_id,
                                               "contract_amount": new_amount, "delta": delta}

            return self._idempotent(connection, request_id=request_id,
                                    action="adjust_contract", payload=payload, create=create)

    def cancel_project(self, *, request_id: str, actor_id: str, project_id: str,
                       reason: str, period_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "reason": reason,
                   "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="cancel_project", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            if project["status"] not in ("obligated", "in_progress"):
                raise ConflictError("当前状态不允许取消")
            reason = self._text(reason, "reason", 300)
            period = self._period(connection, period_id)
            allocation = self._allocation_row(connection, project["round_id"],
                                              project["funding_level"])
            obligated, disbursed = self._balances_for_project(
                connection, allocation["allocation_id"], project_id
            )
            release = obligated - disbursed
            now = self._now()
            if release > 0:
                self._insert_ledger(connection, round_id=project["round_id"], project_id=project_id,
                                    period=period, allocation=allocation, entry_type="recover",
                                    amount=release, detail={"cancellation": True, "reason": reason},
                                    actor_id=actor_id, occurred_at=now)
            connection.execute("UPDATE projects SET status='cancelled' WHERE project_id=?",
                               (project_id,))
            append_event(connection, actor_id=actor_id, action="project.cancelled",
                         resource_type="project", resource_id=project_id,
                         detail={"reason": reason, "released": release, "period_id": period_id},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "project", project_id, {"project_id": project_id, "status": "cancelled",
                                               "released": release}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_project", payload=payload, create=create)

    def recover_savings(self, *, request_id: str, actor_id: str, project_id: str,
                        amount: int, reason: str, period_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "project_id": project_id, "amount": amount,
                   "reason": reason, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "finance")
            prior = self._replay_if_seen(
                connection, request_id=request_id, action="recover_savings", payload=payload)
            if prior is not None:
                return prior
            project = self._project(connection, project_id)
            if project["status"] not in ("obligated", "in_progress"):
                raise ConflictError("已结清或取消的项目通过结清流程回收结余")
            amount = self._money(amount, "amount")
            reason = self._text(reason, "reason", 300)
            period = self._period(connection, period_id)
            allocation = self._allocation_row(connection, project["round_id"],
                                              project["funding_level"])
            obligated, disbursed = self._balances_for_project(
                connection, allocation["allocation_id"], project_id
            )
            available = obligated - disbursed
            if amount > available:
                raise ValidationError("回收金额超过该项目尚未支付的承诺余额")
            now = self._now()
            self._insert_ledger(connection, round_id=project["round_id"], project_id=project_id,
                                period=period, allocation=allocation, entry_type="recover",
                                amount=amount, detail={"reason": reason},
                                actor_id=actor_id, occurred_at=now)
            append_event(connection, actor_id=actor_id, action="savings.recovered",
                         resource_type="project", resource_id=project_id,
                         detail={"amount": amount, "reason": reason, "period_id": period_id},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "project", project_id, {"project_id": project_id, "recovered": amount}

            return self._idempotent(connection, request_id=request_id,
                                    action="recover_savings", payload=payload, create=create)

    # ---- 查询：组合总览、资金全链路、政策换版模拟 ---------------------------

    def get_round(self, round_id: str) -> dict[str, Any]:
        connection = self.database.connection
        round_row = self._round(connection, round_id)
        allocations = []
        for row in connection.execute(
            "SELECT * FROM round_allocations WHERE round_id=? ORDER BY funding_level",
            (round_id,),
        ).fetchall():
            obligated, disbursed = self._balances(connection, row["allocation_id"])
            allocations.append({"allocation_id": row["allocation_id"],
                                "funding_level": row["funding_level"],
                                "amount": row["amount"],
                                "emergency_reserve": row["emergency_reserve"],
                                "obligated": obligated, "disbursed": disbursed,
                                "remaining": row["amount"] - obligated})
        projects = []
        for row in connection.execute(
            "SELECT * FROM projects WHERE round_id=? ORDER BY COALESCE(rank,999999), project_id",
            (round_id,),
        ).fetchall():
            item = self._project_view(row)
            item["status"] = row["status"]
            score = connection.execute(
                "SELECT * FROM project_scores WHERE project_id=?", (row["project_id"],)
            ).fetchone()
            if score:
                item["score"] = {"total": score["total_score"], "rank": score["rank"],
                                 "decision": score["decision"],
                                 "reasons": json.loads(score["reasons_json"]),
                                 "factors": json.loads(score["factor_json"]),
                                 "basis": json.loads(score["basis_json"]),
                                 "factor_names": FACTOR_NAMES,
                                 "policy_id": score["policy_id"]}
            projects.append(item)
        flags = [{"project_a": row["project_a"], "project_b": row["project_b"],
                  "flag_type": row["flag_type"], "blocking": bool(row["blocking"]),
                  "phase": row["phase"], "detail": json.loads(row["detail_json"])}
                 for row in connection.execute(
                     "SELECT * FROM project_flags WHERE round_id=? ORDER BY created_at,flag_id",
                     (round_id,)).fetchall()]
        return {"round": {"round_id": round_row["round_id"], "name": round_row["name"],
                          "deadline_at": round_row["deadline_at"], "status": round_row["status"],
                          "bound_policy_id": round_row["bound_policy_id"],
                          "frozen_year": round_row["frozen_year"],
                          "evidence_hash": round_row["evidence_hash"],
                          "frozen_at": round_row["frozen_at"]},
                "allocations": allocations, "projects": projects, "flags": flags}

    def trace_funds(self, project_id: str) -> dict[str, Any]:
        """返回一笔资金从排名、占用到结算的全过程记录。"""

        connection = self.database.connection
        project = self._project(connection, project_id)
        score = connection.execute(
            "SELECT * FROM project_scores WHERE project_id=?", (project_id,)
        ).fetchone()
        ranking = None
        if score:
            policy = connection.execute(
                "SELECT version_tag FROM scoring_policies WHERE policy_id=?",
                (score["policy_id"],),
            ).fetchone()
            ranking = {"rank": score["rank"], "total_score": score["total_score"],
                       "decision": score["decision"],
                       "reasons": json.loads(score["reasons_json"]),
                       "factors": json.loads(score["factor_json"]),
                       "factor_names": FACTOR_NAMES,
                       "policy_version_tag": policy["version_tag"] if policy else None}
        flags = [{"other": row["project_b"] if row["project_a"] == project_id else row["project_a"],
                  "flag_type": row["flag_type"], "phase": row["phase"]}
                 for row in connection.execute(
                     "SELECT * FROM project_flags WHERE project_a=? OR project_b=?",
                     (project_id, project_id)).fetchall()]
        milestones = [{"code": row["code"], "title": row["title"], "weight": row["weight"],
                       "retention_pct": row["retention_pct"], "verified_by": row["verified_by"],
                       "verified_at": row["verified_at"]}
                      for row in connection.execute(
                          "SELECT * FROM project_milestones WHERE project_id=? ORDER BY position",
                          (project_id,)).fetchall()]
        payments = [{"payment_id": row["payment_id"], "milestone_code": row["milestone_code"],
                     "period_id": row["period_id"], "gross": row["gross"],
                     "retained": row["retained"], "released": row["released"]}
                    for row in connection.execute(
                        "SELECT * FROM payments WHERE project_id=? ORDER BY created_at",
                        (project_id,)).fetchall()]
        ledger = [{"entry_id": row["entry_id"], "period_id": row["period_id"],
                   "entry_type": row["entry_type"], "amount": row["amount"],
                   "funding_level": row["funding_level"],
                   "detail": json.loads(row["detail_json"]),
                   "created_by": row["created_by"], "created_at": row["created_at"]}
                  for row in connection.execute(
                      "SELECT * FROM ledger_entries WHERE project_id=? ORDER BY rowid",
                      (project_id,)).fetchall()]
        reviews = [{"review_id": row["review_id"], "reviewer_id": row["reviewer_id"],
                    "result": row["result"], "note": row["note"], "created_at": row["created_at"]}
                   for row in connection.execute(
                       "SELECT * FROM project_reviews WHERE project_id=? ORDER BY created_at",
                       (project_id,)).fetchall()]
        totals = {"committed": sum(e["amount"] for e in ledger
                                   if e["entry_type"] in ("commit", "adjust")),
                  "recovered": sum(e["amount"] for e in ledger
                                   if e["entry_type"] == "recover"),
                  "retention_released": sum(e["amount"] for e in ledger
                                            if e["entry_type"] == "release"),
                  "disbursed": sum(e["amount"] for e in ledger
                                   if e["entry_type"] in ("pay", "release"))}
        totals["open_commitment"] = totals["committed"] - totals["recovered"] - totals["disbursed"]
        return {"project": self._project_view(project) | {"status": project["status"],
                                                          "contract_amount": project["contract_amount"],
                                                          "submitted_by": project["submitted_by"]},
                "round_id": project["round_id"], "ranking": ranking, "flags": flags,
                "milestones": milestones, "reviews": reviews, "payments": payments,
                "ledger": ledger, "totals": totals}

    def simulate_policy(self, *, round_id: str, policy_version_tag: str) -> dict[str, Any]:
        """用冻结证据按新政策重算未批准项目的组合，不落账、不改历史。"""

        connection = self.database.connection
        round_row = self._round(connection, round_id)
        if round_row["status"] not in ("frozen", "approved", "closed"):
            raise ConflictError("轮次尚未冻结，无法模拟政策换版")
        candidate_policy = self._policy_by_tag(connection, policy_version_tag)
        weights = json.loads(candidate_policy["spec_json"])["weights"]
        old_policy = connection.execute(
            "SELECT * FROM scoring_policies WHERE policy_id=?",
            (round_row["bound_policy_id"],),
        ).fetchone()

        project_rows = connection.execute(
            "SELECT * FROM projects WHERE round_id=? AND emergency=0 ORDER BY project_id",
            (round_id,),
        ).fetchall()
        rescored = []
        for row in project_rows:
            evidence = json.loads(row["evidence_json"])
            result = score_project(evidence, weights)
            rescored.append((row, result))
        rescored.sort(key=lambda pair: (-pair[1]["total_score"], pair[0]["project_id"]))
        budgets = self._discretionary_budgets(connection, round_id)
        conflicts = self._conflict_map(connection, round_id)
        ranked = [{
            "project_id": row["project_id"], "funding_level": row["funding_level"],
            "requested_amount": row["requested_amount"], "total_score": result["total_score"],
            "rank": rank, "depends_on": row["depends_on"],
            "conflicts": conflicts.get(row["project_id"], ()),
        } for rank, (row, result) in enumerate(rescored, start=1)]
        portfolio = select_portfolio(ranked, budgets)
        new_decisions = {item["project_id"]: item for item in portfolio["decisions"]}
        new_ranks = {item["project_id"]: item["rank"] for item in portfolio["decisions"]}
        new_scores = {row["project_id"]: result["total_score"] for row, result in rescored}

        comparison = []
        for row in project_rows:
            old = connection.execute(
                "SELECT * FROM project_scores WHERE project_id=?", (row["project_id"],)
            ).fetchone()
            new = new_decisions[row["project_id"]]
            comparison.append({
                "project_id": row["project_id"], "title": row["title"],
                "old_rank": old["rank"], "new_rank": new_ranks[row["project_id"]],
                "old_score": old["total_score"], "new_score": new_scores[row["project_id"]],
                "old_decision": old["decision"], "new_decision": new["decision"],
                "new_reasons": new["reasons"],
                "actual_status": row["status"],
                "changed": old["decision"] != new["decision"] or old["rank"] != new_ranks[row["project_id"]],
            })
        return {
            "round_id": round_id, "historical_policy": old_policy["version_tag"],
            "candidate_policy": candidate_policy["version_tag"],
            "note": "历史批准决定保持不变，本结果仅为未批准项目的换版影响测算",
            "spent": portfolio["spent"], "remaining": portfolio["remaining"],
            "projects": comparison,
            "decision_changes": [item["project_id"] for item in comparison
                                 if item["old_decision"] != item["new_decision"]],
        }

    # ---- 内部辅助 -------------------------------------------------------

    def _replay_if_seen(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> WriteReceipt | None:
        """状态流转动作在改变前置状态前先做幂等回放，保证重发得到原回执。"""

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _parse_ts(self, value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("时间戳必须是 ISO 8601 格式") from exc
        if parsed.tzinfo is None:
            raise ValidationError("时间戳必须包含时区")
        return parsed

    def _discretionary_budgets(self, connection, round_id: str) -> dict[str, int]:
        """常规组合可分配额度 = 各级总额度 − 紧急备用额度。"""

        allocations = self._allocations(connection, round_id)
        return {level: allocations[level]["amount"] - allocations[level]["emergency_reserve"]
                for level in FUNDING_LEVELS}

    def _span(self, start_km: Any, end_km: Any) -> tuple[float, float]:
        try:
            start_km, end_km = float(start_km), float(end_km)
        except (TypeError, ValueError) as exc:
            raise ValidationError("桩号必须是数字") from exc
        if not 0 <= start_km < end_km:
            raise ValidationError("桩号区间无效：要求 0 <= start_km < end_km")
        return start_km, end_km

    def _money(self, value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数（元）")
        return value

    def _validate_milestones(self, milestones: Any) -> list[dict[str, Any]]:
        if not isinstance(milestones, list) or not milestones:
            raise ValidationError("milestones 必须是非空数组")
        rows: list[dict[str, Any]] = []
        total_weight = 0
        seen: set[str] = set()
        for item in milestones:
            if not isinstance(item, dict):
                raise ValidationError("里程碑必须是对象")
            code = self._identifier(item.get("code", ""), "milestone.code")
            if code in seen:
                raise ValidationError("里程碑编号重复")
            seen.add(code)
            title = self._text(item.get("title", ""), "milestone.title")
            weight = item.get("weight")
            retention = item.get("retention_pct", 0)
            if not isinstance(weight, int) or isinstance(weight, bool) or not 1 <= weight <= 100:
                raise ValidationError("里程碑 weight 必须是 1~100 的整数")
            if not isinstance(retention, int) or isinstance(retention, bool) or not 0 <= retention <= 100:
                raise ValidationError("retention_pct 必须是 0~100 的整数")
            planned_date = item.get("planned_date")
            if planned_date is not None:
                planned_date = self._text(planned_date, "milestone.planned_date", 40)
            total_weight += weight
            rows.append({"code": code, "title": title, "weight": weight,
                         "retention_pct": retention, "planned_date": planned_date})
        if total_weight != 100:
            raise ValidationError("里程碑权重之和必须为 100")
        return rows

    def _round(self, connection, round_id: str):
        row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?", (round_id,)).fetchone()
        if row is None:
            raise NotFoundError("申报轮次不存在")
        return row

    def _project(self, connection, project_id: str):
        row = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return row

    def _milestone(self, connection, project_id: str, code: str):
        row = connection.execute(
            "SELECT * FROM project_milestones WHERE project_id=? AND code=?", (project_id, code)
        ).fetchone()
        if row is None:
            raise NotFoundError("里程碑不存在")
        return row

    def _period(self, connection, period_id: str):
        row = connection.execute(
            "SELECT * FROM accounting_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("会计期间不存在")
        if row["status"] == "closed":
            raise ConflictError("会计期间已关账，不能在该期间记新账")
        return row

    def _open_period(self, connection):
        row = connection.execute(
            "SELECT * FROM accounting_periods WHERE status='open' ORDER BY opened_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ConflictError("尚未打开任何会计期间，不能形成承诺或支付")
        return row

    def _allocation_row(self, connection, round_id: str, funding_level: str):
        row = connection.execute(
            "SELECT * FROM round_allocations WHERE round_id=? AND funding_level=?",
            (round_id, funding_level),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{funding_level} 级资金额度尚未下达")
        return row

    def _allocations(self, connection, round_id: str) -> dict[str, dict[str, int]]:
        result = {level: {"amount": 0, "emergency_reserve": 0} for level in FUNDING_LEVELS}
        for row in connection.execute("SELECT * FROM round_allocations WHERE round_id=?", (round_id,)):
            result[row["funding_level"]] = {"amount": row["amount"],
                                            "emergency_reserve": row["emergency_reserve"]}
        return result

    def _balances(self, connection, allocation_id: str) -> tuple[int, int]:
        """返回某额度（全部项目合计）的已承诺与已拨付。

        commit 增加承诺，adjust 为带符号增减，recover 收回未用承诺；
        pay 与 release（保留金拨付）构成已拨付，但不冲减承诺。
        """

        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN entry_type='commit' THEN amount "
            "WHEN entry_type='adjust' THEN amount "
            "WHEN entry_type='recover' THEN -amount ELSE 0 END),0) AS obligated, "
            "COALESCE(SUM(CASE WHEN entry_type IN ('pay','release') THEN amount ELSE 0 END),0) AS disbursed "
            "FROM ledger_entries WHERE allocation_id=?",
            (allocation_id,),
        ).fetchone()
        return row["obligated"], row["disbursed"]

    def _balances_for_project(self, connection, allocation_id: str, project_id: str) -> tuple[int, int]:
        """返回单个项目在共享额度上的已承诺与已拨付。"""

        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN entry_type='commit' THEN amount "
            "WHEN entry_type='adjust' THEN amount "
            "WHEN entry_type='recover' THEN -amount ELSE 0 END),0) AS obligated, "
            "COALESCE(SUM(CASE WHEN entry_type IN ('pay','release') THEN amount ELSE 0 END),0) AS disbursed "
            "FROM ledger_entries WHERE allocation_id=? AND project_id=?",
            (allocation_id, project_id),
        ).fetchone()
        return row["obligated"], row["disbursed"]

    def _reserve_remaining(self, connection, round_id: str, funding_level: str) -> int:
        allocation = connection.execute(
            "SELECT * FROM round_allocations WHERE round_id=? AND funding_level=?",
            (round_id, funding_level),
        ).fetchone()
        if allocation is None:
            raise NotFoundError(f"{funding_level} 级资金额度尚未下达")
        used = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN l.entry_type IN ('commit','adjust') THEN l.amount "
            "WHEN l.entry_type='recover' THEN -l.amount ELSE 0 END),0) AS amount "
            "FROM ledger_entries l JOIN projects p ON p.project_id=l.project_id "
            "WHERE l.allocation_id=? AND p.emergency=1",
            (allocation["allocation_id"],),
        ).fetchone()["amount"]
        return allocation["emergency_reserve"] - used

    def _insert_ledger(self, connection, *, round_id, project_id, period, allocation,
                       entry_type: str, amount: int, detail: dict[str, Any],
                       actor_id: str, occurred_at: str) -> None:
        if period["status"] == "closed":
            raise ConflictError("会计期间已关账，不能在该期间记新账")
        connection.execute(
            "INSERT INTO ledger_entries(entry_id,round_id,project_id,period_id,allocation_id,"
            "funding_level,entry_type,amount,detail_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, round_id, project_id, period["period_id"],
             allocation["allocation_id"], allocation["funding_level"], entry_type, amount,
             canonical_json(detail), actor_id, occurred_at),
        )

    def _would_cycle(self, connection, round_id: str, parent_id: str, new_id: str) -> bool:
        seen = set()
        current = parent_id
        while current:
            if current == new_id or current in seen:
                return True
            seen.add(current)
            row = connection.execute(
                "SELECT depends_on FROM projects WHERE round_id=? AND project_id=?",
                (round_id, current),
            ).fetchone()
            if row is None:
                return False
            current = row["depends_on"]
        return False

    def _project_view(self, row) -> dict[str, Any]:
        return {"project_id": row["project_id"], "round_id": row["round_id"],
                "external_key": row["external_key"], "title": row["title"],
                "organization_id": row["organization_id"], "segment_id": row["segment_id"],
                "route_code": row["route_code"], "start_km": row["start_km"],
                "end_km": row["end_km"], "window_start": row["window_start"],
                "window_end": row["window_end"], "funding_level": row["funding_level"],
                "requested_amount": row["requested_amount"], "depends_on": row["depends_on"],
                "emergency": bool(row["emergency"]),
                "evidence_hash": row["evidence_hash"]}

    def _insert_pair_flags(self, connection, round_id: str, new_project_id: str) -> None:
        """新申报入库后，立即识别它与同轮既有常规申报之间的冲突。"""

        rows = connection.execute(
            "SELECT * FROM projects WHERE round_id=? AND emergency=0 AND project_id!=?",
            (round_id, new_project_id),
        ).fetchall()
        new_row = connection.execute("SELECT * FROM projects WHERE project_id=?",
                                     (new_project_id,)).fetchone()
        projects = [self._project_view(row) for row in rows] + [self._project_view(new_row)]
        for flag in detect_flags(projects):
            if new_project_id not in (flag.project_a, flag.project_b):
                continue
            self._insert_flag(connection, round_id, flag, "submission")

    def _persist_flags(self, connection, round_id: str, flags) -> None:
        for flag in flags:
            exists = connection.execute(
                "SELECT 1 FROM project_flags WHERE round_id=? AND project_a=? AND project_b=? "
                "AND flag_type=?",
                (round_id, flag.project_a, flag.project_b, flag.flag_type),
            ).fetchone()
            if not exists:
                self._insert_flag(connection, round_id, flag, "frozen")

    def _insert_flag(self, connection, round_id: str, flag, phase: str) -> None:
        connection.execute(
            "INSERT INTO project_flags(flag_id,round_id,project_a,project_b,flag_type,blocking,"
            "phase,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, round_id, flag.project_a, flag.project_b, flag.flag_type,
             1 if flag.blocking else 0, phase, canonical_json(flag.detail), self._now()),
        )

    def _conflict_map(self, connection, round_id: str) -> dict[str, tuple[str, ...]]:
        result: dict[str, set[str]] = {}
        for row in connection.execute(
            "SELECT project_a,project_b FROM project_flags WHERE round_id=? AND blocking=1",
            (round_id,),
        ):
            result.setdefault(row["project_a"], set()).add(row["project_b"])
            result.setdefault(row["project_b"], set()).add(row["project_a"])
        return {key: tuple(value) for key, value in result.items()}

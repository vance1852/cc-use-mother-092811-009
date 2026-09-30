"""养护资金决策与执行服务。

覆盖：路段与多维证据登记、版本化评分政策、轮次冻结（证据快照+政策版本）、
组合排名与各级资金约束分配、重复申报/拆项/施工窗口互斥识别、紧急抢修限额例外
与独立复核、里程碑承诺与支付、变更/取消/结余回收、会计期间关账、政策换版模拟、
资金全链路追踪。所有写入走幂等回执与哈希审计链。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from typing import Any, Callable, Iterable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .funding_storage import initialize_funding
from .scoring import (
    DEFAULT_POLICY,
    EVIDENCE_DIMENSIONS,
    score_application,
    validate_policy,
)
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
FUNDING_LEVELS = ("central", "provincial", "county")
WATERFALL = ("county", "provincial", "central")  # 县级先兜底，不足逐级申请

EVIDENCE_SCHEMA: dict[str, dict[str, type]] = {
    "condition": {"pci": (int, float)},
    "population": {"served_population": int, "sole_access": bool},
    "alternatives": {"alternative_routes": int},
    "hazard": {"hazard_level": (int, float)},
    "traffic": {"aadt": int},
}
HISTORY_FLAGS = {"repeated_repair", "last_repair_within_year"}


def _parse_day(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc
    return value


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start < b_end and b_start < a_end


def _windows_overlap(a: tuple[str | None, str | None], b: tuple[str | None, str | None]) -> bool:
    if not all([a[0], a[1], b[0], b[1]]):
        return False
    return a[0] < b[1] and b[0] < a[1]


class FundingService:
    """养护资金决策与执行的领域服务。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        initialize_funding(database)
        self._ensure_default_policy()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _amount(self, value: Any, field: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{field} 必须是正数（万元）")
        return round(float(value), 2)

    def _actor(self, connection, actor_id: str, roles: Iterable[str] | None = None):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        if roles is not None and row["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")
        return row

    def _early_replay(self, connection, *, request_id: str, action: str,
                      payload: dict[str, Any]) -> dict[str, Any] | None:
        """在业务状态校验之前解析幂等重放，避免因状态已迁移而拒绝重发。"""

        request_id = self._id(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != digest(payload):
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        return None

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _open_period(self, connection):
        return connection.execute(
            "SELECT * FROM accounting_periods WHERE status='open' ORDER BY opened_at DESC, period_id DESC LIMIT 1"
        ).fetchone()

    def _require_open_period(self, connection):
        period = self._open_period(connection)
        if period is None:
            raise ConflictError("当前没有开放的会计期间，不能登记资金台账")
        return period

    def _ensure_default_policy(self) -> None:
        with self.database.transaction(immediate=True) as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM scoring_policies").fetchone()["c"]
            if not count:
                connection.execute(
                    "INSERT INTO scoring_policies(policy_version,rules_json,rules_hash,status,"
                    "created_by,created_at) VALUES(?,?,?,'active','system',?)",
                    (DEFAULT_POLICY["version"], canonical_json(DEFAULT_POLICY),
                     digest(DEFAULT_POLICY), self._now()),
                )

    # ------------------------------------------------------------------ 路段与证据

    def register_segment(self, *, request_id: str, actor_id: str, segment_id: str,
                         route_code: str, name: str, length_km: float,
                         chainage_start: float, chainage_end: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "segment_id": segment_id, "route_code": route_code,
                   "name": name, "length_km": length_km, "chainage_start": chainage_start,
                   "chainage_end": chainage_end}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id, {"admin", "highway"})
            segment_id = self._id(segment_id, "segment_id")
            route_code = self._text(route_code, "route_code", 40)
            name = self._text(name, "name")
            if not isinstance(length_km, (int, float)) or length_km <= 0:
                raise ValidationError("length_km 必须是正数")
            if not isinstance(chainage_start, (int, float)) or not isinstance(chainage_end, (int, float)):
                raise ValidationError("桩号必须是数字")
            if chainage_end <= chainage_start:
                raise ValidationError("chainage_end 必须大于 chainage_start")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO road_segments(segment_id,organization_id,route_code,name,"
                        "length_km,chainage_start,chainage_end,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (segment_id, actor["organization_id"], route_code, name, float(length_km),
                         float(chainage_start), float(chainage_end), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("路段编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="funding.segment.registered",
                            resource_type="road_segment", resource_id=segment_id,
                            detail={"route_code": route_code, "name": name})
                return "road_segment", segment_id, {"segment_id": segment_id}

            return self._idempotent(connection, request_id=request_id, action="funding.register_segment",
                                    payload=payload, create=create)

    def add_evidence(self, *, request_id: str, actor_id: str, segment_id: str,
                     dimension: str, payload: dict[str, Any], effective_at: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        body = {"actor_id": actor_id, "segment_id": segment_id, "dimension": dimension,
                "payload": payload, "effective_at": effective_at}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "highway", "operator"})
            segment_id = self._id(segment_id, "segment_id")
            if dimension not in EVIDENCE_DIMENSIONS:
                raise ValidationError(f"证据维度必须是: {','.join(EVIDENCE_DIMENSIONS)}")
            effective_at = self._text(effective_at, "effective_at", 40)
            self._validate_evidence_payload(dimension, payload)
            if connection.execute("SELECT 1 FROM road_segments WHERE segment_id=?",
                                  (segment_id,)).fetchone() is None:
                raise NotFoundError("路段不存在")
            evidence_id = uuid.uuid4().hex
            payload_json = canonical_json(payload)

            def create():
                connection.execute(
                    "INSERT INTO segment_evidence(evidence_id,segment_id,dimension,payload_json,"
                    "payload_hash,effective_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (evidence_id, segment_id, dimension, payload_json, digest(payload),
                     effective_at, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="funding.evidence.added",
                            resource_type="segment_evidence", resource_id=evidence_id,
                            detail={"segment_id": segment_id, "dimension": dimension,
                                    "effective_at": effective_at, "payload_hash": digest(payload)})
                return "segment_evidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent(connection, request_id=request_id, action="funding.add_evidence",
                                    payload=body, create=create)

    def _validate_evidence_payload(self, dimension: str, payload: dict[str, Any]) -> None:
        if dimension == "maintenance_history":
            for flag in HISTORY_FLAGS:
                if flag in payload and not isinstance(payload[flag], bool):
                    raise ValidationError(f"{flag} 必须是布尔值")
            if "repair_count_3y" in payload and (
                not isinstance(payload["repair_count_3y"], int) or payload["repair_count_3y"] < 0
            ):
                raise ValidationError("repair_count_3y 必须是非负整数")
            return
        schema = EVIDENCE_SCHEMA.get(dimension, {})
        for key, expected_type in schema.items():
            if key not in payload:
                raise ValidationError(f"{dimension} 证据缺少字段 {key}")
            value = payload[key]
            if expected_type is bool:
                if not isinstance(value, bool):
                    raise ValidationError(f"{key} 必须是布尔值")
            elif isinstance(value, bool) or not isinstance(value, expected_type):
                raise ValidationError(f"{key} 类型不正确")
        if dimension == "condition" and not 0 <= payload["pci"] <= 100:
            raise ValidationError("pci 必须在 0-100 之间")
        if dimension == "hazard" and not 0 <= payload["hazard_level"] <= 100:
            raise ValidationError("hazard_level 必须在 0-100 之间")
        if dimension == "population" and payload["served_population"] < 0:
            raise ValidationError("served_population 不能为负")
        if dimension == "alternatives" and payload["alternative_routes"] < 0:
            raise ValidationError("alternative_routes 不能为负")
        if dimension == "traffic" and payload["aadt"] < 0:
            raise ValidationError("aadt 不能为负")

    # ------------------------------------------------------------------ 评分政策

    def register_policy(self, *, request_id: str, actor_id: str,
                        rules: dict[str, Any], activate: bool = True) -> dict[str, Any]:
        body = {"actor_id": actor_id, "rules": rules, "activate": activate}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            if not isinstance(rules, dict) or "version" not in rules:
                raise ValidationError("政策规则必须包含 version")
            version = self._id(str(rules["version"]), "policy_version")
            try:
                validate_policy(rules)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

            def create():
                if connection.execute("SELECT 1 FROM scoring_policies WHERE policy_version=?",
                                      (version,)).fetchone():
                    raise ConflictError("政策版本已经存在，评分规则只能换版不能修改")
                status = "active" if activate else "draft"
                if activate:
                    connection.execute(
                        "UPDATE scoring_policies SET status='retired' WHERE status='active'"
                    )
                connection.execute(
                    "INSERT INTO scoring_policies(policy_version,rules_json,rules_hash,status,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (version, canonical_json(rules), digest(rules), status, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="funding.policy.registered",
                            resource_type="scoring_policy", resource_id=version,
                            detail={"version": version, "activate": activate,
                                    "rules_hash": digest(rules)})
                return "scoring_policy", version, {"policy_version": version, "status": status}

            return self._idempotent(connection, request_id=request_id, action="funding.register_policy",
                                    payload=body, create=create)

    # ------------------------------------------------------------------ 轮次与资金盘子

    def create_round(self, *, request_id: str, actor_id: str, round_id: str, name: str,
                     deadline_at: str, emergency_quota: float = 0) -> dict[str, Any]:
        body = {"actor_id": actor_id, "round_id": round_id, "name": name,
                "deadline_at": deadline_at, "emergency_quota": emergency_quota}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            round_id = self._id(round_id, "round_id")
            name = self._text(name, "name")
            deadline_at = self._text(deadline_at, "deadline_at", 40)
            if not isinstance(emergency_quota, (int, float)) or emergency_quota < 0:
                raise ValidationError("emergency_quota 必须是非负数")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO funding_rounds(round_id,name,deadline_at,emergency_quota,status,"
                        "created_by,created_at) VALUES(?,?,?,?, 'open', ?,?)",
                        (round_id, name, deadline_at, float(emergency_quota),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("轮次编号已经存在") from exc
                if emergency_quota:
                    connection.execute(
                        "INSERT INTO funding_envelopes(envelope_id,round_id,level,amount,created_at)"
                        " VALUES(?,?, 'emergency', ?,?)",
                        (uuid.uuid4().hex, round_id, float(emergency_quota), self._now()),
                    )
                self._audit(connection, actor_id=actor_id, action="funding.round.created",
                            resource_type="funding_round", resource_id=round_id,
                            detail={"name": name, "deadline_at": deadline_at,
                                    "emergency_quota": emergency_quota})
                return "funding_round", round_id, {"round_id": round_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id, action="funding.create_round",
                                    payload=body, create=create)

    def set_envelope(self, *, request_id: str, actor_id: str, round_id: str,
                     level: str, amount: float) -> dict[str, Any]:
        body = {"actor_id": actor_id, "round_id": round_id, "level": level, "amount": amount}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            round_id = self._id(round_id, "round_id")
            if level not in FUNDING_LEVELS:
                raise ValidationError(f"level 必须是: {','.join(FUNDING_LEVELS)}")
            if not isinstance(amount, (int, float)) or amount < 0:
                raise ValidationError("amount 必须是非负数")
            round_row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?",
                                           (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("轮次不存在")
            if round_row["status"] != "open":
                raise ConflictError("轮次冻结后不能调整资金盘子")

            def create():
                envelope_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO funding_envelopes(envelope_id,round_id,level,amount,created_at)"
                    " VALUES(?,?,?,?,?) ON CONFLICT(round_id,level) DO UPDATE SET amount=excluded.amount",
                    (envelope_id, round_id, level, float(amount), self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="funding.envelope.set",
                            resource_type="funding_envelope", resource_id=round_id,
                            detail={"level": level, "amount": amount})
                return "funding_envelope", f"{round_id}:{level}", {"round_id": round_id, "level": level}

            return self._idempotent(connection, request_id=request_id, action="funding.set_envelope",
                                    payload=body, create=create)

    # ------------------------------------------------------------------ 项目申报

    def submit_application(
        self, *, request_id: str, actor_id: str, round_id: str, application_id: str,
        segment_id: str, title: str, amount_requested: float,
        chainage_start: float, chainage_end: float,
        work_type: str = "routine", window_start: str | None = None,
        window_end: str | None = None, depends_on: list[str] | None = None,
        milestones: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        depends_on = depends_on or []
        milestones = milestones or []
        body = {"actor_id": actor_id, "round_id": round_id, "application_id": application_id,
                "segment_id": segment_id, "title": title, "amount_requested": amount_requested,
                "chainage_start": chainage_start, "chainage_end": chainage_end,
                "work_type": work_type, "window_start": window_start, "window_end": window_end,
                "depends_on": depends_on, "milestones": milestones}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "highway"})
            round_id = self._id(round_id, "round_id")
            application_id = self._id(application_id, "application_id")
            segment_id = self._id(segment_id, "segment_id")
            title = self._text(title, "title")
            amount = self._amount(amount_requested, "amount_requested")
            if not isinstance(chainage_start, (int, float)) or not isinstance(chainage_end, (int, float)):
                raise ValidationError("施工桩号必须是数字")
            if chainage_end <= chainage_start:
                raise ValidationError("chainage_end 必须大于 chainage_start")
            if work_type not in ("routine", "emergency"):
                raise ValidationError("work_type 必须是 routine 或 emergency")
            if (window_start is None) != (window_end is None):
                raise ValidationError("施工窗口必须同时提供起止日期")
            if window_start and window_end:
                _parse_day(window_start, "window_start")
                _parse_day(window_end, "window_end")
                if window_end <= window_start:
                    raise ValidationError("施工窗口结束日期必须晚于开始日期")
            if not isinstance(depends_on, list) or any(not isinstance(x, str) for x in depends_on):
                raise ValidationError("depends_on 必须是申请编号字符串数组")
            round_row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?",
                                           (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("轮次不存在")
            segment = connection.execute("SELECT * FROM road_segments WHERE segment_id=?",
                                         (segment_id,)).fetchone()
            if segment is None:
                raise NotFoundError("路段不存在")
            if not _overlap(segment["chainage_start"], segment["chainage_end"],
                            float(chainage_start), float(chainage_end)):
                raise ValidationError("申报施工区间不在路段登记范围内")
            if work_type == "routine" and round_row["status"] != "open":
                raise ConflictError("常规养护申报已截止；紧急抢修请走 emergency 限额例外")

            parsed_milestones = self._parse_milestones(milestones, amount)
            for dep in depends_on:
                dep_row = connection.execute(
                    "SELECT * FROM project_applications WHERE application_id=? AND round_id=?",
                    (dep, round_id),
                ).fetchone()
                if dep_row is None:
                    raise ValidationError(f"依赖项目 {dep} 不在本批次内")
            self._assert_no_cycle(connection, application_id, round_id, depends_on)
            # 同批次同区间即时预警（最终在批准前还会全量复核）
            self._assert_no_live_overlap(connection, round_row, segment, round_id,
                                         float(chainage_start), float(chainage_end),
                                         application_id)

            def create():
                status = "emergency_pending" if work_type == "emergency" else "submitted"
                review_status = "pending" if work_type == "emergency" else "not_required"
                try:
                    connection.execute(
                        "INSERT INTO project_applications(application_id,round_id,segment_id,title,"
                        "amount_requested,chainage_start,chainage_end,work_type,window_start,"
                        "window_end,depends_on_json,milestones_json,status,review_status,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (application_id, round_id, segment_id, title, amount,
                         float(chainage_start), float(chainage_end), work_type,
                         window_start, window_end, canonical_json(depends_on),
                         canonical_json(parsed_milestones), status, review_status,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("申请编号已经存在") from exc
                for index, item in enumerate(parsed_milestones, start=1):
                    connection.execute(
                        "INSERT INTO project_milestones(milestone_id,application_id,seq,name,"
                        "amount,due_date,status) VALUES(?,?,?,?,?,?,'planned')",
                        (uuid.uuid4().hex, application_id, index, item["name"],
                         item["amount"], item["due_date"]),
                    )
                self._audit(connection, actor_id=actor_id, action="funding.application.submitted",
                            resource_type="project_application", resource_id=application_id,
                            detail={"round_id": round_id, "segment_id": segment_id,
                                    "work_type": work_type, "amount": amount, "status": status})
                return "project_application", application_id, {
                    "application_id": application_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="funding.submit_application", payload=body, create=create)

    def _parse_milestones(self, milestones: list[dict[str, Any]], total: float) -> list[dict[str, Any]]:
        if not milestones:
            raise ValidationError("至少要定义一个资金里程碑")
        cleaned: list[dict[str, Any]] = []
        total_amount = 0.0
        seqs: set[int] = set()
        for item in milestones:
            if not isinstance(item, dict):
                raise ValidationError("里程碑必须是对象")
            seq = item.get("seq")
            if not isinstance(seq, int) or seq < 1 or seq in seqs:
                raise ValidationError("里程碑 seq 必须是唯一正整数")
            seqs.add(seq)
            name = self._text(item.get("name", ""), "milestone.name", 100)
            amount = self._amount(item.get("amount"), "milestone.amount")
            due_date = _parse_day(self._text(item.get("due_date", ""), "milestone.due_date", 20),
                                  "milestone.due_date")
            cleaned.append({"seq": seq, "name": name, "amount": amount, "due_date": due_date})
            total_amount += amount
        if abs(round(total_amount, 2) - total) > 0.01:
            raise ValidationError("里程碑金额之和必须等于申请金额")
        return sorted(cleaned, key=lambda item: item["seq"])

    def _assert_no_cycle(self, connection, application_id: str, round_id: str,
                         depends_on: list[str]) -> None:
        visiting: set[str] = set()

        def walk(node: str, trail: list[str]) -> None:
            if node == application_id:
                raise ValidationError(f"项目依赖存在环: {' -> '.join(trail + [node])}")
            if node in visiting:
                return
            visiting.add(node)
            row = connection.execute(
                "SELECT depends_on_json FROM project_applications WHERE application_id=? AND round_id=?",
                (node, round_id),
            ).fetchone()
            if row:
                for parent in json.loads(row["depends_on_json"]):
                    walk(parent, trail + [node])

        for dep in depends_on:
            walk(dep, [application_id])

    def _assert_no_live_overlap(self, connection, round_row, segment, round_id: str,
                                start: float, end: float, application_id: str) -> None:
        """即时检查：本批次及历史已批准项目中的同路线桩号重叠。"""

        rows = connection.execute(
            "SELECT a.application_id, a.chainage_start, a.chainage_end, a.status, s.route_code "
            "FROM project_applications a JOIN road_segments s ON s.segment_id=a.segment_id "
            "WHERE a.round_id=? AND a.application_id<>? AND a.status IN "
            "('submitted','ranked','approved','settled','emergency_pending','emergency_approved')",
            (round_id, application_id),
        ).fetchall()
        for row in rows:
            if row["route_code"] == segment["route_code"] and _overlap(
                start, end, row["chainage_start"], row["chainage_end"]
            ):
                raise ConflictError(
                    f"与本批次申请 {row['application_id']} 在路线 {segment['route_code']} 上桩号重叠，"
                    "涉嫌重复申报或同一路段拆项"
                )
        prior = connection.execute(
            "SELECT a.application_id, s.route_code, a.chainage_start, a.chainage_end, r.round_id "
            "FROM project_applications a JOIN road_segments s ON s.segment_id=a.segment_id "
            "JOIN funding_rounds r ON r.round_id=a.round_id "
            "WHERE s.route_code=? AND a.status IN ('approved','settled','emergency_approved') "
            "AND r.deadline_at <= ?",
            (segment["route_code"], round_row["deadline_at"]),
        ).fetchall()
        for row in prior:
            if _overlap(start, end, row["chainage_start"], row["chainage_end"]):
                raise ConflictError(
                    f"该区间在轮次 {row['round_id']} 已由 {row['application_id']} 获批，不能重复申报"
                )

    # ------------------------------------------------------------------ 冻结与评分

    def freeze_round(self, *, request_id: str, actor_id: str, round_id: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "round_id": round_id}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            round_id = self._id(round_id, "round_id")
            round_row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?",
                                           (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("轮次不存在")

            def create():
                if round_row["status"] != "open":
                    raise ConflictError("轮次已经冻结，证据与政策不能再次冻结")
                policy = connection.execute(
                    "SELECT * FROM scoring_policies WHERE status='active'"
                ).fetchone()
                if policy is None:
                    raise ConflictError("没有生效中的评分政策")
                rules = json.loads(policy["rules_json"])
                cutoff = round_row["deadline_at"]
                connection.execute(
                    "UPDATE funding_rounds SET status='frozen', policy_version=?, "
                    "evidence_cutoff_at=?, frozen_at=? WHERE round_id=?",
                    (policy["policy_version"], cutoff, self._now(), round_id),
                )
                snapshot_count = self._snapshot_evidence(connection, round_id, cutoff)
                scored = self._score_round(connection, round_id, rules, policy["policy_version"])
                self._audit(connection, actor_id=actor_id, action="funding.round.frozen",
                            resource_type="funding_round", resource_id=round_id,
                            detail={"policy_version": policy["policy_version"],
                                    "evidence_cutoff_at": cutoff,
                                    "snapshots": snapshot_count, "scored": scored,
                                    "rules_hash": policy["rules_hash"]})
                return "funding_round", round_id, {"round_id": round_id, "status": "frozen",
                                                   "policy_version": policy["policy_version"],
                                                   "snapshots": snapshot_count, "scored": scored}

            return self._idempotent(connection, request_id=request_id, action="funding.freeze_round",
                                    payload=body, create=create)

    def _snapshot_evidence(self, connection, round_id: str, cutoff: str) -> int:
        """把每个申报路段在截止时点前的最新各维度证据固化。"""

        connection.execute("DELETE FROM round_evidence_snapshots WHERE round_id=?", (round_id,))
        rows = connection.execute(
            "WITH ranked AS ("
            "SELECT e.segment_id, e.dimension, e.payload_json, e.payload_hash, e.effective_at, "
            "ROW_NUMBER() OVER (PARTITION BY e.segment_id, e.dimension "
            "ORDER BY e.effective_at DESC, e.created_at DESC, e.evidence_id DESC) AS rn "
            "FROM segment_evidence e "
            "WHERE e.segment_id IN (SELECT DISTINCT segment_id FROM project_applications WHERE round_id=?) "
            "AND e.effective_at <= ?"
            ") SELECT segment_id, dimension, payload_json, payload_hash, effective_at "
            "FROM ranked WHERE rn=1",
            (round_id, cutoff),
        ).fetchall()
        connection.executemany(
            "INSERT INTO round_evidence_snapshots(round_id,segment_id,dimension,payload_json,"
            "payload_hash,effective_at) VALUES(?,?,?,?,?,?)",
            [(round_id, r["segment_id"], r["dimension"], r["payload_json"],
              r["payload_hash"], r["effective_at"]) for r in rows],
        )
        return len(rows)

    def _frozen_evidence(self, connection, round_id: str, segment_id: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "SELECT dimension, payload_json FROM round_evidence_snapshots "
            "WHERE round_id=? AND segment_id=?",
            (round_id, segment_id),
        ).fetchall()
        return {r["dimension"]: json.loads(r["payload_json"]) for r in rows}

    def _latest_evidence(self, connection, segment_id: str, as_of: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "WITH ranked AS (SELECT dimension, payload_json, "
            "ROW_NUMBER() OVER (PARTITION BY dimension ORDER BY effective_at DESC, created_at DESC) rn "
            "FROM segment_evidence WHERE segment_id=? AND effective_at<=?) "
            "SELECT dimension, payload_json FROM ranked WHERE rn=1",
            (segment_id, as_of),
        ).fetchall()
        return {r["dimension"]: json.loads(r["payload_json"]) for r in rows}

    def _blocking_ids(self, connection, round_id: str) -> set[str]:
        blocking: set[str] = set()
        rows = connection.execute(
            "SELECT depends_on_json FROM project_applications WHERE round_id=? "
            "AND status NOT IN ('cancelled','rejected')",
            (round_id,),
        ).fetchall()
        for row in rows:
            blocking.update(json.loads(row["depends_on_json"]))
        return blocking

    def _score_round(self, connection, round_id: str, rules: dict[str, Any],
                     policy_version: str) -> int:
        blocking = self._blocking_ids(connection, round_id)
        apps = connection.execute(
            "SELECT * FROM project_applications WHERE round_id=? AND status='submitted'",
            (round_id,),
        ).fetchall()
        results = []
        for app in apps:
            evidence = self._frozen_evidence(connection, round_id, app["segment_id"])
            result = score_application(evidence, rules,
                                       is_blocking=app["application_id"] in blocking)
            results.append((app["application_id"], result))
        results.sort(key=lambda item: (-item[1]["total"], item[0]))
        for rank, (application_id, result) in enumerate(results, start=1):
            connection.execute(
                "INSERT INTO project_scores(round_id,application_id,policy_version,eligible,"
                "total_score,rank,dimension_scores_json,rationale_json,scored_at) "
                "VALUES(?,?,?,1,?,?,?,?,?)",
                (round_id, application_id, policy_version, result["total"], rank,
                 canonical_json(result["dimensions"]), canonical_json(result["rationale"]),
                 self._now()),
            )
            connection.execute(
                "UPDATE project_applications SET status='ranked' WHERE application_id=?",
                (application_id,),
            )
        return len(results)

    # ------------------------------------------------------------------ 批准前查重

    def detect_conflicts(self, actor_id: str, round_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._actor(connection, actor_id, {"admin", "finance", "highway", "reviewer", "auditor"})
            if connection.execute("SELECT 1 FROM funding_rounds WHERE round_id=?",
                                  (round_id,)).fetchone() is None:
                raise NotFoundError("轮次不存在")
            return self._conflicts(connection, round_id)

    def _conflicts(self, connection, round_id: str) -> dict[str, Any]:
        apps = connection.execute(
            "SELECT a.*, s.route_code FROM project_applications a JOIN road_segments s "
            "ON s.segment_id=a.segment_id WHERE a.round_id=? AND a.status IN "
            "('submitted','ranked','approved','settled','waitlisted','emergency_approved') "
            "ORDER BY a.application_id",
            (round_id,),
        ).fetchall()
        duplicate_pairs: list[dict[str, str]] = []
        window_pairs: list[dict[str, str]] = []
        dependency_violations: list[dict[str, str]] = []
        seen_unordered: set[tuple[str, str]] = set()
        for i, left in enumerate(apps):
            left_deps = json.loads(left["depends_on_json"])
            for right in apps[i + 1:]:
                pair = tuple(sorted((left["application_id"], right["application_id"])))
                geometry = (left["route_code"] == right["route_code"] and _overlap(
                    left["chainage_start"], left["chainage_end"],
                    right["chainage_start"], right["chainage_end"]))
                same_segment = left["segment_id"] == right["segment_id"]
                related = (geometry or same_segment
                           or right["application_id"] in left_deps
                           or left["application_id"] in json.loads(right["depends_on_json"]))
                if geometry and pair not in seen_unordered:
                    seen_unordered.add(pair)
                    duplicate_pairs.append({
                        "applications": list(pair), "route_code": left["route_code"],
                        "reason": "同一路线桩号重叠：重复申报或同一路段拆项",
                    })
                if related and _windows_overlap(
                    (left["window_start"], left["window_end"]),
                    (right["window_start"], right["window_end"]),
                ):
                    window_pairs.append({
                        "applications": list(pair),
                        "reason": "互斥施工窗口重叠（同一路段或存在前后依赖）",
                    })
            for dep_id in left_deps:
                dep = next((a for a in apps if a["application_id"] == dep_id), None)
                if dep is None:
                    continue
                if (left["window_start"] and dep["window_end"]
                        and left["window_start"] < dep["window_end"]):
                    dependency_violations.append({
                        "application_id": left["application_id"], "depends_on": dep_id,
                        "reason": "后续项目开工早于前置项目完工，依赖顺序无法兑现",
                    })
        cross_round: list[dict[str, str]] = []
        for app in apps:
            prior = connection.execute(
                "SELECT a.application_id, r.round_id FROM project_applications a "
                "JOIN road_segments s ON s.segment_id=a.segment_id "
                "JOIN funding_rounds r ON r.round_id=a.round_id "
                "WHERE s.route_code=? AND a.application_id<>? AND r.round_id<>? "
                "AND a.status IN ('approved','settled','emergency_approved')",
                (app["route_code"], app["application_id"], round_id),
            ).fetchall()
            for row in prior:
                dep_app = connection.execute(
                    "SELECT chainage_start, chainage_end FROM project_applications WHERE application_id=?",
                    (row["application_id"],),
                ).fetchone()
                if _overlap(app["chainage_start"], app["chainage_end"],
                            dep_app["chainage_start"], dep_app["chainage_end"]):
                    cross_round.append({
                        "application_id": app["application_id"],
                        "prior_application_id": row["application_id"],
                        "prior_round_id": row["round_id"],
                        "reason": "与历史已批准项目同路线桩号重叠，涉嫌重复申报",
                    })
        return {"duplicate_pairs": duplicate_pairs, "window_conflicts": window_pairs,
                "dependency_violations": dependency_violations,
                "cross_round_duplicates": cross_round,
                "has_conflict": bool(duplicate_pairs or window_pairs
                                     or dependency_violations or cross_round)}

    # ------------------------------------------------------------------ 组合决策

    def decide_portfolio(self, *, request_id: str, actor_id: str, round_id: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "round_id": round_id}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            round_id = self._id(round_id, "round_id")
            round_row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?",
                                           (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("轮次不存在")
            if round_row["status"] not in ("frozen", "finalized"):
                raise ConflictError("轮次必须先冻结才能形成投资组合")
            promotion = round_row["status"] == "finalized"
            conflicts = self._conflicts(connection, round_id)
            if conflicts["has_conflict"]:
                raise ConflictError({"message": "批准前查重发现冲突，必须先取消或整改",
                                     "conflicts": conflicts})
            period = self._require_open_period(connection)
            policy_version = round_row["policy_version"]

            def create():
                scores = connection.execute(
                    "SELECT * FROM project_scores WHERE round_id=? ORDER BY rank",
                    (round_id,),
                ).fetchall()
                balances = self._envelope_balances(connection, round_id)
                decisions = []
                approved_count = 0
                for score in scores:
                    app = connection.execute(
                        "SELECT * FROM project_applications WHERE application_id=?",
                        (score["application_id"],),
                    ).fetchone()
                    if promotion:
                        # 定稿后只处理候补递补（如其他项目取消释放了额度）
                        if app["status"] != "waitlisted":
                            continue
                    elif app["status"] != "ranked":
                        # 冻结后、决策前撤项或拒绝的项目不进入组合
                        continue
                    amount = app["amount_requested"]
                    allocation = self._waterfall(balances, amount)
                    if allocation is None:
                        if promotion:
                            # 本轮仍无可用额度，保持候补
                            decision = {"application_id": app["application_id"],
                                        "rank": score["rank"], "score": score["total_score"],
                                        "decision": "waitlisted",
                                        "reason": "递补时各级资金余额仍不足以全额安排"}
                            decisions.append(decision)
                            continue
                        connection.execute(
                            "UPDATE project_applications SET status='waitlisted', "
                            "decision_reason=?, decided_at=? WHERE application_id=?",
                            (f"排名第 {score['rank']}，但各级资金余额不足以全额安排",
                             self._now(), app["application_id"]),
                        )
                        decision = {"application_id": app["application_id"], "rank": score["rank"],
                                    "score": score["total_score"], "decision": "waitlisted",
                                    "reason": "资金约束下无法全额安排"}
                    else:
                        for level, part in allocation.items():
                            balances[level] = round(balances[level] - part, 2)
                            envelope_id = self._envelope_id(connection, round_id, level)
                            self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                               round_id=round_id, application_id=app["application_id"],
                                               entry_type="commit", amount=part,
                                               detail={"policy_version": policy_version,
                                                       "rank": score["rank"], "level": level},
                                               actor_id=actor_id)
                        connection.execute(
                            "UPDATE project_applications SET status='approved', amount_approved=?, "
                            "allocation_json=?, decision_reason=?, decided_at=? WHERE application_id=?",
                            (amount, canonical_json(allocation),
                             (f"排名第 {score['rank']}，其他项目取消释放额度后递补纳入"
                              if promotion else f"排名第 {score['rank']}，各级资金可全额安排"),
                             self._now(), app["application_id"]),
                        )
                        connection.execute(
                            "UPDATE project_milestones SET status='reserved', reserved_amount=amount "
                            "WHERE application_id=?",
                            (app["application_id"],),
                        )
                        approved_count += 1
                        decision = {"application_id": app["application_id"], "rank": score["rank"],
                                    "score": score["total_score"], "decision": "approved",
                                    "allocation": allocation, "promoted": promotion,
                                    "reason": ("其他项目取消释放额度后按排名递补"
                                               if promotion else "按排名与资金级次约束纳入组合")}
                    decisions.append(decision)
                connection.execute(
                    "UPDATE funding_rounds SET status='finalized' WHERE round_id=?", (round_id,)
                )
                self._audit(connection, actor_id=actor_id,
                            action=("funding.portfolio.promoted" if promotion
                                    else "funding.portfolio.decided"),
                            resource_type="funding_round", resource_id=round_id,
                            detail={"policy_version": policy_version,
                                    "approved": approved_count,
                                    "waitlisted": len(decisions) - approved_count,
                                    "promotion": promotion})
                return "funding_round", round_id, {"round_id": round_id, "status": "finalized",
                                                   "decisions": decisions,
                                                   "approved": approved_count,
                                                   "waitlisted": len(decisions) - approved_count}

            return self._idempotent(connection, request_id=request_id,
                                    action="funding.decide_portfolio", payload=body, create=create)

    def _envelope_id(self, connection, round_id: str, level: str) -> str:
        row = connection.execute(
            "SELECT envelope_id FROM funding_envelopes WHERE round_id=? AND level=?",
            (round_id, level),
        ).fetchone()
        return row["envelope_id"] if row else None

    def _envelope_balances(self, connection, round_id: str) -> dict[str, float]:
        envelopes = connection.execute(
            "SELECT envelope_id, level, amount FROM funding_envelopes WHERE round_id=?",
            (round_id,),
        ).fetchall()
        balances = {row["level"]: float(row["amount"]) for row in envelopes}
        committed: dict[str, float] = {level: 0.0 for level in balances}
        spent: dict[str, float] = {level: 0.0 for level in balances}
        rows = connection.execute(
            "SELECT envelope_id, entry_type, amount FROM funding_ledger WHERE round_id=?",
            (round_id,),
        ).fetchall()
        level_by_id = {row["envelope_id"]: row["level"] for row in envelopes}
        for row in rows:
            level = level_by_id.get(row["envelope_id"])
            if level is None:
                continue
            kind, amount = row["entry_type"], float(row["amount"])
            if kind in ("commit", "emergency_commit"):
                committed[level] += amount
            elif kind == "payment":
                committed[level] -= amount
                spent[level] += amount
            elif kind == "change":
                committed[level] += amount
            elif kind in ("cancel_release",):
                committed[level] -= amount
            elif kind == "recovery":
                spent[level] -= amount
        return {level: round(balances[level] - committed[level] - spent[level], 2)
                for level in balances}

    def _waterfall(self, balances: dict[str, float], amount: float) -> dict[str, float] | None:
        """按县级→省级→中央顺序兜底；任一层缺口且无下一层则整体不安排。"""

        remaining = amount
        allocation: dict[str, float] = {}
        for level in WATERFALL:
            if level not in balances:
                continue
            if remaining <= 0:
                break
            part = round(min(balances[level], remaining), 2)
            if part > 0:
                allocation[level] = part
                remaining = round(remaining - part, 2)
        return allocation if remaining == 0 else None

    # ------------------------------------------------------------------ 紧急抢修限额例外

    def submit_emergency_review(self, *, request_id: str, actor_id: str, application_id: str,
                                verdict: str, notes: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "application_id": application_id,
                "verdict": verdict, "notes": notes}
        with self.database.transaction(immediate=True) as connection:
            reviewer = self._actor(connection, actor_id, {"admin", "reviewer", "auditor"})
            replay = self._early_replay(connection, request_id=request_id,
                                        action="funding.emergency_review", payload=body)
            if replay:
                return replay
            application_id = self._id(application_id, "application_id")
            if verdict not in ("approved", "rejected"):
                raise ValidationError("verdict 必须是 approved 或 rejected")
            notes = self._text(notes, "notes", 1000)
            app = connection.execute("SELECT * FROM project_applications WHERE application_id=?",
                                     (application_id,)).fetchone()
            if app is None:
                raise NotFoundError("申请不存在")
            if app["work_type"] != "emergency":
                raise ValidationError("独立复核只适用于紧急抢修限额例外")
            if app["status"] != "emergency_pending":
                raise ConflictError("该紧急抢修已经完成复核")
            if actor_id == app["created_by"]:
                raise PermissionDenied("独立复核人不能是申报人本人")
            if reviewer["organization_id"] != "system" and \
                    reviewer["organization_id"] == self._creator_org(connection, app["created_by"]) \
                    and reviewer["role"] != "admin":
                raise PermissionDenied("独立复核须由公路申报单位之外的人员完成")

            def create():
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO independent_reviews(review_id,application_id,reviewer_id,verdict,"
                    "notes,created_at) VALUES(?,?,?,?,?,?)",
                    (review_id, application_id, actor_id, verdict, notes, self._now()),
                )
                if verdict == "approved":
                    period = self._require_open_period(connection)
                    round_id = app["round_id"]
                    balances = self._envelope_balances(connection, round_id)
                    amount = app["amount_requested"]
                    if balances.get("emergency", 0.0) < amount:
                        # 现场复核通过，但紧急限额余额不足：作为业务决定拒绝并留痕
                        connection.execute(
                            "UPDATE project_applications SET review_status='approved',"
                            "status='rejected', decision_reason=?, decided_at=? "
                            "WHERE application_id=?",
                            (f"独立复核通过但紧急限额余额不足（可用 "
                             f"{balances.get('emergency', 0.0):g}，申请 {amount:g}）",
                             self._now(), application_id),
                        )
                        new_status = "rejected"
                    else:
                        envelope_id = self._envelope_id(connection, round_id, "emergency")
                        self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                           round_id=round_id, application_id=application_id,
                                           entry_type="emergency_commit", amount=amount,
                                           detail={"review_id": review_id, "emergency": True},
                                           actor_id=actor_id)
                        connection.execute(
                            "UPDATE project_applications SET review_status='approved',"
                            "status='emergency_approved', amount_approved=?, "
                            "allocation_json=?, decided_at=? WHERE application_id=?",
                            (amount, canonical_json({"emergency": amount}), self._now(),
                             application_id),
                        )
                        connection.execute(
                            "UPDATE project_milestones SET status='reserved', reserved_amount=amount "
                            "WHERE application_id=?",
                            (application_id,),
                        )
                        self._score_emergency(connection, app, period)
                        new_status = "emergency_approved"
                else:
                    connection.execute(
                        "UPDATE project_applications SET review_status='rejected', status='rejected',"
                        " decision_reason=?, decided_at=? WHERE application_id=?",
                        (f"独立复核未通过：{notes}", self._now(), application_id),
                    )
                    new_status = "rejected"
                self._audit(connection, actor_id=actor_id, action="funding.emergency.reviewed",
                            resource_type="project_application", resource_id=application_id,
                            detail={"verdict": verdict, "review_id": review_id})
                return "independent_review", review_id, {"review_id": review_id,
                                                         "application_id": application_id,
                                                         "status": new_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="funding.emergency_review", payload=body, create=create)

    def _creator_org(self, connection, actor_id: str) -> str:
        row = connection.execute("SELECT organization_id FROM actors WHERE actor_id=?",
                                 (actor_id,)).fetchone()
        return row["organization_id"] if row else ""

    def _score_emergency(self, connection, app, period) -> None:
        policy = connection.execute(
            "SELECT * FROM scoring_policies WHERE status='active'"
        ).fetchone()
        rules = json.loads(policy["rules_json"])
        evidence = self._latest_evidence(connection, app["segment_id"], self._now())
        result = score_application(evidence, rules, is_blocking=False)
        connection.execute(
            "INSERT INTO project_scores(round_id,application_id,policy_version,eligible,"
            "total_score,rank,dimension_scores_json,rationale_json,scored_at) "
            "VALUES(?,?,?,1,?,NULL,?,?,?)",
            (app["round_id"], app["application_id"], policy["policy_version"], result["total"],
             canonical_json(result["dimensions"]), canonical_json(result["rationale"]), self._now()),
        )

    # ------------------------------------------------------------------ 里程碑支付与结算

    def _locked_app(self, connection, application_id: str):
        app = connection.execute("SELECT * FROM project_applications WHERE application_id=?",
                                 (application_id,)).fetchone()
        if app is None:
            raise NotFoundError("申请不存在")
        if app["status"] not in ("approved", "emergency_approved"):
            raise ConflictError("只有已批准且未取消/结清的项目才能登记执行台账")
        return app

    def pay_milestone(self, *, request_id: str, actor_id: str, application_id: str,
                      seq: int, amount: float) -> dict[str, Any]:
        body = {"actor_id": actor_id, "application_id": application_id, "seq": seq, "amount": amount}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            replay = self._early_replay(connection, request_id=request_id,
                                        action="funding.pay_milestone", payload=body)
            if replay:
                return replay
            application_id = self._id(application_id, "application_id")
            if not isinstance(seq, int) or seq < 1:
                raise ValidationError("seq 必须是正整数")
            amount = self._amount(amount, "amount")
            app = self._locked_app(connection, application_id)
            milestone = connection.execute(
                "SELECT * FROM project_milestones WHERE application_id=? AND seq=?",
                (application_id, seq),
            ).fetchone()
            if milestone is None:
                raise NotFoundError("里程碑不存在")
            if milestone["status"] == "paid":
                raise ConflictError("里程碑已经支付，支付不可冲销（如须追回走 recovery）")
            if round(amount + milestone["paid_amount"], 2) > milestone["amount"] + 0.001:
                raise ValidationError("里程碑累计支付不能超过其承诺金额")
            period = self._require_open_period(connection)

            def create():
                allocation = json.loads(app["allocation_json"])
                splits = self._payment_splits(connection, app, amount)
                entry_ids = []
                for envelope_level, part in splits.items():
                    envelope_id = self._envelope_id(connection, app["round_id"], envelope_level)
                    entry = self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                               round_id=app["round_id"], application_id=application_id,
                                               entry_type="payment", amount=part,
                                               detail={"seq": seq, "milestone": milestone["name"],
                                                       "allocation": allocation},
                                               actor_id=actor_id)
                    entry_ids.append(entry)
                connection.execute(
                    "UPDATE project_milestones SET paid_amount=ROUND(paid_amount+?, 2), "
                    "status=CASE WHEN ROUND(paid_amount+?,2)>=ROUND(amount,2) THEN 'paid' "
                    "ELSE 'reserved' END WHERE milestone_id=?",
                    (amount, amount, milestone["milestone_id"]),
                )
                self._maybe_settle(connection, application_id)
                self._audit(connection, actor_id=actor_id, action="funding.milestone.paid",
                            resource_type="project_application", resource_id=application_id,
                            detail={"seq": seq, "amount": amount, "ledger_entries": entry_ids})
                return "milestone_payment", f"{application_id}:{seq}", {
                    "application_id": application_id, "seq": seq, "paid_amount": amount}

            return self._idempotent(connection, request_id=request_id, action="funding.pay_milestone",
                                    payload=body, create=create)

    def _payment_splits(self, connection, app, amount: float) -> dict[str, float]:
        """按各级资金承诺的剩余未付占比分摊本次支付。"""

        allocation: dict[str, float] = {
            level: float(part) for level, part in json.loads(app["allocation_json"]).items()
        }
        paid_by_level: dict[str, float] = {level: 0.0 for level in allocation}
        rows = connection.execute(
            "SELECT envelope_id, amount FROM funding_ledger WHERE application_id=? AND entry_type='payment'",
            (app["application_id"],),
        ).fetchall()
        for row in rows:
            level_row = connection.execute(
                "SELECT level FROM funding_envelopes WHERE envelope_id=?", (row["envelope_id"],)
            ).fetchone()
            if level_row and level_row["level"] in paid_by_level:
                paid_by_level[level_row["level"]] += float(row["amount"])
        remaining = {level: round(allocation[level] - paid_by_level[level], 2)
                     for level in allocation if round(allocation[level] - paid_by_level[level], 2) > 0}
        total_remaining = round(sum(remaining.values()), 2)
        if total_remaining + 0.001 < amount:
            raise ConflictError("支付金额超过各级承诺的剩余未付额度")
        splits: dict[str, float] = {}
        allocated = 0.0
        ordered = list(remaining)
        for level in ordered[:-1]:
            part = round(amount * remaining[level] / total_remaining, 2)
            splits[level] = part
            allocated = round(allocated + part, 2)
        if ordered:
            splits[ordered[-1]] = round(amount - allocated, 2)  # 尾差并入最后一级
        return {level: part for level, part in splits.items() if abs(part) > 0.001}

    def _maybe_settle(self, connection, application_id: str) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS paid "
            "FROM project_milestones WHERE application_id=?",
            (application_id,),
        ).fetchone()
        if row["total"] and row["total"] == row["paid"]:
            connection.execute(
                "UPDATE project_applications SET status='settled' WHERE application_id=? "
                "AND status IN ('approved','emergency_approved')",
                (application_id,),
            )

    # ------------------------------------------------------------------ 变更、取消、回收

    def change_project(self, *, request_id: str, actor_id: str, application_id: str,
                       new_amount: float, reason: str,
                       milestones: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        body = {"actor_id": actor_id, "application_id": application_id,
                "new_amount": new_amount, "reason": reason, "milestones": milestones}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance", "highway"})
            replay = self._early_replay(connection, request_id=request_id,
                                        action="funding.change_project", payload=body)
            if replay:
                return replay
            application_id = self._id(application_id, "application_id")
            new_amount = self._amount(new_amount, "new_amount")
            reason = self._text(reason, "reason", 500)
            app = self._locked_app(connection, application_id)
            paid = connection.execute(
                "SELECT COALESCE(SUM(paid_amount),0) AS paid FROM project_milestones WHERE application_id=?",
                (application_id,),
            ).fetchone()["paid"]
            if new_amount < paid - 0.001:
                raise ValidationError("变更后金额不能低于已支付金额")
            period = self._require_open_period(connection)
            parsed = self._parse_milestones(milestones, new_amount) if milestones else None
            if parsed:
                for item in parsed:
                    existing = connection.execute(
                        "SELECT paid_amount FROM project_milestones WHERE application_id=? AND seq=?",
                        (application_id, item["seq"]),
                    ).fetchone()
                    if existing and item["amount"] < existing["paid_amount"] - 0.001:
                        raise ValidationError(f"里程碑 {item['seq']} 新金额低于已支付金额")

            def create():
                delta = round(new_amount - app["amount_approved"], 2)
                if abs(delta) > 0.001:
                    if delta > 0:
                        balances = self._envelope_balances(connection, app["round_id"])
                        cover = self._waterfall(
                            {level: balances.get(level, 0.0) for level in WATERFALL}, delta)
                        if cover is None:
                            raise ConflictError("各级资金余额不足以覆盖变更追加")
                        allocation = json.loads(app["allocation_json"])
                        for level, part in cover.items():
                            envelope_id = self._envelope_id(connection, app["round_id"], level)
                            self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                               round_id=app["round_id"], application_id=application_id,
                                               entry_type="change", amount=part,
                                               detail={"delta": part, "reason": reason},
                                               actor_id=actor_id)
                            allocation[level] = round(allocation.get(level, 0.0) + part, 2)
                    else:
                        allocation = self._reduce_allocation(connection, app, period, -delta,
                                                             reason, actor_id)
                    connection.execute(
                        "UPDATE project_applications SET amount_approved=?, allocation_json=? "
                        "WHERE application_id=?",
                        (new_amount, canonical_json(allocation), application_id),
                    )
                if parsed:
                    connection.execute("DELETE FROM project_milestones WHERE application_id=?",
                                       (application_id,))
                    for item in parsed:
                        connection.execute(
                            "INSERT INTO project_milestones(milestone_id,application_id,seq,name,"
                            "amount,due_date,status,reserved_amount,paid_amount) "
                            "VALUES(?,?,?,?,?,?,'reserved',?,0)",
                            (uuid.uuid4().hex, application_id, item["seq"], item["name"],
                             item["amount"], item["due_date"], item["amount"]),
                        )
                    self._reconcile_paid_milestones(connection, application_id)
                self._audit(connection, actor_id=actor_id, action="funding.project.changed",
                            resource_type="project_application", resource_id=application_id,
                            detail={"old_amount": app["amount_approved"], "new_amount": new_amount,
                                    "delta": delta, "reason": reason})
                return "project_change", application_id, {
                    "application_id": application_id, "amount_approved": new_amount, "delta": delta}

            return self._idempotent(connection, request_id=request_id, action="funding.change_project",
                                    payload=body, create=create)

    def _reduce_allocation(self, connection, app, period, reduction: float, reason: str,
                           actor_id: str) -> dict[str, float]:
        allocation = {level: float(part) for level, part in json.loads(app["allocation_json"]).items()}
        paid_by_level = self._paid_by_level(connection, app["application_id"])
        left = reduction
        for level in ("emergency",) + WATERFALL:
            if left <= 0:
                break
            releasable = round(allocation.get(level, 0.0) - paid_by_level.get(level, 0.0), 2)
            part = round(min(max(releasable, 0.0), left), 2)
            if part > 0:
                envelope_id = self._envelope_id(connection, app["round_id"], level)
                self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                   round_id=app["round_id"], application_id=app["application_id"],
                                   entry_type="change", amount=-part,
                                   detail={"delta": -part, "reason": reason}, actor_id=actor_id)
                allocation[level] = round(allocation[level] - part, 2)
                left = round(left - part, 2)
        if left > 0.001:
            raise ConflictError("承诺可释放额度不足，缩减退回应先处理已支付部分（走追回）")
        return allocation

    def _paid_by_level(self, connection, application_id: str) -> dict[str, float]:
        result: dict[str, float] = {}
        rows = connection.execute(
            "SELECT fe.level, SUM(l.amount) AS paid FROM funding_ledger l "
            "JOIN funding_envelopes fe ON fe.envelope_id=l.envelope_id "
            "WHERE l.application_id=? AND l.entry_type='payment' GROUP BY fe.level",
            (application_id,),
        ).fetchall()
        for row in rows:
            result[row["level"]] = float(row["paid"])
        return result

    def _reconcile_paid_milestones(self, connection, application_id: str) -> None:
        """变更里程碑后，按台账历史支付总额恢复 paid_amount（已关账期间不受影响）。"""

        rows = connection.execute(
            "SELECT l.detail_json, SUM(l.amount) AS amount FROM funding_ledger l "
            "WHERE l.application_id=? AND l.entry_type='payment' GROUP BY l.detail_json",
            (application_id,),
        ).fetchall()
        paid_by_seq: dict[int, float] = {}
        for row in rows:
            seq = json.loads(row["detail_json"]).get("seq")
            if isinstance(seq, int):
                paid_by_seq[seq] = paid_by_seq.get(seq, 0.0) + float(row["amount"])
        for seq, paid in paid_by_seq.items():
            connection.execute(
                "UPDATE project_milestones SET paid_amount=?, "
                "status=CASE WHEN ?>=ROUND(amount,2) THEN 'paid' ELSE 'reserved' END "
                "WHERE application_id=? AND seq=?",
                (round(paid, 2), round(paid, 2), application_id, seq),
            )
        self._maybe_settle(connection, application_id)

    def cancel_project(self, *, request_id: str, actor_id: str, application_id: str,
                       reason: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "application_id": application_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance", "highway"})
            replay = self._early_replay(connection, request_id=request_id,
                                        action="funding.cancel_project", payload=body)
            if replay:
                return replay
            application_id = self._id(application_id, "application_id")
            reason = self._text(reason, "reason", 500)
            app = connection.execute("SELECT * FROM project_applications WHERE application_id=?",
                                     (application_id,)).fetchone()
            if app is None:
                raise NotFoundError("申请不存在")
            if app["status"] in ("cancelled", "rejected", "settled"):
                raise ConflictError("当前状态的项目不能取消")
            pre_approval = app["status"] in ("submitted", "ranked", "waitlisted")
            period = self._open_period(connection) if not pre_approval else None
            if period is None and not pre_approval:
                raise ConflictError("当前没有开放的会计期间，不能取消已批准项目")

            def create():
                if not pre_approval:
                    allocation = {level: float(part) for level, part in
                                  json.loads(app["allocation_json"]).items()}
                    paid_by_level = self._paid_by_level(connection, application_id)
                    for level, committed in allocation.items():
                        release = round(committed - paid_by_level.get(level, 0.0), 2)
                        if release > 0:
                            envelope_id = self._envelope_id(connection, app["round_id"], level)
                            self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                               round_id=app["round_id"], application_id=application_id,
                                               entry_type="cancel_release", amount=release,
                                               detail={"reason": reason}, actor_id=actor_id)
                connection.execute(
                    "UPDATE project_applications SET status='cancelled', decision_reason=? "
                    "WHERE application_id=?",
                    (f"项目取消：{reason}", application_id),
                )
                self._audit(connection, actor_id=actor_id, action="funding.project.cancelled",
                            resource_type="project_application", resource_id=application_id,
                            detail={"reason": reason})
                return "project_cancellation", application_id, {
                    "application_id": application_id, "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id, action="funding.cancel_project",
                                    payload=body, create=create)

    def recover_funds(self, *, request_id: str, actor_id: str, application_id: str,
                      amount: float, reason: str) -> dict[str, Any]:
        """结余/违规资金追回：只能针对已结算项目，且只能进入开放期间。"""

        body = {"actor_id": actor_id, "application_id": application_id,
                "amount": amount, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance", "auditor"})
            replay = self._early_replay(connection, request_id=request_id,
                                        action="funding.recover_funds", payload=body)
            if replay:
                return replay
            application_id = self._id(application_id, "application_id")
            amount = self._amount(amount, "amount")
            reason = self._text(reason, "reason", 500)
            app = connection.execute("SELECT * FROM project_applications WHERE application_id=?",
                                     (application_id,)).fetchone()
            if app is None:
                raise NotFoundError("申请不存在")
            if app["status"] != "settled":
                raise ConflictError("结余回收只适用于已结算项目")
            period = self._require_open_period(connection)
            paid_by_level = self._paid_by_level(connection, application_id)
            recovered = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS amount FROM funding_ledger "
                "WHERE application_id=? AND entry_type='recovery'",
                (application_id,),
            ).fetchone()["amount"]
            if amount > sum(paid_by_level.values()) - recovered + 0.001:
                raise ValidationError("累计追回不能超过已支付净额")

            def create():
                # 按原支付层级等比例追回
                left = amount
                net_paid = {level: paid for level, paid in paid_by_level.items() if paid > 0}
                total_paid = sum(net_paid.values())
                entries = []
                for level, paid in net_paid.items():
                    part = round(min(paid, amount * paid / total_paid), 2) if total_paid else 0.0
                    if part <= 0 or left <= 0:
                        continue
                    part = min(part, left)
                    envelope_id = self._envelope_id(connection, app["round_id"], level)
                    entry = self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                               round_id=app["round_id"], application_id=application_id,
                                               entry_type="recovery", amount=round(part, 2),
                                               detail={"reason": reason}, actor_id=actor_id)
                    entries.append(entry)
                    left = round(left - part, 2)
                if left > 0.01:  # 四舍五入尾差挂到第一层级
                    level = next(iter(net_paid))
                    envelope_id = self._envelope_id(connection, app["round_id"], level)
                    self._write_ledger(connection, period=period, envelope_id=envelope_id,
                                       round_id=app["round_id"], application_id=application_id,
                                       entry_type="recovery", amount=round(left, 2),
                                       detail={"reason": reason, "rounding": True},
                                       actor_id=actor_id)
                self._audit(connection, actor_id=actor_id, action="funding.funds.recovered",
                            resource_type="project_application", resource_id=application_id,
                            detail={"amount": amount, "reason": reason})
                return "fund_recovery", application_id, {
                    "application_id": application_id, "recovered": amount}

            return self._idempotent(connection, request_id=request_id, action="funding.recover_funds",
                                    payload=body, create=create)

    def _write_ledger(self, connection, *, period, envelope_id, round_id: str,
                      application_id: str, entry_type: str, amount: float,
                      detail: dict[str, Any], actor_id: str) -> str:
        """向当前开放期间写入台账；期间一旦关账，任何写入都被拒绝。"""

        if period["status"] != "open":
            raise ConflictError(f"会计期间 {period['period_id']} 已关账，不能改写历史期间")
        entry_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO funding_ledger(entry_id,period_id,envelope_id,round_id,application_id,"
            "milestone_id,entry_type,amount,detail_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, period["period_id"], envelope_id, round_id, application_id,
             detail.get("milestone_id"), entry_type, float(amount), canonical_json(detail),
             actor_id, self._now()),
        )
        return entry_id

    # ------------------------------------------------------------------ 会计期间

    def open_period(self, *, request_id: str, actor_id: str, period_id: str,
                    label: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "period_id": period_id, "label": label}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            period_id = self._id(period_id, "period_id")
            label = self._text(label, "label")
            existing = self._open_period(connection)
            if existing is not None:
                raise ConflictError(f"会计期间 {existing['period_id']} 尚未关账")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO accounting_periods(period_id,label,status,opened_at) "
                        "VALUES(?,?,'open',?)",
                        (period_id, label, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("会计期间编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="funding.period.opened",
                            resource_type="accounting_period", resource_id=period_id,
                            detail={"label": label})
                return "accounting_period", period_id, {"period_id": period_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id, action="funding.open_period",
                                    payload=body, create=create)

    def close_period(self, *, request_id: str, actor_id: str, period_id: str) -> dict[str, Any]:
        body = {"actor_id": actor_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            self._actor(connection, actor_id, {"admin", "finance"})
            period_id = self._id(period_id, "period_id")

            def create():
                row = connection.execute("SELECT * FROM accounting_periods WHERE period_id=?",
                                         (period_id,)).fetchone()
                if row is None:
                    raise NotFoundError("会计期间不存在")
                if row["status"] == "closed":
                    raise ConflictError("会计期间已经关账，关账决定不可逆")
                connection.execute(
                    "UPDATE accounting_periods SET status='closed', closed_at=? WHERE period_id=?",
                    (self._now(), period_id),
                )
                self._audit(connection, actor_id=actor_id, action="funding.period.closed",
                            resource_type="accounting_period", resource_id=period_id,
                            detail={"closed_at": self._now()})
                return "accounting_period", period_id, {"period_id": period_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id, action="funding.close_period",
                                    payload=body, create=create)

    # ------------------------------------------------------------------ 政策换版模拟

    def simulate_policy(self, *, actor_id: str, round_id: str,
                        policy_version: str) -> dict[str, Any]:
        """用另一版政策对冻结证据重新排名，但不落库、不影响任何历史决定。"""

        with self.database.transaction() as connection:
            self._actor(connection, actor_id, {"admin", "finance", "auditor"})
            round_row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?",
                                           (round_id,)).fetchone()
            if round_row is None:
                raise NotFoundError("轮次不存在")
            if round_row["status"] == "open":
                raise ConflictError("轮次尚未冻结，没有可比较的冻结证据")
            policy = connection.execute("SELECT * FROM scoring_policies WHERE policy_version=?",
                                        (policy_version,)).fetchone()
            if policy is None:
                raise NotFoundError("政策版本不存在")
            rules = json.loads(policy["rules_json"])
            blocking = self._blocking_ids(connection, round_id)
            rows = connection.execute(
                "SELECT a.application_id, a.segment_id, a.amount_requested, a.status, "
                "s.total_score AS actual_score, s.rank AS actual_rank "
                "FROM project_applications a LEFT JOIN project_scores s "
                "ON s.application_id=a.application_id AND s.round_id=a.round_id "
                "WHERE a.round_id=? AND a.status NOT IN ('cancelled','rejected')",
                (round_id,),
            ).fetchall()
            simulated = []
            for row in rows:
                evidence = self._frozen_evidence(connection, round_id, row["segment_id"])
                result = score_application(evidence, rules,
                                           is_blocking=row["application_id"] in blocking)
                simulated.append({"application_id": row["application_id"],
                                  "score": result["total"],
                                  "actual_score": row["actual_score"],
                                  "actual_rank": row["actual_rank"],
                                  "status": row["status"],
                                  "amount": row["amount_requested"]})
            simulated.sort(key=lambda item: (-item["score"], item["application_id"]))
            # 用当前真实可用余额（已扣除已批准/已结算项目占用）模拟候补项目能否纳入
            real_balances = self._envelope_balances(connection, round_id)
            balances = {level: amount for level, amount in real_balances.items()
                        if level != "emergency"}
            for new_rank, item in enumerate(simulated, start=1):
                item["simulated_rank"] = new_rank
                item["rank_delta"] = (row_actual_delta(item, new_rank))
                if item["status"] in ("approved", "settled", "emergency_approved"):
                    item["simulated_decision"] = "already_decided"
                    continue
                allocation = self._waterfall(balances, item["amount"])
                if allocation is None:
                    item["simulated_decision"] = "waitlisted"
                else:
                    item["simulated_decision"] = "would_approve"
                    for level, part in allocation.items():
                        balances[level] = round(balances[level] - part, 2)
            comparison = [{"application_id": item["application_id"],
                           "actual_rank": item["actual_rank"],
                           "simulated_rank": item["simulated_rank"],
                           "rank_delta": item["rank_delta"],
                           "actual_score": item["actual_score"],
                           "simulated_score": item["score"],
                           "status": item["status"],
                           "simulated_decision": item["simulated_decision"]}
                          for item in simulated
                          if item["status"] not in ("approved", "settled", "emergency_approved")]
            return {"round_id": round_id, "baseline_policy_version": round_row["policy_version"],
                    "simulated_policy_version": policy_version,
                    "note": "模拟结果仅供比较，历史批准决定不重算、不改写",
                    "items": comparison}

    # ------------------------------------------------------------------ 查询与全链路追踪

    def get_round(self, actor_id: str, round_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._actor(connection, actor_id, {"admin", "finance", "highway", "reviewer", "auditor"})
            row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?",
                                     (round_id,)).fetchone()
            if row is None:
                raise NotFoundError("轮次不存在")
            envelopes = [{"level": r["level"], "amount": r["amount"]} for r in connection.execute(
                "SELECT level, amount FROM funding_envelopes WHERE round_id=? ORDER BY level",
                (round_id,))]
            balances = self._envelope_balances(connection, round_id)
            return {"round_id": row["round_id"], "name": row["name"],
                    "deadline_at": row["deadline_at"], "status": row["status"],
                    "policy_version": row["policy_version"],
                    "evidence_cutoff_at": row["evidence_cutoff_at"],
                    "frozen_at": row["frozen_at"],
                    "emergency_quota": row["emergency_quota"],
                    "envelopes": envelopes, "balances": balances}

    def get_portfolio(self, actor_id: str, round_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._actor(connection, actor_id, {"admin", "finance", "highway", "reviewer", "auditor"})
            if connection.execute("SELECT 1 FROM funding_rounds WHERE round_id=?",
                                  (round_id,)).fetchone() is None:
                raise NotFoundError("轮次不存在")
            rows = connection.execute(
                "SELECT a.application_id, a.title, a.segment_id, a.amount_requested, "
                "a.amount_approved, a.status, a.allocation_json, a.decision_reason, "
                "s.total_score, s.rank FROM project_applications a "
                "LEFT JOIN project_scores s ON s.application_id=a.application_id "
                "WHERE a.round_id=? ORDER BY COALESCE(s.rank, 999999), a.application_id",
                (round_id,),
            ).fetchall()
            return {"round_id": round_id, "items": [{
                "application_id": r["application_id"], "title": r["title"],
                "segment_id": r["segment_id"], "score": r["total_score"], "rank": r["rank"],
                "amount_requested": r["amount_requested"], "amount_approved": r["amount_approved"],
                "status": r["status"], "allocation": json.loads(r["allocation_json"] or "{}"),
                "decision_reason": r["decision_reason"],
            } for r in rows]}

    def get_trace(self, actor_id: str, application_id: str) -> dict[str, Any]:
        """追踪一笔资金：申报证据→评分排名→占用→里程碑支付→结算/回收的全过程。"""

        with self.database.transaction() as connection:
            self._actor(connection, actor_id, {"admin", "finance", "highway", "reviewer", "auditor"})
            app = connection.execute("SELECT * FROM project_applications WHERE application_id=?",
                                     (application_id,)).fetchone()
            if app is None:
                raise NotFoundError("申请不存在")
            round_row = connection.execute("SELECT * FROM funding_rounds WHERE round_id=?",
                                           (app["round_id"],)).fetchone()
            evidence_rows = connection.execute(
                "SELECT dimension, payload_hash, effective_at FROM round_evidence_snapshots "
                "WHERE round_id=? AND segment_id=? ORDER BY dimension",
                (app["round_id"], app["segment_id"]),
            ).fetchall()
            if app["work_type"] == "emergency" and not evidence_rows:
                evidence_rows = []
            score_row = connection.execute(
                "SELECT * FROM project_scores WHERE application_id=?", (application_id,)
            ).fetchone()
            milestones = [{
                "seq": r["seq"], "name": r["name"], "amount": r["amount"],
                "due_date": r["due_date"], "status": r["status"],
                "reserved_amount": r["reserved_amount"], "paid_amount": r["paid_amount"],
            } for r in connection.execute(
                "SELECT * FROM project_milestones WHERE application_id=? ORDER BY seq",
                (application_id,))]
            ledger = [{
                "entry_id": r["entry_id"], "period_id": r["period_id"],
                "envelope_id": r["envelope_id"], "entry_type": r["entry_type"],
                "amount": r["amount"], "detail": json.loads(r["detail_json"]),
                "created_by": r["created_by"], "created_at": r["created_at"],
            } for r in connection.execute(
                "SELECT * FROM funding_ledger WHERE application_id=? ORDER BY created_at, entry_id",
                (application_id,))]
            review_row = connection.execute(
                "SELECT * FROM independent_reviews WHERE application_id=?", (application_id,)
            ).fetchone()
            committed = sum(e["amount"] for e in ledger
                            if e["entry_type"] in ("commit", "emergency_commit"))
            paid = sum(e["amount"] for e in ledger if e["entry_type"] == "payment")
            recovered = sum(e["amount"] for e in ledger if e["entry_type"] == "recovery")
            released = sum(e["amount"] for e in ledger
                           if e["entry_type"] in ("cancel_release",))
            changed = sum(e["amount"] for e in ledger if e["entry_type"] == "change")
            return {
                "application_id": application_id,
                "round_id": app["round_id"],
                "round_status": round_row["status"],
                "policy_version_at_freeze": round_row["policy_version"],
                "segment_id": app["segment_id"],
                "title": app["title"],
                "work_type": app["work_type"],
                "status": app["status"],
                "review_status": app["review_status"],
                "amount_requested": app["amount_requested"],
                "amount_approved": app["amount_approved"],
                "allocation": json.loads(app["allocation_json"] or "{}"),
                "window": [app["window_start"], app["window_end"]],
                "depends_on": json.loads(app["depends_on_json"]),
                "decision_reason": app["decision_reason"],
                "created_at": app["created_at"],
                "frozen_evidence": [{"dimension": r["dimension"], "payload_hash": r["payload_hash"],
                                     "effective_at": r["effective_at"]} for r in evidence_rows],
                "scoring": None if score_row is None else {
                    "policy_version": score_row["policy_version"],
                    "total_score": score_row["total_score"], "rank": score_row["rank"],
                    "dimensions": json.loads(score_row["dimension_scores_json"]),
                    "rationale": json.loads(score_row["rationale_json"]),
                    "scored_at": score_row["scored_at"],
                },
                "independent_review": None if review_row is None else {
                    "review_id": review_row["review_id"], "reviewer_id": review_row["reviewer_id"],
                    "verdict": review_row["verdict"], "notes": review_row["notes"],
                    "created_at": review_row["created_at"],
                },
                "milestones": milestones,
                "ledger": ledger,
                "money_summary": {"committed": round(committed + changed - released, 2),
                                  "paid": round(paid, 2), "recovered": round(recovered, 2),
                                  "outstanding": round(committed + changed - released - paid, 2)},
            }


def row_actual_delta(item: dict[str, Any], new_rank: int) -> int | None:
    if item["actual_rank"] is None:
        return None
    return item["actual_rank"] - new_rank  # 正数表示换版后排名上升

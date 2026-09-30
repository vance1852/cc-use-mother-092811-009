"""养护项目评分引擎：版本化、确定性、可逐项解释。

评分政策是一份不可变 JSON 规则，轮次冻结时固定 policy_version。
本模块只做纯计算，不触碰数据库，便于对同一批证据用不同政策做模拟。
"""

from __future__ import annotations

from typing import Any

# 证据维度：评分只接受这些键
EVIDENCE_DIMENSIONS = (
    "condition",            # 设施状况：PCI 路况指数 0-100
    "population",           # 服务人口：served_population + sole_access
    "alternatives",         # 替代性：alternative_routes
    "hazard",               # 灾害暴露：hazard_level 0-100
    "maintenance_history",  # 历史维修：repair_count_3y / last_repair_within_year / repeated_repair
    "traffic",              # 交通量：aadt 年均日交通量
)

DEFAULT_POLICY: dict[str, Any] = {
    "version": "v2026.1",
    "weights": {
        "condition": 0.25,
        "population": 0.15,
        "alternatives": 0.20,
        "hazard": 0.25,
        "maintenance_history": 0.10,
        "traffic": 0.05,
    },
    "condition": {"pci_poor": 40.0, "pci_good": 90.0},
    "population": {"high": 20000, "sole_access_bonus": 15.0},
    "alternatives": {"none_score": 100.0, "per_route_drop": 35.0},
    "hazard": {"low": 10.0, "high": 90.0},
    "maintenance_history": {
        "repeated_repair_score": 80.0,
        "repaired_recent_score": 25.0,
        "aging_score": 60.0,
    },
    "traffic": {"low": 500, "high": 20000},
    "blocking_bonus": 3.0,  # 被其他项目依赖时的排序加分（封顶计入总分）
}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def validate_policy(rules: dict[str, Any]) -> None:
    """校验政策规则结构，权重之和必须为 1。"""

    weights = rules.get("weights")
    if not isinstance(weights, dict):
        raise ValueError("政策缺少 weights")
    missing = [d for d in EVIDENCE_DIMENSIONS if d not in weights]
    if missing:
        raise ValueError(f"政策缺少维度权重: {','.join(missing)}")
    if any(not isinstance(weights[d], (int, float)) or weights[d] < 0 for d in EVIDENCE_DIMENSIONS):
        raise ValueError("维度权重必须是非负数")
    if abs(sum(weights[d] for d in EVIDENCE_DIMENSIONS) - 1.0) > 1e-9:
        raise ValueError("维度权重之和必须为 1")
    for section in ("condition", "population", "alternatives", "hazard",
                    "maintenance_history", "traffic"):
        if not isinstance(rules.get(section), dict):
            raise ValueError(f"政策缺少 {section} 映射参数")


def _score_condition(payload: dict[str, Any], params: dict[str, Any]) -> tuple[float, str]:
    pci = float(payload["pci"])
    poor, good = float(params["pci_poor"]), float(params["pci_good"])
    # PCI 越差得分越高：<=poor 记 100，>=good 记 0
    points = clamp((good - pci) / (good - poor) * 100.0)
    return points, f"PCI={pci:g}，破损越重需求越急（{poor:g}/{good:g} 线性映射）"


def _score_population(payload: dict[str, Any], params: dict[str, Any]) -> tuple[float, str]:
    people = max(0, int(payload.get("served_population", 0)))
    high = float(params["high"])
    points = clamp(people / high * 100.0)
    sole = bool(payload.get("sole_access", False))
    if sole:
        points = clamp(points + float(params["sole_access_bonus"]))
    tag = "，且为唯一通达路线" if sole else ""
    return points, f"服务人口 {people}{tag}，按 {high:g} 人满分线性映射"


def _score_alternatives(payload: dict[str, Any], params: dict[str, Any]) -> tuple[float, str]:
    routes = max(0, int(payload.get("alternative_routes", 0)))
    points = clamp(float(params["none_score"]) - routes * float(params["per_route_drop"]))
    if routes == 0:
        note = "无替代路线，断通即断联"
    else:
        note = f"有 {routes} 条替代路线，每条扣 {params['per_route_drop']:g}"
    return points, note


def _score_hazard(payload: dict[str, Any], params: dict[str, Any]) -> tuple[float, str]:
    level = float(payload["hazard_level"])
    low, high = float(params["low"]), float(params["high"])
    points = clamp((level - low) / (high - low) * 100.0)
    return points, f"灾害暴露指数 {level:g}（{low:g}/{high:g} 线性映射）"


def _score_history(payload: dict[str, Any], params: dict[str, Any]) -> tuple[float, str]:
    if bool(payload.get("repeated_repair", False)):
        return float(params["repeated_repair_score"]), "同一病害三年内反复维修，存在结构性欠账"
    if bool(payload.get("last_repair_within_year", False)):
        return float(params["repaired_recent_score"]), "近一年已维修，短期资金需求低"
    return float(params["aging_score"]), "近期未维修且无反复维修记录，按常规老化计分"


def _score_traffic(payload: dict[str, Any], params: dict[str, Any]) -> tuple[float, str]:
    aadt = max(0, int(payload.get("aadt", 0)))
    low, high = float(params["low"]), float(params["high"])
    points = clamp((aadt - low) / (high - low) * 100.0)
    return points, f"年均日交通量 {aadt}（{low:g}/{high:g} 线性映射）"


_DIMENSION_FN = {
    "condition": _score_condition,
    "population": _score_population,
    "alternatives": _score_alternatives,
    "hazard": _score_hazard,
    "maintenance_history": _score_history,
    "traffic": _score_traffic,
}


def score_application(
    evidence: dict[str, dict[str, Any]],
    rules: dict[str, Any],
    *,
    is_blocking: bool = False,
) -> dict[str, Any]:
    """对一个项目的冻结证据打分。

    evidence: {dimension: payload}，缺维度按 0 分处理并在理由中注明。
    返回 {total, dimensions: {dim: points}, rationale: {dim: note}, missing: [...]}。
    """

    validate_policy(rules)
    weights = rules["weights"]
    dimensions: dict[str, float] = {}
    rationale: dict[str, str] = {}
    missing: list[str] = []
    total = 0.0
    for dim in EVIDENCE_DIMENSIONS:
        payload = evidence.get(dim)
        if not payload:
            dimensions[dim] = 0.0
            rationale[dim] = "轮次冻结时缺少该维度证据，按 0 分处理"
            missing.append(dim)
            continue
        try:
            points, note = _DIMENSION_FN[dim](payload, rules[dim])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"维度 {dim} 的证据字段不完整: {exc}") from exc
        dimensions[dim] = round(points, 2)
        rationale[dim] = note
        total += weights[dim] * points
    if is_blocking:
        bonus = float(rules.get("blocking_bonus", 0.0))
        total = clamp(total + bonus)
        rationale["__dependency__"] = f"本项目是其他项目的前置依赖，排序加分 {bonus:g}"
    else:
        rationale["__dependency__"] = "无项目依赖加分"
    return {
        "total": round(total, 2),
        "dimensions": dimensions,
        "rationale": rationale,
        "missing": missing,
    }

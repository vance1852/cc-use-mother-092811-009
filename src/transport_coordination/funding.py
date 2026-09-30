"""养护资金决策使用的纯规则：评分政策、冲突识别与资金组合选择。

本模块不接触数据库和时钟，全部函数对结构化字典求值，便于离线复核与
在不改变历史决定的情况下试算不同政策版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 评分因子键，顺序同时决定投资组合解释中的展示顺序。
FACTOR_KEYS = (
    "condition",       # 设施状况
    "service",         # 服务人口
    "alternative",     # 替代性（唯一通达）
    "disaster",        # 灾害暴露
    "maintenance",     # 历史维修投入回报
    "dependency",      # 项目依赖协同
)

FACTOR_NAMES = {
    "condition": "设施状况",
    "service": "服务人口",
    "alternative": "替代性",
    "disaster": "灾害暴露",
    "maintenance": "历史维修",
    "dependency": "项目依赖",
}

FUNDING_LEVELS = ("central", "provincial", "county")
LEVEL_NAMES = {"central": "中央", "provincial": "省级", "county": "县级"}

DUPLICATE_THRESHOLD = 0.97   # 证据/位置相似度达到该值视为重复申报
SPLIT_GAP_KM = 1.0           # 同路线相邻申报间隔小于该值视为拆项


def validate_policy(spec: dict[str, Any]) -> dict[str, float]:
    """校验评分政策并返回归一化后的权重表。"""

    if not isinstance(spec, dict):
        raise ValueError("评分政策必须是对象")
    weights = spec.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("评分政策必须包含非空 weights")
    normalized: dict[str, float] = {}
    total = 0.0
    for key in FACTOR_KEYS:
        raw = weights.get(key, 0)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
            raise ValueError(f"权重 {key} 必须是非负数字")
        value = float(raw)
        normalized[key] = value
        total += value
    extra = set(weights) - set(FACTOR_KEYS)
    if extra:
        raise ValueError(f"存在未知评分因子: {sorted(extra)}")
    if round(total, 6) <= 0:
        raise ValueError("权重之和必须大于零")
    for key in normalized:
        normalized[key] = round(normalized[key] / total, 6)
    return normalized


def score_project(evidence: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    """按 0~100 标准化各因子并加权汇总，同时给出可解释依据。"""

    def clip(value: Any, field: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"证据字段 {field} 必须是数字")
        value = float(value)
        if not 0.0 <= value <= 100.0:
            raise ValueError(f"证据字段 {field} 必须落在 0~100")
        return value

    condition = clip(evidence.get("condition_index"), "condition_index")
    service_population = evidence.get("service_population", 0)
    if not isinstance(service_population, int) or isinstance(service_population, bool) or service_population < 0:
        raise ValueError("service_population 必须是非负整数")
    sole_access = bool(evidence.get("sole_access", False))
    alternative_count = evidence.get("alternative_routes", 0)
    if not isinstance(alternative_count, int) or isinstance(alternative_count, bool) or alternative_count < 0:
        raise ValueError("alternative_routes 必须是非负整数")
    disaster = clip(evidence.get("disaster_exposure"), "disaster_exposure")
    history_ratio = evidence.get("maintenance_history_ratio", 0.0)
    if not isinstance(history_ratio, (int, float)) or isinstance(history_ratio, bool) or not 0.0 <= history_ratio <= 3.0:
        raise ValueError("maintenance_history_ratio 必须落在 0~3")
    blocked_dependents = evidence.get("blocked_dependents", 0)
    if not isinstance(blocked_dependents, int) or isinstance(blocked_dependents, bool) or blocked_dependents < 0:
        raise ValueError("blocked_dependents 必须是非负整数")

    # 设施状况指数越高越差（PCI 反向口径，由申报方换算后提交）。
    condition_score = condition
    # 服务人口对数饱和，避免高流量干线单纯按规模压过通达性项目。
    service_score = 0.0 if service_population <= 0 else min(100.0, 18.0 * _log10(service_population + 1))
    # 唯一通达且无替代路线得满分，每多一条替代路线折减。
    alternative_score = 100.0 if sole_access and alternative_count == 0 else max(
        0.0, 100.0 - alternative_count * 25.0 - (0.0 if sole_access else 10.0)
    )
    disaster_score = disaster
    # 历史维修长期欠账（比值低）得分高；比值超过 1 后不再加分。
    maintenance_score = max(0.0, min(100.0, 100.0 * (1.0 - history_ratio / 3.0)))
    # 每个被前置依赖卡住的项目加 20 分，封顶 100。
    dependency_score = min(100.0, blocked_dependents * 20.0)

    factors = {
        "condition": round(condition_score, 3),
        "service": round(service_score, 3),
        "alternative": round(alternative_score, 3),
        "disaster": round(disaster_score, 3),
        "maintenance": round(maintenance_score, 3),
        "dependency": round(dependency_score, 3),
    }
    total = round(sum(factors[key] * weights[key] for key in FACTOR_KEYS), 3)
    basis = {
        "condition_index": condition,
        "service_population": service_population,
        "sole_access": sole_access,
        "alternative_routes": alternative_count,
        "disaster_exposure": disaster,
        "maintenance_history_ratio": float(history_ratio),
        "blocked_dependents": blocked_dependents,
    }
    return {"factors": factors, "basis": basis, "total_score": total}


def _log10(value: float) -> float:
    import math

    return math.log10(value)


def location_overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    """返回两个申报在同一编码路线上的桩号重叠长度（公里），不同路线为 0。"""

    if a["route_code"] != b["route_code"]:
        return 0.0
    left = max(float(a["start_km"]), float(b["start_km"]))
    right = min(float(a["end_km"]), float(b["end_km"]))
    return max(0.0, right - left)


def detect_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """重复申报：同一（或高度重合）路段、证据几乎一致。"""

    overlap = location_overlap(a, b)
    span = min(float(a["end_km"]) - float(a["start_km"]), float(b["end_km"]) - float(b["start_km"]))
    if span <= 0 or overlap / span < DUPLICATE_THRESHOLD:
        return False
    return a.get("evidence_hash") == b.get("evidence_hash")


def detect_split(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """同一路段拆项：同路线、申报区间重叠或留有小于阈值的短口，且来自同一组织。"""

    if a["route_code"] != b["route_code"] or a.get("organization_id") != b.get("organization_id"):
        return False
    if location_overlap(a, b) > 0:
        return True
    gap = max(float(a["start_km"]), float(b["start_km"])) - min(float(a["end_km"]), float(b["end_km"]))
    return 0.0 <= gap < SPLIT_GAP_KM


def windows_conflict(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """互斥施工窗口：位置重叠且施工时间窗相交。"""

    if location_overlap(a, b) <= 0:
        return False
    start_a, end_a = a.get("window_start"), a.get("window_end")
    start_b, end_b = b.get("window_start"), b.get("window_end")
    if not (start_a and end_a and start_b and end_b):
        return False
    return max(start_a, start_b) <= min(end_a, end_b)


@dataclass(frozen=True)
class Flag:
    project_a: str
    project_b: str
    flag_type: str  # duplicate | split | window
    blocking: bool
    detail: dict[str, Any]


def detect_flags(projects: list[dict[str, Any]]) -> list[Flag]:
    """在一个轮次的全部申报之间识别重复、拆项与施工窗口互斥。

    重复申报与拆项是批准前必须阻断的硬冲突；施工窗口互斥默认阻断，
    但若两个项目本就不会同时入选（同一硬冲突组），标记为非阻断信息。
    """

    flags: list[Flag] = []
    for i in range(len(projects)):
        for j in range(i + 1, len(projects)):
            a, b = projects[i], projects[j]
            if detect_duplicate(a, b):
                flags.append(Flag(a["project_id"], b["project_id"], "duplicate", True,
                                  {"overlap_km": round(location_overlap(a, b), 3)}))
            if detect_split(a, b):
                flags.append(Flag(a["project_id"], b["project_id"], "split", True,
                                  {"route_code": a["route_code"]}))
            if windows_conflict(a, b):
                flags.append(Flag(a["project_id"], b["project_id"], "window", True,
                                  {"window": [a.get("window_start"), a.get("window_end"),
                                              b.get("window_start"), b.get("window_end")]}))
    return flags


def dependency_blocked(project: dict[str, Any], selected: set[str],
                       by_id: dict[str, Any], visiting: frozenset = frozenset()) -> bool:
    """项目的前置链未入选、被拒或形成环时不可入选。"""

    depends_on = project.get("depends_on")
    if not depends_on:
        return False
    if depends_on not in selected:
        return True
    if depends_on in visiting:
        return True
    parent = by_id.get(depends_on)
    if parent is None:
        return True
    return dependency_blocked(parent, selected, by_id, visiting | {project["project_id"]})


def select_portfolio(ranked: list[dict[str, Any]], budgets: dict[str, int]) -> dict[str, Any]:
    """在各级资金约束下按排名贪心选择投资组合。

    ranked 已按分数降序排列（平分由调用方用 project_id 打破）。每个项目只
    消耗其申报级次的额度；前置项目未入选则跳过。任何硬冲突（重复/拆项/
    窗口）中的两方至多入选一个，优先保留排名靠前的一方。
    """

    budgets = {level: int(budgets.get(level, 0)) for level in FUNDING_LEVELS}
    spent = {level: 0 for level in FUNDING_LEVELS}
    by_id = {item["project_id"]: item for item in ranked}
    selected: set[str] = set()
    blocked_partners: dict[str, str] = {}
    for item in ranked:
        for other in item.get("conflicts", ()):  # 冲突对端项目 id
            blocked_partners.setdefault(other, item["project_id"])

    # 按排名顺序决定；遇到前置链未决的子项目时，先递归决定其前置链，
    # 等于在该子项目的排名位置上对整条链做捆绑评估，避免低排名项目
    # 仅凭扫描顺序挤占高排名子项目的额度。
    decisions: dict[str, dict[str, Any]] = {}

    def resolve(item: dict[str, Any], chain: frozenset[str] = frozenset()) -> None:
        pid = item["project_id"]
        if pid in decisions or pid in chain:
            return
        parent_id = item.get("depends_on")
        if parent_id:
            parent = by_id.get(parent_id)
            if parent is not None:
                resolve(parent, chain | {pid})

        level = item["funding_level"]
        amount = int(item["requested_amount"])
        reasons: list[str] = []
        if pid in blocked_partners and blocked_partners[pid] in selected:
            reasons.append(f"冲突方 {blocked_partners[pid]} 已优先入选")
        if dependency_blocked(item, selected, by_id):
            reasons.append("前置依赖项目未入选")
        if spent[level] + amount > budgets[level]:
            reasons.append(f"{LEVEL_NAMES[level]}资金额度不足")
        accepted = not reasons
        if accepted:
            selected.add(pid)
            spent[level] += amount
        decisions[pid] = {"project_id": pid, "decision": "funded" if accepted else "deferred",
                          "reasons": reasons, "rank": item["rank"], "score": item["total_score"]}

    for item in ranked:
        resolve(item)

    ordered = [decisions[item["project_id"]] for item in ranked]
    return {
        "decisions": ordered,
        "selected": sorted(selected),
        "spent": spent,
        "budgets": budgets,
        "remaining": {level: budgets[level] - spent[level] for level in FUNDING_LEVELS},
    }

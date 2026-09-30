import unittest

from transport_coordination.funding import (
    detect_flags,
    detect_split,
    detect_duplicate,
    score_project,
    select_portfolio,
    validate_policy,
    windows_conflict,
)


def project(pid, route="X1", start=0.0, end=10.0, **overrides):
    data = {"project_id": pid, "route_code": route, "start_km": start, "end_km": end,
            "organization_id": "o1", "funding_level": "county", "requested_amount": 100,
            "evidence_hash": "h", "window_start": None, "window_end": None,
            "depends_on": None}
    data.update(overrides)
    return data


def evidence(**overrides):
    data = {"condition_index": 50, "service_population": 1000, "sole_access": False,
            "alternative_routes": 1, "disaster_exposure": 50,
            "maintenance_history_ratio": 1.0, "blocked_dependents": 0}
    data.update(overrides)
    return data


class PolicyValidationTest(unittest.TestCase):
    def test_weights_are_normalized_to_sum_one(self):
        weights = validate_policy({"weights": {"condition": 2, "disaster": 2}})
        self.assertAlmostEqual(weights["condition"], 0.5)
        self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_unknown_factor_rejected(self):
        with self.assertRaises(ValueError):
            validate_policy({"weights": {"condition": 1, "mileage": 2}})

    def test_all_zero_rejected(self):
        with self.assertRaises(ValueError):
            validate_policy({"weights": {"condition": 0}})

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            validate_policy({"weights": {"condition": -1}})


class ScoringTest(unittest.TestCase):
    W = validate_policy({"weights": {"condition": 1, "service": 1, "alternative": 1,
                                     "disaster": 1, "maintenance": 1, "dependency": 1}})

    def test_sole_access_without_alternative_scores_full_alternative(self):
        rural = score_project(evidence(sole_access=True, alternative_routes=0), self.W)
        arterial = score_project(evidence(sole_access=False, alternative_routes=2,
                                          service_population=200000), self.W)
        self.assertEqual(rural["factors"]["alternative"], 100.0)
        self.assertLess(arterial["factors"]["alternative"], 100.0)

    def test_high_disaster_rural_can_outrank_low_disaster_arterial(self):
        # 提高灾害与替代性权重后，乡村唯一通达路应能排过高流量低灾害干线。
        weights = validate_policy({"weights": {"alternative": 3, "disaster": 4, "condition": 1,
                                               "service": 1, "maintenance": 1}})
        rural = score_project(evidence(condition_index=80, sole_access=True, alternative_routes=0,
                                       disaster_exposure=95, maintenance_history_ratio=0.1), weights)
        arterial = score_project(evidence(condition_index=60, service_population=200000,
                                          alternative_routes=2, disaster_exposure=15,
                                          maintenance_history_ratio=1.0), weights)
        self.assertGreater(rural["total_score"], arterial["total_score"])

    def test_basis_is_returned_for_explanation(self):
        result = score_project(evidence(sole_access=True), self.W)
        self.assertTrue(result["basis"]["sole_access"])
        self.assertEqual(set(result["factors"]),
                         {"condition", "service", "alternative", "disaster",
                          "maintenance", "dependency"})

    def test_out_of_range_evidence_rejected(self):
        with self.assertRaises(ValueError):
            score_project(evidence(disaster_exposure=120), self.W)


class ConflictDetectionTest(unittest.TestCase):
    def test_overlapping_same_evidence_is_duplicate(self):
        a = project("a", start=0, end=10, evidence_hash="same")
        b = project("b", start=0.5, end=9.5, evidence_hash="same")
        self.assertTrue(detect_duplicate(a, b))

    def test_overlap_with_different_evidence_is_not_duplicate(self):
        a = project("a", start=0, end=10, evidence_hash="h1")
        b = project("b", start=0.5, end=9.5, evidence_hash="h2")
        self.assertFalse(detect_duplicate(a, b))

    def test_adjacent_same_route_same_org_is_split(self):
        a = project("a", start=0, end=10)
        b = project("b", start=10.4, end=15)
        self.assertTrue(detect_split(a, b))
        self.assertTrue(detect_flags([a, b]))

    def test_gap_too_large_is_not_split(self):
        a = project("a", start=0, end=10)
        b = project("b", start=13, end=15)
        self.assertFalse(detect_split(a, b))

    def test_different_route_not_split(self):
        a = project("a", route="X1")
        b = project("b", route="X2", start=0, end=10)
        self.assertFalse(detect_split(a, b))

    def test_overlapping_window_conflict(self):
        a = project("a", start=0, end=10, window_start="2027-05-01", window_end="2027-06-01")
        b = project("b", start=5, end=15, window_start="2027-05-20", window_end="2027-07-01")
        self.assertTrue(windows_conflict(a, b))

    def test_overlapping_route_but_separate_windows_ok(self):
        a = project("a", start=0, end=10, window_start="2027-05-01", window_end="2027-06-01")
        b = project("b", start=5, end=15, window_start="2027-07-01", window_end="2027-08-01")
        self.assertFalse(windows_conflict(a, b))


class PortfolioSelectionTest(unittest.TestCase):
    def _ranked(self, items):
        ranked = []
        for rank, item in enumerate(items, start=1):
            row = {"rank": rank, "total_score": 100 - rank, "conflicts": (),
                   "depends_on": None, "funding_level": "county"}
            row.update(item)
            ranked.append(row)
        return ranked

    def test_respects_per_level_budget(self):
        ranked = self._ranked([
            {"project_id": "a", "funding_level": "county", "requested_amount": 600},
            {"project_id": "b", "funding_level": "county", "requested_amount": 500},
        ])
        result = select_portfolio(ranked, {"central": 0, "provincial": 0, "county": 1000})
        self.assertEqual(result["selected"], ["a"])
        decision_b = next(d for d in result["decisions"] if d["project_id"] == "b")
        self.assertEqual(decision_b["decision"], "deferred")
        self.assertTrue(any("额度不足" in r for r in decision_b["reasons"]))

    def test_dependency_requires_parent(self):
        ranked = self._ranked([
            {"project_id": "child", "requested_amount": 10, "depends_on": "parent"},
            {"project_id": "parent", "requested_amount": 995},
        ])
        # 预算只够 child，但 parent 落选，child 必须连带落选。
        result = select_portfolio(ranked, {"central": 0, "provincial": 0, "county": 100})
        self.assertEqual(result["selected"], [])

    def test_conflict_pair_keeps_higher_rank_only(self):
        ranked = self._ranked([
            {"project_id": "a", "requested_amount": 10, "conflicts": ("b",)},
            {"project_id": "b", "requested_amount": 10, "conflicts": ("a",)},
        ])
        result = select_portfolio(ranked, {"central": 0, "provincial": 0, "county": 1000})
        self.assertEqual(result["selected"], ["a"])


if __name__ == "__main__":
    unittest.main()

import unittest

from transport_coordination.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(1, result["records"])
        # 养护资金决策与执行链路关键结果
        self.assertTrue(result["village_ranks_first"])
        self.assertTrue(result["village_settled"])
        self.assertTrue(result["closed_period_blocks_writes"])
        self.assertTrue(result["emergency_has_independent_review"])
        self.assertEqual("v2026.1", result["frozen_policy"])


if __name__ == "__main__":
    unittest.main()

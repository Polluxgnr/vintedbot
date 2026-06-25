from __future__ import annotations

import unittest

from vf.scoring import rank_rows
from vf.engine import sort_rows_by_sort_by
from vf.market import build_search_plan


class SortingAndPriceTests(unittest.TestCase):
    def test_build_search_plan_contains_price_bounds(self):
        plan = build_search_plan(
            "PS4 Slim",
            platforms=["vinted"],
            category_hint="console_ps4",
            price_min=80,
            price_max=160,
        )
        self.assertTrue(plan.variants, "SearchPlan should contain at least one variant")
        for v in plan.variants:
            if v.platform != "vinted":
                continue
            self.assertGreaterEqual(int(v.price_min or 0), 80)
            self.assertLessEqual(int(v.price_max or 9999), 160)
            self.assertIn("price_from=", v.url)
            self.assertIn("price_to=", v.url)

    def test_sort_rows_by_sort_by_price(self):
        rows = [
            {"id": "a", "total_price": 120, "rank_final_score": 70},
            {"id": "b", "total_price": 80, "rank_final_score": 60},
            {"id": "c", "total_price": 160, "rank_final_score": 90},
        ]
        asc = sort_rows_by_sort_by(rows, "price_asc")
        self.assertEqual([r["id"] for r in asc], ["b", "a", "c"])
        desc = sort_rows_by_sort_by(rows, "price_desc")
        self.assertEqual([r["id"] for r in desc], ["c", "a", "b"])

    def test_sort_rows_by_sort_by_ai(self):
        rows = [
            {"id": "a", "rank_final_score": 77.2},
            {"id": "b", "rank_final_score": 88.0},
            {"id": "c", "rank_final_score": 12.0},
        ]
        out = sort_rows_by_sort_by(rows, "ai")
        self.assertEqual([r["id"] for r in out], ["b", "a", "c"])

    def test_ai_ranker_produces_full_ranking(self):
        rows = [
            {"id": "1", "value_score_v2": 70, "ai_relevance": 80, "spec_confidence": 0.9},
            {"id": "2", "value_score_v2": 72, "ai_relevance": 40, "spec_confidence": 0.4, "risk_flags": "ban"},
            {"id": "3", "value_score_v2": 68, "ai_relevance": 78, "spec_confidence": 0.8, "ai_completeness": 85},
        ]
        ranked = rank_rows(rows, category="console_ps4", criteria_plan={"label": "PS4"})
        self.assertEqual(len(ranked), 3)
        for r in ranked:
            self.assertIn("rank_final", r)
            self.assertIn("rank_final_score", r)
            self.assertIn("ai_adjustment", r)
            self.assertIn("rank_reason", r)
            self.assertIn("criteria_hits", r)
        self.assertNotEqual(ranked[0]["id"], "2")


if __name__ == "__main__":
    unittest.main()


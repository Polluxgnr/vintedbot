"""Tests — scoring expert générique (veto technique, specs gonflées)."""
from __future__ import annotations

import unittest

from vf.scoring import (
    apply_category_heuristics,
    apply_hyper_pro_scoring,
    build_market_context,
    is_technical_veto_row,
    select_top_candidates,
)


class ExpertScoringTests(unittest.TestCase):
    def test_viewsonic_svga_veto(self):
        row = {
            "id": "vs",
            "title": "ViewSonic PJD5155 SVGA vidéoprojecteur",
            "price": 45.0,
            "total_price": 45.0,
            "item_type": "projector",
            "platform": "vinted",
            "triage_decision": "keep",
            "seller_feedback_count": 3,
        }
        apply_category_heuristics(row, "projector")
        self.assertTrue(row.get("is_obsolete"))
        self.assertTrue(row.get("technical_veto"))
        ctx = build_market_context([row])
        apply_hyper_pro_scoring([row], ctx)
        self.assertLessEqual(row["score_composite"], 15.0)
        self.assertLessEqual(row["best_deal_score"], 15.0)
        self.assertEqual(row.get("deal_tier"), "rejet")
        self.assertTrue(is_technical_veto_row(row))

    def test_vankyo_inflated_lumens(self):
        row = {
            "id": "vk",
            "title": "Vankyo Leisure 3 projecteur 6700 lumens HDMI",
            "price": 80.0,
            "total_price": 80.0,
            "item_type": "projector",
            "lumens": 6700,
            "has_hdmi": True,
        }
        apply_category_heuristics(row, "projector")
        self.assertTrue(row.get("inflated_specs"))
        self.assertEqual(row.get("brand_tier"), 3)
        self.assertLessEqual(int(row.get("lumens") or 0), 700)
        self.assertGreaterEqual(int(row.get("lumens") or 0), 600)

    def test_epson_ok(self):
        quality = {
            "id": "ep",
            "title": "Epson EH-TW650 3500 lumens Full HD HDMI",
            "price": 220.0,
            "total_price": 220.0,
            "item_type": "projector",
            "lumens": 3500,
            "resolution": "1080p",
            "projector_brand": "epson",
            "has_hdmi": True,
            "platform": "vinted",
            "triage_decision": "keep",
            "seller_feedback_count": 8,
        }
        cheap_svga = {
            "id": "vs",
            "title": "ViewSonic PJD5155 SVGA",
            "price": 45.0,
            "total_price": 45.0,
            "item_type": "projector",
            "platform": "vinted",
            "triage_decision": "keep",
        }
        rows = [cheap_svga, quality]
        for r in rows:
            apply_category_heuristics(r, "projector")
        ctx = build_market_context(rows)
        apply_hyper_pro_scoring(rows, ctx)
        self.assertFalse(quality.get("technical_veto"))
        self.assertFalse(quality.get("is_obsolete"))
        self.assertEqual(quality.get("brand_tier"), 1)
        self.assertGreater(quality["best_deal_score"], 40.0)
        candidates, _ = select_top_candidates(rows, category="projector", query="video projecteur", limit=5)
        ids = [str(c.get("id")) for c in candidates]
        self.assertIn("ep", ids)
        self.assertNotIn("vs", ids)

    def test_philips_neopix_tier3_not_auto_deal(self):
        row = {
            "id": "np",
            "title": "Philips NeoPix 94 projecteur portable HDMI",
            "price": 94.0,
            "total_price": 94.0,
            "item_type": "projector",
            "has_hdmi": True,
            "lumens": 2000,
        }
        apply_category_heuristics(row, "projector")
        self.assertEqual(row.get("brand_tier"), 3)


if __name__ == "__main__":
    unittest.main()

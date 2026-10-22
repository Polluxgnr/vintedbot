"""Tests top5_analyst — selection et fallback par categorie."""
from __future__ import annotations

import unittest

from vf.scoring import _fallback_top5, analyze_top5


def _phone_rows():
    return [
        {
            "id": "p1",
            "title": "iPhone 15 128 Go batterie 88%",
            "price": 350,
            "total_price": 350,
            "score_composite": 80,
            "deal_tier": "ok",
        },
        {
            "id": "p2",
            "title": "iPhone 15 Pro Max 256 Go",
            "price": 500,
            "total_price": 500,
            "score_composite": 75,
            "deal_tier": "ok",
        },
    ]


def _velo_rows():
    return [
        {
            "id": "v1",
            "title": "VTT Scott taille M carbone",
            "price": 600,
            "total_price": 600,
            "score_composite": 78,
            "deal_tier": "ok",
        },
        {
            "id": "v2",
            "title": "Velo ville taille L",
            "price": 250,
            "total_price": 250,
            "score_composite": 70,
            "deal_tier": "ok",
        },
    ]


class Top5AnalystTests(unittest.TestCase):
    def test_analyze_top5_phone_fallback(self):
        data = analyze_top5(
            _phone_rows(),
            query="iPhone 15 128 Go",
            category="phone",
            use_ai=False,
        )
        self.assertEqual(len(data["top5"]), 2)
        self.assertEqual(data["category"], "phone")
        top = data["top5"][0]
        self.assertIn("extracted_specs", top)
        specs = top["extracted_specs"]
        self.assertEqual(specs.get("battery_health_pct"), 88)

    def test_analyze_top5_velo_fallback(self):
        data = analyze_top5(
            _velo_rows(),
            query="velo carbone M",
            category="velo",
            use_ai=False,
        )
        self.assertEqual(data["category"], "bicycle")
        top = data["top5"][0]
        specs = top.get("extracted_specs") or {}
        self.assertNotIn("battery_health_pct", specs)
        highlights = " ".join(top.get("highlights") or []).lower()
        self.assertNotIn("batterie", highlights)

    def test_fallback_includes_market_summary(self):
        from vf.scoring import select_top_candidates

        candidates, ctx = select_top_candidates(_phone_rows(), category="phone", query="iphone 15")
        fb = _fallback_top5(candidates, ctx)
        self.assertTrue(fb.get("market_summary"))


if __name__ == "__main__":
    unittest.main()

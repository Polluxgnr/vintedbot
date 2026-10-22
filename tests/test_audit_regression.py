"""Regression tests - refactor intent/triage + badges."""
from __future__ import annotations
import unittest
from vf.engine import SearchIntent, parse_search_intent, triage_title
from vf.intent import primary_product_family
from vf.scoring import apply_category_heuristics, row_spec_badges, spec_badges
from vf.triage import TitleDecision, classify_rejected_listing

class AuditRegressionTests(unittest.TestCase):
    def test_intent_module_new3ds_strict(self):
        si = parse_search_intent("New Nintendo 3DS XL")
        self.assertIn("console_ds", si.product_families)
        self.assertEqual(primary_product_family(si), "console_ds")

    def test_technical_veto_obsolete_projector(self):
        row = {"title": "Mini projecteur LED 8000 lumens", "price": 25, "item_type": "projector", "resolution": "svga", "legacy_ports_only": True}
        apply_category_heuristics(row, "projector")
        self.assertTrue(row.get("technical_veto") or row.get("is_obsolete"))
        badges = row_spec_badges(row, "projector")
        self.assertTrue(any("Obsol" in b or "Veto" in b for b in badges))

    def test_bon_plan_badge(self):
        badges = spec_badges({"deal_tier": "pepite", "completeness_score": 85}, "console_ps4")
        self.assertIn("Bon plan", badges)

if __name__ == "__main__":
    unittest.main()

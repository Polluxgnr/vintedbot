"""Tests for VF Finder improvements."""
import unittest
from vf.market import build_search_plan, price_band_variants
from vf.models import category_config_dict, get_category_config
from vf.scoring import apply_universal_tags

class PriceBandScrapeTests(unittest.TestCase):
    def test_price_band_variants_intersect_50_200_projector(self):
        bands = price_band_variants("videoprojecteur", category_hint="projector", price_from=50, price_to=200)
        self.assertTrue(bands)
        for band in bands:
            self.assertGreaterEqual(int(band["price_from"]), 50)
            self.assertLessEqual(int(band["price_to"]), 200)

    def test_build_search_plan_uses_price_bands(self):
        plan = build_search_plan("videoprojecteur epson", platforms=["vinted"], category_hint="projector", price_min=50, price_max=200)
        vinted = [v for v in plan.variants if v.platform == "vinted"]
        self.assertTrue(vinted)
        for v in vinted:
            self.assertGreaterEqual(int(v.price_min or 0), 50)
            self.assertLessEqual(int(v.price_max or 9999), 200)

class CategoryConfigTests(unittest.TestCase):
    def test_projector_config_loads(self):
        cfg = get_category_config("projector")
        self.assertIsNotNone(cfg)
        self.assertIn("lumens", cfg.specs_a_extraire)
        self.assertEqual(category_config_dict("projector")["categorie"], "projector")

    def test_console_ds_config_loads(self):
        cfg = get_category_config("console_ds")
        self.assertIsNotNone(cfg)
        self.assertIn("variant", cfg.specs_a_extraire)

class UniversalTagsTests(unittest.TestCase):
    def test_dinosaur_maps_to_obsolete(self):
        row = {"item_type": "projector", "quality_tier": "dinosaur", "legacy_ports_only": True, "risk_flags": "no_hdmi", "pack_score": 45, "has_hdmi": False}
        apply_universal_tags(row)
        self.assertTrue(row["is_obsolete"])
        self.assertEqual(row["completeness_score"], 45)
        self.assertIn("HDMI", row["completeness_missing"])

    def test_modern_row_not_obsolete(self):
        row = {"item_type": "console_ds", "quality_tier": "ok", "has_controller": True, "ai_completeness": 80}
        apply_universal_tags(row)
        self.assertFalse(row["is_obsolete"])
        self.assertEqual(row["completeness_score"], 80)

if __name__ == "__main__":
    unittest.main()

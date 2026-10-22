"""Tests for idle refinement mapping."""
import unittest

from vf.market import (
    idle_refinement_specs,
    refinements_to_buyer_notes,
    refinements_to_pre_filter_defaults,
    refinements_to_preflight_overrides,
    unified_market_specs,
)


class RefinementMappingTests(unittest.TestCase):
    def test_projector_idle_specs_include_controls(self):
        specs = idle_refinement_specs("projector", "videoprojecteur epson")
        ids = {s["id"] for s in specs}
        self.assertIn("lumens_min", ids)
        self.assertIn("projector_brand", ids)
        self.assertIn("resolution", ids)
        self.assertIn("hdmi", ids)
        self.assertIn("usage", ids)
        self.assertIn("quality_min", ids)
        lum = next(s for s in specs if s["id"] == "lumens_min")
        self.assertEqual(lum.get("type"), "slider")

    def test_projector_refinements_map_to_preflight_and_filters(self):
        ref = {
            "lumens_min": "3000",
            "projector_brand": "epson",
            "resolution": "1080p",
            "hdmi": "required",
            "usage": "cinema",
            "quality_min": "70",
            "price_min": "80",
            "price_max": "500",
        }
        pf = refinements_to_preflight_overrides(ref)
        self.assertTrue(pf.get("min_lumens_2000"))
        self.assertTrue(pf.get("require_hdmi"))
        self.assertTrue(pf.get("proj_brand"))
        defaults = refinements_to_pre_filter_defaults(ref, "projector")
        self.assertEqual(defaults.get("lumens_band"), "3000+")
        self.assertEqual(defaults.get("resolution"), "1080p")
        self.assertEqual(defaults.get("has_hdmi"), "yes")
        self.assertEqual(defaults.get("projector_brand"), "epson")
        self.assertEqual(defaults.get("value_score_min"), "70")
        self.assertEqual(defaults.get("price_min"), "80")
        notes = refinements_to_buyer_notes(ref)
        self.assertIn("Epson", notes)
        self.assertIn("3000", notes)

    def test_unified_specs_honor_refinements(self):
        ref = {"lumens_min": "2500", "hdmi": "required", "projector_brand": "benq"}
        specs = unified_market_specs("projector", "projecteur", ref)
        by_id = {s["id"]: s for s in specs}
        self.assertTrue(by_id["require_hdmi"].get("default"))
        self.assertTrue(by_id["min_lumens_2000"].get("default"))
        self.assertIn("2500", by_id["min_lumens_2000"].get("label", ""))

    def test_console_refinements_unchanged(self):
        specs = idle_refinement_specs("console_ds", "nintendo 3ds")
        ids = {s["id"] for s in specs}
        self.assertIn("pack_level", ids)
        self.assertNotIn("projector_brand", ids)


if __name__ == "__main__":
    unittest.main()

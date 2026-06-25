import unittest

from vf.engine import discover_criteria_plan, plan_early_exclude_match


class TestCriteriaPlan(unittest.TestCase):
    def test_early_exclude_match(self):
        plan = {"early_excludes": ["compresseur", "clio"], "version": 1}
        self.assertTrue(plan_early_exclude_match("Compresseur clim Renault Clio", plan))
        self.assertIsNone(plan_early_exclude_match("Climatiseur mobile 9000 BTU", plan))

    def test_discover_offline_plan_shape(self):
        # offline mode: no API key required, should still return a sanitized plan
        res = discover_criteria_plan(query="clim", category="ac", sample_rows=[{"title": "Climatiseur mobile 9000 BTU", "item_type": "ac"}], buyer_priorities="")
        self.assertIsInstance(res.plan, dict)
        self.assertEqual(res.plan.get("version"), 1)
        self.assertIn("ui_filters", res.plan)
        self.assertIn("early_excludes", res.plan)
        self.assertIn("spec_patterns", res.plan)
        ui_ids = {f.get("id") for f in res.plan.get("ui_filters") or []}
        self.assertIn("ac_unit_only", ui_ids)


if __name__ == '__main__':
    unittest.main()

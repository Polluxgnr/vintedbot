from __future__ import annotations
import unittest
from vf.engine import SearchIntent, TitleDecision, triage_title
from vf.engine import triage_learning_hit
from vf.market import CatalogItem

def _item(title, price=60.0):
    return CatalogItem(id="t1", title=title, price=price, total_price=price)

def _ps4_intent():
    return SearchIntent(raw="PS4", search_text="PS4", product_families=["console_ps4"], platform_hints=["ps4"])

class TriagePs4Tests(unittest.TestCase):
    def test_short_ps4_keep(self):
        tr = triage_title(_item("Ps4", 60), _ps4_intent())
        self.assertEqual(tr.decision, TitleDecision.KEEP)
        self.assertNotIn("empty_title", tr.reasons)

    def test_ps4_plus_jeux_keep(self):
        tr = triage_title(_item("PS4 + jeux", 75), _ps4_intent())
        self.assertEqual(tr.decision, TitleDecision.KEEP)

    def test_jeux_ps4_keep(self):
        tr = triage_title(_item("Jeux Ps4", 67), _ps4_intent())
        self.assertEqual(tr.decision, TitleDecision.KEEP)

    def test_xbox_reject(self):
        tr = triage_title(_item("Xbox 360 RGH", 80), _ps4_intent())
        self.assertEqual(tr.decision, TitleDecision.REJECT)
        self.assertIn("wrong_platform", tr.reasons)

    def test_famicom_reject(self):
        tr = triage_title(_item("Jeu Super Famicom", 25), _ps4_intent())
        self.assertEqual(tr.decision, TitleDecision.REJECT)

    def test_switch_reject(self):
        tr = triage_title(_item("Nintendo Switch OLED", 200), _ps4_intent())
        self.assertEqual(tr.decision, TitleDecision.REJECT)

    def test_generic_console_keep(self):
        tr = triage_title(_item("Console", 90), _ps4_intent())
        self.assertEqual(tr.decision, TitleDecision.KEEP)

    def test_learned_faux_doute_skipped(self):
        self.assertIsNone(triage_learning_hit("PS4 + jeux", "console_ps4"))

if __name__ == "__main__":
    unittest.main()

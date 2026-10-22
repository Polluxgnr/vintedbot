from __future__ import annotations

import unittest

from vf.engine import (
    SearchIntent,
    TitleDecision,
    can_promote_doubtful_row,
    demote_strict_intent_mismatches,
    promote_doubtful_listings,
    row_matches_strict_intent,
    triage_title,
)
from vf.market import CatalogItem, enrich_search_intent


def _item(title: str, price: float = 120.0, item_id: str = "1") -> CatalogItem:
    return CatalogItem(id=item_id, title=title, price=price, total_price=price)


def _new3ds_xl_intent() -> SearchIntent:
    si = SearchIntent(
        raw="New Nintendo 3DS XL",
        search_text="New Nintendo 3DS XL",
        product_families=["console_ds"],
    )
    enrich_search_intent(si, query="New Nintendo 3DS XL")
    return si


class TriageNew3dsStrictTests(unittest.TestCase):
    def test_enrich_sets_strict_ds_family(self):
        si = _new3ds_xl_intent()
        self.assertEqual(si.required_ds_family, "new3ds_xl")

    def test_triage_rejects_classic_3ds_xl(self):
        si = _new3ds_xl_intent()
        tr = triage_title(_item("Nintendo 3DS XL noir"), si)
        self.assertEqual(tr.decision, TitleDecision.REJECT)
        self.assertTrue(any("wrong_ds_generation" in r for r in tr.reasons))

    def test_triage_keeps_new_3ds_xl(self):
        si = _new3ds_xl_intent()
        tr = triage_title(_item("New Nintendo 3DS XL + chargeur"), si)
        self.assertEqual(tr.decision, TitleDecision.KEEP)

    def test_row_mismatch_detected(self):
        si = _new3ds_xl_intent()
        row = {
            "title": "Nintendo 3DS XL",
            "ds_model_family": "3ds_xl",
            "required_ds_family": "new3ds_xl",
            "triage_decision": "keep",
        }
        self.assertFalse(row_matches_strict_intent(row, si))

    def test_promote_blocked_for_wrong_generation(self):
        si = _new3ds_xl_intent()
        row = {
            "title": "Nintendo 3DS XL",
            "ds_model_family": "3ds_xl",
            "required_ds_family": "new3ds_xl",
            "triage_decision": "doubtful",
            "triage_reasons": "wrong_ds_generation:3ds_xl",
            "score_composite": 72,
            "ai_title_relevance": 85,
        }
        self.assertFalse(can_promote_doubtful_row(row, intent=si, category="console_ds"))
        promoted = promote_doubtful_listings([row], category="console_ds", intent=si)
        self.assertEqual(promoted, 0)
        self.assertEqual(row["triage_decision"], "doubtful")

    def test_demote_removes_mismatch_from_offers(self):
        si = _new3ds_xl_intent()
        rows = [
            {
                "id": "a",
                "title": "New 3DS XL",
                "ds_model_family": "new3ds_xl",
                "required_ds_family": "new3ds_xl",
                "triage_decision": "keep",
                "best_deal_score": 78,
            },
            {
                "id": "b",
                "title": "Nintendo 3DS XL",
                "ds_model_family": "3ds_xl",
                "required_ds_family": "new3ds_xl",
                "triage_decision": "keep",
                "best_deal_score": 65,
            },
        ]
        n = demote_strict_intent_mismatches(rows, si)
        self.assertEqual(n, 1)
        self.assertEqual(rows[1]["triage_decision"], "doubtful")
        self.assertTrue(rows[1].get("deal_blocked"))
        offers = [
            r
            for r in rows
            if str(r.get("triage_decision") or "") != "doubtful" and not r.get("intent_mismatch")
        ]
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["id"], "a")


if __name__ == "__main__":
    unittest.main()

"""Tests triage / scoring / filtres videoprojecteur."""
from __future__ import annotations

import unittest

from vf.engine import SearchIntent, build_projector_filters, triage_title
from vf.market import CatalogItem
from vf.scoring import (
    apply_hyper_pro_scoring,
    build_market_context,
    extract_listing_specs,
    has_projector_signal,
    is_projector_accessory_text,
    projector_quality_value_score,
    select_top_candidates,
)


def _projector_intent() -> SearchIntent:
    return SearchIntent(raw="video projecteur", search_text="video projecteur", product_families=["projector"])


def _item(title: str, price: float = 120.0, item_id: str = "1") -> CatalogItem:
    return CatalogItem(id=item_id, title=title, price=price, total_price=price)


class ProjectorTriageTests(unittest.TestCase):
    def test_splitter_hdmi_rejected(self):
        tr = triage_title(_item("Splitter HDMI Goobay 2 sorties"), _projector_intent())
        self.assertEqual(tr.decision.value, "reject")
        self.assertTrue(any("accessory" in r for r in tr.reasons), tr.reasons)

    def test_hdmi_extender_rejected(self):
        tr = triage_title(_item("Extenseur HDMI 60m repeteur signal"), _projector_intent())
        self.assertEqual(tr.decision.value, "reject")

    def test_wireless_display_adapter_rejected(self):
        tr = triage_title(
            _item("Adaptateur affichage sans fil True 4K 60Hz HDMI neuf"),
            _projector_intent(),
        )
        self.assertEqual(tr.decision.value, "reject")
        self.assertTrue(
            any(r in ("projector_accessory", "not_projector") or "accessory" in r for r in tr.reasons),
            tr.reasons,
        )

    def test_miracast_adapter_rejected(self):
        tr = triage_title(
            _item("Miracast Adaptateur HDMI sans Fil, Miroir d'écran"),
            _projector_intent(),
        )
        self.assertEqual(tr.decision.value, "reject")

    def test_wireless_adapter_score_very_low(self):
        row = {
            "id": "adapter",
            "title": "Adaptateur affichage sans fil True 4K 60Hz HDMI",
            "price": 46.0,
            "total_price": 53.0,
            "item_type": "projector",
            "platform": "vinted",
            "triage_decision": "keep",
        }
        ctx = build_market_context([row])
        apply_hyper_pro_scoring([row], ctx)
        self.assertLessEqual(row["best_deal_score"], 5.0)
        self.assertLess(projector_quality_value_score(row), 10.0)

    def test_top_candidates_exclude_wireless_adapter(self):
        rows = [
            {
                "id": "adapter",
                "title": "Adaptateur affichage sans fil True 4K 60Hz HDMI neuf",
                "price": 46.0,
                "total_price": 53.0,
                "item_type": "projector",
                "quality_tier": "ok",
                "triage_decision": "keep",
            },
            {
                "id": "real",
                "title": "BenQ W1700 1080p 2200 lumens HDMI",
                "price": 280.0,
                "total_price": 280.0,
                "item_type": "projector",
                "lumens": 2200,
                "resolution": "1080p",
                "projector_brand": "benq",
                "has_hdmi": True,
                "quality_tier": "ok",
                "triage_decision": "keep",
                "seller_feedback_count": 5,
            },
        ]
        candidates, _ = select_top_candidates(rows, category="projector", query="video projecteur", limit=5)
        ids = [str(c.get("id")) for c in candidates]
        self.assertNotIn("adapter", ids)
        self.assertIn("real", ids)

    def test_uhd38x_kept(self):
        tr = triage_title(_item("Videoprojecteur Optoma UHD38x 4K"), _projector_intent())
        self.assertEqual(tr.decision.value, "keep", tr.reasons)

    def test_uhd38x_title_only_kept(self):
        tr = triage_title(_item("Optoma UHD38x"), _projector_intent())
        self.assertIn(tr.decision.value, ("keep", "doubtful"), tr.reasons)

    def test_splitter_score_very_low(self):
        row = {
            "id": "split",
            "title": "Splitter HDMI Goobay",
            "price": 56.0,
            "total_price": 56.0,
            "item_type": "projector",
            "platform": "vinted",
            "triage_decision": "keep",
        }
        ctx = build_market_context([row])
        apply_hyper_pro_scoring([row], ctx)
        self.assertLessEqual(row["best_deal_score"], 12.0)
        self.assertLess(projector_quality_value_score(row), 15.0)

    def test_real_projector_scores_higher_than_splitter(self):
        splitter = {
            "id": "s",
            "title": "Splitter HDMI 2 ports Goobay",
            "price": 56.0,
            "total_price": 56.0,
            "item_type": "projector",
            "platform": "vinted",
        }
        projector = {
            "id": "p",
            "title": "Epson EH-TW650 3500 lumens Full HD HDMI",
            "price": 220.0,
            "total_price": 220.0,
            "item_type": "projector",
            "lumens": 3500,
            "resolution": "1080p",
            "projector_brand": "epson",
            "has_hdmi": True,
            "platform": "vinted",
            "seller_feedback_count": 8,
        }
        rows = [splitter, projector]
        ctx = build_market_context(rows)
        apply_hyper_pro_scoring(rows, ctx)
        self.assertGreater(rows[1]["best_deal_score"], rows[0]["best_deal_score"])

    def test_lumens_filter_1080p(self):
        rows = [
            {
                "id": "a",
                "title": "Epson 2500 lumens 1080p HDMI",
                "lumens": 2500,
                "resolution": "1080p",
                "has_hdmi": True,
                "item_type": "projector",
            },
            {
                "id": "b",
                "title": "Mini projecteur 720p",
                "lumens": 400,
                "resolution": "720p",
                "item_type": "projector",
            },
        ]
        controls = build_projector_filters(rows)
        lum_ctrl = next(c for c in controls if c.id == "lumens_band")
        res_ctrl = next(c for c in controls if c.id == "resolution")
        self.assertIn("1080p", res_ctrl.options)
        self.assertIn("1000-3000", lum_ctrl.options)
        kept = [r for r in rows if lum_ctrl.extract(r, "1000-3000") and res_ctrl.extract(r, "1080p")]
        self.assertEqual([r["id"] for r in kept], ["a"])

    def test_has_projector_signal(self):
        self.assertFalse(has_projector_signal({"title": "Splitter HDMI Goobay", "item_type": "projector"}))
        specs = extract_listing_specs({"title": "Optoma UHD38x 4K"}, "projector")
        self.assertTrue(
            has_projector_signal(
                {"title": "Optoma UHD38x 4K", "item_type": "projector", "extracted_specs": specs}
            )
        )

    def test_top_candidates_exclude_splitter(self):
        rows = [
            {
                "id": "split",
                "title": "Splitter HDMI Goobay",
                "price": 56.0,
                "total_price": 56.0,
                "item_type": "projector",
                "quality_tier": "ok",
                "triage_decision": "keep",
            },
            {
                "id": "real",
                "title": "BenQ W1700 1080p 2200 lumens HDMI",
                "price": 280.0,
                "total_price": 280.0,
                "item_type": "projector",
                "lumens": 2200,
                "resolution": "1080p",
                "projector_brand": "benq",
                "has_hdmi": True,
                "quality_tier": "ok",
                "triage_decision": "keep",
                "seller_feedback_count": 5,
            },
        ]
        candidates, _ = select_top_candidates(rows, category="projector", query="video projecteur", limit=5)
        ids = [str(c.get("id")) for c in candidates]
        self.assertNotIn("split", ids)
        self.assertIn("real", ids)

    def test_is_projector_accessory_text(self):
        self.assertTrue(is_projector_accessory_text("Splitter HDMI Goobay"))
        self.assertTrue(is_projector_accessory_text("Adaptateur affichage sans fil True 4K 60Hz HDMI"))
        self.assertTrue(is_projector_accessory_text("Miracast Adaptateur HDMI sans Fil"))
        self.assertFalse(is_projector_accessory_text("Videoprojecteur Epson EB-955WH"))


if __name__ == "__main__":
    unittest.main()

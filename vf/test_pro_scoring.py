"""Unit tests - hyper-pro scoring."""
from __future__ import annotations
import time
import unittest
from vf.scoring import (
    apply_hyper_pro_scoring,
    build_market_context,
    classify_deal_tier,
    compute_best_deal_score,
    compute_hyper_pro_score,
    hyper_pro_value_score,
    projector_quality_value_score,
)

def _good_deal(**kw):
    base = {"id":"1","title":"New Nintendo 3DS XL + chargeur + stylet","description":"Tres bon etat ecran nickel","price":128.0,"total_price":132.0,"platform":"vinted","ds_model_family":"new3ds_xl","required_ds_family":"new3ds_xl","product_model_key":"new3ds_xl","triage_decision":"keep","ai_title_relevance":88,"pack_score":78.0,"has_power":True,"seller_feedback_count":24,"seller_rating":0.92,"quality_tier":"ok","spec_confidence":0.78}
    base.update(kw)
    return base

def _dropship(**kw):
    base = {"id":"2","title":"Console neuf sous blister envoi rapide stock europeen","description":"100% neuf garantie 2 ans","price":79.0,"total_price":79.0,"platform":"wallapop","ds_model_family":"new3ds_xl","triage_decision":"keep","seller_feedback_count":0,"seller_rating":0.0,"quality_tier":"ok"}
    base.update(kw)
    return base

class ProScoringTests(unittest.TestCase):
    def test_good_deal_scores_high(self):
        rows = [_good_deal(), _good_deal(id="3", price=165.0, total_price=168.0)]
        ctx = build_market_context(rows)
        result = compute_hyper_pro_score(rows[0], ctx)
        self.assertGreaterEqual(result["score_composite"], 70.0)
        self.assertIn(result["deal_tier"], ("pepite", "bonne_affaire"))
        self.assertLess(result["dropship_risk_score"], 35.0)

    def test_dropship_capped_and_demoted(self):
        rows = [_good_deal(), _dropship()]
        ctx = build_market_context(rows)
        result = compute_hyper_pro_score(rows[1], ctx)
        self.assertGreaterEqual(result["dropship_risk_score"], 55.0)
        self.assertEqual(result["deal_tier"], "dropship_suspect")
        self.assertLessEqual(result["score_composite"], 30.0)

    def test_dropship_ranks_below_real_deal(self):
        rows = [_good_deal(), _dropship()]
        apply_hyper_pro_scoring(rows, build_market_context(rows))
        self.assertGreater(hyper_pro_value_score(rows[0]), hyper_pro_value_score(rows[1]))

    def test_pepite_tier_rules(self):
        row = _good_deal()
        row["spec_confidence"] = 0.75
        self.assertEqual(
            classify_deal_tier(78.0, {"price_value": 72.0, "relevance": 80.0}, dropship_risk=20.0, row=row),
            "pepite",
        )
        row_low = _good_deal()
        row_low["spec_confidence"] = 0.4
        self.assertNotEqual(
            classify_deal_tier(78.0, {"price_value": 72.0, "relevance": 80.0}, dropship_risk=20.0, row=row_low),
            "pepite",
        )

    def test_batch_performance(self):
        rows = [_good_deal(id=str(i), price=120+i) for i in range(300)]
        rows += [_dropship(id=f"d{i}") for i in range(20)]
        t0 = time.perf_counter()
        apply_hyper_pro_scoring(rows)
        self.assertLess(time.perf_counter()-t0, 2.0)

    def test_best_deal_prefers_quality_projector(self):
        cheap = {
            "id": "cheap", "item_type": "projector", "title": "Epson basique",
            "price": 74.0, "total_price": 74.0, "lumens": 800, "platform": "vinted",
            "seller_feedback_count": 2, "triage_decision": "keep",
        }
        quality = {
            "id": "qual", "item_type": "projector",
            "title": "Epson EH-TW650 3500 lumens Full HD HDMI",
            "price": 220.0, "total_price": 220.0, "lumens": 3500,
            "resolution": "1080p", "projector_brand": "epson",
            "product_model_key": "epson_eh_tw650", "platform": "vinted",
            "seller_feedback_count": 12, "seller_rating": 0.9, "triage_decision": "keep",
        }
        rows = [cheap, quality]
        ctx = build_market_context(rows)
        apply_hyper_pro_scoring(rows, ctx)
        self.assertGreater(rows[1]["best_deal_score"], rows[0]["best_deal_score"])
        self.assertGreater(projector_quality_value_score(quality), projector_quality_value_score(cheap))

    def test_display_rank_matches_best_deal(self):
        row = _good_deal()
        ctx = build_market_context([row])
        apply_hyper_pro_scoring([row], ctx)
        self.assertEqual(row["display_rank_score"], row["best_deal_score"])
        self.assertGreaterEqual(row["best_deal_score"], 50.0)

    def test_ps4_console_beats_game_lot(self):
        from vf.scoring import is_game_only_listing

        console = {
            "id": "c1",
            "title": "PS4 Slim avec manette",
            "price": 95.0,
            "total_price": 99.0,
            "platform": "vinted",
            "item_type": "console_ps4",
            "triage_decision": "keep",
            "seller_feedback_count": 5,
            "seller_rating": 0.9,
            "quality_tier": "ok",
        }
        games = {
            "id": "g1",
            "title": "Lot de 8 jeux PS4",
            "price": 72.0,
            "total_price": 75.0,
            "platform": "leboncoin",
            "item_type": "console_ps4",
            "triage_decision": "keep",
            "seller_feedback_count": 5,
            "quality_tier": "ok",
        }
        self.assertTrue(is_game_only_listing(games, category="console_ps4"))
        self.assertFalse(is_game_only_listing(console, category="console_ps4"))
        rows = [console, games]
        ctx = build_market_context(rows)
        apply_hyper_pro_scoring(rows, ctx)
        self.assertGreater(rows[0]["best_deal_score"], rows[1]["best_deal_score"])
        self.assertEqual(rows[1]["deal_tier"], "rejet")

    def test_ps4_complete_pack_scores_higher(self):
        from vf.scoring import apply_hyper_pro_scoring, build_market_context, console_completeness_score

        bare = {
            "id": "bare",
            "title": "PS4 slim sans manette",
            "price": 75.0,
            "total_price": 79.0,
            "platform": "vinted",
            "item_type": "console_ps4",
            "triage_decision": "keep",
            "seller_feedback_count": 5,
            "variant": "slim",
            "storage_gb": 500,
            "controllers_count": 0,
            "has_controller": False,
        }
        complete = {
            "id": "full",
            "title": "PS4 Slim 1To + 2 manettes + FIFA + GTA",
            "price": 130.0,
            "total_price": 135.0,
            "platform": "vinted",
            "item_type": "console_ps4",
            "triage_decision": "keep",
            "seller_feedback_count": 8,
            "variant": "slim",
            "storage_gb": 1000,
            "controllers_count": 2,
            "games_count": 4,
            "premium_games": ["fifa", "gta"],
            "has_box": True,
            "has_hdmi": True,
            "has_power_cable": True,
            "condition": "tres bon",
        }
        pack_bare, _ = console_completeness_score(bare)
        pack_full, _ = console_completeness_score(complete)
        self.assertGreater(pack_full, pack_bare)
        rows = [bare, complete]
        ctx = build_market_context(rows)
        apply_hyper_pro_scoring(rows, ctx)
        self.assertGreater(rows[1]["best_deal_score"], rows[0]["best_deal_score"])


    def test_ac_accessory_scores_low(self):
        from vf.scoring import apply_hyper_pro_scoring, build_market_context, compute_best_deal_score

        unit = {
            "id": "u1",
            "title": "Climatiseur mobile Klarstein 9000 BTU",
            "price": 180.0,
            "total_price": 185.0,
            "platform": "vinted",
            "item_type": "ac",
            "triage_decision": "keep",
            "seller_feedback_count": 5,
        }
        remote = {
            "id": "r1",
            "title": "Télécommande clim universelle",
            "price": 12.0,
            "total_price": 12.0,
            "platform": "vinted",
            "item_type": "ac",
            "triage_decision": "reject",
        }
        rows = [unit, remote]
        ctx = build_market_context(rows)
        apply_hyper_pro_scoring(rows, ctx)
        self.assertGreater(rows[0]["best_deal_score"], rows[1]["best_deal_score"])
        self.assertLessEqual(compute_best_deal_score(remote, ctx), 10.0)

    def test_seller_trust_contextual_not_uniform(self):
        from vf.scoring import apply_seller_trust_to_row, seller_trust_adjustment

        cheap_new = {"total_price": 35.0, "seller_feedback_count": 0}
        adj_cheap, _, _ = seller_trust_adjustment(cheap_new)
        expensive_new = {"total_price": 120.0, "seller_feedback_count": 0}
        adj_exp, _, _ = seller_trust_adjustment(expensive_new)
        self.assertLess(adj_cheap, 0)
        self.assertLess(adj_exp, 0)
        self.assertGreater(adj_cheap, adj_exp)  # pas de -30 uniforme

        trusted = {"total_price": 90.0, "seller_feedback_count": 60, "seller_rating": 0.9}
        adj_tr, label, _ = seller_trust_adjustment(trusted)
        self.assertGreater(adj_tr, 0)
        self.assertIn("fiable", label.lower())

        row = dict(expensive_new)
        apply_seller_trust_to_row(row)
        self.assertTrue(row.get("deal_capped"))

    def test_model_market_median_ps4_by_variant_storage(self):
        from vf.engine import apply_model_market_stats

        rows = [
            {
                "id": "1",
                "item_type": "console_ps4",
                "variant": "slim",
                "storage_gb": 500,
                "total_price": 140.0,
                "product_model_key": "ps4_slim_500gb",
            },
            {
                "id": "2",
                "item_type": "console_ps4",
                "variant": "slim",
                "storage_gb": 500,
                "total_price": 160.0,
                "product_model_key": "ps4_slim_500gb",
            },
            {
                "id": "3",
                "item_type": "console_ps4",
                "variant": "pro",
                "storage_gb": 1000,
                "total_price": 220.0,
                "product_model_key": "ps4_pro_1tb",
            },
        ]
        apply_model_market_stats(rows, min_samples=2)
        self.assertEqual(rows[0].get("model_market_median_eur"), 150.0)
        self.assertIsNone(rows[2].get("model_market_median_eur"))  # seul exemplaire pro


if __name__ == "__main__":
    unittest.main()

"""Tests spec_profile — extraction et criteres par categorie."""
from __future__ import annotations

import unittest

from vf.scoring import (
    build_eval_context,
    compact_specs_line,
    extract_listing_specs,
    normalize_category,
    profile_for_category,
    spec_badges,
)
from vf.scoring import select_top_candidates


class SpecProfileTests(unittest.TestCase):
    def test_phone_extracts_battery(self):
        specs = extract_listing_specs(
            {"title": "iPhone 13 128 Go batterie 87% tres bon etat", "description": ""},
            "phone",
        )
        self.assertEqual(specs.get("battery_health_pct"), 87)
        self.assertEqual(specs.get("storage_gb"), 128)

    def test_velo_extracts_taille_no_battery_dimension(self):
        cat = normalize_category("velo")
        self.assertEqual(cat, "bicycle")
        specs = extract_listing_specs(
            {"title": "VTT Trek taille M cadre 54 carbone Shimano Deore", "description": ""},
            cat,
        )
        self.assertTrue(specs.get("frame_size"))
        self.assertNotIn("battery_health_pct", specs)
        prof = profile_for_category(cat)
        dim_ids = [d.id for d in prof.dimensions]
        self.assertIn("frame_size", dim_ids)
        self.assertNotIn("battery_health_pct", dim_ids)

    def test_projector_extracts_lumens(self):
        specs = extract_listing_specs(
            {"title": "Epson EH-TW650 3500 lumens HDMI tres bon etat", "description": ""},
            "projector",
        )
        self.assertEqual(specs.get("lumens"), 3500)
        self.assertEqual(specs.get("projector_brand"), "epson")

    def test_top5_prompt_includes_category_criteria(self):
        rows = [
            {
                "id": "1",
                "title": "VTT carbone taille M",
                "price": 400,
                "total_price": 400,
                "score_composite": 70,
                "deal_tier": "ok",
            }
        ]
        _, ctx = select_top_candidates(rows, category="velo", query="velo carbone M", limit=10)
        self.assertEqual(ctx["category"], "bicycle")
        self.assertIn("Taille roues", ctx["llm_criteria_hint"])
        self.assertIn("frame_size", [d["id"] for d in ctx["dimensions"]])

    def test_spec_badges_phone_vs_velo(self):
        phone = spec_badges({"battery_health_pct": 87, "storage_gb": 128}, "phone")
        self.assertTrue(any("87" in b for b in phone))
        velo = spec_badges({"frame_size": "M", "frame_material": "Carbone"}, "velo")
        self.assertTrue(any("Taille" in b for b in velo))
        self.assertFalse(any("batterie" in b.lower() for b in velo))

    def test_projector_detects_screen_only(self):
        specs = extract_listing_specs(
            {"title": "Ecran de projection seul 2m", "description": ""},
            "projector",
        )
        self.assertTrue(specs.get("is_screen_only"))

    def test_projector_mini_caps_lumens(self):
        specs = extract_listing_specs(
            {"title": "Mini projecteur poche 3300 lumens marketing LED", "description": ""},
            "projector",
        )
        self.assertTrue(specs.get("is_mini_projector"))
        self.assertLessEqual(int(specs.get("lumens") or 9999), 500)

    def test_ps4_extracts_controller_and_storage(self):
        specs = extract_listing_specs(
            {"title": "PS4 Slim 1To avec 2 manettes HDMI", "description": ""},
            "console_ps4",
        )
        self.assertEqual(specs.get("variant"), "slim")
        self.assertEqual(specs.get("storage_gb"), 1000)
        self.assertEqual(specs.get("controller_count"), 2)
        self.assertTrue(specs.get("has_controller"))

    def test_bicycle_extracts_wheel_and_brand(self):
        specs = extract_listing_specs(
            {"title": "VTT Trek 29 pouces taille L Shimano Deore VAE Bosch", "description": ""},
            "bicycle",
        )
        self.assertEqual(specs.get("wheel_size"), "29")
        self.assertEqual(specs.get("bike_brand"), "Trek")
        self.assertTrue(specs.get("is_ebike"))
        self.assertEqual(specs.get("bike_usage"), "vtt")

    def test_ps4_detects_accessory_volant(self):
        specs = extract_listing_specs(
            {"title": "Volant Thrustmaster T300 RS", "description": ""},
            "console_ps4",
        )
        self.assertTrue(specs.get("is_accessory_only"))

    def test_ps4_detects_games_only_lot(self):
        specs = extract_listing_specs(
            {"title": "Lot de 8 jeux PS4", "description": ""},
            "console_ps4",
        )
        self.assertTrue(specs.get("is_games_only"))
        self.assertEqual(specs.get("listing_type"), "games_only")

    def test_early_spec_filter_drops_games_only(self):
        from vf.scoring import apply_early_spec_filters

        class _Item:
            def __init__(self, i, title):
                self.id = i
                self.title = title
                self.description = ""

        items = [_Item("1", "Lot de 12 jeux PS4"), _Item("2", "PS4 Slim 500Go console")]
        kept, dropped, reasons = apply_early_spec_filters(items, category="console_ps4")
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(str(kept[0].id), "2")
        self.assertEqual(reasons.get("1"), "jeux_seuls")


class InteractiveFilterTests(unittest.TestCase):
    def test_projector_filter_controls(self):
        from vf.engine import build_interactive_filters

        rows = [
            {"id": "1", "title": "Epson EH-TW650 3500 lumens HDMI", "price": 250, "projector_brand": "epson", "lumens": 3500},
            {"id": "2", "title": "Optoma HD146X 1080p", "price": 180, "projector_brand": "optoma"},
        ]
        controls = build_interactive_filters("projector", rows)
        ids = {c.id for c in controls}
        self.assertIn("lumens_band", ids)
        self.assertIn("value_score_min", ids)
        self.assertNotIn("pack_tier", ids)

    def test_value_score_prefers_lumens_over_cheap(self):
        from vf.scoring import projector_quality_value_score

        cheap = projector_quality_value_score(
            {"item_type": "projector", "title": "Epson", "lumens": 800, "price": 50, "total_price": 50}
        )
        quality = projector_quality_value_score(
            {"item_type": "projector", "title": "Epson EH-TW650", "lumens": 3500, "resolution": "1080p",
             "projector_brand": "epson", "price": 220, "total_price": 220}
        )
        self.assertGreater(quality, cheap)

    def test_bicycle_filter_controls(self):
        from vf.engine import build_interactive_filters

        rows = [
            {"id": "1", "title": "VTT Trek 29 pouces", "price": 400},
            {"id": "2", "title": "VTC Decathlon 27.5", "price": 250},
        ]
        controls = build_interactive_filters("bicycle", rows)
        ids = {c.id for c in controls}
        self.assertIn("wheel_size", ids)
        self.assertIn("bike_brand", ids)
        self.assertIn("is_ebike", ids)

    def test_console_ps4_filter_controls(self):
        from vf.engine import build_interactive_filters

        rows = [
            {"id": "1", "title": "PS4 Slim 500Go 1 manette", "price": 120, "has_controller": True},
            {"id": "2", "title": "Lot de 8 jeux PS4", "price": 70, "is_games_only": True, "listing_type": "games_only"},
        ]
        controls = build_interactive_filters("console_ps4", rows)
        ids = {c.id for c in controls}
        self.assertIn("listing_type", ids)
        self.assertIn("controller_count", ids)
        self.assertIn("ps4_variant", ids)


class CategoryBrainTests(unittest.TestCase):
    def test_enrich_intent_ps4_slim_1to(self):
        from vf.engine import enrich_intent_from_query, smart_filter_defaults

        intent = enrich_intent_from_query("PS4 slim 1to", "console_ps4")
        self.assertEqual(intent.get("ps4_variant"), "slim")
        defaults = smart_filter_defaults("console_ps4", "PS4 slim 1to", {})
        self.assertEqual(defaults.get("listing_type"), "console")
        self.assertEqual(defaults.get("livraison_uniquement"), "on")

    def test_suspicious_price_cluster(self):
        from vf.engine import detect_suspicious_price_clusters

        rows = [{"id": str(i), "price": 74.5, "total_price": 74.5} for i in range(12)]
        clusters = detect_suspicious_price_clusters(rows, min_count=8)
        self.assertGreaterEqual(len(clusters), 8)

    def test_spec_confidence(self):
        from vf.engine import compute_spec_confidence

        high = compute_spec_confidence(
            {
                "title": "PS4 Slim 1To + 2 manettes",
                "description": "Console en bon état avec câbles",
                "item_type": "console_ps4",
            },
            "console_ps4",
        )
        low = compute_spec_confidence({"title": "jeu ps4", "description": ""}, "console_ps4")
        self.assertGreater(high, low)

    def test_ac_extracts_btu_and_brand(self):
        specs = extract_listing_specs(
            {"title": "Climatiseur mobile Klarstein 9000 BTU blanc", "description": ""},
            "ac",
        )
        self.assertEqual(specs.get("capacity_btu"), 9000)
        self.assertEqual(specs.get("brand"), "klarstein")
        self.assertTrue(specs.get("ac_is_unit"))
        self.assertTrue(specs.get("product_model_key", "").startswith("ac_"))

    def test_ac_detects_accessory_remote(self):
        specs = extract_listing_specs(
            {"title": "Télécommande clim universelle", "description": ""},
            "ac",
        )
        self.assertTrue(specs.get("is_accessory_only"))
        self.assertFalse(specs.get("ac_is_unit"))

    def test_ac_compact_specs_line(self):
        line = compact_specs_line(
            {"capacity_btu": 9000, "brand": "klarstein", "ac_type": "clim mobile"},
            "clim",
        )
        self.assertIn("9000 BTU", line)
        self.assertIn("Klarstein", line)

    def test_listing_age_hours_format(self):
        from datetime import datetime, timedelta, timezone
        from vf.scoring import enrich_row_freshness, format_listing_age, listing_age_hours

        ts = datetime.now(tz=timezone.utc) - timedelta(hours=3)
        iso = ts.strftime("%Y-%m-%dT%H:%M:%S")
        row = {"platform": "vinted", "posted_at": iso}
        enrich_row_freshness(row)
        self.assertEqual(row.get("age_text"), "Il y a 3 h")
        self.assertAlmostEqual(float(row.get("age_hours") or 0), 3.0, delta=0.2)
        self.assertEqual(format_listing_age(None, iso), "Il y a 3 h")


if __name__ == "__main__":
    unittest.main()

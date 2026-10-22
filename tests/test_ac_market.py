"""Tests complets marche AC (climatisation) - triage, BTU, scoring, Top5."""
from __future__ import annotations

import unittest

from vf.scoring import detect_accessory
from vf.engine import _CATEGORY_FALLBACK_EXCLUDES, plan_early_exclude_match
from vf.engine import _AC_ACCESSORY_RE, _custom_ac_is_unit, normalize_filter_spec
from vf.engine import parse_search_intent, triage_title
from vf.market import CatalogItem
from vf.scoring import extract_ac_specs, normalize_category


def _listing(title: str, description: str = "") -> dict:
    return {"id": "t1", "title": title, "description": description}


def _triage(title: str) -> str:
    item = CatalogItem(id=1, title=title, platform="vinted")
    intent = parse_search_intent("clim")
    return triage_title(item, intent).decision.value


class TestACTriage(unittest.TestCase):
    def test_compresseur_auto_reject(self):
        self.assertEqual(_triage("Compresseur clim auto Renault Clio"), "reject")

    def test_rafraichisseur_reject(self):
        self.assertEqual(_triage("Rafraichisseur d air mobile"), "reject")

    def test_location_vacances_reject(self):
        self.assertEqual(_triage("Location vacances villa avec clim"), "reject")

    def test_clim_mobile_keep(self):
        self.assertIn(_triage("Climatiseur mobile 9000 BTU"), ("keep", "doubtful"))

    def test_parse_intent_ac_family(self):
        si = parse_search_intent("clim")
        self.assertIn("ac", si.product_families)

    def test_false_positive_clothing_rejected(self):
        self.assertEqual(_triage("Abrigo North Face"), "reject")
        self.assertEqual(_triage("Veste classique clim Rossignol"), "reject")

    def test_false_positive_shoes_rejected(self):
        self.assertEqual(_triage("Nike Pegasus Shield"), "reject")
        self.assertEqual(_triage("Lafuma Access Clim M/W"), "reject")

    def test_false_positive_toys_and_detector_rejected(self):
        self.assertEqual(_triage("Tank clim t-206"), "reject")
        self.assertEqual(_triage("Coche BMW 323i de Clim"), "reject")
        self.assertEqual(_triage("Détecteur de gaz Clim"), "reject")

    def test_false_positive_watch_rejected(self):
        self.assertEqual(_triage("Reloj smartwatch TOUS"), "reject")

    def test_klarstein_portable_ac_not_box_pack_accessory(self):
        title = (
            "Clim mobile Klarstein : Breeze Box Smart 7000 BTU "
            "Climatiseur mobile Blanc"
        )
        self.assertIn(_triage(title), ("keep", "doubtful"))
        item = CatalogItem(id=1, title=title, platform="vinted")
        acc = detect_accessory(item, search_hint="clim", title_only=True, market_category="ac")
        self.assertFalse(acc.is_accessory)

    def test_composteur_reject(self):
        self.assertEqual(_triage("Composteur de clim a vendre"), "reject")

    def test_telecommande_clim_reject(self):
        self.assertEqual(_triage("Telecommande climatiseur universelle"), "reject")

    def test_tuyau_flexible_reject(self):
        self.assertEqual(_triage("Tuyau flexible de clim mobile 150cm"), "reject")

    def test_filtre_clim_reject(self):
        self.assertEqual(_triage("Filtre de climatiseur Daikin"), "reject")

    def test_split_keep(self):
        self.assertIn(_triage("Monosplit Daikin 12000 BTU reversible"), ("keep", "doubtful"))


class TestACFallbackExcludes(unittest.TestCase):
    def _plan(self) -> dict:
        return {"version": 1, "category": "ac", "early_excludes": list(_CATEGORY_FALLBACK_EXCLUDES["ac"])}

    def test_composteur_excluded(self):
        self.assertIsNotNone(plan_early_exclude_match("composteur de clim", self._plan()))

    def test_clim_mobile_not_excluded(self):
        self.assertIsNone(plan_early_exclude_match("climatiseur mobile 9000 btu", self._plan()))

    def test_veste_clim_excluded(self):
        self.assertIsNotNone(plan_early_exclude_match("veste classique clim rossignol", self._plan()))

    def test_nike_excluded(self):
        self.assertIsNotNone(plan_early_exclude_match("nike pegasus shield", self._plan()))


class TestExtractAcSpecs(unittest.TestCase):
    def test_btu_extraction(self):
        self.assertEqual(extract_ac_specs(_listing("Clim mobile 9000 BTU Delonghi")).get("capacity_btu"), 9000)

    def test_breeze_box_unit_not_accessory(self):
        title = "Clim mobile Klarstein : Breeze Box Smart 7000 BTU Climatiseur mobile Blanc"
        specs = extract_ac_specs(_listing(title))
        self.assertTrue(specs.get("ac_is_unit"))
        self.assertFalse(specs.get("is_accessory_only"))

    def test_normalize_category_aliases(self):
        self.assertEqual(normalize_category("clim"), "ac")


class TestACFilterEngine(unittest.TestCase):
    def test_ac_unit_row_passes_unit_only(self):
        spec = normalize_filter_spec(
            {
                "id": "_ac_is_unit",
                "label": "Unite clim",
                "type": "select",
                "field": "_ac_is_unit",
                "operator": "custom",
                "options": ["all", "unit_only"],
            }
        )
        row = {"id": "b", "title": "Climatiseur mobile 9000 BTU Delonghi"}
        self.assertTrue(_custom_ac_is_unit(row, spec, "unit_only"))

    def test_ac_accessory_re_no_match_unit(self):
        self.assertFalse(_AC_ACCESSORY_RE.search("Climatiseur mobile 9000 BTU"))


class TestACRanking(unittest.TestCase):
    def test_unit_ranks_above_accessory(self):
        from vf.scoring import rank_rows

        rows = [
            {
                "id": "accessory",
                "title": "Telecommande climatiseur universelle",
                "is_accessory": True,
                "best_deal_score": 60.0,
            },
            {
                "id": "unit",
                "title": "Climatiseur mobile 9000 BTU Delonghi",
                "best_deal_score": 60.0,
                "capacity_btu": 9000,
            },
        ]
        ranked = rank_rows(rows, category="ac")
        rank_by_id = {r["id"]: r["rank_final"] for r in ranked}
        self.assertLess(rank_by_id["unit"], rank_by_id["accessory"])


if __name__ == "__main__":
    unittest.main()

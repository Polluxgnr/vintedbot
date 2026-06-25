"""Triage rules for AC / climatisation searches."""
from __future__ import annotations

import unittest

from vf.scoring import detect_accessory
from vf.engine import SearchIntent, parse_search_intent, triage_title
from vf.market import CatalogItem


def _triage(title: str) -> str:
    item = CatalogItem(id=1, title=title, platform="vinted")
    intent = parse_search_intent("clim")
    return triage_title(item, intent).decision.value


class TriageAcTests(unittest.TestCase):
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

    def test_nike_clima_fit_rejected(self):
        self.assertEqual(_triage("Nike Dri-FIT Clima-FIT T-shirt homme"), "reject")

    def test_adidas_climawarm_rejected(self):
        self.assertEqual(_triage("Adidas Climawarm Hoodie taille M"), "reject")

    def test_telecommande_rejected(self):
        self.assertEqual(_triage("Télécommande clim Daikin universelle"), "reject")

    def test_kit_fenetre_rejected(self):
        self.assertEqual(_triage("Kit fenêtre climatiseur mobile"), "reject")

    def test_composteur_megane_rejected(self):
        self.assertEqual(_triage("Composteur clim Mégane"), "reject")

    def test_klarstein_portable_ac_not_box_pack_accessory(self):
        title = (
            "Clim mobile Klarstein : Breeze Box Smart 7000 BTU "
            "Climatiseur mobile Blanc"
        )
        self.assertIn(_triage(title), ("keep", "doubtful"))
        item = CatalogItem(id=1, title=title, platform="vinted")
        acc = detect_accessory(item, search_hint="clim", title_only=True, market_category="ac")
        self.assertFalse(acc.is_accessory)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from vf.engine import apply_universal_filters, evaluate_spec, normalize_filter_spec


class FilterEngineTests(unittest.TestCase):
    def _spec(self, **kwargs):
        return normalize_filter_spec({"id": kwargs.pop("id", "test"), **kwargs})

    def test_includes_games_all_keeps_consoles_without_games(self):
        spec = self._spec(
            id="includes_games",
            label="Jeux inclus",
            type="select",
            field="includes_games",
            operator="custom",
            options=["all", "yes", "no"],
            option_labels={"all": "Tous", "yes": "Avec jeux", "no": "Sans jeux"},
        )
        rows = [
            {"id": "a", "title": "PS4 Slim sans manette"},
            {"id": "b", "title": "PS4 + FIFA 22", "includes_games": True, "games_count": 1},
        ]
        out = apply_universal_filters(rows, {"includes_games": "all"}, [spec])
        self.assertEqual({r["id"] for r in out}, {"a", "b"})
        out_yes = apply_universal_filters(rows, {"includes_games": "yes"}, [spec])
        self.assertEqual([r["id"] for r in out_yes], ["b"])
        out_no = apply_universal_filters(rows, {"includes_games": "no"}, [spec])
        self.assertEqual([r["id"] for r in out_no], ["a"])

    def test_controller_count_filter(self):
        spec = self._spec(
            id="controller_count",
            label="Manettes",
            type="select",
            field="_controller_count",
            operator="custom",
            options=["all", "0", "1", "2plus"],
        )
        rows = [
            {"id": "a", "controller_count": 0},
            {"id": "b", "controller_count": 1},
            {"id": "c", "controller_count": 2},
        ]
        self.assertEqual(
            {r["id"] for r in apply_universal_filters(rows, {"controller_count": "2plus"}, [spec])},
            {"c"},
        )
        self.assertEqual(
            {r["id"] for r in apply_universal_filters(rows, {"controller_count": "all"}, [spec])},
            {"a", "b", "c"},
        )

    def test_french_option_labels_normalize(self):
        spec = self._spec(
            id="includes_games",
            label="Jeux inclus",
            type="select",
            field="includes_games",
            operator="custom",
            options=["all", "yes", "no"],
        )
        row = {"id": "x", "title": "Console seule"}
        self.assertTrue(evaluate_spec(row, spec, "Tous"))
        self.assertFalse(evaluate_spec({"id": "y", "title": "Console seule"}, spec, "Avec jeux"))


if __name__ == "__main__":
    unittest.main()

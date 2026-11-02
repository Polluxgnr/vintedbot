from test_core import make
from vinted_hunter.criteria import apply_to_spec, mismatch, parse_request
from vinted_hunter.models import Condition, SearchSpec


def test_carhartt_request():
    p = parse_request("tous les tee carhartt moins de 10 euros en super bon état avec un logo graphique")
    assert p["query"] == "t-shirt carhartt" and p["price_max"] == 10
    assert Condition.VERY_GOOD.value in p["conditions"] and "logo graphique" in p["labels"]


def test_personal_pack_request_filters_listings():
    spec = apply_to_spec(SearchSpec(), parse_request("pack chaussettes boss neuf taille M unicolore pour moi"))
    assert spec.goal == "personal" and spec.sizes == ["M"] and "pour moi" in spec.describe()
    ok = make("Lot de 3 paires chaussettes Boss noires", 15, size="M", description="pack neuf, logo brodé")
    striped = make("Pack chaussettes Boss rayées", 15, size="M", description="lot de 3")
    wrong_size = make("Pack chaussettes Boss", 15, size="XL", description="lot de 3")
    single = make("Chaussettes Boss noires", 8, size="M", description="une paire")
    assert mismatch(ok, spec) is None
    assert mismatch(striped, spec) and mismatch(wrong_size, spec) and mismatch(single, spec)


def test_plain_query_untouched():
    p = parse_request("ps5 slim digital")
    assert p["query"] == "ps5 slim digital" and not p["must"] and p["goal"] == "resell"

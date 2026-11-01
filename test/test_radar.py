from test_core import CATALOG, make
from vinted_hunter.bot.views import radar_view
from vinted_hunter.config import RuntimeSettings
from vinted_hunter.db import Database
from vinted_hunter.market import estimate
from vinted_hunter.models import Condition
from vinted_hunter.scoring import build_deal, radar_worthy


def _deal(i: int, price: float, niche: str, key: str, ref: float):
    l = make(f"Item {i}", price, id=i, model_key=f"{niche}:{key}", niche_id=niche, details_loaded=True)
    return build_deal(l, estimate(Condition.VERY_GOOD, reference=ref, comps=[(ref, Condition.VERY_GOOD)] * 10),
                      CATALOG.get(niche), RuntimeSettings())


def test_radar_worthy_filters():
    good = _deal(1, 120, "consoles", "switch_oled", 220)
    assert radar_worthy(good)
    assert not radar_worthy(_deal(2, 230, "consoles", "switch_oled", 220))        # no profit
    unknown = good.model_copy(update={"listing": good.listing.model_copy(update={"model_key": "sig:x"})})
    assert not radar_worthy(unknown)                                              # model not identified


async def test_radar_sort_filter_and_render(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    try:
        deals = [_deal(1, 120, "consoles", "switch_oled", 220), _deal(2, 60, "consoles", "switch_lite", 110),
                 _deal(3, 150, "mini_pc_homelab", "mac_mini_m1", 300)]
        await db.save_radar(deals)
        by_profit, total = await db.radar(sort="profit")
        assert total == 3 and by_profit[0].listing.id == 3          # Mac mini has the biggest profit
        only_consoles, n = await db.radar(niche="consoles")
        assert n == 2 and {d.listing.id for d in only_consoles} == {1, 2}
        assert {nid: c for nid, c, _ in await db.radar_niches()}["consoles"] == 2
        _, big_only = await db.radar(min_profit=100)
        assert big_only == 1                                        # profit filter
        await db.update_radar_price(3, "sold", None)
        assert (await db.radar())[1] == 2                           # sold listings disappear
        from vinted_hunter.bot.views import radar_state
        st = radar_state("tp:profit:all:0:24:0:0:0")
        text, kb = radar_view(by_profit, total, st, await db.radar_niches(), {n.id: n.label for n in CATALOG})
        assert "3 affaires encore en ligne" in text
        assert any(b.callback_data == "rv:3" for row in kb.inline_keyboard for b in row)
        assert (await db.get_deal(1)).listing.id == 1                # radar deals open as cards
        assert set((await db.cleanup()).keys()) == {"price_obs", "radar", "verdicts", "listings"}
    finally:
        await db.close()

from datetime import datetime, timedelta, timezone

import pytest

from test_core import make
from vinted_hunter.ai.llm import extract_json
from vinted_hunter.db import Database
from vinted_hunter.market import estimate
from vinted_hunter.models import Condition
from vinted_hunter.vinted.parser import item_status


def test_item_status_from_page():
    ld = '<script type="application/ld+json">{"@type":"Product","offers":{"price":45,"availability":"%s"}}</script>'
    assert item_status(ld % "InStock") == ("active", 45.0)
    assert item_status(ld % "https://schema.org/SoldOut")[0] == "sold"
    reserved = ld % "InStock" + r'{\"is_reserved\":true,\"can_buy\":false}'
    assert item_status(reserved)[0] == "reserved"


def test_real_sales_dominate_valuation():
    sold = [(150.0, Condition.VERY_GOOD)] * 12
    v = estimate(Condition.VERY_GOOD, reference=220, sold=sold)
    assert "ventes réelles (12)" in v.sources
    assert v.fair_value is not None and v.fair_value < 190                    # pulled towards what buyers actually paid


async def test_sales_stats(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    try:
        items = [make(f"Switch OLED {i}", 200, id=i, model_key="consoles:switch_oled", niche_id="consoles")
                 for i in range(6)]
        await db.track(items)
        await db.track([make("x", 1, id=99, model_key="sig:nobrand:x")])          # signatures are not tracked
        past = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        await db.conn.execute("UPDATE tracking SET first_seen=?, last_checked=?", (past, past))
        await db.conn.commit()
        due = await db.due_for_check(10)
        assert len(due) == 6
        await db.update_tracking(0, "sold", 180)
        await db.update_tracking(1, "sold", 190)
        await db.update_tracking(2, "gone", None)
        s = await db.sales("consoles:switch_oled")
        assert (s["tracked"], s["sold"], s["gone"], s["active"]) == (6, 3, 1, 3)   # gone = sold (404 once sold)
        assert s["median_days"] == pytest.approx(4, abs=0.1)
        assert s["sell_through"] == pytest.approx(3 / 6, abs=0.01)
        assert sorted(p for p, _ in s["prices"]) == [180, 190, 200]
        assert (await db.tracking_totals())["sold"] == 2
    finally:
        await db.close()


def test_truncated_json_recovered():
    cut = '{"recommendation": "BUY", "resale_eur": 220, "red_flags": ["a", "b"], "summary": "très long texte coup'
    assert extract_json(cut) == {"recommendation": "BUY", "resale_eur": 220, "red_flags": ["a", "b"]}
    assert extract_json('```json\n{"ok": true}\n```') == {"ok": True}


async def test_llm_circuit_breaker_fails_fast():
    import httpx
    from vinted_hunter.ai.llm import LLM

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("reset by peer")

    async def spend():
        return 0.0

    async def record(*_):
        return None

    llm = LLM("key", spend_today=spend, record_usage=record)
    llm._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="injoignable"):
        await llm.complete("m", "s", "u", tier=2)
    first = calls["n"]
    with pytest.raises(RuntimeError, match="injoignable"):
        await llm.complete("m", "s", "u", tier=2)
    assert first == 2 and calls["n"] == 2          # second call fails instantly, no network attempt
    await llm.close()


async def test_vinted_stops_on_first_429_and_caps_hourly():
    import httpx
    from vinted_hunter.vinted.client import VintedClient, VintedUnavailable

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200 if request.url.path == "/" else 429, text="")

    c = VintedClient("www.vinted.fr", min_interval=0, max_per_hour=5)
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(VintedUnavailable, match="10 min"):
        await c._get("/items/1", as_json=False)
    assert calls["n"] == 2 and c.circuit_open          # bootstrap + one 429, no retry
    with pytest.raises(VintedUnavailable):
        await c._get("/items/2", as_json=False)
    assert calls["n"] == 2                             # paused: no network at all
    assert c.requests_last_hour == 2 and c.has_budget(1.0) is False   # circuit open => no background budget
    await c.close()


async def test_cheapest_first_search_keeps_the_query():
    import httpx
    from vinted_hunter.models import SearchSpec
    from vinted_hunter.vinted.client import VintedClient

    seen = {}

    def handler(request):
        seen["order"] = request.url.params.get("order")
        return httpx.Response(200, text="")

    c = VintedClient("www.vinted.fr", min_interval=0)
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.search(SearchSpec(query="carhartt detroit"), order="price_low_to_high")
    assert seen["order"] == "relevance"
    await c.close()


def test_irrelevant_results_are_detected():
    from vinted_hunter.vinted.client import _relevant
    junk = [make(t, 1) for t in ["Jean Jules skinny", "Set camicie", "Polo fila", "Top shein"] * 5]
    good = [make(t, 90) for t in ["Carhartt Detroit jacket", "Veste Carhartt", "Detroit WIP"] * 5]
    assert not _relevant("Carhartt Detroit jacket", junk) and _relevant("Carhartt Detroit jacket", good)

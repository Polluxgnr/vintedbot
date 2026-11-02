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
        assert (s["tracked"], s["sold"], s["gone"], s["active"]) == (6, 2, 1, 3)
        assert s["median_days"] == pytest.approx(4, abs=0.1)
        assert s["sell_through"] == pytest.approx(2.5 / 6, abs=0.01)
        assert sorted(p for p, _ in s["prices"]) == [180, 190]
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

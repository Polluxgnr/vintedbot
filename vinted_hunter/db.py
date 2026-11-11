"""SQLite persistence (async, WAL).

Every listing the bot ever sees is stored as a price observation: this is the
free "market memory" the valuation engine uses for comparables.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .config import RuntimeSettings
from .models import AIVerdict, Condition, Deal, Listing, SearchSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    niche_id TEXT, model_key TEXT, title TEXT, price REAL, brand TEXT, condition INTEGER,
    url TEXT, photo TEXT, seller_id INTEGER, data TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_listings_model ON listings(niche_id, model_key);

CREATE TABLE IF NOT EXISTS price_obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER, niche_id TEXT, model_key TEXT, condition INTEGER, price REAL, observed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_model ON price_obs(model_key, observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_time ON price_obs(observed_at);
CREATE INDEX IF NOT EXISTS ix_listings_seen ON listings(first_seen);

CREATE TABLE IF NOT EXISTS verdicts (
    listing_id INTEGER PRIMARY KEY, tier INTEGER, data TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS deals (
    listing_id INTEGER PRIMARY KEY, chat_id INTEGER, watch_id INTEGER, score INTEGER, profit REAL,
    fair_value REAL, price REAL, status TEXT DEFAULT 'alerted', data TEXT,
    alerted_at TEXT, buy_price REAL, sell_price REAL, closed_at TEXT
);

CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT, niche_id TEXT, spec TEXT,
    active INTEGER DEFAULT 1, created_at TEXT, last_run TEXT, hits INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT, model TEXT, tier INTEGER, calls INTEGER, cost REAL, tokens_in INTEGER, tokens_out INTEGER,
    PRIMARY KEY (day, model, tier)
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

-- Sales database: identified listings are re-checked over time. Vinted has no public "sold" flag,
-- so a sale is read from the item page (sold out / reserved / gone). This gives REAL sale prices,
-- days-to-sell and sell-through per model - the best evidence of what an item is worth.
CREATE TABLE IF NOT EXISTS tracking (
    listing_id INTEGER PRIMARY KEY, model_key TEXT, niche_id TEXT, condition INTEGER,
    first_price REAL, last_price REAL, first_seen TEXT, last_checked TEXT, checks INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',          -- active | reserved | sold | gone
    closed_at TEXT, priority INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tracking_due ON tracking(status, last_checked);
CREATE INDEX IF NOT EXISTS ix_tracking_model ON tracking(model_key, status);

-- Radar: every good deal seen (not only the alerted pépites), for the sortable "Top" view.
CREATE TABLE IF NOT EXISTS radar (
    listing_id INTEGER PRIMARY KEY, niche_id TEXT, score INTEGER, profit REAL, margin REAL, discount REAL,
    price REAL, pepite INTEGER DEFAULT 0, data TEXT, seen_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_radar_seen ON radar(seen_at);

-- Learned prices: recomputed every 6 h from everything observed and sold. Replaces the YAML seed
-- prices progressively, so valuations improve the longer the bot runs.
CREATE TABLE IF NOT EXISTS model_stats (
    model_key TEXT PRIMARY KEY, n INTEGER, calibrated REAL, ask_median REAL, sold_median REAL,
    median_7d REAL, median_30d REAL, trend_pct REAL, updated_at TEXT, upper_calibrated REAL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._stats_cache: dict[str, dict | None] = {}

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=15000")   # bot + CLI can share the file safely
        await self._conn.executescript(SCHEMA)
        cols = {r[1] for r in await (await self._conn.execute("PRAGMA table_info(model_stats)")).fetchall()}
        if "upper_calibrated" not in cols:
            await self._conn.execute("ALTER TABLE model_stats ADD COLUMN upper_calibrated REAL")
        radar_cols = {r[1] for r in await (await self._conn.execute("PRAGMA table_info(radar)")).fetchall()}
        for col, decl in (("expected", "REAL"), ("tier", "INTEGER DEFAULT 1"), ("scam", "INTEGER DEFAULT 0"),
                          ("verified", "INTEGER DEFAULT 0")):
            if col not in radar_cols:
                await self._conn.execute(f"ALTER TABLE radar ADD COLUMN {col} {decl}")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() not called"
        return self._conn

    async def _all(self, sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    async def _one(self, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    # ------------------------------------------------------------- listings
    async def known_prices(self, ids: list[int]) -> dict[int, float]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await self._all(f"SELECT id, price FROM listings WHERE id IN ({marks})", tuple(ids))
        return {r["id"]: r["price"] for r in rows}

    async def upsert_listings(self, listings: list[Listing]) -> None:
        """Store listings and record a price observation for new items and price changes."""
        if not listings:
            return
        now = _now()
        known = await self.known_prices([l.id for l in listings])
        for l in listings:
            if l.id not in known or abs(known[l.id] - l.price) > 0.01:
                await self.conn.execute(
                    "INSERT INTO price_obs(listing_id, niche_id, model_key, condition, price, observed_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (l.id, l.niche_id, l.model_key, int(l.condition), l.price, now),
                )
            await self.conn.execute(
                """INSERT INTO listings(id, niche_id, model_key, title, price, brand, condition, url, photo,
                                        seller_id, data, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET price=excluded.price, last_seen=excluded.last_seen,
                       model_key=COALESCE(excluded.model_key, listings.model_key),
                       niche_id=COALESCE(excluded.niche_id, listings.niche_id),
                       data=COALESCE(excluded.data, listings.data)""",
                (l.id, l.niche_id, l.model_key, l.title, l.price, l.brand, int(l.condition), l.url,
                 l.photos[0] if l.photos else None, l.seller.id,
                 None if (l.model_key or "sig:").startswith("sig:") else l.model_dump_json(), now, now),
            )
        await self.conn.commit()

    async def track(self, listings: list[Listing], priority: int = 0) -> None:
        """Start tracking identified (non-signature) listings; raise priority for near misses."""
        now = _now()
        for l in listings:
            if not l.model_key or l.model_key.startswith("sig:"):
                continue
            await self.conn.execute(
                """INSERT INTO tracking(listing_id, model_key, niche_id, condition, first_price, last_price,
                                        first_seen, last_checked, priority)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(listing_id) DO UPDATE SET priority=MAX(priority, excluded.priority),
                       model_key=excluded.model_key""",
                (l.id, l.model_key, l.niche_id, int(l.condition), l.price, l.price, now, now, priority),
            )
        await self.conn.commit()

    async def due_for_check(self, limit: int, min_age_hours: float = 6) -> list[aiosqlite.Row]:
        """Oldest-checked active listings first; near misses (priority) jump the queue."""
        before = (datetime.now(timezone.utc) - timedelta(hours=min_age_hours)).isoformat()
        return await self._all(
            "SELECT listing_id, model_key, niche_id, last_price, priority FROM tracking "
            "WHERE status IN ('active','reserved') AND last_checked < ? "
            "ORDER BY priority DESC, last_checked ASC LIMIT ?", (before, limit))

    async def update_tracking(self, listing_id: int, status: str, price: float | None) -> None:
        now = _now()
        await self.conn.execute(
            "UPDATE tracking SET status=?, last_price=COALESCE(?, last_price), last_checked=?, checks=checks+1, "
            "closed_at=CASE WHEN ? IN ('sold','gone') AND closed_at IS NULL THEN ? ELSE closed_at END "
            "WHERE listing_id=?", (status, price, now, status, now, listing_id))
        await self.conn.commit()

    async def sales(self, model_key: str, days: int = 90) -> dict[str, Any]:
        """Real sales of a model: prices, days-to-sell, sell-through.

        Vinted answers 404 to anonymous visitors once an item is sold, so 'gone' is counted as a sale (it can
        also be a deletion; the error is small next to ignoring every sale)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = await self._all(
            "SELECT status, last_price, condition, first_seen, closed_at FROM tracking "
            "WHERE model_key=? AND first_seen>=?", (model_key, since))
        sold = [r for r in rows if r["status"] in ("sold", "gone")]
        gone = [r for r in rows if r["status"] == "gone"]
        active = [r for r in rows if r["status"] in ("active", "reserved")]
        days_to_sell = sorted(
            (datetime.fromisoformat(r["closed_at"]) - datetime.fromisoformat(r["first_seen"])).total_seconds() / 86400
            for r in sold if r["closed_at"])
        closed = len(sold)
        return {
            "tracked": len(rows), "sold": len(sold), "gone": len(gone), "active": len(active),
            "prices": [(r["last_price"], Condition.parse(r["condition"])) for r in sold],
            "median_days": round(statistics.median(days_to_sell), 1) if days_to_sell else None,
            "sell_through": round(closed / len(rows), 2) if len(rows) >= 5 else None,
        }

    async def calibrate(self, min_obs: int = 15) -> int:
        """Learn each model's 'very good' resale price from its last 30 days of observations and sales.

        Asking prices are condition-normalized, outliers trimmed, then discounted 10 % (asks are not sales);
        confirmed sales, when there are enough, are trusted more. Also computes the 7-day vs 30-day trend.
        """
        from .market import ASK_TO_SALE, robust_center

        since30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        since7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        keys = await self._all(
            "SELECT model_key, COUNT(*) AS n FROM price_obs WHERE observed_at>=? AND model_key IS NOT NULL "
            "AND model_key NOT LIKE 'sig:%' GROUP BY model_key HAVING n>=?", (since30, min_obs))
        updated = 0
        for row in keys:
            key = row["model_key"]
            obs = await self._all("SELECT price, condition, observed_at FROM price_obs WHERE model_key=? AND observed_at>=?",
                                  (key, since30))
            base = [(r["price"] / Condition.parse(r["condition"]).value_factor, r["observed_at"]) for r in obs if r["price"]]
            if len(base) < min_obs:
                continue
            ask, _, _ = robust_center([b for b, _ in base])
            # Fake-prone categories: the cheap end is full of fakes and partial items -> also learn the upper market.
            ordered = sorted(b for b, _ in base)
            upper = robust_center(ordered[int(len(ordered) * 0.4):])[0] * ASK_TO_SALE
            recent = [b for b, t in base if t >= since7]
            med7 = robust_center(recent)[0] if len(recent) >= 5 else None
            sales = await self.sales(key, days=60)
            sold = [p / c.value_factor for p, c in sales["prices"]]
            sold_med = robust_center(sold)[0] if len(sold) >= 3 else None
            calibrated = ask * ASK_TO_SALE
            if sold_med:
                w = min(0.7, len(sold) / 20)
                calibrated = calibrated * (1 - w) + sold_med * w
            trend = round((med7 / ask - 1) * 100, 1) if med7 else None
            await self.conn.execute(
                """INSERT INTO model_stats(model_key, n, calibrated, ask_median, sold_median, median_7d, median_30d,
                                           trend_pct, updated_at, upper_calibrated) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(model_key) DO UPDATE SET n=excluded.n, calibrated=excluded.calibrated,
                       ask_median=excluded.ask_median, sold_median=excluded.sold_median, median_7d=excluded.median_7d,
                       median_30d=excluded.median_30d, trend_pct=excluded.trend_pct, updated_at=excluded.updated_at,
                       upper_calibrated=excluded.upper_calibrated""",
                (key, len(base), round(calibrated, 2), round(ask, 2), sold_med and round(sold_med, 2),
                 med7 and round(med7, 2), round(ask, 2), trend, _now(), round(upper, 2)))
            updated += 1
        await self.conn.commit()
        self._stats_cache.clear()
        return updated

    async def model_stats(self, model_key: str | None) -> dict | None:
        if not model_key or model_key.startswith("sig:"):
            return None
        if model_key not in self._stats_cache:
            row = await self._one("SELECT * FROM model_stats WHERE model_key=?", (model_key,))
            self._stats_cache[model_key] = dict(row) if row else None
        return self._stats_cache[model_key]

    async def learning_summary(self) -> dict[str, Any]:
        row = await self._one("SELECT COUNT(*) AS models, COALESCE(SUM(n),0) AS obs, MAX(updated_at) AS at FROM model_stats")
        discovered = await self._one(
            "SELECT COUNT(*) AS n FROM (SELECT model_key FROM listings WHERE model_key LIKE 'ai:%' "
            "GROUP BY model_key HAVING COUNT(*) >= 3)")
        movers = await self._all(
            "SELECT model_key, trend_pct, calibrated FROM model_stats WHERE trend_pct IS NOT NULL "
            "AND ABS(trend_pct) >= 5 ORDER BY ABS(trend_pct) DESC LIMIT 5")
        return {"models": row["models"], "obs": row["obs"], "updated_at": row["at"],
                "discovered": discovered["n"], "movers": [dict(m) for m in movers]}

    async def tracking_totals(self) -> dict[str, int]:
        rows = await self._all("SELECT status, COUNT(*) AS n FROM tracking GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    async def top_models(self, limit: int = 8) -> list[aiosqlite.Row]:
        """Fastest-selling models: most confirmed sales, then shortest time on the market."""
        return await self._all(
            """SELECT model_key, COUNT(*) AS sold,
                      AVG(julianday(closed_at) - julianday(first_seen)) AS days, AVG(last_price) AS price
               FROM tracking WHERE status IN ('sold','gone') AND closed_at IS NOT NULL
               GROUP BY model_key HAVING sold >= 2 ORDER BY sold DESC, days ASC LIMIT ?""", (limit,))

    async def relabel_model(self, listing_id: int, model_key: str) -> None:
        """AI identified the exact model: re-key the observation so future comparables improve."""
        await self.conn.execute("UPDATE listings SET model_key=? WHERE id=?", (model_key, listing_id))
        await self.conn.execute("UPDATE price_obs SET model_key=? WHERE listing_id=?", (model_key, listing_id))
        await self.conn.commit()

    async def get_listing(self, listing_id: int) -> Listing | None:
        row = await self._one("SELECT data FROM listings WHERE id=?", (listing_id,))
        return Listing.model_validate_json(row["data"]) if row and row["data"] else None

    async def comparables(self, model_key: str, *, days: int = 45, exclude_id: int | None = None,
                          limit: int = 400) -> list[tuple[float, Condition]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = await self._all(
            "SELECT price, condition FROM price_obs WHERE model_key=? AND observed_at>=? AND listing_id IS NOT ? "
            "ORDER BY observed_at DESC LIMIT ?",
            (model_key, since, exclude_id, limit),
        )
        return [(r["price"], Condition.parse(r["condition"])) for r in rows]

    async def distinct_comparables(self, model_key: str, *, days: int = 14, exclude_ids: set[int] = frozenset(),
                                   limit: int = 300) -> list[tuple[float, Condition]]:
        """One price per listing (its latest), so a listing seen 10 times counts once."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = await self._all(
            # SQLite: bare columns next to MAX() come from the row holding the max -> each listing's latest price.
            "SELECT listing_id, price, condition, MAX(observed_at) AS t FROM price_obs WHERE model_key=? "
            "AND observed_at>=? GROUP BY listing_id ORDER BY t DESC LIMIT ?",
            (model_key, since, limit + len(exclude_ids)),
        )
        out, seen = [], set(exclude_ids)
        for r in rows:
            if r["listing_id"] not in seen:
                seen.add(r["listing_id"])
                out.append((r["price"], Condition.parse(r["condition"])))
        return out[:limit]

    async def market_snapshot(self, model_key: str, days: int = 45) -> dict[str, Any]:
        prices = sorted(p for p, _ in await self.comparables(model_key, days=days))
        if not prices:
            return {"count": 0}
        q = statistics.quantiles(prices, n=4) if len(prices) >= 4 else [prices[0], statistics.median(prices), prices[-1]]
        return {"count": len(prices), "p25": q[0], "median": statistics.median(prices), "p75": q[-1],
                "min": prices[0], "max": prices[-1]}

    # ------------------------------------------------------------- verdicts
    async def get_verdict(self, listing_id: int) -> AIVerdict | None:
        row = await self._one("SELECT data FROM verdicts WHERE listing_id=?", (listing_id,))
        return AIVerdict.model_validate_json(row["data"]) if row else None

    async def save_verdict(self, listing_id: int, verdict: AIVerdict) -> None:
        await self.conn.execute(
            "INSERT INTO verdicts(listing_id, tier, data, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(listing_id) DO UPDATE SET tier=excluded.tier, data=excluded.data, updated_at=excluded.updated_at",
            (listing_id, verdict.tier, verdict.model_dump_json(), _now()),
        )
        await self.conn.commit()

    # ---------------------------------------------------------------- deals
    async def was_alerted(self, listing_id: int, chat_id: int) -> bool:
        return await self._one("SELECT 1 FROM deals WHERE listing_id=? AND chat_id=?", (listing_id, chat_id)) is not None

    async def save_deal(self, deal: Deal, chat_id: int) -> None:
        await self.conn.execute(
            """INSERT INTO deals(listing_id, chat_id, watch_id, score, profit, fair_value, price, data, alerted_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(listing_id) DO UPDATE SET score=excluded.score, profit=excluded.profit,
                   fair_value=excluded.fair_value, price=excluded.price, data=excluded.data""",
            (deal.listing.id, chat_id, deal.watch_id, deal.score, deal.profit, deal.valuation.fair_value,
             deal.listing.price, deal.model_dump_json(), _now()),
        )
        await self.conn.commit()

    async def get_deal(self, listing_id: int) -> Deal | None:
        row = await self._one("SELECT data FROM deals WHERE listing_id=?", (listing_id,)) \
            or await self._one("SELECT data FROM radar WHERE listing_id=?", (listing_id,))
        return Deal.model_validate_json(row["data"]) if row else None

    # ---------------------------------------------------------------- radar
    async def save_radar(self, deals: list[Deal]) -> None:
        now = _now()
        for d in deals:
            fv = d.valuation.fair_value or 0
            discount = round((1 - d.listing.price / fv) * 100, 1) if fv else None
            expected = round((d.profit or 0) * d.valuation.confidence, 2)      # profit you can count on
            scam = int(bool(d.inspection and d.inspection.scams) or bool(d.wardrobe and d.wardrobe.suspect))
            await self.conn.execute(
                """INSERT INTO radar(listing_id, niche_id, score, profit, margin, discount, price, pepite, data, seen_at,
                                     expected, tier, scam, verified)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(listing_id) DO UPDATE SET score=excluded.score, profit=excluded.profit,
                       margin=excluded.margin, discount=excluded.discount, price=excluded.price,
                       pepite=MAX(radar.pepite, excluded.pepite), data=excluded.data, expected=excluded.expected,
                       tier=MAX(radar.tier, excluded.tier), scam=excluded.scam,
                       verified=MAX(radar.verified, excluded.verified)""",
                (d.listing.id, d.listing.niche_id, d.score, d.profit, d.margin_pct, discount, d.listing.price,
                 int(d.pepite), d.model_dump_json(), now, expected, d.verdict.tier if d.verdict else 1, scam,
                 int(d.market_check is not None)),
            )
        await self.conn.commit()

    RADAR_SORTS = {"valeur": "r.pepite DESC, r.expected DESC", "score": "r.pepite DESC, r.score DESC",
                   "profit": "r.profit DESC", "marge": "r.margin DESC", "remise": "r.discount DESC",
                   "recent": "r.seen_at DESC"}

    def _radar_where(self, hours: int, niche: str | None, pepites: bool, analysed: bool,
                     min_profit: float) -> tuple[str, list]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        # Sold, reserved-then-gone or deleted listings and anything scam-flagged never show up.
        where = ["r.seen_at>=?", "r.scam=0", "COALESCE(t.status,'active') IN ('active','reserved')"]
        args: list = [since]
        if niche:
            where.append("r.niche_id=?")
            args.append(niche)
        if pepites:
            where.append("r.pepite=1")
        if analysed:
            where.append("r.tier>=2")
        if min_profit:
            where.append("r.profit>=?")
            args.append(min_profit)
        return " AND ".join(where), args

    async def radar(self, *, hours: int = 24, sort: str = "valeur", niche: str | None = None, pepites: bool = False,
                    analysed: bool = False, min_profit: float = 0, limit: int = 10,
                    offset: int = 0) -> tuple[list[Deal], int]:
        where, args = self._radar_where(hours, niche, pepites, analysed, min_profit)
        base = f"FROM radar r LEFT JOIN tracking t ON t.listing_id=r.listing_id WHERE {where}"
        total = (await self._one(f"SELECT COUNT(*) AS n {base}", tuple(args)))["n"]
        rows = await self._all(
            f"SELECT r.data {base} ORDER BY {self.RADAR_SORTS.get(sort, self.RADAR_SORTS['valeur'])} LIMIT ? OFFSET ?",
            (*args, limit, offset))
        return [Deal.model_validate_json(r["data"]) for r in rows], total

    async def radar_niches(self, hours: int = 24, *, pepites: bool = False, analysed: bool = False,
                           min_profit: float = 0) -> list[tuple[str, int, float]]:
        """Per niche: number of live deals and best profit."""
        where, args = self._radar_where(hours, None, pepites, analysed, min_profit)
        rows = await self._all(
            f"SELECT r.niche_id, COUNT(*) AS n, MAX(r.profit) AS best FROM radar r "
            f"LEFT JOIN tracking t ON t.listing_id=r.listing_id WHERE {where} AND r.niche_id IS NOT NULL "
            "GROUP BY r.niche_id ORDER BY n DESC", tuple(args))
        return [(r["niche_id"], r["n"], r["best"] or 0) for r in rows]

    async def update_radar_price(self, listing_id: int, status: str, price: float | None) -> None:
        """After a live availability check: remove dead listings, refresh prices."""
        if status in ("sold", "gone"):
            await self.conn.execute("DELETE FROM radar WHERE listing_id=?", (listing_id,))
        elif price is not None:
            await self.conn.execute("UPDATE radar SET price=? WHERE listing_id=?", (price, listing_id))
        await self.conn.commit()

    async def cleanup(self) -> dict[str, int]:
        """Retention: keep the database fast. Sales history (tracking) is kept; raw noise is not."""
        cut = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()  # noqa: E731
        out = {}
        for table, col, days in (("price_obs", "observed_at", 120), ("radar", "seen_at", 14),
                                 ("verdicts", "updated_at", 30)):
            cur = await self.conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cut(days),))
            out[table] = cur.rowcount
        cur = await self.conn.execute(
            "DELETE FROM listings WHERE last_seen < ? AND id NOT IN (SELECT listing_id FROM tracking) "
            "AND id NOT IN (SELECT listing_id FROM deals)", (cut(30),))
        out["listings"] = cur.rowcount
        await self.conn.commit()
        return out

    async def set_deal_status(self, listing_id: int, status: str, *, buy_price: float | None = None,
                              sell_price: float | None = None) -> None:
        await self.conn.execute(
            "UPDATE deals SET status=?, buy_price=COALESCE(?, buy_price), sell_price=COALESCE(?, sell_price), "
            "closed_at=CASE WHEN ?='sold' THEN ? ELSE closed_at END WHERE listing_id=?",
            (status, buy_price, sell_price, status, _now(), listing_id),
        )
        await self.conn.commit()

    async def portfolio(self, chat_id: int) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT listing_id, status, buy_price, sell_price, fair_value, profit, data, alerted_at, closed_at "
            "FROM deals WHERE chat_id=? AND status IN ('bought','sold') ORDER BY alerted_at DESC",
            (chat_id,),
        )

    async def top_deals(self, chat_id: int, hours: int = 24, limit: int = 10) -> list[Deal]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = await self._all(
            "SELECT data FROM deals WHERE chat_id=? AND alerted_at>=? AND status='alerted' ORDER BY score DESC LIMIT ?",
            (chat_id, since, limit),
        )
        return [Deal.model_validate_json(r["data"]) for r in rows]

    # -------------------------------------------------------------- watches
    async def add_watch(self, chat_id: int, kind: str, name: str, spec: SearchSpec) -> int:
        cur = await self.conn.execute(
            "INSERT INTO watches(chat_id, kind, name, niche_id, spec, created_at) VALUES (?,?,?,?,?,?)",
            (chat_id, kind, name, spec.niche_id, spec.model_dump_json(), _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def watches(self, chat_id: int | None = None, *, active_only: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM watches WHERE 1=1", []
        if chat_id is not None:
            sql += " AND chat_id=?"
            args.append(chat_id)
        if active_only:
            sql += " AND active=1"
        rows = await self._all(sql + " ORDER BY id", tuple(args))
        return [{**dict(r), "spec": SearchSpec.model_validate_json(r["spec"])} for r in rows]

    async def niche_watch(self, chat_id: int, niche_id: str) -> dict | None:
        rows = [w for w in await self.watches(chat_id) if w["kind"] == "niche" and w["niche_id"] == niche_id]
        return rows[0] if rows else None

    async def set_watch_active(self, watch_id: int, active: bool) -> None:
        await self.conn.execute("UPDATE watches SET active=? WHERE id=?", (int(active), watch_id))
        await self.conn.commit()

    async def delete_watch(self, watch_id: int) -> None:
        await self.conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
        await self.conn.commit()

    async def touch_watch(self, watch_id: int, hits: int) -> None:
        await self.conn.execute("UPDATE watches SET last_run=?, hits=hits+? WHERE id=?", (_now(), hits, watch_id))
        await self.conn.commit()

    # ------------------------------------------------------------- AI usage
    async def record_ai_usage(self, model: str, tier: int, cost: float, tokens_in: int, tokens_out: int) -> None:
        await self.conn.execute(
            """INSERT INTO ai_usage(day, model, tier, calls, cost, tokens_in, tokens_out) VALUES (?,?,?,1,?,?,?)
               ON CONFLICT(day, model, tier) DO UPDATE SET calls=calls+1, cost=cost+excluded.cost,
                   tokens_in=tokens_in+excluded.tokens_in, tokens_out=tokens_out+excluded.tokens_out""",
            (_today(), model, tier, cost, tokens_in, tokens_out),
        )
        await self.conn.commit()

    async def ai_spend_today(self) -> float:
        row = await self._one("SELECT COALESCE(SUM(cost),0) AS c FROM ai_usage WHERE day=?", (_today(),))
        return float(row["c"]) if row else 0.0

    async def ai_usage_today(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT tier, model, calls, cost FROM ai_usage WHERE day=? ORDER BY tier", (_today(),))

    # ---------------------------------------------------------------- stats
    async def stats(self, chat_id: int) -> dict[str, Any]:
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        seen = await self._one("SELECT COUNT(*) AS n FROM listings WHERE first_seen>=?", (day_ago,))
        total = await self._one("SELECT COUNT(*) AS n FROM listings")
        alerts = await self._one("SELECT COUNT(*) AS n, COALESCE(SUM(profit),0) AS p FROM deals "
                                 "WHERE chat_id=? AND alerted_at>=?", (chat_id, day_ago))
        realized = await self._one("SELECT COUNT(*) AS n, COALESCE(SUM(sell_price - buy_price),0) AS p FROM deals "
                                   "WHERE chat_id=? AND status='sold'", (chat_id,))
        return {"seen_24h": seen["n"], "seen_total": total["n"], "alerts_24h": alerts["n"],
                "potential_24h": alerts["p"], "sold": realized["n"], "realized": realized["p"]}

    # ----------------------------------------------------------- kv / prefs
    async def kv_get(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.conn.execute("INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (key, value))
        await self.conn.commit()

    async def runtime_settings(self, chat_id: int) -> RuntimeSettings:
        raw = await self.kv_get(f"settings:{chat_id}")
        return RuntimeSettings.model_validate_json(raw) if raw else RuntimeSettings()

    async def save_runtime_settings(self, chat_id: int, rs: RuntimeSettings) -> None:
        await self.kv_set(f"settings:{chat_id}", rs.model_dump_json())

    async def owners(self) -> set[int]:
        raw = await self.kv_get("owners")
        return set(json.loads(raw)) if raw else set()

    async def add_owner(self, chat_id: int) -> None:
        owners = await self.owners() | {chat_id}
        await self.kv_set("owners", json.dumps(sorted(owners)))

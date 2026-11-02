from datetime import datetime, timedelta, timezone

import pytest

from vinted_hunter.ai.cascade import AICascade
from vinted_hunter.ai.llm import extract_json
from vinted_hunter.config import ROOT, RuntimeSettings
from vinted_hunter.db import Database
from vinted_hunter.market import estimate
from vinted_hunter.models import AIVerdict, Condition, Listing, Recommendation, SearchSpec, Seller
from vinted_hunter.niches import NicheCatalog, normalize, signature_key
from vinted_hunter.scoring import build_deal, is_alert_worthy, reject_reason
from vinted_hunter.vinted.parser import item_id_from_url, parse_catalog_item, parse_item_page

CATALOG = NicheCatalog.load(ROOT / "config" / "niches.yaml")


def make(title, price, cond=Condition.VERY_GOOD, **kw) -> Listing:
    return Listing(id=kw.pop("id", 1), title=title, price=price, condition=cond, url="https://www.vinted.fr/items/1",
                   photos=["a", "b", "c"], seller=Seller(id=1, rating=4.9, reviews=120),
                   posted_at=datetime.now(timezone.utc) - timedelta(minutes=5), **kw)


# ---------------------------------------------------------------- niches
@pytest.mark.parametrize("niche,title,key", [
    ("apple_tech", "AirPods Pro 2 USB-C neufs", "airpods_pro2"),
    ("apple_tech", "Airpods pro première génération", "airpods_pro1"),
    ("sneakers", "Nike Dunk Low Panda noir blanc 42", "dunk_panda"),
    ("sneakers", "Nike dunk low UNC", "dunk_low"),
    ("film_cameras", "Olympus µ[mju:] II appareil argentique", "mju2"),
    ("film_cameras", "Canon AE-1 Program + 50mm", "canon_ae1"),
    ("streetwear_designer", "Veste C.P. Company goggle", "cp_goggle"),
    ("consoles", "PS5 édition digitale", "ps5_digital"),
    ("consoles", "Playstation 5 lecteur + manette", "ps5_disc"),
    ("audio", "Sony WH-1000XM4 noir", "xm4"),
])
def test_identify_models(niche, title, key):
    ref = CATALOG.get(niche).identify(title)
    assert ref is not None and ref.key == key, (title, ref)


def test_guess_niche():
    assert CATALOG.guess("airpods pro 2").id == "apple_tech"
    assert CATALOG.guess("contax t2").id == "film_cameras"
    assert CATALOG.guess("lot lego vrac").id == "lego"


def test_excludes():
    apple = CATALOG.get("apple_tech")
    assert reject_reason(make("Coque AirPods Pro 2 silicone", 8), apple)
    assert reject_reason(make("AirPods Pro 2 pour pièces HS", 30), apple)
    assert reject_reason(make("AirPods Pro 2 boîte vide", 30), apple)
    assert reject_reason(make("AirPods Pro 2 comme neufs", 90), apple) is None
    sneakers = CATALOG.get("sneakers")
    assert reject_reason(make("Nike Dunk Low Panda TD bébé", 30), sneakers)


def test_retro_allows_hs():
    # Repairers want broken consoles in retro gaming
    assert reject_reason(make("Game Boy Color HS", 20), CATALOG.get("retro_gaming")) is None


def test_size_filter():
    spec = SearchSpec(query="dunk", size_hint="42")
    assert reject_reason(make("Dunk low", 50, size="43"), None, spec)
    assert reject_reason(make("Dunk low", 50, size="42"), None, spec) is None


def test_signature_key_stable():
    assert signature_key("Veste Barbour Beaufort taille M", "Barbour") == signature_key("Barbour beaufort veste L", "Barbour")


def test_normalize():
    assert normalize("Arc'teryx Béta  LT") == "arc teryx beta lt"


# ---------------------------------------------------------------- valuation & scoring
def test_estimate_condition_scaling():
    v_new = estimate(Condition.NEW_WITH_TAGS, reference=100)
    v_ok = estimate(Condition.SATISFACTORY, reference=100)
    assert v_new.fair_value > 100 > v_ok.fair_value


def test_estimate_uses_comps_and_trims_outliers():
    comps = [(100, Condition.VERY_GOOD)] * 20 + [(1, Condition.VERY_GOOD), (5000, Condition.VERY_GOOD)]
    v = estimate(Condition.VERY_GOOD, comps=comps)
    assert 85 <= v.fair_value <= 95          # median * ask-to-sale discount
    assert v.comps_count == 22


def test_confidence_grows_with_agreement():
    agree = estimate(Condition.VERY_GOOD, reference=100, comps=[(110, Condition.VERY_GOOD)] * 20, ai_estimate=100)
    disagree = estimate(Condition.VERY_GOOD, reference=100, comps=[(300, Condition.VERY_GOOD)] * 20, ai_estimate=40)
    assert agree.confidence > disagree.confidence


def test_good_deal_scores_high_and_alerts():
    rs = RuntimeSettings(alert_mode="normal", min_profit_eur=15, min_margin_pct=25, min_score=60)
    consoles = CATALOG.get("consoles")
    l = make("Nintendo Switch OLED complète", 120, favourites=8, views=40)
    deal = build_deal(l, estimate(l.condition, reference=220, comps=[(215, Condition.VERY_GOOD)] * 15), consoles, rs)
    assert deal.profit > 40
    assert deal.score >= 60
    assert is_alert_worthy(deal, rs, consoles)


def test_fake_prone_niche_capped_until_photo_check():
    rs = RuntimeSettings()
    apple = CATALOG.get("apple_tech")
    l = make("AirPods Pro 2", 70, favourites=8, views=40)
    val = estimate(l.condition, reference=140, comps=[(135, Condition.VERY_GOOD)] * 15)
    unverified = build_deal(l, val, apple, rs)
    assert unverified.score <= 55 and "auth_non_verifiee" in unverified.flags
    verified = build_deal(l, val, apple, rs, AIVerdict(tier=3, authenticity_risk=0.05, condition_score=8))
    assert verified.score > 55


def test_overpriced_never_alerts():
    rs = RuntimeSettings()
    l = make("AirPods Pro 2", 150)
    deal = build_deal(l, estimate(l.condition, reference=140), CATALOG.get("apple_tech"), rs)
    assert deal.profit < 0 and not is_alert_worthy(deal, rs, None)


def test_scam_price_flag_and_ai_veto():
    rs = RuntimeSettings()
    apple = CATALOG.get("apple_tech")
    l = make("AirPods Pro 2 neufs scellés", 25)
    val = estimate(l.condition, reference=140)
    clean = build_deal(l, val, apple, rs)
    assert "prix_suspect" in clean.flags
    vetoed = build_deal(l, val, apple, rs, AIVerdict(tier=3, authenticity_risk=0.9, recommendation=Recommendation.SKIP))
    assert vetoed.score < clean.score
    assert not is_alert_worthy(vetoed, rs, apple)


def test_landed_cost_includes_fees():
    deal = build_deal(make("x", 100), estimate(Condition.VERY_GOOD, reference=200), CATALOG.get("apple_tech"),
                      RuntimeSettings())
    assert deal.landed_cost == pytest.approx(100 + 5 + 0.7 + 5)


# ---------------------------------------------------------------- inspection (full description)
from vinted_hunter.inspection import assess_seller, inspect_listing  # noqa: E402


@pytest.mark.parametrize("desc,missing", [
    ("Remarque: j'ai décidé de ne pas vendre les manettes avec car elles bug", {"manettes/Joy-Con"}),
    ("Switch OLED sans dock ni manettes, juste la console", {"manettes/Joy-Con", "dock"}),
    ("Console seule, très bon état", {"manettes/Joy-Con", "dock", "alimentation/câbles"}),
    ("Joy-con non fournis. Dock inclus.", {"manettes/Joy-Con"}),
    ("Aucune rayure, avec manettes, dock et chargeur", set()),
    ("Sans rayure, manettes incluses", set()),
    ("Vendue complète avec boîte d'origine", set()),
])
def test_missing_parts_negation_aware(desc, missing):
    insp = inspect_listing(make("Nintendo Switch OLED", 150, description=desc), CATALOG.get("consoles"))
    assert {f.label.replace(" absent(e)s", "") for f in insp.missing} == missing


def test_defects_and_positives():
    insp = inspect_listing(make("iPhone 13", 300, description="Écran fissuré en bas. Batterie à changer. Facture dispo."),
                           CATALOG.get("apple_tech"))
    labels = {f.label for f in insp.defects}
    assert {"écran fissuré", "batterie usée"} <= labels
    assert "facture" in insp.positives
    clean = inspect_listing(make("iPhone 13", 300, description="Aucune rayure, jamais tombé, fonctionne parfaitement"),
                            CATALOG.get("apple_tech"))
    assert not clean.defects and "testé/fonctionnel" in clean.positives
    dysf = inspect_listing(make("x", 1, description="elles deviennent disfonctionnel"), None)
    assert "testé/fonctionnel" not in dysf.positives


def test_missing_controllers_lower_value():
    rs = RuntimeSettings()
    consoles = CATALOG.get("consoles")
    val = estimate(Condition.VERY_GOOD, reference=220, new_price=349)
    full = build_deal(make("Switch OLED", 100), val, consoles, rs)
    partial = build_deal(make("Pack Nintendo switch oled", 100, description="je ne vends pas les manettes"),
                         val, consoles, rs)
    assert partial.valuation.fair_value == pytest.approx(full.valuation.fair_value * 0.78, rel=0.01)
    assert partial.profit < full.profit and "incomplet" in partial.flags


def test_retail_cap_for_tech_not_collectibles():
    rs = RuntimeSettings()
    v = estimate(Condition.NEW_WITH_TAGS, reference=330, new_price=349)
    assert build_deal(make("Switch OLED", 100, cond=Condition.NEW_WITH_TAGS), v, CATALOG.get("consoles"), rs
                      ).valuation.fair_value <= 349 * 0.9
    s = estimate(Condition.NEW_WITH_TAGS, reference=200, new_price=190)
    assert build_deal(make("Jordan 4", 100, cond=Condition.NEW_WITH_TAGS), s, CATALOG.get("sneakers"), rs
                      ).valuation.fair_value > 190


def test_seller_assessment():
    from vinted_hunter.models import Inspection, Valuation
    good = make("x", 100)
    assert assess_seller(good, Inspection(), Valuation(fair_value=150)).level == "ok"
    newbie = make("x", 30, description="contactez moi sur whatsapp")
    newbie.seller.reviews, newbie.seller.rating = 0, None
    insp = inspect_listing(newbie, None)
    check = assess_seller(newbie, insp, Valuation(fair_value=150))
    assert check.level == "risque" and "contact hors Vinted demandé" in check.reasons


def test_relative_dates():
    from vinted_hunter.vinted.parser import parse_relative
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert parse_relative("Il y a une heure", now) == now - timedelta(hours=1)
    assert parse_relative("il y a 3 jours", now) == now - timedelta(days=3)
    assert parse_relative("Vu la dernière fois :  il y a 12 minutes", now) == now - timedelta(minutes=12)
    assert parse_relative("à l'instant", now) == now


def test_explain_deal_renders():
    from vinted_hunter.bot.views import deal_caption, explain_deal
    deal = build_deal(make("Pack Nintendo switch oled", 100, description="je ne vends pas les manettes"),
                      estimate(Condition.VERY_GOOD, reference=220, new_price=349), CATALOG.get("consoles"),
                      RuntimeSettings())
    text = explain_deal(deal)
    assert "manettes" in text and "Score" in text and "Cote finale" in text
    cap = deal_caption(deal)
    assert "Prix neuf" in cap and "Incomplet" in cap and len(cap) <= 1024


# ---------------------------------------------------------------- parsing
def test_parse_catalog_item():
    raw = {"id": 42, "title": "Contax T2", "price": {"amount": "450.0", "currency_code": "EUR"},
           "total_item_price": {"amount": "473.2"}, "brand_title": "Contax", "status": "Très bon état",
           "url": "https://www.vinted.fr/items/42-contax-t2", "favourite_count": 9, "view_count": 50,
           "photo": {"url": "https://img/1.jpg", "high_resolution": {"timestamp": 1_700_000_000}},
           "user": {"id": 7, "login": "bob", "feedback_reputation": 0.98, "feedback_count": 12}}
    l = parse_catalog_item(raw, "www.vinted.fr")
    assert l.price == 450 and l.condition is Condition.VERY_GOOD and l.photos == ["https://img/1.jpg"]
    assert l.seller.rating == pytest.approx(4.9) and l.posted_at.year == 2023


def test_parse_item_page_ld_json():
    page = '<script type="application/ld+json">{"@type":"Product","description":"Super état, testé","image":["u1","u2"]}</script>'
    l = parse_item_page(page, make("x", 10))
    assert l.description == "Super état, testé" and l.photos == ["u1", "u2"] and l.details_loaded


def test_item_id_from_url():
    assert item_id_from_url("https://www.vinted.fr/items/123456-nike-dunk?ref=x") == 123456


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Voici: {"items": [1]} merci') == {"items": [1]}
    assert extract_json("nope") is None


def test_verdict_merge_keeps_earlier_alarm():
    prev = AIVerdict(tier=2, authenticity_risk=0.8, red_flags=["prix trop bas"])
    merged = AICascade._merge(prev, {"authenticity_risk": 0.0, "condition_score": 8, "red_flags": ["rayure"],
                                     "recommendation": "buy"}, tier=3, cost=0.001)
    assert merged.tier == 3 and merged.authenticity_risk == pytest.approx(0.2)
    assert merged.red_flags == ["prix trop bas", "rayure"] and merged.recommendation is Recommendation.BUY


# ---------------------------------------------------------------- database
async def test_db_observations_and_comparables(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    try:
        items = [make(f"AirPods Pro 2 #{i}", 100 + i, id=i, model_key="apple_tech:airpods_pro2") for i in range(6)]
        await db.upsert_listings(items)
        await db.upsert_listings(items)                       # unchanged prices -> no new observation
        assert len(await db.comparables("apple_tech:airpods_pro2")) == 6
        items[0] = items[0].model_copy(update={"price": 80})
        await db.upsert_listings(items)                       # price drop -> new observation
        assert len(await db.comparables("apple_tech:airpods_pro2")) == 7
        assert (await db.known_prices([0]))[0] == 80
        await db.record_ai_usage("m", 2, 0.01, 10, 10)
        assert await db.ai_spend_today() == pytest.approx(0.01)
    finally:
        await db.close()


def test_parse_grid_summary_with_commas_in_title():
    from vinted_hunter.vinted.parser import parse_grid_summary
    r = parse_grid_summary("Scatola originale Contax T2, No Camera, Marque: contax, Taille: M, État: Bon état, 1 280,00 €, 1 344.70 €")
    assert r["title"] == "Scatola originale Contax T2, No Camera"
    assert (r["brand"], r["size"], r["condition"], r["price"], r["total"]) == ("contax", "M", "Bon état", 1280.0, 1344.7)


def test_parse_catalog_html():
    from vinted_hunter.vinted.parser import parse_catalog_html
    block = ('<div data-testid="product-item-id-77"><img src="https://img/310x430/a.webp?s=1" alt="x"/>'
             '<a href="/items/77-contax-t2?referrer=catalog" class="o" data-testid="product-item-id-77--overlay-link" '
             'title="Contax T2, Marque: contax, État: Très bon état, 700.00 €, 735.70 €" target="_self"></a>'
             '<span data-testid="favourite-count-text">25</span></div>')
    [l] = parse_catalog_html(block, "www.vinted.fr")
    assert (l.id, l.title, l.price, l.favourites, l.condition) == (77, "Contax T2", 700.0, 25, Condition.VERY_GOOD)
    assert l.url == "https://www.vinted.fr/items/77-contax-t2"


@pytest.mark.parametrize("title,rejected", [
    ("Contax T2 Box Titanium Black", True),
    ("Scatola originale Contax T2, No Camera", True),
    ("Contax T2 libretto istruzioni", True),
    ("Contax T2 Data Back", True),
    ("Contax T2 with original hardcase and box", False),
    ("Contax T2 + étui cuir", False),
])
def test_accessory_only(title, rejected):
    assert bool(reject_reason(make(title, 50), CATALOG.get("film_cameras"))) is rejected


def test_accessory_rule_keeps_sealed_lego_and_watches():
    assert reject_reason(make("LEGO 10294 Titanic neuf boîte scellée", 400), CATALOG.get("lego")) is None
    assert reject_reason(make("Seiko 5 automatique bracelet acier", 80), CATALOG.get("watches")) is None


def test_description_keyword_stuffing_ignored():
    niche = CATALOG.get("film_cameras")
    assert niche.identify("Ricoh AF-50 testée", "Contax T2 Yashica T4 Olympus mju ii") is None


# ---------------------------------------------------------------- regressions from live alerts (2026-11-01)
@pytest.mark.parametrize("title", ["Gorro de lana para invierno", "Arc'teryx Bird Head Beanie Mütze Schwarz",
                                   "Berretto a maglia Arc'teryx, ideale per inverno"])
def test_outdoor_hats_excluded(title):
    assert reject_reason(make(title, 10), CATALOG.get("outdoor_premium"))


def test_console_games_never_priced_as_consoles():
    consoles = CATALOG.get("consoles")
    assert consoles.identify("Monster Sanctuary Switch") is None
    assert reject_reason(make("Jeu Mario Kart Switch", 30), consoles)
    assert reject_reason(make("Nintendo Switch OLED + 3 jeux", 200), consoles) is None


def test_arcteryx_tee_not_priced_as_jacket():
    assert CATALOG.get("outdoor_premium").identify("Arc'teryx Bird Logo Long Sleeve T-Shirt").key == "arc_tee"


async def test_query_pool_only_for_matching_titles(tmp_path):
    from vinted_hunter.engine import Hunter
    hunter = Hunter.__new__(Hunter)
    consoles = [make(f"Nintendo Switch 2 console {i}", 400 + i, id=i) for i in range(8)]
    words, prices = hunter.query_pool(consoles, "nintendo switch 2", None, None)
    assert words == {"nintendo", "switch", "2"} and len(prices) == 8
    assert not words <= set(normalize("Monster Sanctuary Switch").split())


# ---------------------------------------------------------------- pépites gate
def _pepite_candidate(price=120, **kw):
    from vinted_hunter.models import MarketCheck
    l = make("Nintendo Switch OLED complète", price, favourites=8, views=40, description="Testée, complète avec boîte",
             model_key="consoles:switch_oled", details_loaded=True, **kw)
    val = estimate(l.condition, reference=220, comps=[(215, Condition.VERY_GOOD)] * 20, new_price=349)
    return build_deal(l, val, CATALOG.get("consoles"), RuntimeSettings())


def test_pepite_requires_live_market_proof():
    from vinted_hunter.models import MarketCheck
    from vinted_hunter.scoring import gate_fails
    rs, consoles = RuntimeSettings(quality_mode=False), CATALOG.get("consoles")
    deal = _pepite_candidate()
    assert gate_fails(deal, consoles, rs, require_market=False) == []
    assert "marché pas encore vérifié" in gate_fails(deal, consoles, rs)
    proven = deal.model_copy(update={"market_check": MarketCheck(n=14, median=215, cheaper=0, percentile=1.0)})
    assert gate_fails(proven, consoles, rs) == [] and is_alert_worthy(proven, rs, consoles)
    crowded = deal.model_copy(update={"market_check": MarketCheck(n=14, median=150, cheaper=4)})
    fails = gate_fails(crowded, consoles, rs)
    assert any("moins chères" in f for f in fails) and any("médiane" in f for f in fails)


def test_pepite_rejects_ordinary_and_too_good_to_be_true():
    from vinted_hunter.scoring import gate_fails
    rs, consoles = RuntimeSettings(), CATALOG.get("consoles")
    assert any(f.startswith("remise") for f in gate_fails(_pepite_candidate(price=170), consoles, rs, require_market=False))
    assert any("autre produit" in f for f in gate_fails(_pepite_candidate(price=40), consoles, rs, require_market=False))


def test_pepite_generic_model_and_fake_niche_blocked():
    from vinted_hunter.scoring import gate_fails
    rs = RuntimeSettings()
    street = CATALOG.get("streetwear_designer")
    l = make("Stone Island polo", 20, model_key="streetwear_designer:stone_other", details_loaded=True)
    d = build_deal(l, estimate(l.condition, reference=70), street, rs)
    assert "modèle pas identifié précisément" in gate_fails(d, street, rs, require_market=False)
    lux = CATALOG.get("luxury")
    l2 = make("Louis Vuitton Neverfull MM", 300, model_key="luxury:lv_neverfull", details_loaded=True)
    d2 = build_deal(l2, estimate(l2.condition, reference=900), lux, rs)
    assert any("vérification photo IA obligatoire" in f for f in gate_fails(d2, lux, rs, require_market=False))


@pytest.mark.parametrize("niche,title", [
    ("consoles", "The Legend of Zelda 40th Anniversary Nintendo Switch 2"),
    ("consoles", "Manette Switch 2 Pro Zelda Ocarina of Time"),
    ("retro_gaming", "Carte Pokémon Donphan 40/123 - HeartGold SoulSilver"),
    ("retro_gaming", "gamecube controller joystick Nintendo"),
    ("pokemon_tcg", "Lot de carte manga one piece etb EB05"),
    ("film_cameras", "Olympus mju 9000 Digicam - Complete Set"),
    ("streetwear_designer", "Jogging Stone Island Junior kaki"),
])
def test_live_false_matches_no_longer_identified(niche, title):
    assert CATALOG.get(niche).identify(title) is None, title


def test_quality_mode_wants_new_low_risk_established():
    from vinted_hunter.scoring import gate_fails, quality_fails
    rs, consoles = RuntimeSettings(), CATALOG.get("consoles")
    used = _pepite_candidate()
    assert any("neuf exigé" in f for f in quality_fails(used, consoles))
    new = _pepite_candidate()
    new = new.model_copy(update={"listing": new.listing.model_copy(update={"condition": Condition.NEW})})
    assert quality_fails(new, consoles) == []
    newbie = new.model_copy(update={"listing": new.listing.model_copy(
        update={"seller": new.listing.seller.model_copy(update={"reviews": 1})})})
    assert any("vendeur peu établi" in f for f in quality_fails(newbie, consoles))
    luxury = CATALOG.get("luxury")
    unverified = new.model_copy(update={"inspection": new.inspection.model_copy(update={"verification": None})})
    assert any("contrefaçons" in f for f in quality_fails(unverified, luxury))
    assert quality_fails(new, luxury) == []            # Vinted's authenticity check makes it acceptable
    assert any("neuf exigé" in f for f in gate_fails(used, consoles, rs, require_market=False))


def test_quality_weighs_in_score():
    from vinted_hunter.models import Valuation
    from vinted_hunter.scoring import build_deal
    rs, val = RuntimeSettings(), Valuation(fair_value=300, low=260, high=340, confidence=0.8)
    new = build_deal(make("Switch OLED", 150, Condition.NEW_WITH_TAGS), val, CATALOG.get("consoles"), rs)
    used = build_deal(make("Switch OLED", 150, Condition.GOOD), val, CATALOG.get("consoles"), rs)
    assert new.score_parts.get("neuf avec étiquette") == 7 and "état moyen" in used.score_parts

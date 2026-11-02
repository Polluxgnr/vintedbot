"""Regressions from the "ps5" session: accessories polluting prices, PS5 variants, loading screen."""
import pytest

from test_core import CATALOG, make
from vinted_hunter.bot.app import Progress

CONSOLES = CATALOG.get("consoles")


@pytest.mark.parametrize("title", [
    "Cache ps5 rouge", "Socle ps5 slim", "Silicone pour ps5 slim", "Carcasa PS5 Pro Black",
    "Boîte vide PlayStation 5 Pro (PS5 Pro)", "COVER Ps5 Fat Digital Spiderman Custom", "Manette ps5 Blanche",
    "Stellar Blade - Jeu PS5", "Pack PlayStation Portal (PS5) + Sacoche", "Station de refroidissement ps5",
])
def test_ps5_accessories_never_identified_as_console(title):
    assert CONSOLES.identify(title) is None


@pytest.mark.parametrize("title,key", [
    ("PS5 Slim Digital 1To", "ps5_slim_digital"),
    ("PS5 slim édition standard + 2 manettes", "ps5_slim_disc"),
    ("PS5 Pro 2To neuve", "ps5_pro"),
    ("Ps5 digital", "ps5_digital"),
    ("PS5 fat lecteur blu-ray", "ps5_disc"),
    ("PlayStation 5 disk 825gb con controller", "ps5_disc"),
    ("Console PS5", "ps5_unknown"),
])
def test_ps5_variants(title, key):
    assert CONSOLES.identify(title).key == key


def test_price_floor_keeps_games_out_of_console_prices():
    from vinted_hunter.engine import Hunter
    hunter = Hunter.__new__(Hunter)
    game = hunter._identify(make("FIFA 21 ps5", 3), CONSOLES)
    console = hunter._identify(make("Console PS5", 320), CONSOLES)
    assert game.model_key.startswith("sig:") and console.model_key == "consoles:ps5_unknown"


def test_wanted_ads_rejected():
    assert CONSOLES.excluded_by("je cherche ps5 a marseille")
    assert CONSOLES.excluded_by("cerco ps5 in provincia")


class _FakeMessage:
    def __init__(self):
        self.texts = []

    async def edit_text(self, text, **_):
        self.texts.append(text)


async def test_progress_checklist():
    msg = _FakeMessage()
    p = Progress(msg, "🔎 Chasse : ps5")
    await p("🔎 Recherche sur Vinted 3/3 pages · 248 annonces")
    p._last_edit = 0
    await p("📖 Lecture des descriptions 4/8")
    screen = msg.texts[-1]
    assert "✅ Recherche sur Vinted 3/3 pages · 248 annonces" in screen
    assert "⏳ <b>Lecture des descriptions 4/8</b>  ▓▓▓▓▓░░░░░" in screen
    assert "➖ Filtres, cote & tri" in screen          # skipped stage
    assert "⬜ Analyse IA du texte" in screen          # upcoming stage


def test_ai_scam_signal_marks_seller_and_card():
    from vinted_hunter.bot.views import deal_caption
    from vinted_hunter.config import RuntimeSettings
    from vinted_hunter.market import estimate
    from vinted_hunter.models import AIVerdict, Condition
    from vinted_hunter.scoring import build_deal, gate_fails
    verdict = AIVerdict(tier=3, model_name="PS5 Slim lecteur", red_flags=["paiement par virement exigé"],
                        missing_parts=["Photo du numéro de série", "Boîte d'origine non visible", "manette"])
    l = make("Ps5 Slim", 170, model_key="consoles:ps5_slim_disc", details_loaded=True, description="Complet, testé")
    deal = build_deal(l, estimate(Condition.VERY_GOOD, reference=350), CONSOLES, RuntimeSettings(), verdict)
    assert deal.seller_check.level == "risque" and "vendeur risqué" in gate_fails(deal, CONSOLES, RuntimeSettings())
    card = deal_caption(deal)
    assert card.startswith("🚨 <b>ARNAQUE PROBABLE</b>")
    assert "Incomplet</b> : manette" in card and "Photo" not in card.split("Incomplet")[1].split("\n")[0]


def test_specs_extraction_and_missing_specs():
    from vinted_hunter.inspection import extract_specs, inspect_listing
    assert extract_specs("Lenovo M720q i5-8500T 16Go RAM 256Go SSD") == {"storage": "256 Go", "ram": "16 Go", "cpu": "I5 8500T"}
    assert extract_specs("iPhone 13 128 Go batterie 89%") == {"storage": "128 Go", "battery": "89 %"}
    phone = inspect_listing(make("iPhone 13", 300, description="Très bon état"), CATALOG.get("apple_tech"))
    assert phone.missing_specs == ["storage", "battery"] and phone.verification == "electronics"
    full = inspect_listing(make("iPhone 13 128 Go", 300, description="batterie 91 %"), CATALOG.get("apple_tech"))
    assert full.missing_specs == []
    cheap = inspect_listing(make("Manette", 20), CONSOLES)
    assert cheap.verification is None                       # under the verification minimum price


def test_missing_specs_block_pepite_and_verification_scores():
    from vinted_hunter.config import RuntimeSettings
    from vinted_hunter.market import estimate
    from vinted_hunter.models import Condition
    from vinted_hunter.scoring import build_deal, gate_fails
    apple, rs = CATALOG.get("apple_tech"), RuntimeSettings()
    l = make("iPhone 13", 150, model_key="apple_tech:iphone13", details_loaded=True, description="Comme neuf")
    deal = build_deal(l, estimate(Condition.VERY_GOOD, reference=280), apple, rs)
    assert any(f.startswith("specs à vérifier") for f in gate_fails(deal, apple, rs, require_market=False))
    assert deal.score_parts["vérification Vinted dispo"] == 5


def test_id_clock_estimates_listing_age():
    from datetime import datetime, timedelta, timezone
    from vinted_hunter.engine import Hunter
    h = Hunter.__new__(Hunter)
    h._clock = []
    t0 = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    for k in range(30):                                  # ids grow ~1000 per minute
        h._clock_add(10_000_000 + k * 1000, t0 + timedelta(minutes=k))
    est = h._clock_estimate(10_015_500)
    assert abs((est - (t0 + timedelta(minutes=15.5))).total_seconds()) < 1
    assert h._clock_estimate(1) is not None              # extrapolates below the range


def test_scam_network_detection():
    from vinted_hunter.engine import Hunter
    from vinted_hunter.models import Seller
    ring = []
    for i in range(5):
        l = make(f"Valve Steam Deck Oled 512gb Black Handheld {i}", 250, id=100 + i,
                 model_key="consoles:steamdeck_oled", details_loaded=i < 3)
        ring.append(l.model_copy(update={"seller": Seller(id=i + 1, reviews=0 if i < 3 else None)}))
    honest = make("Steam Deck OLED 512", 420, id=200, model_key="consoles:steamdeck_oled", details_loaded=True)
    flagged = Hunter._scam_networks(ring + [honest])
    assert set(flagged) == {100, 101, 102, 103, 104} and 200 not in flagged
    assert "réseau d'arnaque" in flagged[100]


def test_relists_collapse_to_one_card():
    from vinted_hunter.bot.app import _dedupe_relists
    from types import SimpleNamespace
    a = make("Steam deck OLED 512GB", 400, id=1, model_key="consoles:steam_deck_oled")
    b = make("Steam Deck OLED 512gb", 400, id=2, model_key="consoles:steam_deck_oled")
    c = make("Steam Deck OLED", 380, id=3, model_key="consoles:steam_deck_oled")
    deals = [SimpleNamespace(listing=l) for l in (a, b, c)]
    assert [d.listing.id for d in _dedupe_relists(deals)] == [1, 3]


@pytest.mark.parametrize("text,lang", [
    ("Steam deck OLED 512GB usata poco, alimentatore compatibile ma non UFFICIALE", "it"),
    ("Condizioni ottime", "it"),
    ("Console très bon état, vendue avec sa boîte", "fr"),
    ("Maillot de foot Uhlsport gardien de but Coupe de la Ligue", "fr"),
    ("Hermès Evelyne 16 schwarz Gold wie neu, mit Dustbag", "de"),
    ("Great condition, never used, with box", "en"),
    ("Intel NUC NUC7i3BNHXF 2.4GHz i3-7100U Mini PC", None),
    ("Dell XPS 13 9310 in zeer goede staat", "nl"),
])
def test_language_detection(text, lang):
    from vinted_hunter.lang import detect
    assert detect(text) == lang


def test_foreign_card_is_translated_or_flagged():
    from vinted_hunter.bot.views import deal_caption
    from vinted_hunter.scoring import build_deal
    from vinted_hunter.models import Valuation
    from vinted_hunter.config import RuntimeSettings
    l = make("Steam deck OLED usata poco", 300, description="Condizioni ottime, usata poco", lang="it")
    d = build_deal(l, Valuation(fair_value=450, low=400, high=500, confidence=0.8), None, RuntimeSettings())
    cap = deal_caption(d)
    assert "Annonce en l'italien" in cap and "indisponible" in cap
    d2 = d.model_copy(update={"listing": l.model_copy(update={"translation": {"title": "Steam deck OLED peu utilisé",
                                                                              "text": "Très bon état, peu utilisé"}})})
    cap2 = deal_caption(d2)
    assert "traduite" in cap2 and "Très bon état, peu utilisé" in cap2 and "Condizioni" not in cap2


@pytest.mark.parametrize("title,desc,label", [
    ("Nintendo switch", "Console che non si accende non so il motivo venduta così com’è senza accessori", "ne fonctionne pas"),
    ("Olympus Mju I with original case | READ", "it has a battery drain issue", "batterie défaillante"),
    ("PS5", "Konsole startet nicht, für Bastler", "ne fonctionne pas"),
    ("iPhone 13", "No enciende, para piezas", "ne fonctionne pas"),
])
def test_foreign_defects_detected(title, desc, label):
    from vinted_hunter.inspection import inspect_listing
    insp = inspect_listing(make(title, 60, description=desc), None)
    assert label in [f.label for f in insp.defects]


@pytest.mark.parametrize("desc", ["Never broken, works perfectly, no issues", "Condizioni ottime, mai rotto",
                                  "Très bon état, fonctionne parfaitement"])
def test_healthy_listings_have_no_major_defect(desc):
    from vinted_hunter.inspection import inspect_listing
    assert not [f for f in inspect_listing(make("Switch OLED", 200, description=desc), None).defects if f.impact >= 0.3]


def test_broken_item_is_never_a_pepite():
    from vinted_hunter.config import RuntimeSettings
    from vinted_hunter.models import Valuation
    from vinted_hunter.scoring import build_deal, gate_fails
    l = make("Nintendo switch", 65, description="Console che non si accende", details_loaded=True)
    d = build_deal(l, Valuation(fair_value=150, low=120, high=180, confidence=0.9), None, RuntimeSettings())
    assert any(f.startswith("défaut majeur") for f in gate_fails(d, None, RuntimeSettings(), require_market=False))

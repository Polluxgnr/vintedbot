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

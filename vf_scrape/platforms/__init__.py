from .cashexpress import CashExpressScraper
from .ebay import EbayScraper
from .leboncoin import LeBonCoinScraper
from .rakuten import RakutenScraper
from .vinted import VintedScraper
from .wallapop import WallapopScraper

SCRAPERS = {
    "vinted": VintedScraper,
    "leboncoin": LeBonCoinScraper,
    "lbc": LeBonCoinScraper,
    "ebay": EbayScraper,
    "wallapop": WallapopScraper,
    "rakuten": RakutenScraper,
    "cashexpress": CashExpressScraper,
    "cash-express": CashExpressScraper,
}


def get_scraper(name: str, **kwargs):
    key = (name or "vinted").lower().replace("cash-express", "cashexpress")
    cls = SCRAPERS.get(key)
    if cls is None:
        raise ValueError(f"Plateforme scrape inconnue: {name}")
    return cls(**kwargs)

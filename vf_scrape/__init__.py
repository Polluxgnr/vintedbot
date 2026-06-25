"""
VF Scrape — extraction catalogue multi-plateformes (complément de vf/).

Chaque plateforme vit dans ``vf_scrape/platforms/`` avec logs verbeux et tests CLI isolés.
La partie 2 (triage, scoring, IA) reste dans ``vf/``.
"""

from .orchestrator import ScrapeJob, run_scrape
from .types import PlatformScrapeStats, RawListing

__all__ = ["ScrapeJob", "run_scrape", "RawListing", "PlatformScrapeStats"]

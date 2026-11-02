"""Tiny stop-word language detector: vinted.fr shows listings from Italy, Spain, Germany, the Netherlands...
Cards must read in French or English only, so foreign text is spotted here and translated by the AI."""

from __future__ import annotations

import re

_WORDS = {
    "fr": "le la les un une des de du en au aux sur ce cette mon ma et est avec pour sans très bon état neuf "
          "jamais porté fonctionne envoi vends taille comme quelques peu boîte acheté prix",
    "en": "the and with for without very good condition new never worn works shipping selling size box bought "
          "used only great",
    "it": "il lo gli una con per senza molto buono buone condizioni ottime ottimo nuovo usato mai funzionante "
          "spedizione vendo scatola perfette perfetto della delle sono anche come usata usate usati poco tutto "
          "tutti originale ottima ottimi nuova vendesi prezzo trattabile graffi piccolo colore regalo",
    "es": "el los las una con por sin muy buen buenas estado nuevo usado nunca funciona envío vendo caja perfecto "
          "talla del está también",
    "de": "der die das und mit für ohne sehr gut guter zustand neu gebraucht nie funktioniert versand verkaufe "
          "größe ist nicht auch wie",
    "nl": "de het een met voor zonder zeer goede staat nieuw gebruikt nooit werkt verzending verkoop maat ook "
          "niet zijn",
    "pt": "o os uma com para sem muito bom estado novo usado nunca funciona envio vendo caixa tamanho também",
    "pl": "bez bardzo dobry stan nowy używany nigdy działa wysyłka sprzedam rozmiar jest",
}
_SETS = {k: set(v.split()) for k, v in _WORDS.items()}
_TOKEN = re.compile(r"[a-zàâäçéèêëîïôöùûüÿñßąćęłńóśźż]{2,}")

NAMES = {"it": "l'italien", "es": "l'espagnol", "de": "l'allemand", "nl": "le néerlandais", "pt": "le portugais",
         "pl": "le polonais"}


def detect(text: str | None) -> str | None:
    """'fr', 'en', 'it'... or None when the text is too short or ambiguous to tell."""
    tokens = _TOKEN.findall((text or "").lower())
    if len(tokens) < 2:
        return None
    hits = {k: sum(t in s for t in tokens) for k, s in _SETS.items()}
    best = max(hits, key=lambda k: hits[k])
    ranked = sorted(hits.values(), reverse=True)
    if ranked[0] < 2 or ranked[0] == ranked[1]:
        return None
    return best


def is_foreign(text: str | None) -> bool:
    lang = detect(text)
    return lang is not None and lang not in ("fr", "en")

# Vinted Hunter v3

[![CI](https://github.com/Polluxgnr/vintedbot/actions/workflows/ci.yml/badge.svg)](https://github.com/Polluxgnr/vintedbot/actions/workflows/ci.yml)

Finds mispriced Vinted listings, checks them with AI (text + photos) and against the live market, and pushes only the real pépites to Telegram, with buttons to analyse, negotiate and track profit.

> The previous project in this repository (VF Finder, a multi-platform finder) is kept on the [`vf-finder`](https://github.com/Polluxgnr/vintedbot/tree/vf-finder) branch.

## What it scans

- **Where:** vinted.fr (configurable with `VINTED_DOMAIN`). It reads the same server-rendered search and item pages a browser gets, because Vinted retired its public catalog JSON endpoint.
- **What:** 18 niches with about 200 reference models (`config/niches.yaml`). Each niche has 8–18 search queries that rotate, newest listings first, about 46 listings per page.
- **How much:** about 5,000 listings per hour in the first live runs, with 3 s between requests. Productive niches are polled more often (adaptive scheduling) and idle ones less.
- **What it keeps:** every listing is stored as a price observation (the market memory), and listings matched to a catalog model enter the sales database.

## How it decides: the cost cascade

Every listing goes down a funnel. Cheap steps see everything; expensive steps see very little.

| Tier | What | Cost |
|---|---|---|
| 0 | Rules: budget, condition, size, accessory-only listings, games/controllers/cards/kids' items (model guards), fakes and replicas, parts-only | free |
| 1 | Reads the whole description: missing parts (controllers, dock, lens, charging case…), defects, scam signals (WhatsApp, payment outside Vinted), seller trust | free |
| 1 | Valuation: reference price + live comparables + **real sales** from the sales database, normalized by condition, capped at 90 % of retail for non-collectibles | free |
| — | Item page fetch (description, all photos, seller rating, upload date), only for promising listings | 1 request |
| 2 | **Haiku 5.5** batch triage, ~20 listings per call: exact model, relevance, missing parts, new price, fake risk, seller legitimacy | ~$0.0008 per call |
| 3 | **Haiku 5.5 vision** on 4 photos, with a 1k-token reasoning budget: condition, visible missing parts, authenticity | ~$0.0015/item |
| 4 | **Sonnet 5.5** verdict, with a 3k-token reasoning budget: BUY/NEGOTIATE/SKIP, max buy price, time to sell, negotiation message, resale ad | ~$0.04/item |
| 5 | **Opus 5.5** expert opinion, only when you press the button | on demand |

A daily AI budget (editable in Telegram) stops the expensive tiers first. Verdicts are cached, so nothing is paid for twice.

## "Pépites only" alerts (default)

An alert fires only when **every** check passes:
- **Exact model identified.** Catch-all models don't count.
- **Description read.** Missing parts and defects are deducted from the value.
- **Price ≥ 35 % under the estimate,** but not under 30 % of it (that usually means a different product or a scam).
- **Money:** profit ≥ 30 € (or the niche minimum) and margin ≥ 40 %.
- **Confidence:** price-estimate confidence ≥ 60 % and score ≥ 70.
- **Seller** not 🔴, and no scam signals.
- **Live market check:** the bot searches the exact model on Vinted right then and compares against at least 5 identical listings, with prices normalized to the same condition. At most 1 may be cheaper, and the price must be ≥ 25 % under their median.
- **Fake-prone niches** (luxury, watches, Apple, smartphones): the AI photo check is mandatory.

Every card says what blocked it (🚫), and 🔍 *Détail du calcul* shows each number and check.

## Sales database

Vinted doesn't publish sales, so the bot tracks identified listings and re-checks them (6 every 10 min) to see whether they sold, were reserved, disappeared or dropped in price. This gives, per model:
- **Real sale prices,** the strongest source in the price estimate.
- **Median days-to-sell,** shown as ⚡ on cards and used as a liquidity bonus in the score.
- **Sell-through rate.**

Near misses are tracked first: if a seller lowers the price and the listing then passes every check, you get the alert. The database cleans itself (raw observations are kept 120 days, the radar 14 days).

## Radar (Top)

Every good deal seen is stored, not only the pépites. Requirements: an identified model, a real profit, and no 🔴 seller or scam signals.

`/top` lists them over 24 h, 3 days or 7 days. You can:
- sort by ⭐ score, 💰 profit, 📈 margin, 🏷 discount or 🆕 most recent;
- filter by niche;
- open any line as a full card.

## Niches

| Niche | Highlights |
|---|---|
| 🖥️ Mini PC & homelab | Mac mini M1/M2/M4, NUC, ThinkCentre Tiny, EliteDesk Mini, Raspberry Pi 5, Jetson, Synology |
| 💻 PC hardware | RTX 3060→4090, RX 6800/7800, Ryzen X3D, ThinkPad X1, XPS |
| 📱 Android smartphones | Galaxy S23/S24/Ultra, Z Flip/Fold, Pixel 7–9 |
| 🍏 Apple | AirPods, iPad, MacBook, iPhone, Apple Watch |
| 💎 Luxury | LV, Hermès, Chanel, Dior, Goyard, Celine, Prada, Gucci |
| 🎮 Consoles · 👾 Retro gaming · ⚡ Pokémon/TCG · 🧱 LEGO | |
| 👟 Sneakers · 🏔️ Outdoor · 🧥 Streetwear · ⚽ Football shirts | |
| 📷 Film cameras · 📸 Digital cameras · 🎧 Audio · ⌚ Watches · 🏠 Home premium | |

Resale prices are seeds; live comparables and real sales take over as data accumulates. Edit the YAML freely.

## Telegram

- **Plain message,** e.g. *"mini pc pour héberger une IA, moins de 300€"*: the AI turns it into a search.
- **A Vinted link:** full analysis (photos, price estimate, live market check, verdict, negotiation message).
- **/hunt:** guided 5-question wizard.
- **/top:** the sortable radar.
- **/price:** market prices by condition, plus real sales.
- **/niches, /watches, /portfolio, /stats, /settings.**
- **/aide:** how the price estimate, confidence, score, seller trust, pépites, the sales database and the AI cascade work.
- **Daily digest** at 9:00.

## Setup

1. **Telegram:** **@BotFather** → `/newbot` → copy the token.
2. **AI:** an OpenRouter key from https://openrouter.ai/keys (a few dollars of credit lasts a long time).
3. **Run:** `start.bat` (creates `.env` on the first run; paste both keys).
4. **Claim:** send `/start` to your bot. The first chat becomes the owner.

## Hosting (free)

The bot must run on a **home internet connection**. Vinted blocks datacenter IPs: tested from cloud servers, it answers HTTP 429 after a few requests. Free cloud tiers therefore don't work.

- **Your Windows PC (now):** double-click `installer_demarrage_auto.bat`. After that, the bot:
  - starts in the background at every login, with no window;
  - restarts by itself if it crashes;
  - logs to `%USERPROFILE%\.vinted_hunter\bot.log`.

  Set **Settings → System → Power → Sleep: Never** so it keeps running. To remove it, run `scripts\uninstall_autostart.ps1`.
- **A mini PC or Raspberry Pi at home (later):** copy the project and `.env` to it, then run `docker compose up -d --build`. The Docker setup:
  - works on x86 and ARM;
  - uses Paris time for the 9:00 digest;
  - keeps the database on a persistent volume and checks the bot's health;
  - restarts the bot after crashes and reboots.

  Logs: `docker compose logs -f`.

Only one copy of the bot can run per machine: a second copy stops itself, because two copies on the same Telegram token would conflict.

CLI:
```bash
python -m vinted_hunter hunt "mac mini m2" --max 400
```
```bash
python -m vinted_hunter analyze https://www.vinted.fr/items/123
```

## Notes

- **Polite client:** 3 s between requests, backoff, and a circuit breaker on 429. No proxy rotation or anti-bot evasion.
- **Database location:** `%USERPROFILE%\.vinted_hunter`, outside any synced folder.
- **Tests:** `pip install -r requirements-dev.txt && python -m pytest`.

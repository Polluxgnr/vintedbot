# Vinted Hunter v3

[![CI](https://github.com/Polluxgnr/vintedbot/actions/workflows/ci.yml/badge.svg)](https://github.com/Polluxgnr/vintedbot/actions/workflows/ci.yml)

Finds mispriced Vinted listings, checks them with AI (text + photos) and against the live market, and pushes only the real pépites to Telegram, with buttons to analyse, negotiate and track profit.

> The previous project in this repository (VF Finder, a multi-platform finder) is kept on the [`vf-finder`](https://github.com/Polluxgnr/vintedbot/tree/vf-finder) branch.

## Démarrage rapide (Windows)

1. **Une seule fois :** double-clique `installer_demarrage_auto.bat` (le bot démarre seul à chaque ouverture de session), puis lance `scripts\creer_raccourcis.ps1` (clic droit → *Exécuter avec PowerShell*). Deux raccourcis apparaissent sur le bureau.
2. **Quand tu arrives sur ton PC :** rien à faire, le bot tourne déjà. Pour le (re)lancer à la main, double-clique **Vinted Hunter** sur le bureau : il redémarre le bot en arrière-plan et ouvre Telegram sur le bot.
3. **Pour l'arrêter :** double-clique **Arrêter Vinted Hunter**.
4. **Si rien ne vient :** le journal est dans `%USERPROFILE%\.vinted_hunter\bot.log`. Le raccourci l'ouvre tout seul si le démarrage échoue.

Le PC doit rester allumé (Paramètres → Système → Alimentation → Veille : Jamais).

## Recherches précises et alertes perso

Écris ce que tu veux en une phrase, le bot en tire des filtres stricts, même sans IA :

| Tu écris | Le bot filtre |
|---|---|
| *tee carhartt moins de 10 € en super bon état avec un logo graphique* | t-shirt Carhartt · ≤ 10 € · neuf/très bon état · l'annonce doit parler de logo/print |
| *pack chaussettes boss neuf taille M unicolore pour moi* | pack/lot · neuf · taille M · exclut rayé/motif/camo · mode **perso** |
| *alerte moi pour un rolex submariner sous 5000 €* | alerte permanente créée directement |

- **Mode perso** (« pour moi », « for me ») : tu reçois chaque annonce qui colle aux critères et au budget, d'un vendeur sûr, même sans marge de revente.
- **Mode revente** (par défaut) : seulement les vraies pépites.
- Après chaque recherche, **👁 M'alerter sur cette recherche** la transforme en alerte.

## What it scans

- **Where:** vinted.fr (configurable with `VINTED_DOMAIN`). It reads the same server-rendered search and item pages a browser gets, because Vinted retired its public catalog JSON endpoint.
- **What:** 19 niches with about 290 reference models (`config/niches.yaml`). Each niche has 8–18 search queries that rotate, newest listings first, about 46 listings per page.
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
- **Confidence:** price-estimate confidence ≥ 60 % and score ≥ 55.
- **Seller** not 🔴, and no scam signals.
- **Live market check:** the bot searches the exact model on Vinted right then and compares against at least 5 identical listings, with prices normalized to the same condition. At most 1 may be cheaper, and the price must be ≥ 25 % under their median.
- **Fake-prone niches** (luxury, watches, Apple, smartphones): the AI photo check is mandatory.

Every card says what blocked it (🚫), and 🔍 *Détail du calcul* shows each number and check.

## Specs & Vinted verification

- **Specs:** storage, RAM, CPU and battery health are read from the title and description. When a spec that drives the price is missing (e.g. storage on a phone, CPU/RAM on a mini PC), the card shows *"À demander au vendeur"*. That listing can't be a pépite until the AI confirms the specs. The AI also cross-checks title, description and photos (settings screenshots, labels): a contradiction blocks the pépite.
- **Vinted verification:** electronics and luxury/designer items above the eligibility price are marked *"Vérification Vinted probablement dispo"* (+5 score). This is Vinted's optional paid check, to confirm at checkout. In fake-prone niches, it can replace the photo-check requirement.

## Database that grows and learns

- **Harvester:** every request not needed for the live feeds crawls all 18 niches and ~260 catalog models (cheapest first, several pages). Everything goes into price memory and sales tracking, at no AI cost. For a one-off big run, use `python -m vinted_hunter harvest --minutes 60` (stop the bot first so they don't share the request budget).
- **Calibration:** every 6 h, each model's price is relearned from 30 days of observations and confirmed sales. Fake-prone niches use the upper part of the market. Learnt prices are bounded between −40 % and +50 % of the seed price, and 7-day trends are tracked.
- **Niche study:** `/etude` in Telegram, or `python -m vinted_hunter study --json study.json`. It ranks niches and models by opportunity: deals per day × typical profit, weighted by fake risk and price homogeneity. Each model is measured against its own observed market.

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
| 📷 Film cameras · 📸 Digital cameras · ⌚ Watches · 🏠 Home premium | |
| 🎧 Audio & speakers | Sony XM4/XM5, Bose, AirPods Max, Sonos Era/Five/Move/Arc, Marshall Stanmore/Woburn, B&O, JBL Boombox/PartyBox, Devialet, KEF |
| 📽 Projectors | Epson EH-TW, BenQ W2700/TK850, XGIMI Halo/Horizon/MoGo, Samsung Freestyle, Nebula, Dangbei |

Every card shows **🔎 Avant d'acheter**: the 2–3 checks that prove this kind of item is genuine and working (serial number, IMEI, shutter count, projected image…).

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

- **Your home IP is protected:** the bot runs on the same connection you buy from, so a Vinted ban would hurt. It waits 3–4 s between requests and makes at most **360 requests per hour** (`VINTED_MAX_REQUESTS_PER_HOUR` in `.env`). The harvester only uses half of that, so searches always have room. On the **first** 429/403 it stops everything for 10 min, doubling up to 1 h if blocks repeat. No proxy rotation or anti-bot evasion. Raising the cap means more data, at your own risk; 360–500 is the sensible range.
- **Languages:** vinted.fr shows listings from all over Europe. Foreign listings are translated into French, by the AI or, when OpenRouter is unreachable, by the free MyMemory API. Defects are read in FR/EN/IT/ES/DE/NL/PT (« non si accende », « defekt », « for parts »…), and a broken or « sold as is » item is never a pépite.
- **Database location:** `%USERPROFILE%\.vinted_hunter`, outside any synced folder.
- **Tests:** `pip install -r requirements-dev.txt && python -m pytest`.

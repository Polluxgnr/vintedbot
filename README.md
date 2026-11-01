# Vinted Hunter v3

Finds underpriced Vinted listings, checks them with AI (text + photos), and pushes resell-worthy deals to Telegram with buttons to analyse, negotiate and track profit.

## How it finds deals: the cost cascade

Every listing goes down a funnel. Cheap steps see everything; expensive steps see very little.

| Tier | What | Cost |
|---|---|---|
| 0 | Rules: budget, condition, size, accessory-only listings ("boîte seule", "notice"), fakes/replicas, parts-only | free |
| 1 | Local valuation: reference price + **live comparables** from every listing the bot has ever seen (outliers trimmed, condition-normalized) → landed cost (price + 5 % + 0.70 € protection + shipping) → net profit → score | free |
| — | Item page fetch (description, all photos, seller rating), only for promising items | 1 request |
| 2 | **Haiku 5.5** batch triage, ~20 listings per call: exact model, relevance, resale estimate, fake risk | ~$0.00003/item |
| 3 | **Haiku 5.5 vision** on up to 4 photos: condition score, defects, authenticity checks from the niche checklist | ~$0.001/item |
| 4 | **Sonnet 5.5** deep verdict on exceptional deals only: BUY/NEGOTIATE/SKIP, max buy price, time to sell, negotiation message, ready-to-post resale ad | ~$0.04/item |
| 5 | **Opus 5.5** "expert opinion", only when you press the button | on demand |

Other cost and safety controls:
- **Budget:** a daily AI budget, editable in Telegram. Expensive tiers stop first, and cheap triage keeps running.
- **Caching:** verdicts are cached per listing, so nothing is paid for twice.
- **Self-calibrating prices:** when the AI names a model the catalog doesn't know, the listing is re-keyed so its comparables cluster correctly.
- **Without an OpenRouter key:** the bot still works on tiers 0 and 1.

## "Pépites only" mode (default)

An alert fires only when **every** check passes:
- **Exact model identified.** Catch-all models don't count, and neither do games, controllers, cards or kids' items.
- **Description read in full.** Missing parts and defects are deducted from the value.
- **Price at least 35 % under the estimate,** but not under 30 % of it (that usually means a different product or a scam).
- **Money:** profit ≥ 30 € (or the niche minimum) and margin ≥ 40 %.
- **Confidence:** price-estimate confidence ≥ 60 % and score ≥ 70.
- **Seller** not 🔴, and no scam signals.
- **Live market check:** the bot searches the exact model on Vinted right then and compares against at least 5 identical listings, with prices normalized to the same condition. At most 1 may be cheaper, and the price must be ≥ 25 % under their median.
- **Fake-prone niches:**
  - Luxury, watches, Apple: AI photo check is mandatory.
  - Sneakers, streetwear and similar: without AI, you need a 🟢 seller and a plausible price.

Every card says what blocked it (🚫), and 🔍 *Détail du calcul* shows each check. Switch back to `normal` in ⚙️ Réglages.

## Niches (`config/niches.yaml`)

The file has 15 curated niches and about 140 reference models, each with a resale price, fake red flags, and a photo checklist:

- Sneakers
- Outdoor premium (Arc'teryx, TNF, Patagonia)
- Streetwear & designer
- Apple & tech
- Consoles
- Retro gaming
- Pokémon & TCG
- LEGO
- Film cameras
- Digital cameras
- Audio
- Watches
- Home premium (Dyson, Thermomix…)
- Luxury (reinforced checks)
- Vintage football shirts

The resale prices are **seeds**. Real observed prices take over as data accumulates. Edit the file freely; it's plain YAML.

## Telegram

- **Plain message:** *"AirPods Pro 2 under 90€ to resell"*. The AI turns it into a search, then you choose ⚡ Search / 👁 Watch / ✏️ Refine.
- **A Vinted link:** full analysis (photos, price, risks, verdict, negotiation message).
- **/hunt:** a 5-question guided wizard (product or niche, budget, condition, size, resell vs. personal use, minimum profit, now or continuous).
- **/niches:** turn continuous niche watching on or off.
- **/watches:** your alerts (pause or delete).
- **/price `product`:** market prices by condition.
- **/top:** best deals from the last 24 h.
- **/portfolio:** bought → sold → real profit.
- **/stats:** listings scanned, alerts, AI spend per tier.
- **/settings:** min profit, min margin, min score, AI budget, auto-expert, pause, quiet hours (22h–7h).

Every alert has these buttons: 🛒 Vinted · 🧠 Full analysis · 💬 Message to seller · ✅ Bought · 🙈 Ignore.

## Setup (5 min)

1. **Create a bot:** in Telegram, open **@BotFather** → `/newbot` → pick a name → copy the token.
2. **Get an AI key:** create one at https://openrouter.ai/keys and add a few dollars of credit. Normal use costs cents per day.
3. **Run it:** double-click `start.bat`. The first run creates `.env` and opens it; paste both keys, save, and run `start.bat` again.
4. **Claim the bot:** send `/start` to your bot. The first chat to do so becomes the owner, and the recommended niches switch on.

CLI, no Telegram needed:
```bash
python -m vinted_hunter hunt "contax t2" --max 900
```
```bash
python -m vinted_hunter price "airpods pro 2"
```
```bash
python -m vinted_hunter analyze https://www.vinted.fr/items/123
```

## Hosting

The bot is a long-running process: a watch loop plus a SQLite "market memory".
- **Your PC:** `start.bat`. The database lives in `%USERPROFILE%\.vinted_hunter`, deliberately outside Google Drive, because a synced SQLite file gets corrupted.
- **Any server / VPS:** `docker compose up -d --build` (with `.env` next to it).
- **Cloudflare is not a good fit:**
  - Workers can't run this Python process.
  - Cloudflare Containers need the paid plan, sleep when idle, and have no persistent disk, so the market memory would be wiped on every restart.
  - Datacenter IPs also get rate-limited by Vinted faster than a home connection does.

## Notes

- Vinted retired its public catalog JSON endpoint, so the bot reads the same server-rendered pages a browser does.
- It's a polite client: 3 s between requests, backoff, and a circuit breaker on 429. No proxy rotation or anti-bot evasion. If Vinted slows you down, the bot waits.
- Tests: `pip install -r requirements-dev.txt`, then `python -m pytest`.
- The old Selenium/Streamlit draft is untouched in `tests/`.

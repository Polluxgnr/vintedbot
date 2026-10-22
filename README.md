# VF Finder

[![CI](https://github.com/Polluxgnr/vintedbot/actions/workflows/ci.yml/badge.svg)](https://github.com/Polluxgnr/vintedbot/actions/workflows/ci.yml)

*Version française : [README.fr.md](README.fr.md)*

A tool that finds good second-hand deals for me. I describe what I want ("video projector, 50 to 900 €"), it searches Vinted, Leboncoin, eBay, Wallapop, Rakuten and Cash Express, throws away listings that are not what I asked for, reads the specs out of each title and description, and ranks what is left by value for money. It has a desktop GUI and a CLI.

## Why it is harder than it sounds

- **Anti-bot protection.** Vinted, Leboncoin (DataDome) and eBay block automated browsing. Each platform has its own scraper with persisted cookies, rate limits, cool-downs and a visible-browser mode to solve a captcha once. A threading deadlock on Vinted was fixed by switching to a re-entrant lock and adding a boot timeout.
- **Search results are noisy.** Searching "PS4" returns controllers, games and empty boxes. Titles go through rules first (keep / reject / unsure); only the unsure ones are sent to an LLM, which keeps cost and latency low.
- **"Cheap" is not "good".** Each category has hard veto rules: a projector without HDMI, an iCloud-locked phone, a cracked DS screen are rejected even at 1 €. The rest is scored on extracted specs, brand tier, completeness and discount against the reference price.

## How it works

```
search intent -> multi-platform scrape -> title triage -> spec extraction -> veto + scoring -> top 5 -> CSV / Excel / report
```

Categories (projector, Nintendo DS, PlayStation, bike, air conditioner, phone) are defined in one place, `CategoryConfig` in `vf/models.py`: brands by tier, specs to extract, veto flags, minimum discount per tier. Adding a category means adding an entry there.

## Run it

```bash
git clone https://github.com/Polluxgnr/vintedbot.git
cd vintedbot
pip install -r requirements.txt
cp .env.example .env            # OPENROUTER_API_KEY is optional

python -m vf --gui
python -m vf --q "video projecteur" --category projector --price-min 50 --price-max 900
python -m vf --q "new 3ds xl" --category console_ds --headed   # visible browser for captchas
```

LLM features (triage of unsure titles, image check of the top results) are off unless you pass `--ai` with an OpenRouter key; the rules alone work fine.

`python -m vf_scrape` is a separate bench to test each scraper on its own.

## Tests

```bash
VF_NO_AI=1 python -m unittest discover -s tests -t .
```

138 unit tests on intent parsing, triage, spec extraction, scoring and ranking. They run on Python 3.10 to 3.12 in GitHub Actions.

## Layout

```
vf/
  __main__.py   CLI
  app.py        CustomTkinter GUI
  pipeline.py   runs the steps in order
  intent.py     what the user is looking for
  market.py     multi-platform search
  triage.py     keep / reject / unsure on titles
  scoring.py    spec extraction, value score, top deals
  models.py     category definitions
  ai.py         OpenRouter calls
  export.py     CSV, Excel, reports
vf_scrape/      one scraper per platform + benchmark CLI
tests/
```

## Stack

Python, Selenium with undetected-chromedriver, requests / aiohttp, BeautifulSoup, CustomTkinter, openpyxl, OpenRouter (optional).

## License

MIT

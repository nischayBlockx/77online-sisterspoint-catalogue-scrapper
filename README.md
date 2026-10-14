# Sisters Point B2B catalogue scraper

A bot that logs in to **https://b2b.sisterspoint.com/**, goes through the site
**one category at a time**, opens every product and writes:

| File | What it is |
|---|---|
| `output/catalogue_all.xlsx` | One row per variant (colour + size / EAN), with the **same 29 columns as the TBI reference catalogue** (Brand, ItemMainCategory, EAN, ItemCode, …, Available Stock) |
| `output/shopify_products_all.csv` | A **Shopify product import CSV**: one product per style, with Color / Size options, barcode, SKU, price, cost, stock and images |
| `output/by_category/catalogue_<category>.xlsx`, `shopify_<category>.csv` | The same two files split by category |
| `output/products.jsonl` | Raw scraped data, used to resume an interrupted run |

## Setup (one time)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env              # put your B2B username + password in .env
cp config.example.yaml config.yaml
```

## Run it

```bash
python -m sp_scraper login                  # 1. check the login works (saves the session)
python -m sp_scraper categories             # 2. see which categories the bot found
python -m sp_scraper scrape --category dresses --limit 5   # 3. small test run
python -m sp_scraper scrape                 # 4. everything, category by category
```

* `--category NAME` (repeatable) scrapes only categories whose name/URL contains `NAME`.
* `--limit N` stops after N products per category (useful for testing).
* `--headed` shows the browser window so you can watch the bot.
* Runs resume automatically: re-running `scrape` skips products already in
  `output/products.jsonl`. Use `--fresh` to start over.
* `python -m sp_scraper export` rebuilds the Excel/CSV files without scraping
  (e.g. after changing the Shopify price settings in `config.yaml`).
* `python -m sp_scraper convert some_catalogue.xlsx` turns any catalogue in
  the reference format (like the TBI file) into a Shopify CSV.

## If something is not picked up

The bot detects the login form, menu categories, product tiles, pagination /
"load more" / infinite scroll, and product data (schema.org JSON-LD, spec
tables in English or Danish, size grids, and JSON the page loads in the
background) automatically. If the site's markup isn't recognised:

1. Run `python -m sp_scraper discover`. It logs in and saves the HTML and a
   screenshot of the home, category and product pages to `output/debug/`,
   along with `discover_report.json` showing what was extracted.
2. Set the missing CSS selectors in `config.yaml` (`selectors:` section), or
   list the categories explicitly under `categories:`.

Failed products are logged in `output/errors.log`, with a screenshot in `output/debug/`.

## Importing into Shopify

Shopify admin → **Products → Import** → choose `shopify_products_all.csv` (or a
per-category file). Products are imported as **draft** by default. Check them,
then set them to active (or set `shopify.status: active` in `config.yaml`).

Pricing: `Variant Price` = RRP from the site × `price_multiplier`. If there's
no RRP, it's wholesale × `markup_on_wholesale`. `Cost per item` = wholesale × `price_multiplier`.

## Tests

```bash
pip install pytest && python -m pytest tests
```

The tests run the real bot end to end against a local mock B2B site
(`tests/mock_site/server.py`).

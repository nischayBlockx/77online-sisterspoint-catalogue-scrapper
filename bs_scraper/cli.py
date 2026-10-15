"""CLI entry point for Bestseller MORE scraper."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from sp_scraper.exporters import (
    append_product_jsonl, load_products_jsonl,
    products_to_rows, write_catalogue_xlsx, write_shopify_csv,
)
from sp_scraper.models import Product, Variant

from .extract import extract_product_from_api
from .scraper import BestsellerScraper

log = logging.getLogger("bs_scraper")


def _setup_logging(verbose: bool = False):
    fmt = "%(asctime)s %(levelname)-5s %(message)s" if verbose else "%(asctime)s %(levelname)-5s %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format=fmt, datefmt="%H:%M:%S", stream=sys.stderr)


def cmd_login(args):
    """Interactive login — opens browser for OTP."""
    with BestsellerScraper(headless=False) as bot:
        if bot.login_interactive():
            print("Login successful! Session saved.")
            return 0
        print("Login failed.")
        return 2


def cmd_discover(args):
    """Save raw API data for debugging selectors."""
    out = Path(args.output) / "debug_bestseller"
    with BestsellerScraper(headless=args.headless) as bot:
        if not bot.ensure_auth():
            return 2
        bot.discover(args.list_id, out)
    print(f"Debug files saved to {out}")
    return 0


def cmd_download_excel(args):
    """Download the platform's own Excel export."""
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / "bs_favouritelist_export.xlsx"
    with BestsellerScraper(headless=args.headless) as bot:
        if not bot.ensure_auth():
            return 2
        if bot.export_favourite_list_excel(args.list_id, xlsx_path):
            print(f"Excel export saved to {xlsx_path}")
            return 0
        print("Excel export failed.")
        return 1


def cmd_scrape(args):
    """Scrape products from a favourite list."""
    out_dir = Path(args.output)
    jsonl_path = out_dir / "bs_products.jsonl"

    if args.fresh and jsonl_path.exists():
        jsonl_path.unlink()
        log.info("Cleared previous data (--fresh)")

    # Load already-scraped products for resume
    existing = load_products_jsonl(jsonl_path)
    scraped_urls = {p.url for p in existing}
    log.info("Resuming: %d products already scraped", len(existing)) if existing else None

    with BestsellerScraper(headless=args.headless) as bot:
        if not bot.ensure_auth():
            return 2

        # Fetch all products from favourite list
        log.info("Fetching favourite list %s...", args.list_id)
        items = bot.get_all_favourite_products(args.list_id)
        if not items:
            log.error("No products found in favourite list")
            return 1

        log.info("Found %d items in favourite list", len(items))

        new_count = 0
        for i, item in enumerate(items, 1):
            p = extract_product_from_api(item, category=args.category or "Favourites")
            if not p or not p.style_code:
                log.warning("[%d/%d] Skipped: no style code", i, len(items))
                continue
            if p.url in scraped_urls:
                continue

            append_product_jsonl(jsonl_path, p)
            scraped_urls.add(p.url)
            new_count += 1
            log.info("[%d/%d] %s | %s | %d variants",
                     i, len(items), p.style_code, p.style_name, len(p.variants))

        log.info("Scraped %d new products (%d total)", new_count, len(scraped_urls))

    # Export
    _export(jsonl_path, out_dir, args)
    return 0


def cmd_export(args):
    """Re-export from existing JSONL."""
    out_dir = Path(args.output)
    jsonl_path = out_dir / "bs_products.jsonl"
    if not jsonl_path.exists():
        print(f"No data file found at {jsonl_path}. Run 'scrape' first.")
        return 1
    _export(jsonl_path, out_dir, args)
    return 0


def _export(jsonl_path: Path, out_dir: Path, args):
    products = load_products_jsonl(jsonl_path)
    if not products:
        log.warning("No products to export")
        return

    cfg = {"default_brand": "BESTSELLER", "default_gender": ""}
    rows = products_to_rows(products, cfg)

    xlsx_path = out_dir / "bs_catalogue_all.xlsx"
    write_catalogue_xlsx(rows, xlsx_path)

    csv_path = out_dir / "bs_shopify_products_all.csv"
    shop_cfg = {
        "shopify": {
            "status": "draft",
            "published": False,
            "price_multiplier": 1.0,
            "markup_on_wholesale": 2.5,
            "track_inventory": True,
        }
    }
    n = write_shopify_csv(rows, csv_path, shop_cfg)

    log.info("All: %d variants / %d products -> %s, %s",
             len(rows), len(products), xlsx_path, csv_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bs_scraper", description="Bestseller MORE B2B Scraper")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("login", help="Interactive login (opens browser)")

    p_disc = sub.add_parser("discover", help="Save raw API data for debugging")
    p_disc.add_argument("--list-id", default="ZLs6MUPd7w", help="Favourite list ID")
    p_disc.add_argument("--output", default="output", help="Output directory")

    p_scrape = sub.add_parser("scrape", help="Scrape products from favourite list")
    p_scrape.add_argument("--list-id", default="ZLs6MUPd7w", help="Favourite list ID")
    p_scrape.add_argument("--category", default="", help="Category name override")
    p_scrape.add_argument("--output", default="output", help="Output directory")
    p_scrape.add_argument("--fresh", action="store_true", help="Ignore resume data")

    p_export = sub.add_parser("export", help="Re-export from existing data")
    p_export.add_argument("--output", default="output", help="Output directory")

    p_dlxl = sub.add_parser("download-excel", help="Download platform Excel export")
    p_dlxl.add_argument("--list-id", default="ZLs6MUPd7w", help="Favourite list ID")
    p_dlxl.add_argument("--output", default="output", help="Output directory")

    args = parser.parse_args(argv)
    args.headless = not args.headed
    _setup_logging(args.verbose)

    commands = {
        "login": cmd_login, "discover": cmd_discover, "scrape": cmd_scrape,
        "export": cmd_export, "download-excel": cmd_download_excel,
    }
    if not args.command:
        parser.print_help()
        return 1
    return commands[args.command](args)
